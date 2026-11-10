"""Behaviour of the 2026-11-10 audit fixes in the server wrapper.

The handler runs on an ephemeral loopback port (plain HTTP) with the runtime
directories isolated by conftest. ffmpeg, whisper, the formatter pipeline and
SillyTavern reads are stubbed; nothing real is probed, started or contacted.
"""
from __future__ import annotations

import gzip
import http.client
import importlib.util
import json
import logging
import os
import pathlib
import socket
import sys
import threading
import time
import uuid
from importlib.machinery import SourceFileLoader

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


def _load():
    name = f"calliope_server_audit_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.fixture
def serve(mod):
    servers = []

    def start():
        httpd = mod._TLSThreadingHTTPServer(("127.0.0.1", 0), mod.DictationHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return httpd.server_address[1]

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def _json(data):
    return json.loads(data.decode())


# ─── /warm (contract 1) ──────────────────────────────────────────────

@pytest.mark.parametrize("state,status", [("warming", 202), ("ready", 200)])
def test_warm_reports_state(mod, serve, monkeypatch, state, status):
    monkeypatch.setattr(mod, "prewarm_whisper", lambda: state)
    resp, data = _request(serve(), "POST", "/warm")
    assert resp.status == status
    assert _json(data) == {"status": state}


# ─── /transcribe: normalization, entry_id, timing (contracts 2, 5, 6) ──

def _stub_transcribe(mod, monkeypatch, seen):
    def fake_normalize(src, dst, container):
        seen["container"] = container
        seen["src"] = pathlib.Path(src)
        assert seen["src"].read_bytes() == b"fake-audio"
        pathlib.Path(dst).write_bytes(b"RIFF-normalized")
        return 2.34

    def fake_transcribe(path, **kwargs):
        seen["wav"] = pathlib.Path(path)
        seen["src_gone_at_whisper"] = not seen["src"].exists()
        kwargs["timing"]["whisper_boot"] = time.monotonic()
        return "steps closer", []

    def fake_pipeline(text, mode, **kwargs):
        seen["wav_gone_at_pipeline"] = not seen["wav"].exists()
        seen["pipeline_kwargs"] = kwargs
        return "*Steps closer.*", False, "", text

    monkeypatch.setattr(mod, "normalize_audio", fake_normalize)
    monkeypatch.setattr(mod, "transcribe_with_confidence", fake_transcribe)
    monkeypatch.setattr(mod, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(mod, "snapshot_state", lambda: {"fresh": False})


def test_transcribe_normalizes_returns_entry_id_and_logs_ordered_timing(
        mod, serve, monkeypatch, caplog):
    seen: dict = {}
    _stub_transcribe(mod, monkeypatch, seen)
    caplog.set_level(logging.INFO, logger="dictation-server")
    mod.session_transcript[:] = []
    resp, data = _request(
        serve(), "POST", "/transcribe?mode=rp_enhance", body=b"fake-audio",
        headers={"Content-Type": "audio/mp4;codecs=mp4a.40.2"},
    )
    payload = _json(data)
    assert resp.status == 200
    assert seen["container"] == "mp4"
    assert seen["src_gone_at_whisper"] is True
    assert seen["wav_gone_at_pipeline"] is True
    assert not seen["wav"].exists()
    assert seen["pipeline_kwargs"]["chat_id"] == ""
    assert payload["entry_id"]
    assert [e["id"] for e in mod.session_transcript if e["role"] == "user"] == [payload["entry_id"]]

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("timing "))
    trace = json.loads(line[len("timing "):])
    assert trace["audio_s"] == 2.3
    assert trace["whisper_cold"] is True
    stages = list(trace["stages_ms"])
    expected = ["context", "upload", "ffmpeg", "whisper_boot", "whisper"]
    assert [s for s in stages if s in expected] == expected
    assert stages[-1] == "respond"
    assert "steps closer" not in line.lower()


def test_transcribe_rejects_unknown_media_type(mod, serve, monkeypatch):
    called = []
    monkeypatch.setattr(mod, "normalize_audio", lambda *a: called.append(a))
    monkeypatch.setattr(mod, "snapshot_state", lambda: {"fresh": False})
    resp, data = _request(serve(), "POST", "/transcribe?mode=plain", body=b"x",
                          headers={"Content-Type": "application/x-mpegurl"})
    assert resp.status == 415
    assert _json(data)["code"] == "unsupported_media_type"
    assert called == []


def test_transcribe_distinguishes_missing_ffmpeg(mod, serve, monkeypatch):
    def missing(src, dst, container):
        raise mod.FfmpegMissingError("ffmpeg not found")

    monkeypatch.setattr(mod, "normalize_audio", missing)
    monkeypatch.setattr(mod, "snapshot_state", lambda: {"fresh": False})
    resp, data = _request(serve(), "POST", "/transcribe?mode=plain", body=b"x",
                          headers={"Content-Type": "audio/webm"})
    assert _json(data)["code"] == "ffmpeg_missing"
    assert resp.status == 503


def test_transcribe_rejects_oversized_decoded_audio(mod, serve, monkeypatch):
    def too_long(src, dst, container):
        raise mod.AudioTooLongError("too long")

    monkeypatch.setattr(mod, "normalize_audio", too_long)
    monkeypatch.setattr(mod, "snapshot_state", lambda: {"fresh": False})
    resp, data = _request(serve(), "POST", "/transcribe?mode=plain", body=b"x",
                          headers={"Content-Type": "audio/ogg;codecs=opus"})
    assert resp.status == 413
    assert _json(data)["code"] == "audio_too_long"


# ─── /reformat: SC-04 no-ID regen, F16 validation, contract 3 ─────────

class _ReformatHandler:
    def __init__(self, body, sent):
        self._body = body
        self._sent = sent

    def read_json_body(self):
        return self._body

    def send_json(self, data, status=200):
        self._sent.update(status=status, data=data)

    def send_error_json(self, message, code="error", status=400, **kwargs):
        self._sent.update(status=status, data={"error": message, "code": code})

    def _build_chat_context(self, chat_source):
        return ""


def test_no_id_regeneration_targets_entry_resolved_before_llm(mod, monkeypatch):
    sent: dict = {}
    seen: dict = {}
    monkeypatch.setattr(mod, "snapshot_state", lambda: {"fresh": True, "chatId": "chat-7"})

    def pipeline_with_concurrent_take(text, mode, **kwargs):
        seen.update(kwargs)
        with mod.transcript_lock:
            mod.session_transcript.append(
                {"id": "c", "role": "user", "text": "a newer take", "starred": False, "chat_id": ""})
        return "*Regenerated.*", False, "", text

    monkeypatch.setattr(mod, "run_pipeline", pipeline_with_concurrent_take)
    mod.session_transcript[:] = [
        {"id": "a", "role": "user", "text": "first", "starred": False, "chat_id": ""},
        {"id": "b", "role": "user", "text": "second", "starred": True, "chat_id": "chat-7"},
    ]
    mod.DictationHandler._handle_reformat(
        _ReformatHandler({"text": "second", "mode": "rp_enhance"}, sent), {})

    assert sent["status"] == 200
    assert sent["data"]["entry_id"] == "b"
    by_id = {e["id"]: e for e in mod.session_transcript}
    assert by_id["b"]["text"] == "*Regenerated.*"
    assert by_id["b"]["starred"] is True
    assert by_id["c"]["text"] == "a newer take"
    assert [e["id"] for e in mod.session_transcript] == ["a", "b", "c"]
    assert seen["reformat"] is True
    assert seen["chat_id"] == "chat-7"
    mod.session_transcript[:] = []


@pytest.mark.parametrize("body", [
    {"mode": 1, "text": "hello"},
    {"mode": "rp_enhance", "text": "hello", "chat_source": ["x"]},
    {"mode": "rp_enhance", "text": {"a": 1}},
])
def test_reformat_non_string_fields_are_400_json(mod, serve, body):
    raw = json.dumps(body).encode()
    resp, data = _request(serve(), "POST", "/reformat", body=raw,
                          headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert _json(data)["code"] == "bad_input"


def test_get_reader_exception_returns_json_500(mod, serve, monkeypatch):
    def boom():
        raise RuntimeError("synthetic reader failure")

    monkeypatch.setattr(mod, "discover_personas", boom)
    resp, data = _request(serve(), "GET", "/personas")
    assert resp.status == 500
    assert _json(data)["code"] == "server_error"


def test_scoped_wipe_all_reports_zero_kept_starred(mod, serve):
    mod.session_transcript[:] = [
        {"id": "s", "role": "user", "text": "x", "starred": True, "chat_id": "X"},
        {"id": "o", "role": "user", "text": "y", "starred": True, "chat_id": "Y"},
    ]
    resp, data = _request(serve(), "DELETE", "/transcript?chat=X&all=1")
    assert _json(data) == {"cleared": 1, "kept_starred": 0, "chat": "X"}
    resp, data = _request(serve(), "DELETE", "/transcript?chat=Y")
    assert _json(data)["kept_starred"] == 1
    mod.session_transcript[:] = []


# ─── Exact state chat (contract 4) ───────────────────────────────────

_STATE = {"fresh": True, "chatId": "chat-1", "chatType": "solo",
          "characterId": "Ann.png", "characterName": "Ann"}
_MSGS = [{"name": "Ann", "is_user": False, "is_system": False, "text": "hello there"}]


def test_active_chat_use_state_resolves_exact_chat(mod, serve, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "snapshot_state", lambda: dict(_STATE))
    monkeypatch.setattr(mod.ChatReader, "read_state_chat",
                        staticmethod(lambda state, last_n: calls.append(state) or (_MSGS, False)))

    def newest_guess():
        raise AssertionError("newest-mtime guess must not be used")

    monkeypatch.setattr(mod.ChatReader, "get_active_chat", staticmethod(newest_guess))
    resp, data = _request(serve(), "GET", "/active-chat?use_state=1")
    payload = _json(data)
    assert resp.status == 200
    assert payload["name"] == "Ann"
    assert payload["chatType"] == "solo"
    assert "hello there" in payload["context_text"]
    assert calls and calls[0]["chatId"] == "chat-1"


def test_state_derived_chat_source_uses_exact_chat(mod, monkeypatch):
    monkeypatch.setattr(mod, "snapshot_state", lambda: dict(_STATE))
    monkeypatch.setattr(mod.ChatReader, "read_state_chat",
                        staticmethod(lambda state, last_n: (_MSGS, False)))

    def by_name(*a, **k):
        raise AssertionError("name lookup must not be used for the state's chat")

    monkeypatch.setattr(mod.ChatReader, "read_chat_messages", staticmethod(by_name))
    handler = mod.DictationHandler.__new__(mod.DictationHandler)
    assert "hello there" in handler._build_chat_context("solo:chat-1")
    assert "hello there" in handler._build_chat_context("auto")


# ─── UI headers, gzip/ETag, embed origins (F22/SEC-02, contract 9) ────

def test_static_shell_headers_gzip_and_etag(mod, serve):
    port = serve()
    resp, body = _request(port, "GET", "/", headers={"Accept-Encoding": "gzip"})
    assert resp.status == 200
    assert resp.getheader("Content-Encoding") == "gzip"
    assert gzip.decompress(body).decode() == mod.WEB_UI
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert resp.getheader("Referrer-Policy") == "no-referrer"
    csp = resp.getheader("Content-Security-Policy")
    assert csp.startswith("frame-ancestors 'self'")
    assert "https://localhost:*" in csp
    etag = resp.getheader("ETag")
    assert etag
    resp2, body2 = _request(port, "GET", "/", headers={"If-None-Match": etag})
    assert resp2.status == 304
    assert body2 == b""
    resp3, body3 = _request(port, "GET", "/")
    assert resp3.getheader("Content-Encoding") is None
    assert body3.decode() == mod.WEB_UI


def test_embed_variant_is_no_store_uncompressed_with_allowed_origins(mod, serve):
    resp, body = _request(serve(), "GET", "/?embed=1", headers={"Accept-Encoding": "gzip"})
    assert resp.getheader("Content-Encoding") is None
    assert resp.getheader("Cache-Control") == "no-store"
    assert resp.getheader("ETag") is None
    html = body.decode()
    start = html.index("window.DICTATION_EMBED_CONFIG = ") + len("window.DICTATION_EMBED_CONFIG = ")
    cfg = json.loads(html[start:html.index(";</script>", start)])
    assert "https://localhost" in cfg["allowedOrigins"]
    assert "https://[::1]" in cfg["allowedOrigins"]


def test_json_responses_are_no_store(mod, serve):
    resp, _ = _request(serve(), "GET", "/health")
    assert resp.getheader("Cache-Control") == "no-store"


# ─── SSE ping + subscriber cap (contract 7, F32) ─────────────────────

def _open_sse(port):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    return sock


def _read_until(sock, marker, limit=5.0):
    buf = b""
    deadline = time.monotonic() + limit
    while marker not in buf and time.monotonic() < deadline:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.fixture
def sse(mod, monkeypatch):
    """Fast pings, and wait for closed streams to unsubscribe afterwards."""
    monkeypatch.setattr(mod.DictationHandler, "sse_ping_interval", 0.2)
    yield
    deadline = time.monotonic() + 5
    while mod.events.event_subscribers and time.monotonic() < deadline:
        time.sleep(0.05)


def test_sse_sends_named_ping_event(mod, serve, sse):
    sock = _open_sse(serve())
    try:
        buf = _read_until(sock, b"event: ping")
        assert b"event: ready" in buf
        ping_line = buf.split(b"event: ping\ndata: ", 1)[1].split(b"\n", 1)[0]
        assert isinstance(json.loads(ping_line)["ts"], float)
    finally:
        sock.close()


def test_sse_subscriber_cap_returns_503(mod, serve, sse, monkeypatch):
    monkeypatch.setattr(mod.config, "MAX_SSE_SUBSCRIBERS", 1)
    port = serve()
    first = _open_sse(port)
    try:
        _read_until(first, b"event: ready")
        resp, data = _request(port, "GET", "/events")
        assert resp.status == 503
        assert _json(data)["code"] == "sse_capacity"
    finally:
        first.close()


# ─── Admission cap (SEC-03/F3) ───────────────────────────────────────

def test_handler_thread_cap_refuses_with_503(mod, serve, monkeypatch):
    monkeypatch.setattr(mod.config, "MAX_HANDLER_THREADS", 1)
    port = serve()
    idle = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        time.sleep(0.3)  # let the idle connection occupy the only slot
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            assert _read_until(extra, b"\r\n\r\n").startswith(b"HTTP/1.1 503")
        finally:
            extra.close()
    finally:
        idle.close()


def test_sse_stream_hands_back_its_handler_slot(mod, serve, sse, monkeypatch):
    monkeypatch.setattr(mod.config, "MAX_HANDLER_THREADS", 1)
    port = serve()
    stream = _open_sse(port)
    try:
        _read_until(stream, b"event: ready")
        resp, _ = _request(port, "GET", "/health")
        assert resp.status == 200
    finally:
        stream.close()


# ─── Log redaction (SEC-05) ──────────────────────────────────────────

@pytest.mark.parametrize("target", [
    "/events?%74oken=SECRETVALUE",
    "/events?TOKEN=SECRETVALUE",
    "/?x=1&%63ontext=SECRETVALUE",
    "/?p%61ir=SECRETVALUE",
])
def test_encoded_sensitive_param_names_are_redacted(mod, target):
    line = mod._redact_sensitive_url_params(f'"GET {target} HTTP/1.1" 200 -')
    assert "SECRETVALUE" not in line
    assert "[REDACTED]" in line


def test_innocuous_params_are_kept(mod):
    assert "chat=abc" in mod._redact_sensitive_url_params('"GET /transcript?chat=abc HTTP/1.1"')


# ─── Certificates (F18/SEC-11, O-08) ────────────────────────────────

class _FakeOpenssl:
    """`subprocess` stand-in: `req` writes key/cert, `x509 -enddate` reports expiry."""

    def __init__(self, enddate="notAfter=Jan  1 00:00:00 2099 GMT"):
        self.calls: list[list[str]] = []
        self.enddate = enddate

    def run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[:2] == ["openssl", "req"]:
            pathlib.Path(cmd[cmd.index("-keyout") + 1]).write_text("NEW-KEY")
            pathlib.Path(cmd[cmd.index("-out") + 1]).write_text("NEW-CERT")
            return _Done("")
        if "-enddate" in cmd:
            return _Done(self.enddate)
        return _Done("")


class _Done:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def _live_pair(mod, tmp_path, monkeypatch):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("OLD-CERT")
    key.write_text("OLD-KEY")
    monkeypatch.setattr(mod, "CERT_FILE", cert)
    monkeypatch.setattr(mod, "KEY_FILE", key)
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    return cert, key


def test_self_signed_generation_installs_pair_atomically(mod, tmp_path, monkeypatch):
    cert, key = _live_pair(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "subprocess", _FakeOpenssl())
    mod._generate_self_signed_cert(sans=["127.0.0.1"])
    assert cert.read_text() == "NEW-CERT"
    assert key.read_text() == "NEW-KEY"
    assert key.stat().st_mode & 0o777 == 0o600
    assert not [p for p in tmp_path.iterdir() if ".new." in p.name]


def test_self_signed_generation_rolls_back_on_install_failure(mod, tmp_path, monkeypatch):
    cert, key = _live_pair(mod, tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "subprocess", _FakeOpenssl())
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("synthetic replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", flaky_replace)
    with pytest.raises(OSError):
        mod._generate_self_signed_cert()
    assert cert.read_text() == "OLD-CERT"
    assert key.read_text() == "OLD-KEY"
    assert not [p for p in tmp_path.iterdir() if ".new." in p.name]


def test_cert_expiry_is_cached_until_cert_changes(mod, tmp_path, monkeypatch):
    cert, _key = _live_pair(mod, tmp_path, monkeypatch)
    fake = _FakeOpenssl()
    monkeypatch.setattr(mod, "subprocess", fake)
    monkeypatch.setitem(mod._cert_expiry_cache, "key", None)
    first = mod.cert_days_remaining()
    assert first is not None and first > 0
    assert mod.cert_days_remaining() == first
    assert len(fake.calls) == 1
    cert.write_text("REPLACED-CERT-WITH-DIFFERENT-SIZE")
    mod.cert_days_remaining()
    assert len(fake.calls) == 2


def test_renewal_refreshes_persisted_fingerprint_after_reload(mod, tmp_path, monkeypatch):
    fp_file = tmp_path / "cert.fingerprint"
    fp_file.write_text("STALE\n")
    monkeypatch.setattr(mod, "CERT_FINGERPRINT_FILE", fp_file)
    monkeypatch.setattr(mod, "cert_days_remaining", lambda: 1)
    monkeypatch.setattr(mod, "ensure_ssl_cert", lambda: True)
    monkeypatch.setattr(mod, "_reload_ssl_context_cert", lambda: True)
    monkeypatch.setattr(mod, "cert_fingerprint_sha256", lambda: "AA:BB")
    assert mod._check_and_renew_cert() is True
    assert fp_file.read_text() == "AA:BB\n"


# ─── Token persistence (F20) ────────────────────────────────────────

def _failing_write(tok):
    raise PermissionError("synthetic read-only data dir")


def test_rotate_token_fails_nonzero_when_not_persisted(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(mod, "_write_token_file", _failing_write)
    monkeypatch.setattr(sys, "argv", ["dictation-server", "--rotate-token"])
    assert mod.main() == 1
    captured = capsys.readouterr()
    assert "rotated" not in captured.out
    assert "rotation failed" in captured.err


def test_unpersisted_token_never_claims_saved(mod, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mod, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(mod, "_write_token_file", _failing_write)
    monkeypatch.setitem(mod._token_cache, "loaded", False)
    caplog.set_level(logging.INFO, logger="dictation-server")
    assert mod.ensure_token(log_new_token=False)
    assert not any("Token saved" in r.getMessage() for r in caplog.records)
    assert any("in-memory" in r.getMessage() for r in caplog.records)


# ─── client_request_id (contract addition) ──────────────────────────

@pytest.mark.parametrize("client_id,honoured", [
    ("abcdefghijklmnop", True),
    ("A1_b2-C3d4E5f6G7h8I9j0K1l2M3n4O5p6"[:32], True),
    ("short", False),
    ("has space in it 1234567", False),
    ("x" * 33, False),
])
def test_client_request_id_is_used_verbatim_when_well_formed(mod, serve, monkeypatch, client_id, honoured):
    seen: dict = {}
    _stub_transcribe(mod, monkeypatch, seen)
    events = []
    monkeypatch.setattr(mod, "broadcast_event", lambda event, payload: events.append(payload) or 1)
    import urllib.parse as up
    resp, data = _request(
        serve(), "POST",
        "/transcribe?mode=rp_enhance&client_request_id=" + up.quote(client_id),
        body=b"fake-audio", headers={"Content-Type": "audio/webm"},
    )
    rid = _json(data)["request_id"]
    assert (rid == client_id) is honoured
    assert events and all(p.get("requestId") == rid for p in events if "requestId" in p)
    mod.session_transcript[:] = []


# ─── State adoption only for the state's own chat ───────────────────

def _write_chat(mod, character: str, chat_id: str, line: str) -> None:
    folder = pathlib.Path(mod.config.ST_CHATS_DIR) / character
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{chat_id}.jsonl").write_text(
        json.dumps({"chat_metadata": {}}) + "\n"
        + json.dumps({"name": character, "is_user": False, "mes": line}) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def two_chats(mod, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    ann, bob = f"Ann{tag}", f"Bob{tag}"
    _write_chat(mod, ann, "chat-A", "ann chat line")
    _write_chat(mod, bob, "chat-B", "bob chat line")
    state = {"fresh": True, "chatId": "chat-A", "chatType": "solo",
             "characterId": f"{ann}.png", "characterName": ann,
             "personaId": "persona-x", "mode": "rp_enhance",
             "lastAiMessage": "state last ai", "sceneContinuity": "state scene"}
    monkeypatch.setattr(mod, "snapshot_state", lambda: dict(state))
    return ann, bob


def _transcribe_kwargs(mod, serve, monkeypatch, query):
    seen: dict = {}
    state_fn = mod.snapshot_state  # the two_chats state; _stub_transcribe replaces it
    _stub_transcribe(mod, monkeypatch, seen)
    monkeypatch.setattr(mod, "snapshot_state", state_fn)
    resp, data = _request(serve(), "POST", "/transcribe?use_state=1" + query,
                          body=b"fake-audio", headers={"Content-Type": "audio/webm"})
    assert resp.status == 200, data
    entry_chat = next((e["chat_id"] for e in mod.session_transcript if e["role"] == "user"), None)
    mod.session_transcript[:] = []
    return seen["pipeline_kwargs"], _json(data), entry_chat


def test_explicit_other_chat_does_not_adopt_state_chat_data(mod, serve, monkeypatch, two_chats):
    ann, bob = two_chats
    kwargs, payload, entry_chat = _transcribe_kwargs(
        mod, serve, monkeypatch, f"&chat_source=individual:{bob}")
    assert "bob chat line" in kwargs["chat_context"]
    assert "ann chat line" not in kwargs["chat_context"]
    assert kwargs["context"] == ""
    assert kwargs["scene_continuity"] == ""
    assert kwargs["chat_id"] == ""
    assert payload["chat_id"] == "" and entry_chat == ""
    assert kwargs["character_id"] == ann.lower()  # cast still follows state


def test_no_source_follows_state_chat_exactly(mod, serve, monkeypatch, two_chats):
    ann, _bob = two_chats
    kwargs, payload, entry_chat = _transcribe_kwargs(mod, serve, monkeypatch, "")
    assert "ann chat line" in kwargs["chat_context"]
    assert kwargs["context"] == "state last ai"
    assert kwargs["scene_continuity"] == "state scene"
    assert kwargs["chat_id"] == "chat-A"
    assert payload["chat_id"] == "chat-A" and entry_chat == "chat-A"


def test_manual_source_adopts_cast_only(mod, serve, monkeypatch, two_chats):
    ann, _bob = two_chats
    kwargs, payload, entry_chat = _transcribe_kwargs(mod, serve, monkeypatch, "&chat_source=manual")
    assert kwargs["chat_context"] == ""
    assert kwargs["context"] == ""
    assert kwargs["scene_continuity"] == ""
    assert kwargs["chat_id"] == ""
    assert kwargs["character_id"] == ann.lower()
    assert kwargs["persona_id"] == "persona-x"


def test_reformat_for_other_chat_does_not_take_state_scene(mod, monkeypatch, two_chats):
    _ann, bob = two_chats
    sent: dict = {}
    seen: dict = {}
    monkeypatch.setattr(mod, "run_pipeline",
                        lambda text, mode, **kw: seen.update(kw) or ("*x*", False, "", text))
    handler = _ReformatHandler(
        {"text": "hi", "mode": "rp_enhance", "chat_source": f"individual:{bob}",
         "persist_transcript": False}, sent)
    mod.DictationHandler._handle_reformat(handler, {})
    assert seen["scene_continuity"] == ""
    assert seen["chat_id"] == ""
