"""Idle-shutdown lifecycle for whisper-server and kokoro-server.

Behaviour under test (fake clock, fake systemctl, fake HTTP — nothing real
is probed, started or stopped):

- the whisper idle loop never stops whisper-server while a transcription is
  in flight, and does stop it once the idle window passes with none running;
- the whisper-cli subprocess fallback runs one at a time;
- the Kokoro idle loop never stops Kokoro while a synthesis is in flight;
- a Kokoro boot arms the idle timer, even for a voice listing;
- voice listing serves the cache without booting Kokoro, and the refresh is
  single-flight with the short probe timeout;
- audiobook voice validation reports unknown voices up front.
"""
from __future__ import annotations

import importlib
import io
import json
import pathlib
import sys
import threading
import types

import pytest

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "server"


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
    """Stands in for the `subprocess` module; records every systemctl call."""

    TimeoutExpired = __import__("subprocess").TimeoutExpired

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, cmd, **_kwargs):
        self.calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def actions(self, unit: str) -> list[str]:
        return [c[2] for c in self.calls if c[:2] == ["systemctl", "--user"] and c[3] == unit]


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    if str(SERVER_DIR) not in sys.path:
        monkeypatch.syspath_prepend(str(SERVER_DIR))
    transcribe = importlib.import_module("calliope_server.transcribe")
    tts = importlib.import_module("calliope_server.tts")
    clock = FakeClock()
    systemctl = FakeSystemctl()
    for module in (transcribe, tts):
        monkeypatch.setattr(module, "time", clock)
        monkeypatch.setattr(module, "subprocess", systemctl)
    # Fresh lifecycle state per test; monkeypatch restores the originals.
    monkeypatch.setattr(transcribe, "_last_transcribe_ts", 0.0)
    monkeypatch.setattr(transcribe, "_transcribe_in_flight", 0)
    monkeypatch.setattr(transcribe, "_whisper_lifecycle_lock", threading.Lock())
    monkeypatch.setattr(transcribe, "_fallback_slot", threading.BoundedSemaphore(1))
    monkeypatch.setattr(transcribe, "_whisper_alive_until", 0.0)
    monkeypatch.setattr(transcribe, "_stop_retry_at", 0.0)
    monkeypatch.setattr(transcribe, "_stop_backoff", 0.0)
    monkeypatch.setattr(tts, "_stop_retry_at", 0.0)
    monkeypatch.setattr(tts, "_stop_backoff", 0.0)
    monkeypatch.setattr(transcribe.config, "IDLE_SHUTDOWN_SECONDS", 300)
    monkeypatch.setattr(tts, "_last_tts_ts", 0.0)
    monkeypatch.setattr(tts, "_tts_in_flight", 0)
    monkeypatch.setattr(tts, "_kokoro_lifecycle_lock", threading.Lock())
    monkeypatch.setattr(tts, "_kokoro_voices_cache", None)
    monkeypatch.setattr(tts, "_kokoro_voices_cache_ts", 0.0)
    monkeypatch.setattr(tts, "_kokoro_voices_refresh_lock", threading.Lock())
    monkeypatch.setattr(tts.config, "KOKORO_IDLE_SHUTDOWN_SECONDS", 600)
    return types.SimpleNamespace(
        transcribe=transcribe, tts=tts, clock=clock, systemctl=systemctl,
    )


# ─── whisper-server ──────────────────────────────────────────


def test_whisper_idle_tick_skips_stop_while_request_in_flight(pkg, monkeypatch):
    tr = pkg.transcribe
    monkeypatch.setattr(tr, "_whisper_server_probe", lambda timeout=1.0: True)
    monkeypatch.setattr(tr, "_ensure_whisper_server_alive", lambda **_: True)
    stopped_mid_request: list[bool] = []

    def slow_inference(audio_path, *, prompt="", language="en"):
        # The request has been running far past the idle window.
        pkg.clock.advance(10_000)
        stopped_mid_request.append(tr._idle_shutdown_tick())
        return "hello", {"text": "hello"}

    monkeypatch.setattr(tr, "_transcribe_whisper_server", slow_inference)
    text, _ = tr.transcribe_with_confidence(pathlib.Path("clip.wav"))

    assert text == "hello"
    assert stopped_mid_request == [False]
    assert pkg.systemctl.actions("whisper-server") == []


