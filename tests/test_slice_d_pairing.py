from __future__ import annotations

import importlib.util
import pathlib
import threading
import uuid
from importlib.machinery import SourceFileLoader

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_SRC = ROOT / "server" / "calliope-server"
WEB_UI_SRC = ROOT / "server" / "calliope_server" / "web_ui.py"
EXTENSION_SRC = ROOT / "extension" / "index.js"
DURABLE_TOKEN = "durable-bearer-must-never-enter-pairing-url"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    name = f"calliope_server_slice_d_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SERVER_SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._pairing_codes.clear()
    return module


@pytest.fixture
def mod(tmp_path, monkeypatch):
    module = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "get_token", lambda: DURABLE_TOKEN)
    return module


class PostHandler:
    def __init__(self, path, *, headers=None, body=None):
        self.path = path
        self.headers = headers or {}
        self.body = body or {}
        self.command = "POST"
        self.sent = []

    def require_auth(self):
        # Exercise the endpoint's explicit bootstrap bearer gate directly;
        # exchange is auth-exempt because the one-time code is its credential.
        return True

    def _reject_oversized_body(self, _limit):
        return False

    def read_json_body(self):
        return self.body

    def send_json(self, payload, status=200):
        self.sent.append(("json", status, payload))

    def send_error_json(self, error, code="error", status=400,
                        retry_ok=False, details=None):
        self.sent.append(("error", status, {"error": error, "code": code}))

    def _send_unauthorized(self, reason):
        self.sent.append(("error", 401, {"error": reason, "code": "unauthorized"}))


def _bind(mod, handler, *names):
    for name in names:
        setattr(handler, name, getattr(mod.DictationHandler, name).__get__(handler))
    return handler


def test_pairing_code_creation_requires_explicit_bearer_even_on_auth_bypassed_path(mod):
    missing = _bind(mod, PostHandler("/pair/bootstrap"), "_request_bearer_token")
    mod.DictationHandler.do_POST(missing)
    assert missing.sent == [
        ("error", 401, {"error": "valid bearer token required", "code": "unauthorized"})
    ]

    wrong = _bind(
        mod,
        PostHandler("/pair/bootstrap", headers={"Authorization": "Bearer wrong"}),
        "_request_bearer_token",
    )
    mod.DictationHandler.do_POST(wrong)
    assert wrong.sent[0][1] == 401

    valid = _bind(
        mod,
        PostHandler(
            "/pair/bootstrap",
            headers={"Authorization": f"Bearer {DURABLE_TOKEN}"},
        ),
        "_request_bearer_token",
    )
    mod.DictationHandler.do_POST(valid)
    kind, status, payload = valid.sent[0]
    assert (kind, status) == ("json", 201)
    assert payload["expires_in"] == 120
    assert payload["code"]
    assert DURABLE_TOKEN not in payload["code"]


def test_pairing_code_ttl_and_expired_consume(mod):
    code, ttl = mod.create_pairing_code(DURABLE_TOKEN, now=100.0)
    assert ttl == 120
    assert mod.pairing_code_is_valid(code, now=219.999)
    assert not mod.pairing_code_is_valid(code, now=220.0)
    assert mod.consume_pairing_code(code, now=220.0) == ("", "expired")


def test_pairing_code_consumes_atomically_once_under_race(mod):
    code, _ = mod.create_pairing_code(DURABLE_TOKEN, now=100.0)
    barrier = threading.Barrier(3)
    results = []

    def consume():
        barrier.wait()
        results.append(mod.consume_pairing_code(code, now=101.0))

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert results.count((DURABLE_TOKEN, "ok")) == 1
    assert results.count(("", "invalid")) == 1


def test_exchange_returns_token_once_then_rejects_replay(mod):
    code, _ = mod.create_pairing_code(DURABLE_TOKEN)

    first = PostHandler("/pair/exchange", body={"code": code})
    mod.DictationHandler.do_POST(first)
    assert first.sent == [("json", 200, {"token": DURABLE_TOKEN})]

    replay = PostHandler("/pair/exchange", body={"code": code})
    mod.DictationHandler.do_POST(replay)
    assert replay.sent[0][0:2] == ("error", 404)
    assert replay.sent[0][2]["code"] == "pairing_code_invalid"


