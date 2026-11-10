"""Behaviour of the 2026-11-10 speech-pipeline audit fixes.

Covers ffmpeg normalization arguments and bounds, whisper liveness lease and
prewarm, idle-stop retry/backoff and start-up arming for whisper and Kokoro,
audiobook splitting, and Kokoro's lossless chunking and content-free logs.
Fake clock / fake systemctl / fake model only; nothing real runs.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import pathlib
import sys
import threading
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
KOKORO_SCRIPT = ROOT / "scripts" / "kokoro-server.py"


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSystemctl:
    TimeoutExpired = __import__("subprocess").TimeoutExpired

    def __init__(self):
        self.calls: list[list[str]] = []
        self.stop_rc = 0
        self.active_rc = 3

    def run(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        rc = 0
        if "stop" in cmd:
            rc = self.stop_rc
        elif "is-active" in cmd:
            rc = self.active_rc
        return types.SimpleNamespace(returncode=rc, stdout=b"", stderr=b"")

    def actions(self, unit: str) -> list[str]:
        return [c[2] for c in self.calls if c[:2] == ["systemctl", "--user"] and c[-1] == unit]


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    if str(SERVER_DIR) not in sys.path:
        monkeypatch.syspath_prepend(str(SERVER_DIR))
    tr = importlib.import_module("calliope_server.transcribe")
    tts = importlib.import_module("calliope_server.tts")
    clock = FakeClock()
    systemctl = FakeSystemctl()
    for module in (tr, tts):
        monkeypatch.setattr(module, "time", clock)
        monkeypatch.setattr(module, "subprocess", systemctl)
    for name, value in (("_last_transcribe_ts", 0.0), ("_transcribe_in_flight", 0),
                        ("_whisper_alive_until", 0.0), ("_stop_retry_at", 0.0),
                        ("_stop_backoff", 0.0), ("_prewarm_thread", None)):
        monkeypatch.setattr(tr, name, value)
    monkeypatch.setattr(tr, "_whisper_lifecycle_lock", threading.Lock())
    for name, value in (("_last_tts_ts", 0.0), ("_tts_in_flight", 0),
                        ("_stop_retry_at", 0.0), ("_stop_backoff", 0.0)):
        monkeypatch.setattr(tts, name, value)
    monkeypatch.setattr(tts, "_kokoro_lifecycle_lock", threading.Lock())
    monkeypatch.setattr(tr.config, "IDLE_SHUTDOWN_SECONDS", 300)
    monkeypatch.setattr(tr.config, "KOKORO_IDLE_SHUTDOWN_SECONDS", 600)
    return types.SimpleNamespace(tr=tr, tts=tts, clock=clock, systemctl=systemctl)


# ─── ffmpeg normalization (F12, S4, SEC-04) ─────────────────────────

def test_audio_container_maps_supported_types_only(pkg):
    ac = pkg.tr.audio_container
    assert ac("audio/webm;codecs=opus") == "webm"
    assert ac("Audio/MP4") == "mp4"
    assert ac("audio/ogg; codecs=opus") == "ogg"
    assert ac("audio/wav") == "wav"
    assert ac("application/vnd.apple.mpegurl") is None
    assert ac("") is None


def test_normalize_audio_forces_demuxer_local_protocols_and_duration(pkg, tmp_path, monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        pathlib.Path(cmd[-1]).write_bytes(b"\0" * (44 + 32000))
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(pkg.tr, "subprocess", types.SimpleNamespace(
        run=run, TimeoutExpired=__import__("subprocess").TimeoutExpired))
    monkeypatch.setattr(pkg.tr.config, "MAX_AUDIO_SECONDS", 600)
    dst = tmp_path / "out.wav"
    duration = pkg.tr.normalize_audio(tmp_path / "in.webm", dst, "webm")
    cmd = seen["cmd"]
    i = cmd.index("-i")
    assert "-nostdin" in cmd
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"
    assert cmd.index("-protocol_whitelist") < i
    assert cmd[cmd.index("-f") + 1] == "webm" and cmd.index("-f") < i
    assert cmd[cmd.index("-t") + 1] == "601" and cmd.index("-t") > i  # limit + sentinel
    assert cmd[cmd.index("-ar") + 1] == "16000"
    assert cmd[cmd.index("-ac") + 1] == "1"
    assert duration == pytest.approx(1.0)


def _synthetic_wav(path: pathlib.Path, seconds: float, rate: int = 48000) -> None:
    import math
    import struct
    import wave

    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<hh", s, s)
            for s in (int(8000 * math.sin(2 * math.pi * 440 * n / rate)) for n in range(frames))
        ))


@pytest.mark.parametrize("seconds,accepted", [(1.5, True), (2.0, True), (2.6, False), (5.0, False)])
def test_normalize_audio_real_ffmpeg_duration_boundary(tmp_path, monkeypatch, seconds, accepted):
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    if str(SERVER_DIR) not in sys.path:
        monkeypatch.syspath_prepend(str(SERVER_DIR))
    tr = importlib.import_module("calliope_server.transcribe")
    monkeypatch.setattr(tr.config, "MAX_AUDIO_SECONDS", 2)
    src, dst = tmp_path / "in.wav", tmp_path / "out.wav"
    _synthetic_wav(src, seconds)
    if accepted:
        assert tr.normalize_audio(src, dst, "wav") == pytest.approx(seconds, abs=0.05)
    else:
        with pytest.raises(tr.AudioTooLongError):
            tr.normalize_audio(src, dst, "wav")


def test_normalize_audio_missing_ffmpeg_and_failure(pkg, tmp_path, monkeypatch):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(pkg.tr, "subprocess", types.SimpleNamespace(
        run=missing, TimeoutExpired=__import__("subprocess").TimeoutExpired))
    with pytest.raises(pkg.tr.FfmpegMissingError):
        pkg.tr.normalize_audio(tmp_path / "a", tmp_path / "b.wav", "wav")

    monkeypatch.setattr(pkg.tr, "subprocess", types.SimpleNamespace(
        run=lambda cmd, **k: types.SimpleNamespace(returncode=1, stderr=b"bad data"),
        TimeoutExpired=__import__("subprocess").TimeoutExpired))
    with pytest.raises(pkg.tr.AudioConversionError):
        pkg.tr.normalize_audio(tmp_path / "a", tmp_path / "b.wav", "mp4")


# ─── Whisper liveness lease (O-02) and prewarm (contract 1) ─────────

def test_warm_lease_skips_probe_and_recovers_on_failure(pkg, monkeypatch):
    tr = pkg.tr
    probes = {"n": 0}
    monkeypatch.setattr(tr, "_whisper_server_probe",
                        lambda timeout=1.0: probes.__setitem__("n", probes["n"] + 1) or True)
    results = iter([("one", {}), ("two", {}), RuntimeError("socket closed"), ("three", {})])

    def inference(path, *, prompt="", language="en"):
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(tr, "_transcribe_whisper_server", inference)
    assert tr.transcribe_with_confidence(pathlib.Path("a.wav"))[0] == "one"
    assert probes["n"] == 1
    assert tr.transcribe_with_confidence(pathlib.Path("a.wav"))[0] == "two"
    assert probes["n"] == 1  # lease held: no liveness GET
    assert tr.transcribe_with_confidence(pathlib.Path("a.wav"))[0] == "three"
    assert probes["n"] == 2  # failure dropped the lease and re-probed once
    pkg.clock.advance(tr.config.WHISPER_ALIVE_LEASE_SECONDS + 1)
    tr._idle_shutdown_tick()  # inside idle window: no stop
    assert pkg.systemctl.actions("whisper-server") == []


def test_boot_stamps_whisper_boot(pkg, monkeypatch):
    tr = pkg.tr
    answers = iter([False, True])
    monkeypatch.setattr(tr, "_whisper_server_probe", lambda timeout=1.0: next(answers))
    timing: dict = {}
    assert tr._ensure_whisper_server_alive(boot_timeout=5.0, timing=timing) is True
    assert "whisper_boot" in timing
    assert pkg.systemctl.actions("whisper-server") == ["start"]


def test_prewarm_is_single_flight_and_reports_ready(pkg, monkeypatch):
    tr = pkg.tr
    release = threading.Event()
    started = []

    def slow_ensure(*args, **kwargs):
        started.append(1)
        release.wait(5)
        tr._renew_whisper_lease()
        return True

    monkeypatch.setattr(tr, "_ensure_whisper_server_alive", slow_ensure)
    assert tr.prewarm_whisper() == "warming"
    assert tr.prewarm_whisper() == "warming"
    release.set()
    tr._prewarm_thread.join(5)
    assert started == [1]
    assert tr._last_transcribe_ts > 0
    assert tr.prewarm_whisper() == "ready"


# ─── Idle stop retry/backoff (S5) and start-up arming ───────────────

def test_whisper_failed_stop_keeps_marker_and_retries_with_backoff(pkg, monkeypatch):
    tr = pkg.tr
    monkeypatch.setattr(tr, "_whisper_server_probe", lambda timeout=1.0: True)
    tr._mark_transcribe_activity()
    pkg.systemctl.stop_rc = 1
    pkg.clock.advance(301)
    assert tr._idle_shutdown_tick() is False
    assert tr._last_transcribe_ts > 0
    assert pkg.systemctl.actions("whisper-server") == ["stop"]
    assert tr._idle_shutdown_tick() is False  # inside backoff: no new attempt
    assert pkg.systemctl.actions("whisper-server") == ["stop"]
    pkg.clock.advance(tr.config.IDLE_SHUTDOWN_CHECK_INTERVAL + 1)
    pkg.systemctl.stop_rc = 0
    assert tr._idle_shutdown_tick() is True
    assert pkg.systemctl.actions("whisper-server") == ["stop", "stop"]
    assert tr._last_transcribe_ts == 0.0


def test_kokoro_failed_stop_keeps_marker_and_retries(pkg, monkeypatch):
    tts = pkg.tts
    monkeypatch.setattr(tts, "_kokoro_probe", lambda timeout=1.0: True)
    tts._mark_tts_activity()
    pkg.systemctl.stop_rc = 5
    pkg.clock.advance(601)
    assert tts._kokoro_idle_tick() is False
    assert tts._last_tts_ts > 0
    first_backoff = tts._stop_backoff
    pkg.clock.advance(first_backoff + 1)
    assert tts._kokoro_idle_tick() is False
    assert tts._stop_backoff == min(first_backoff * 2, tts.config.IDLE_STOP_RETRY_MAX_SECONDS)
    pkg.clock.advance(tts._stop_backoff + 1)
    pkg.systemctl.stop_rc = 0
    assert tts._kokoro_idle_tick() is True
    assert pkg.systemctl.actions("kokoro-server") == ["stop", "stop", "stop"]


class _StopLoop(Exception):
    pass


@pytest.mark.parametrize("which", ["kokoro", "whisper"])
def test_idle_loop_arms_timer_for_unit_already_active(pkg, monkeypatch, which):
    pkg.systemctl.active_rc = 0
    if which == "kokoro":
        module, loop, attr, unit = pkg.tts, pkg.tts._kokoro_idle_shutdown_loop, "_last_tts_ts", "kokoro-server"
        monkeypatch.setattr(module.config, "KOKORO_IDLE_SHUTDOWN_DISABLED", False)
    else:
        module, loop, attr, unit = pkg.tr, pkg.tr._idle_shutdown_loop, "_last_transcribe_ts", "whisper-server"
        monkeypatch.setattr(module.config, "IDLE_SHUTDOWN_DISABLED", False)

    def stop_sleep(_seconds):
        raise _StopLoop

    monkeypatch.setattr(pkg.clock, "sleep", stop_sleep)
    with pytest.raises(_StopLoop):
        loop()
    assert getattr(module, attr) > 0
    assert pkg.systemctl.actions(unit) == ["is-active"]


@pytest.mark.parametrize("which", ["whisper", "kokoro"])
def test_idle_instance_never_stops_unit_another_process_is_using(pkg, monkeypatch, which):
    """A second, unused server (e.g. a smoke test on another port) only has
    its own start-up arming; the primary's shared activity stamp must keep
    the unit alive until the shared stamp itself is older than the window."""
    if which == "whisper":
        module, unit, window = pkg.tr, "whisper-server", 300
        monkeypatch.setattr(module, "_whisper_server_probe", lambda timeout=1.0: True)
        arm, tick = (lambda: module._mark_transcribe_activity(shared=False)), module._idle_shutdown_tick
    else:
        module, unit, window = pkg.tts, "kokoro-server", 600
        monkeypatch.setattr(module, "_kokoro_probe", lambda timeout=1.0: True)
        arm, tick = (lambda: module._mark_tts_activity(shared=False)), module._kokoro_idle_tick
    arm()
    assert not module.config.unit_activity_file(unit).exists()
    pkg.clock.advance(window - 10)
    # The primary process records a request now (same stamp file).
    module.config.touch_unit_activity(unit, pkg.clock.time())
    pkg.clock.advance(20)  # this instance is past its own window
    assert tick() is False
    assert pkg.systemctl.actions(unit) == []
    pkg.clock.advance(window)
    assert tick() is True
    assert pkg.systemctl.actions(unit) == ["stop"]


def test_requests_touch_shared_activity_file(pkg):
    tr = pkg.tr
    tr._begin_transcribe_request()
    tr._end_transcribe_request()
    path = tr.config.unit_activity_file("whisper-server")
    assert path.stat().st_mtime == pytest.approx(pkg.clock.time())
    assert path.parent.stat().st_mode & 0o777 == 0o700


# ─── Audiobook splitting (K6) ───────────────────────────────────────

def test_audiobook_long_message_is_split_not_truncated(pkg, monkeypatch):
    tts = pkg.tts
    monkeypatch.setattr(tts.config, "TTS_MAX_TEXT_CHARS", 100)
    sentence = "The quick brown fox jumps over the lazy dog. "
    text = (sentence * 12).strip() + " " + "x" * 250
    messages, *_rest, err = tts._validate_audiobook_request({
        "messages": [{"name": "Ann", "text": text, "voice": "af_heart", "is_user": True}],
    })
    assert err is None
    assert len(messages) > 1
    assert all(len(m["text"]) <= 100 for m in messages)
    assert all(m["name"] == "Ann" and m["voice"] == "af_heart" for m in messages)
    assert all(set(m) == {"name", "text", "voice"} for m in messages)
    assert "".join(m["text"] for m in messages).replace(" ", "") == text.replace(" ", "")


def test_audiobook_cjk_message_splits_losslessly(pkg, monkeypatch):
    tts = pkg.tts
    monkeypatch.setattr(tts.config, "TTS_MAX_TEXT_CHARS", 50)
    text = ("今日は良い天気です。" * 20) + ("あ" * 120)
    messages, *_rest, err = tts._validate_audiobook_request({"messages": [{"text": text}]})
    assert err is None
    assert all(len(m["text"]) <= 50 for m in messages)
    assert "".join(m["text"] for m in messages) == text


# ─── Kokoro script: content-free logs (S1), lossless chunks (S2) ─────

np = None


def _load_kokoro(monkeypatch):
    global np
    np = pytest.importorskip("numpy")
    stub = types.ModuleType("kokoro_onnx")
    stub.Kokoro = object
    monkeypatch.setitem(sys.modules, "kokoro_onnx", stub)
    spec = importlib.util.spec_from_file_location("kokoro_server_audit", KOKORO_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SENTINEL = "zebra-sentinel-private-words"


class _SilentKokoro:
    tokenizer = None

    def create(self, text, voice, speed, lang):
        return np.zeros(0, dtype=np.float32), 24000


def test_kokoro_unrecoverable_chunk_log_has_no_text(monkeypatch, caplog):
    ks = _load_kokoro(monkeypatch)
    caplog.set_level(logging.DEBUG)
    pieces, _sr = ks._synth_recover(_SilentKokoro(), f"{SENTINEL} more words", "af_heart", 1.0)
    assert pieces == []
    assert caplog.records
    assert all(SENTINEL not in r.getMessage() for r in caplog.records)


def test_kokoro_synthesis_failure_log_has_no_text(monkeypatch, caplog):
    import http.client
    import json
    from http.server import ThreadingHTTPServer

    ks = _load_kokoro(monkeypatch)

    def failing(text, voice, speed):
        raise RuntimeError(f"phonemizer choked on {text}")

    monkeypatch.setattr(ks, "_synthesize", failing)
    monkeypatch.setattr(ks, "_ensure_loaded", lambda: None)
    monkeypatch.setattr(ks, "_voice_ids", [])
    caplog.set_level(logging.DEBUG)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ks.KokoroHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"text": SENTINEL, "voice": "af_heart"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("POST", "/synthesize", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert resp.status == 500
    assert SENTINEL.encode() not in data
    assert all(SENTINEL not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("text", [
    "あ" * 150,
    "今日は良い天気です。明日も晴れるでしょう！本当ですか？" * 6,
    "word " * 10 + "x" * 230 + " tail end.",
])
def test_kokoro_chunking_is_lossless(monkeypatch, text):
    ks = _load_kokoro(monkeypatch)
    chunks = ks._split_for_synth(text)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")
