"""Security-hardening coverage (2026-07 audit).

Covers:
  1. Request body size caps (JSON + audio) with 413-before-read.
  2. Path traversal guards on request-supplied IDs (`_safe_child`).
  3. Non-loopback gating of `/` and `/health`.
  4. Bearer token no longer logged in plaintext + `--show-token` CLI.
  5. `DICTATION_ST_DATA_ROOT` env var derivation.
  6. Lock guards on `_token_cache` / `_recent_chats_cache`.
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import pathlib
import subprocess
import sys
import uuid
from importlib.machinery import SourceFileLoader

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


def _load(env: dict[str, str] | None = None):
    """Load the server module fresh, optionally with env overrides."""
    saved: dict[str, str | None] = {}
    env = env or {}
    for k, v in env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        name = f"calliope_server_sec_{uuid.uuid4().hex}"
        loader = SourceFileLoader(name, str(SRC))
        spec = importlib.util.spec_from_loader(name, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module
    finally:
        for k, old in saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


@pytest.fixture(scope="module")
def mod():
    return _load()


class FakeHandler:
    """Duck-typed stand-in for DictationHandler instances in method tests."""

    def __init__(self, headers=None, body=b"", client="192.168.1.50", path="/"):
        self.headers = headers or {}
        self.rfile = io.BytesIO(body)
        self.client_address = (client, 12345)
        self.path = path
        self.command = "GET"
        self.sent: list[tuple] = []
        self.html_sent: list[str] = []

    def _client_addr(self):
        return self.client_address[0]

    def bind(self, mod, *names):
        """Attach real DictationHandler methods (bound to this fake)."""
        for n in names:
            setattr(self, n, getattr(mod.DictationHandler, n).__get__(self))
        return self

    def send_error_json(self, error, code="error", status=400,
                        retry_ok=False, details=None):
        self.sent.append(("error", status, code, error))

    def send_json(self, payload, status=200):
        self.sent.append(("json", status, payload))

    def send_html(self, html):
        self.html_sent.append(html)


# ─── 1. Body size caps ───────────────────────────────────────────────

def test_body_cap_constants_defaults(mod):
    assert mod.MAX_JSON_BODY_BYTES == 1 * 1024 * 1024
    assert mod.MAX_AUDIO_BODY_BYTES == 25 * 1024 * 1024


def test_body_cap_env_override():
    m = _load({"DICTATION_MAX_JSON_BODY_BYTES": "2048",
               "DICTATION_MAX_AUDIO_BODY_BYTES": "4096"})
    assert m.MAX_JSON_BODY_BYTES == 2048
    assert m.MAX_AUDIO_BODY_BYTES == 4096


def test_reject_oversized_body_over_limit_413(mod):
    fh = FakeHandler(headers={"Content-Length": str(mod.MAX_JSON_BODY_BYTES + 1)})
    assert mod.DictationHandler._reject_oversized_body(fh, mod.MAX_JSON_BODY_BYTES)
    kind, status, code, _ = fh.sent[0]
    assert (kind, status) == ("error", 413)


def test_reject_oversized_body_at_limit_passes(mod):
    fh = FakeHandler(headers={"Content-Length": str(mod.MAX_JSON_BODY_BYTES)})
    assert not mod.DictationHandler._reject_oversized_body(fh, mod.MAX_JSON_BODY_BYTES)
    assert fh.sent == []


@pytest.mark.parametrize("bad", ["abc", "-5", "1e9", ""])
def test_reject_oversized_body_malformed_or_negative_400(mod, bad):
    fh = FakeHandler(headers={"Content-Length": bad})
    assert mod.DictationHandler._reject_oversized_body(fh, mod.MAX_JSON_BODY_BYTES)
    kind, status, _, _ = fh.sent[0]
    assert (kind, status) == ("error", 400)


def test_reject_oversized_body_missing_header_passes(mod):
    fh = FakeHandler(headers={})
    assert not mod.DictationHandler._reject_oversized_body(fh, mod.MAX_JSON_BODY_BYTES)


def test_read_json_body_normal(mod):
    payload = json.dumps({"a": 1}).encode()
    fh = FakeHandler(headers={"Content-Length": str(len(payload))}, body=payload)
    assert mod.DictationHandler.read_json_body(fh) == {"a": 1}


def test_read_json_body_oversized_raises(mod):
    fh = FakeHandler(headers={"Content-Length": str(mod.MAX_JSON_BODY_BYTES + 1)})
    with pytest.raises(ValueError):
        mod.DictationHandler.read_json_body(fh)


@pytest.mark.parametrize("bad", ["abc", "-5"])
def test_read_json_body_malformed_length_raises(mod, bad):
    fh = FakeHandler(headers={"Content-Length": bad})
    with pytest.raises(ValueError):
        mod.DictationHandler.read_json_body(fh)


# ─── 2. Path traversal guards ────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "", "../x", "..", "a/b", "a\\b", "a\x00b", "../../etc/passwd",
    "foo/../bar", "..\\..\\win",
])
def test_safe_child_rejects_bad_names(mod, tmp_path, bad):
    assert mod._safe_child(tmp_path, bad, ".md") is None


def test_safe_child_accepts_good_name(mod, tmp_path):
    p = mod._safe_child(tmp_path, "luna-brielle", ".voice.md")
    assert p is not None
    assert p.name == "luna-brielle.voice.md"
    assert p.parent == tmp_path


@pytest.mark.parametrize("name", ["Hmm..", "Wait...", "v1..2", "..leading"])
def test_safe_child_accepts_ellipsis_names(mod, tmp_path, name):
    # ".." as a substring is a legit leaf name (ellipses in character
    # names); only a bare ".." component or an escaping resolve is unsafe.
    p = mod._safe_child(tmp_path, name, ".md")
    assert p is not None
    assert p.parent == tmp_path


def test_load_character_card_traversal_returns_empty(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "CHARACTERS_DIR", tmp_path)
    assert mod.load_character_card("../../etc/passwd") == {}
    assert mod.load_character_card("..") == {}


def test_load_persona_voice_traversal_returns_empty(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PERSONAS_DIR", tmp_path / "personas")
    (tmp_path / "personas").mkdir()
    # Secret sibling outside the personas dir must not be readable via traversal.
    (tmp_path / "secret.md").write_text("## QUICK REFERENCE\nTOP SECRET\n")
    assert mod.load_persona_voice("../secret") == ""


def test_load_persona_full_traversal_returns_empty(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PERSONAS_DIR", tmp_path / "personas")
    (tmp_path / "personas").mkdir()
    (tmp_path / "secret.md").write_text("# Secret\nTOP SECRET\n")
    assert mod.load_persona_full("../secret") == {}


def test_load_persona_voice_legit_still_works(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "PERSONAS_DIR", tmp_path)
    (tmp_path / "ayaz.voice.md").write_text("Speaks tersely.")
    out = mod.load_persona_voice("ayaz")
    assert "Speaks tersely." in out


def test_read_chat_messages_traversal_returns_empty(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "ST_CHATS_DIR", tmp_path / "chats")
    (tmp_path / "chats").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.jsonl").write_text('{"name":"x","mes":"leak"}\n')
    assert mod.ChatReader.read_chat_messages("../outside", "individual") == []


def test_transcribe_fallback_rejects_traversal_model(mod):
    with pytest.raises((FileNotFoundError, ValueError)):
        mod._transcribe_subprocess_fallback(
            pathlib.Path("/tmp/nonexistent.wav"), model="../../etc/passwd"
        )


def test_token_matches_non_ascii_no_crash(mod, monkeypatch):
    # secrets.compare_digest raises TypeError on non-ASCII str operands;
    # a LAN client sending ?token=%C3%A9 must be denied, not crash the
    # handler on the auth-exempt / and /health surface.
    monkeypatch.setattr(mod, "get_token", lambda: "sekret-token-value")
    fh = FakeHandler(client="192.168.1.50", path="/?token=%C3%A9")
    assert not mod.DictationHandler._request_is_privileged(fh)
    fh2 = FakeHandler(client="192.168.1.50",
                      headers={"Authorization": "Bearer café"})
    assert not mod.DictationHandler._request_is_privileged(fh2)


# ─── 3. Non-loopback gating of / and /health ─────────────────────────

def _privileged(mod, fh):
    return mod.DictationHandler._request_is_privileged(fh)


def test_loopback_is_privileged(mod):
    assert _privileged(mod, FakeHandler(client="127.0.0.1"))
    assert _privileged(mod, FakeHandler(client="::1"))


def test_lan_without_token_not_privileged(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_token", lambda: "sekret-token-value")
    assert not _privileged(mod, FakeHandler(client="192.168.1.50"))


def test_lan_with_bearer_header_privileged(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_token", lambda: "sekret-token-value")
    fh = FakeHandler(client="192.168.1.50",
                     headers={"Authorization": "Bearer sekret-token-value"})
    assert _privileged(mod, fh)


def test_lan_with_query_token_privileged(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_token", lambda: "sekret-token-value")
    fh = FakeHandler(client="192.168.1.50", path="/?token=sekret-token-value")
    assert _privileged(mod, fh)


def test_lan_with_wrong_token_not_privileged(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_token", lambda: "sekret-token-value")
    fh = FakeHandler(client="192.168.1.50",
                     headers={"Authorization": "Bearer wrong"},
                     path="/?token=also-wrong")
    assert not _privileged(mod, fh)


def test_root_lan_unpaired_serves_bootstrap(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_token", lambda: "sekret-token-value")
    fh = FakeHandler(client="192.168.1.50", path="/").bind(
        mod, "_request_is_privileged", "_render_html_with_embed")
    mod.DictationHandler._serve_root(fh, {})
    assert len(fh.html_sent) == 1
    page = fh.html_sent[0]
    assert page == mod.PAIRING_BOOTSTRAP_HTML
    assert "sekret-token-value" not in page
    # Bootstrap must be able to recover a sessionStorage token (reload path).
    assert "sessionStorage" in page


def test_root_lan_with_token_serves_full_ui(mod, monkeypatch):
    monkeypatch.setattr(mod, "get_token", lambda: "sekret-token-value")
    fh = FakeHandler(client="192.168.1.50", path="/?token=sekret-token-value").bind(
        mod, "_request_is_privileged", "_render_html_with_embed")
    mod.DictationHandler._serve_root(fh, {})
    assert fh.html_sent[0] != mod.PAIRING_BOOTSTRAP_HTML
    assert len(fh.html_sent[0]) > len(mod.PAIRING_BOOTSTRAP_HTML)


def test_root_loopback_serves_full_ui(mod):
    fh = FakeHandler(client="127.0.0.1", path="/").bind(
        mod, "_request_is_privileged", "_render_html_with_embed")
    mod.DictationHandler._serve_root(fh, {})
    assert fh.html_sent[0] != mod.PAIRING_BOOTSTRAP_HTML


def test_health_payload_unprivileged_is_minimal(mod):
    payload = mod.DictationHandler._health_payload(FakeHandler(), privileged=False)
    assert payload == {"status": "ok"}


def test_health_payload_privileged_is_full(mod):
    payload = mod.DictationHandler._health_payload(FakeHandler(), privileged=True)
    assert payload["status"] == "ok"
    assert "providers" in payload
    assert "mode_count" in payload


# ─── 4. Token logging + --show-token ─────────────────────────────────

def test_new_token_not_logged_in_plaintext(tmp_path, caplog):
    m = _load({"CALLIOPE_DATA_DIR": str(tmp_path)})
    with caplog.at_level(logging.INFO, logger="dictation-server"):
        tok = m.ensure_token(log_new_token=True)
    text = caplog.text
    assert tok not in text
    assert tok[:8] in text          # truncated prefix is fine
    assert "--show-token" in text   # hint to recover the full value


def test_show_token_cli(tmp_path):
    env = os.environ.copy()
    env["CALLIOPE_DATA_DIR"] = str(tmp_path)
    token_file = tmp_path / "token"
    token_file.write_text("cli-test-token-value\n")
    out = subprocess.run(
        [sys.executable, str(SRC), "--show-token"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "cli-test-token-value"


def test_show_token_cli_no_token_file(tmp_path):
    env = os.environ.copy()
    env["CALLIOPE_DATA_DIR"] = str(tmp_path)
    out = subprocess.run(
        [sys.executable, str(SRC), "--show-token"],
        env=env, capture_output=True, text=True,
    )
    assert out.returncode != 0
    assert "no token" in (out.stdout + out.stderr).lower()


# ─── 5. DICTATION_ST_DATA_ROOT ───────────────────────────────────────

def test_st_data_root_default_unchanged(mod):
    root = pathlib.Path("/mnt/hdd/AI/SillyTavern/data/default-user")
    assert mod.ST_DATA_ROOT == root
    assert mod.ST_CHATS_DIR == root / "chats"
    assert mod.ST_GROUPS_DIR == root / "groups"
    assert mod.ST_GROUP_CHATS_DIR == root / "group chats"
    assert mod.CHARACTERS_DIR == root / "characters"


def test_st_data_root_env_derives_all_dirs():
    m = _load({"DICTATION_ST_DATA_ROOT": "/tmp/st-root"})
    root = pathlib.Path("/tmp/st-root")
    assert m.ST_DATA_ROOT == root
    assert m.ST_CHATS_DIR == root / "chats"
    assert m.ST_GROUPS_DIR == root / "groups"
    assert m.ST_GROUP_CHATS_DIR == root / "group chats"
    assert m.CHARACTERS_DIR == root / "characters"


def test_individual_env_overrides_beat_root():
    m = _load({
        "DICTATION_ST_DATA_ROOT": "/tmp/st-root",
        "DICTATION_CHARACTERS_DIR": "/tmp/custom-chars",
        "DICTATION_ST_CHATS_DIR": "/tmp/custom-chats",
    })
    assert m.CHARACTERS_DIR == pathlib.Path("/tmp/custom-chars")
    assert m.ST_CHATS_DIR == pathlib.Path("/tmp/custom-chats")
    assert m.ST_GROUPS_DIR == pathlib.Path("/tmp/st-root/groups")


# ─── 6. Cache locks ──────────────────────────────────────────────────

def test_cache_locks_exist(mod):
    for lock in (mod._token_lock, mod._recent_chats_lock):
        assert hasattr(lock, "acquire") and hasattr(lock, "release")
        # Sanity: usable as context manager and not held.
        with lock:
            pass


# ─── 7. HTML embed injection ─────────────────────────────────────────

def test_render_html_embed_escapes_script_breakout(mod):
    # A crafted ?chat=</script><script>… must not break out of the
    # injected config <script> tag (reflected XSS on the embed page).
    payload = "</script><script>alert(1)</script>"
    fh = FakeHandler(client="127.0.0.1")
    html = mod.DictationHandler._render_html_with_embed(
        fh, {"embed": ["1"], "chat": [payload]}
    )
    assert payload not in html
    assert "\\u003c/script>" in html