@pytest.mark.parametrize("body", [{}, {"code": "unknown"}, [], "bad"])
def test_exchange_rejects_missing_unknown_and_incompatible_payloads(mod, body):
    handler = PostHandler("/pair/exchange", body=body)
    mod.DictationHandler.do_POST(handler)
    assert handler.sent[0][0:2] == ("error", 404)
    assert handler.sent[0][2]["code"] == "pairing_code_invalid"


def test_root_accepts_live_pairing_code_but_rejects_unknown_or_expired(mod):
    code, _ = mod.create_pairing_code(DURABLE_TOKEN, now=100.0)
    assert mod.pairing_code_is_valid(code, now=101.0)
    assert not mod.pairing_code_is_valid("unknown", now=101.0)
    assert not mod.pairing_code_is_valid(code, now=220.0)

    server = SERVER_SRC.read_text(encoding="utf-8")
    assert 'query.get("pair", [""])[0]' in server
    assert "pairing_code_is_valid(pairing_code)" in server
    assert '"/pair/exchange"' in server


def test_extension_pairing_urls_use_bootstrap_code_not_durable_bearer():
    source = EXTENSION_SRC.read_text(encoding="utf-8")
    start = source.index("async function buildPairedPhoneUrl(")
    end = source.index("function drawQrToCanvas(", start)
    pairing = source[start:end]

    assert "`${base}/pair/bootstrap`" in pairing
    assert "headers: { ...authHeaders() }" in pairing
    assert "qp.set('pair', String(data.code))" in pairing
    assert "serverToken" not in pairing
    assert "qp.set('token'" not in pairing
    assert "window.prompt(" not in pairing
    assert "navigator.clipboard.writeText(url)" in pairing


def test_extension_has_no_hidden_pairing_url_or_prompt_exposure():
    source = EXTENSION_SRC.read_text(encoding="utf-8")
    assert "dictation_bridge_pair_qr_url" not in source
    assert "window.prompt('Copy paired phone URL" not in source
    assert "URL/QR contains bearer token" not in source
    assert "bearer-token pairing URL" not in source


def test_phone_exchanges_once_scrubs_history_and_keeps_token_phone_local():
    web_ui = WEB_UI_SRC.read_text(encoding="utf-8")
    bootstrap_start = web_ui.index("// Safe phone bootstrap.")
    bootstrap_end = web_ui.index("let mediaRecorder", bootstrap_start)
    bootstrap = web_ui[bootstrap_start:bootstrap_end]

    assert "params.get('pair')" in bootstrap
    assert "params.delete('pair')" in bootstrap
    assert "history.replaceState(null, '', clean)" in bootstrap
    assert "_origFetch('/pair/exchange'" in bootstrap
    assert "body: JSON.stringify({ code: pairingCode })" in bootstrap
    assert "sessionStorage.setItem('dictationToken', TOKEN)" in bootstrap
    assert "window.calliopeAuthReady = bootstrap" in bootstrap
    assert "params.get('token')" not in bootstrap
    assert "sessionStorage.setItem('dictationToken', url_tok)" not in bootstrap


def test_reload_bootstrap_uses_authorization_header_not_token_url():
    web_ui = WEB_UI_SRC.read_text(encoding="utf-8")
    start = web_ui.index('PAIRING_BOOTSTRAP_HTML = """')
    page = web_ui[start:]

    assert "'Authorization': 'Bearer ' + stored" in page
    assert "params.set('token', stored)" not in page
    assert "location.replace(" not in page


def test_phone_initialization_waits_for_pairing_exchange():
    web_ui = WEB_UI_SRC.read_text(encoding="utf-8")
    assert "function initAfterAuth()" in web_ui
    assert "Promise.resolve(window.calliopeAuthReady).then(init)" in web_ui
    assert "document.addEventListener('DOMContentLoaded', initAfterAuth)" in web_ui


def test_pairing_codes_are_ram_only_and_pair_query_is_log_redacted(mod):
    server = SERVER_SRC.read_text(encoding="utf-8")
    pairing_state = server[server.index("PAIRING_CODE_TTL_SECONDS"):server.index("def _redact_sensitive_url_params")]
    assert "Path(" not in pairing_state
    assert "write" not in pairing_state
    assert mod._redact_sensitive_url_params('GET /?pair=short-code&chat=x HTTP/1.1') == (
        'GET /?pair=[REDACTED]&chat=x HTTP/1.1'
    )