def test_whisper_idle_tick_stops_after_window_with_none_in_flight(pkg, monkeypatch):
    tr = pkg.transcribe
    monkeypatch.setattr(tr, "_whisper_server_probe", lambda timeout=1.0: True)
    monkeypatch.setattr(tr, "_ensure_whisper_server_alive", lambda **_: True)
    monkeypatch.setattr(
        tr, "_transcribe_whisper_server",
        lambda audio_path, *, prompt="", language="en": ("hi", {"text": "hi"}),
    )
    tr.transcribe_with_confidence(pathlib.Path("clip.wav"))

    pkg.clock.advance(299)
    assert tr._idle_shutdown_tick() is False
    assert pkg.systemctl.actions("whisper-server") == []

    pkg.clock.advance(2)
    assert tr._idle_shutdown_tick() is True
    assert pkg.systemctl.actions("whisper-server") == ["stop"]
    # Disarmed after the stop: a further tick does nothing.
    assert tr._idle_shutdown_tick() is False
    assert pkg.systemctl.actions("whisper-server") == ["stop"]


def test_whisper_probe_read_timeout_counts_as_busy_not_dead(pkg, monkeypatch):
    tr = pkg.transcribe

    def busy(req, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr(tr.urllib.request, "urlopen", busy)
    assert tr._whisper_server_probe() is True
    assert tr._ensure_whisper_server_alive() is True
    assert pkg.systemctl.actions("whisper-server") == []


def test_whisper_boot_arms_idle_timer(pkg, monkeypatch):
    tr = pkg.transcribe
    answers = iter([False, True])
    monkeypatch.setattr(tr, "_whisper_server_probe", lambda timeout=1.0: next(answers))
    assert tr._ensure_whisper_server_alive(boot_timeout=5.0) is True
    assert pkg.systemctl.actions("whisper-server") == ["start"]
    assert tr._last_transcribe_ts > 0


def test_subprocess_fallback_is_single_flight(pkg, monkeypatch, tmp_path):
    tr = pkg.transcribe
    monkeypatch.setattr(tr.config, "MODEL_DIR", tmp_path)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"model")

    running = 0
    peak = 0
    guard = threading.Lock()
    release = threading.Event()
    entered = threading.Semaphore(0)

    def fake_run(cmd, **_kwargs):
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
        entered.release()
        release.wait(5)
        with guard:
            running -= 1
        return types.SimpleNamespace(returncode=0, stdout=" text ", stderr="")

    monkeypatch.setattr(tr, "subprocess", types.SimpleNamespace(
        run=fake_run, TimeoutExpired=FakeSystemctl.TimeoutExpired,
    ))
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(
            tr._transcribe_subprocess_fallback(pathlib.Path("a.wav"), model="tiny")))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    assert entered.acquire(timeout=5)
    # Only one whisper-cli may be running; the others wait for the slot.
    assert not entered.acquire(timeout=0.2)
    release.set()
    for t in threads:
        t.join(5)
    assert results == ["text", "text", "text"]
    assert peak == 1


def test_subprocess_fallback_gives_up_when_slot_stays_busy(pkg, monkeypatch, tmp_path):
    tr = pkg.transcribe
    monkeypatch.setattr(tr.config, "MODEL_DIR", tmp_path)
    (tmp_path / "ggml-tiny.bin").write_bytes(b"model")
    monkeypatch.setattr(tr, "_FALLBACK_TIMEOUT_S", 0.05)
    assert tr._fallback_slot.acquire(blocking=False)
    try:
        with pytest.raises(RuntimeError, match="busy"):
            tr._transcribe_subprocess_fallback(pathlib.Path("a.wav"), model="tiny")
    finally:
        tr._fallback_slot.release()
    assert pkg.systemctl.calls == []


# ─── kokoro-server ───────────────────────────────────────────


class _Resp(io.BytesIO):
    status = 200

    def __init__(self, body: bytes, ctype: str = "audio/wav"):
        super().__init__(body)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_kokoro_idle_tick_refuses_stop_during_synthesis(pkg, monkeypatch):
    tts = pkg.tts
    monkeypatch.setattr(tts, "_kokoro_probe", lambda timeout=1.0: True)
    verdicts: list[bool] = []

    def slow_synth(req, timeout):
        pkg.clock.advance(10_000)  # an audiobook segment far past the window
        verdicts.append(tts._kokoro_idle_tick())
        return _Resp(b"RIFF")

    monkeypatch.setattr(tts.urllib.request, "urlopen", slow_synth)
    audio, _ = tts._synthesize_via_kokoro("hello", "af_heart")

    assert audio == b"RIFF"
    assert verdicts == [False]
    assert pkg.systemctl.actions("kokoro-server") == []

    # Once nothing is in flight, the window restarts from the end of synthesis.
    pkg.clock.advance(599)
    assert tts._kokoro_idle_tick() is False
    pkg.clock.advance(2)
    assert tts._kokoro_idle_tick() is True
    assert pkg.systemctl.actions("kokoro-server") == ["stop"]


def test_kokoro_claim_refreshes_idle_timer(pkg, monkeypatch):
    tts = pkg.tts
    monkeypatch.setattr(tts, "_kokoro_probe", lambda timeout=1.0: True)
    tts._mark_tts_activity()
    pkg.clock.advance(10_000)
    # A /tts request checks liveness right before synthesizing.
    assert tts._ensure_kokoro_alive() is True
    assert tts._kokoro_idle_tick() is False
    assert pkg.systemctl.actions("kokoro-server") == []


