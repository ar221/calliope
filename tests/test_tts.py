"""TTS endpoints — Kokoro-82M proxy (validation + transport) unit tests.

Exercises the module-level helpers backing `POST /tts` and `GET /tts/voices`:

- `_validate_tts_request` — input validation (missing text, oversize, bad speed)
- `_synthesize_via_kokoro` — POSTs to kokoro-server; handler maps RuntimeError
  to 502/503 so we assert the helper raises cleanly on transport failure
- `_fetch_kokoro_voices` — TTL cache + graceful stale-on-error fall-back
- `_ensure_kokoro_alive` / `_kokoro_probe` — liveness behaviour with mocked urlopen

The HTTP handler itself (DictationHandler._handle_tts) is a thin wire; we test
its dependencies, not its socket plumbing — same architecture as
test_word_confidence / test_pipeline.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import urllib.error
from importlib.machinery import SourceFileLoader
from unittest.mock import patch, MagicMock

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


@pytest.fixture(scope="module")
def mod():
    loader = SourceFileLoader("calliope_server", str(SRC))
    spec = importlib.util.spec_from_loader("calliope_server", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clear_voices_cache(mod):
    """Each test starts with an empty voices cache so TTL behaviour is deterministic."""
    mod._invalidate_kokoro_voices_cache()
    yield
    mod._invalidate_kokoro_voices_cache()


# ─── _validate_tts_request ─────────────────────────────────


def test_validate_rejects_missing_text(mod):
    text, voice, speed, err = mod._validate_tts_request({})
    assert err is not None
    assert err["code"] == "missing_param"
    assert err["status"] == 400


def test_validate_rejects_blank_text(mod):
    _, _, _, err = mod._validate_tts_request({"text": "   "})
    assert err is not None
    assert err["code"] == "missing_param"


def test_validate_rejects_non_dict_body(mod):
    _, _, _, err = mod._validate_tts_request("not a dict")
    assert err is not None
    assert err["code"] == "bad_input"


def test_validate_rejects_oversize_text(mod):
    huge = "a" * (mod.TTS_MAX_TEXT_CHARS + 1)
    _, _, _, err = mod._validate_tts_request({"text": huge})
    assert err is not None
    assert err["code"] == "text_too_long"
    assert err["status"] == 400


def test_validate_defaults_voice_when_missing(mod):
    text, voice, speed, err = mod._validate_tts_request({"text": "hello"})
    assert err is None
    assert text == "hello"
    assert voice == mod.KOKORO_DEFAULT_VOICE
    assert speed == 1.0


def test_validate_defaults_voice_when_blank(mod):
    _, voice, _, err = mod._validate_tts_request({"text": "hi", "voice": "  "})
    assert err is None
    assert voice == mod.KOKORO_DEFAULT_VOICE


def test_validate_accepts_custom_voice_and_speed(mod):
    text, voice, speed, err = mod._validate_tts_request({
        "text": "ok", "voice": "af_bella", "speed": 1.25,
    })
    assert err is None
    assert voice == "af_bella"
    assert speed == 1.25


def test_validate_rejects_speed_out_of_band(mod):
    _, _, _, err = mod._validate_tts_request({"text": "ok", "speed": 5.0})
    assert err is not None
    assert err["code"] == "bad_input"


def test_validate_rejects_speed_non_numeric(mod):
    _, _, _, err = mod._validate_tts_request({"text": "ok", "speed": "fast"})
    assert err is not None
    assert err["code"] == "bad_input"


# ─── _synthesize_via_kokoro ────────────────────────────────


def _fake_response(body: bytes, content_type: str = "audio/wav", status: int = 200):
    """Build a context-manager-shaped response object for urlopen mocking."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.headers = {"Content-Type": content_type}
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: False
    return resp


def test_synthesize_returns_audio_bytes(mod):
    fake_wav = b"RIFF\x00\x00\x00\x00WAVEfmt fakefakefake"
    with patch.object(mod.urllib.request, "urlopen",
                      return_value=_fake_response(fake_wav)) as mocked:
        audio, ctype = mod._synthesize_via_kokoro("hello world", "af_heart", speed=1.0)
    assert audio == fake_wav
    assert ctype == "audio/wav"
    # Verify body shape sent to kokoro-server.
    call_req = mocked.call_args[0][0]
    sent = json.loads(call_req.data.decode("utf-8"))
    assert sent == {"text": "hello world", "voice": "af_heart", "speed": 1.0}
    assert call_req.full_url.endswith("/synthesize")
    assert call_req.method == "POST"