@pytest.mark.parametrize("claim", [True, False])
def test_kokoro_boot_arms_idle_timer(pkg, monkeypatch, claim):
    tts = pkg.tts
    answers = iter([False, True])
    monkeypatch.setattr(tts, "_kokoro_probe", lambda timeout=1.0: next(answers, True))
    assert tts._last_tts_ts == 0.0
    assert tts._ensure_kokoro_alive(boot_timeout=5.0, claim=claim) is True
    assert pkg.systemctl.actions("kokoro-server") == ["start"]
    assert tts._last_tts_ts > 0

    pkg.clock.advance(601)
    assert tts._kokoro_idle_tick() is True
    assert pkg.systemctl.actions("kokoro-server") == ["start", "stop"]


def test_voice_listing_serves_cache_without_booting(pkg, monkeypatch):
    tts = pkg.tts
    cached = {"voices": [{"id": "af_heart", "label": "af_heart"}]}
    monkeypatch.setattr(tts, "_kokoro_voices_cache", cached)
    monkeypatch.setattr(tts, "_kokoro_voices_cache_ts", pkg.clock.now)
    pkg.clock.advance(3600)  # stale

    def dead(req, timeout):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(tts.urllib.request, "urlopen", dead)
    assert tts._list_kokoro_voices() == cached
    assert pkg.systemctl.calls == []
    assert tts._last_tts_ts == 0.0


def test_voice_listing_cold_cache_boots_kokoro(pkg, monkeypatch):
    tts = pkg.tts
    answers = iter([False, True])
    monkeypatch.setattr(tts, "_kokoro_probe", lambda timeout=1.0: next(answers, True))
    payload = {"voices": [{"id": "af_heart", "label": "af_heart"}]}
    monkeypatch.setattr(
        tts.urllib.request, "urlopen",
        lambda req, timeout: _Resp(json.dumps(payload).encode(), "application/json"),
    )
    assert tts._list_kokoro_voices() == payload
    assert pkg.systemctl.actions("kokoro-server") == ["start"]
    assert tts._last_tts_ts > 0


def test_voices_refresh_uses_probe_timeout(pkg, monkeypatch):
    tts = pkg.tts
    seen: list[float] = []
    payload = {"voices": []}

    def fetch(req, timeout):
        seen.append(timeout)
        return _Resp(json.dumps(payload).encode(), "application/json")

    monkeypatch.setattr(tts.urllib.request, "urlopen", fetch)
    assert tts._fetch_kokoro_voices() == payload
    assert seen == [tts.config.KOKORO_PROBE_TIMEOUT]


def test_voices_refresh_is_single_flight(pkg, monkeypatch):
    tts = pkg.tts
    stale = {"voices": [{"id": "af_heart", "label": "af_heart"}]}
    fresh = {"voices": [{"id": "af_heart", "label": "af_heart"},
                        {"id": "bf_emma", "label": "bf_emma"}]}
    monkeypatch.setattr(tts, "_kokoro_voices_cache", stale)
    monkeypatch.setattr(tts, "_kokoro_voices_cache_ts", pkg.clock.now)
    pkg.clock.advance(3600)

    fetches = 0
    in_fetch = threading.Event()
    finish = threading.Event()

    def slow_fetch(req, timeout):
        nonlocal fetches
        fetches += 1
        in_fetch.set()
        finish.wait(5)
        return _Resp(json.dumps(fresh).encode(), "application/json")

    monkeypatch.setattr(tts.urllib.request, "urlopen", slow_fetch)
    refresher_result: list[dict] = []
    refresher = threading.Thread(target=lambda: refresher_result.append(tts._fetch_kokoro_voices()))
    refresher.start()
    assert in_fetch.wait(5)
    # Concurrent callers during the refresh get the stale list at once.
    assert [tts._fetch_kokoro_voices() for _ in range(5)] == [stale] * 5
    finish.set()
    refresher.join(5)
    assert refresher_result == [fresh]
    assert fetches == 1
    assert tts._fetch_kokoro_voices() == fresh


def test_validate_voices_reports_unknown_ids(pkg, monkeypatch):
    tts = pkg.tts
    monkeypatch.setattr(tts, "_kokoro_voices_cache", {"voices": [
        {"id": "af_heart", "label": "af_heart"}, {"id": "bf_emma", "label": "bf_emma"},
    ]})
    monkeypatch.setattr(tts, "_kokoro_voices_cache_ts", pkg.clock.now)
    assert tts.validate_voices({"af_heart", "zz_gone", "bf_emma", "am_old"}) == ["am_old", "zz_gone"]
    assert tts.validate_voices(["af_heart"]) == []