def test_synthesize_raises_runtime_error_on_transport_failure(mod):
    with patch.object(mod.urllib.request, "urlopen",
                      side_effect=ConnectionRefusedError("nope")):
        with pytest.raises(RuntimeError, match="kokoro-server request failed"):
            mod._synthesize_via_kokoro("hi", "af_heart")


def test_synthesize_raises_on_http_error(mod):
    err = urllib.error.HTTPError(
        "http://127.0.0.1:9002/synthesize", 500, "Internal Server Error", {},
        io.BytesIO(b"boom"),
    )
    with patch.object(mod.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(RuntimeError, match="kokoro-server HTTP 500"):
            mod._synthesize_via_kokoro("hi", "af_heart")


# ─── _fetch_kokoro_voices ──────────────────────────────────


def test_fetch_voices_returns_payload_and_caches(mod):
    body = {"voices": [
        {"id": "af_heart", "label": "af_heart"},
        {"id": "af_bella", "label": "af_bella"},
    ]}
    raw = json.dumps(body).encode()
    with patch.object(mod.urllib.request, "urlopen",
                      return_value=_fake_response(raw, content_type="application/json")
                      ) as mocked:
        first = mod._fetch_kokoro_voices()
        second = mod._fetch_kokoro_voices()  # served from cache
    assert first == body
    assert second == body
    assert mocked.call_count == 1, "Second call should hit the in-memory TTL cache"


def test_fetch_voices_serves_stale_on_upstream_error(mod):
    body = {"voices": [{"id": "af_heart", "label": "af_heart"}]}
    raw = json.dumps(body).encode()
    # Prime cache.
    with patch.object(mod.urllib.request, "urlopen",
                      return_value=_fake_response(raw, content_type="application/json")):
        primed = mod._fetch_kokoro_voices()
    assert primed == body
    # Force refresh; upstream now broken — should serve stale.
    with patch.object(mod.urllib.request, "urlopen",
                      side_effect=ConnectionRefusedError("nope")):
        served = mod._fetch_kokoro_voices(force=True)
    assert served == body


def test_fetch_voices_raises_when_no_cache_and_upstream_dead(mod):
    with patch.object(mod.urllib.request, "urlopen",
                      side_effect=ConnectionRefusedError("nope")):
        with pytest.raises(RuntimeError, match="/voices failed"):
            mod._fetch_kokoro_voices()


def test_fetch_voices_rejects_malformed_response(mod):
    raw = json.dumps({"unexpected": "shape"}).encode()
    with patch.object(mod.urllib.request, "urlopen",
                      return_value=_fake_response(raw, content_type="application/json")):
        with pytest.raises(RuntimeError, match="malformed"):
            mod._fetch_kokoro_voices()


# ─── _kokoro_probe / _ensure_kokoro_alive ──────────────────


def test_kokoro_probe_true_when_alive(mod):
    with patch.object(mod.urllib.request, "urlopen",
                      return_value=_fake_response(b"ok", content_type="application/json")):
        assert mod._kokoro_probe() is True


def test_kokoro_probe_false_when_unreachable(mod):
    with patch.object(mod.urllib.request, "urlopen",
                      side_effect=ConnectionRefusedError("nope")):
        assert mod._kokoro_probe() is False


def test_ensure_kokoro_alive_short_circuits_when_already_up(mod):
    with patch.object(mod, "_kokoro_probe", return_value=True), \
            patch.object(mod.subprocess, "run") as run_mock:
        assert mod._ensure_kokoro_alive() is True
    run_mock.assert_not_called()


def test_ensure_kokoro_alive_returns_false_when_systemctl_fails(mod):
    """systemctl failure path: should not raise, should return False."""
    # Probe always says down; systemctl raises — ensures we still return False.
    with patch.object(mod, "_kokoro_probe", return_value=False), \
            patch.object(mod.subprocess, "run", side_effect=OSError("no systemd")):
        assert mod._ensure_kokoro_alive(boot_timeout=0.0) is False


def test_ensure_kokoro_alive_timeout_when_boot_never_completes(mod):
    """systemctl runs cleanly but probe never flips → False after boot timeout."""
    with patch.object(mod, "_kokoro_probe", return_value=False), \
            patch.object(mod.subprocess, "run", return_value=MagicMock(returncode=0)):
        assert mod._ensure_kokoro_alive(boot_timeout=0.0) is False


# ─── Activity timestamp ────────────────────────────────────


def test_mark_tts_activity_updates_timestamp(mod, monkeypatch):
    """_last_tts_ts moves forward after _mark_tts_activity."""
    # Reset.
    with mod._last_tts_lock:
        mod._last_tts_ts = 0.0
    mod._mark_tts_activity()
    with mod._last_tts_lock:
        assert mod._last_tts_ts > 0.0
