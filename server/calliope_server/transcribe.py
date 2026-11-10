"""Whisper lifecycle + transcription layer.

Owns the persistent whisper-server process management (probe / systemctl
start / idle shutdown), the HTTP transcription path against whisper-server,
the per-request `whisper-cli` subprocess fallback, and word-confidence
parsing. Config values are read via `calliope_server.config` AT CALL TIME
(`config.X`) so tests can monkeypatch `mod.config.<NAME>` and every function
here sees the override. Extracted from the executable `calliope-server`
script (Stage 3 split).
"""

import json
import logging
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from . import config
from .config import _safe_child

log = logging.getLogger("dictation-server")

# Idle-shutdown state. `_last_transcribe_ts` (monotonic) is refreshed when a
# request starts and when it finishes; `_transcribe_in_flight` counts requests
# currently inside `transcribe_with_confidence`. Both are guarded by
# `_last_transcribe_lock`. The idle loop never stops whisper-server while a
# request is in flight or inside the idle window.
_last_transcribe_ts: float = 0.0
_transcribe_in_flight: int = 0
_last_transcribe_lock = threading.Lock()
# Serialises "is whisper-server up / start it" against the idle loop's
# "stop it", so a request can't pass the liveness probe and then lose the
# server to a stop that was already decided.
_whisper_lifecycle_lock = threading.Lock()
_idle_shutdown_thread: threading.Thread | None = None
# O-02: monotonic deadline until which whisper-server is trusted alive without
# a liveness GET (renewed by successful inference/boot, dropped on failure or
# idle stop). S5: a failed idle stop keeps the activity marker and is retried
# no earlier than `_stop_retry_at`, backing off up to IDLE_STOP_RETRY_MAX_SECONDS.
_whisper_alive_until: float = 0.0
_stop_retry_at: float = 0.0
_stop_backoff: float = 0.0
# Single-flight background prewarm for POST /warm.
_prewarm_thread: threading.Thread | None = None
_prewarm_lock = threading.Lock()

# Per-request whisper-cli fallback loads a second copy of the model into VRAM,
# so at most one runs at a time. Later callers wait for the slot (up to the
# same budget a single fallback run gets) instead of spawning another.
_FALLBACK_TIMEOUT_S = 120
_fallback_slot = threading.BoundedSemaphore(1)


def _whisper_server_probe(timeout: float = 1.0) -> bool:
    """Best-effort liveness probe against whisper-server.

    whisper.cpp's whisper-server serves an index page at GET / — any non-error
    response means the process is up and the HIP context is allocated. We
    don't care about response body, only that the socket accepts the request
    within `timeout` seconds.

    A read timeout counts as alive: the loopback socket accepted the
    connection, so the process is up and merely busy with inference. Treating
    that as dead would push the request onto the whisper-cli fallback and load
    a second model into VRAM.
    """
    try:
        req = urllib.request.Request(config.WHISPER_SERVER_URL + "/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Any 2xx/3xx counts as alive. 4xx also counts (process is up,
            # didn't like the request). 5xx is technically up but degraded;
            # treat as alive and let the inference call surface real errors.
            return resp.status < 600
    except urllib.error.HTTPError as e:
        # Returned a real HTTP response — server is alive.
        return e.code < 600
    except TimeoutError:
        # Connected but no response in time: busy, not dead.
        return True
    except Exception:
        return False


def _whisper_lease_valid() -> bool:
    with _last_transcribe_lock:
        return time.monotonic() < _whisper_alive_until


def _renew_whisper_lease() -> None:
    global _whisper_alive_until
    with _last_transcribe_lock:
        _whisper_alive_until = time.monotonic() + config.WHISPER_ALIVE_LEASE_SECONDS


def _drop_whisper_lease() -> None:
    global _whisper_alive_until
    with _last_transcribe_lock:
        _whisper_alive_until = 0.0


def _ensure_whisper_server_alive(boot_timeout: float | None = None, *,
                                 timing: dict | None = None) -> bool:
    """Probe whisper-server; if dead, try `systemctl --user start whisper-server`
    and poll up to `boot_timeout` seconds for liveness. Returns True on alive.

    Runs under `_whisper_lifecycle_lock`, so it waits out an idle stop that is
    already under way and then boots the server again instead of reporting a
    server that is about to disappear. A boot stamps `timing["whisper_boot"]`.
    """
    if boot_timeout is None:
        boot_timeout = config.WHISPER_SERVER_HEALTH_TIMEOUT
    with _whisper_lifecycle_lock:
        if _whisper_server_probe(timeout=1.0):
            _renew_whisper_lease()
            return True
        _drop_whisper_lease()
        log.info("whisper-server not responding — attempting `systemctl --user start whisper-server`")
        try:
            subprocess.run(
                ["systemctl", "--user", "start", "whisper-server"],
                check=False, capture_output=True, timeout=10,
            )
        except Exception as e:
            log.warning(f"systemctl start whisper-server failed: {e}")
            return False
        # The unit may have started even if it isn't healthy yet; arm the idle
        # timer so it is eventually stopped either way.
        _mark_transcribe_activity()

        deadline = time.monotonic() + boot_timeout
        while time.monotonic() < deadline:
            if _whisper_server_probe(timeout=1.0):
                log.info("whisper-server up")
                _renew_whisper_lease()
                if timing is not None:
                    timing["whisper_boot"] = time.monotonic()
                return True
            time.sleep(0.25)
        log.warning("whisper-server did not become healthy within %.1fs", boot_timeout)
        return False


def prewarm_whisper() -> str:
    """POST /warm: refresh the idle timer and boot whisper-server off-thread.

    Never blocks on the boot. Returns "ready" when a live lease is held,
    otherwise "warming" (a single background ensure is running or started).
    """
    global _prewarm_thread
    _mark_transcribe_activity()
    if _whisper_lease_valid():
        return "ready"
    with _prewarm_lock:
        if _prewarm_thread is None or not _prewarm_thread.is_alive():
            t = threading.Thread(target=_prewarm_run, name="whisper-prewarm", daemon=True)
            t.start()
            _prewarm_thread = t
    return "warming"


def _prewarm_run() -> None:
    try:
        _ensure_whisper_server_alive()
    except Exception as e:  # pragma: no cover — defensive, background thread
        log.warning("whisper prewarm failed: %s", e.__class__.__name__)


# ─── Upload normalization (contract: every accepted container → ffmpeg) ──
# Content-Type base → ffmpeg demuxer forced with `-f`, so ffmpeg never
# autoprobes (no playlist/concat/network demuxers) whatever the bytes claim.
AUDIO_CONTAINERS = {
    "audio/webm": "webm",
    "audio/mp4": "mp4",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
}
FFMPEG_TIMEOUT_SECONDS = 30
_WAV_HEADER_SLACK = 4096


class AudioConversionError(Exception):
    """ffmpeg rejected or failed to decode the upload."""


class FfmpegMissingError(Exception):
    """The ffmpeg executable is not installed / not on PATH."""


class AudioTooLongError(Exception):
    """Decoded audio exceeds the decoded-size bound."""


def audio_container(content_type: str) -> str | None:
    """ffmpeg input format for an upload Content-Type (codec params allowed)."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return AUDIO_CONTAINERS.get(base)


# Decode this much past MAX_AUDIO_SECONDS so an over-limit take is detected
# (its decode reaches the sentinel) instead of being silently truncated.
_DURATION_SENTINEL_SECONDS = 1
# Resampler/container padding can push an in-limit clip a hair over MAX.
_DURATION_TOLERANCE_SECONDS = 0.25


def max_decoded_wav_bytes() -> int:
    """Largest 16 kHz mono s16 WAV ffmpeg can produce under the decode cap."""
    return ((config.MAX_AUDIO_SECONDS + _DURATION_SENTINEL_SECONDS) * 16000 * 2
            + _WAV_HEADER_SLACK)


def normalize_audio(src: Path, dst: Path, container: str) -> float:
    """Decode `src` (forced `container` demuxer) to a 16 kHz mono PCM WAV at `dst`.

    Local files only (`-protocol_whitelist file,pipe`), no stdin. Decoding is
    capped one second past MAX_AUDIO_SECONDS; a take whose decoded duration
    exceeds the limit raises AudioTooLongError (never a truncated prefix).
    Returns the decoded duration in seconds.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file,pipe",
        "-f", container, "-i", str(src),
        "-t", str(config.MAX_AUDIO_SECONDS + _DURATION_SENTINEL_SECONDS),
        "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        "-f", "wav", "-y", str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except FileNotFoundError as e:
        raise FfmpegMissingError("ffmpeg not found — install it for audio conversion") from e
    except subprocess.TimeoutExpired as e:
        raise AudioConversionError(
            f"Audio conversion timed out after {FFMPEG_TIMEOUT_SECONDS}s") from e
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()[-200:]
        raise AudioConversionError(f"Audio conversion failed: {detail}")
    size = dst.stat().st_size
    too_long = AudioTooLongError(f"Decoded audio exceeds {config.MAX_AUDIO_SECONDS}s")
    if size > max_decoded_wav_bytes():
        raise too_long
    try:
        with wave.open(str(dst), "rb") as w:
            duration = w.getnframes() / float(w.getframerate() or 16000)
    except (wave.Error, EOFError, OSError):
        duration = max(0, size - 44) / 32000.0
    if duration > config.MAX_AUDIO_SECONDS + _DURATION_TOLERANCE_SECONDS:
        raise too_long
    return duration



def _build_multipart_body(fields: dict[str, str], file_field: str,
                           file_path: Path, file_content_type: str) -> tuple[bytes, str]:
    """Hand-rolled multipart/form-data encoder (stdlib has no helper).

    Returns (body_bytes, boundary). Caller sets
    Content-Type: multipart/form-data; boundary=<boundary>.
    """
    boundary = "----dictationFormBoundary" + secrets.token_hex(16)
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(b"--" + boundary.encode())
        parts.append(crlf)
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode() + crlf + crlf
        )
        parts.append(str(value).encode("utf-8"))
        parts.append(crlf)
    # File part
    file_bytes = Path(file_path).read_bytes()
    parts.append(b"--" + boundary.encode())
    parts.append(crlf)
    parts.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{Path(file_path).name}"'
        ).encode() + crlf
    )
    parts.append(f"Content-Type: {file_content_type}".encode() + crlf + crlf)
    parts.append(file_bytes)
    parts.append(crlf)
    parts.append(b"--" + boundary.encode() + b"--" + crlf)
    return b"".join(parts), boundary


def _transcribe_whisper_server(audio_path: Path, *, prompt: str = "",
                                language: str = "en") -> tuple[str, dict]:
    """POST audio to the persistent whisper-server's /inference endpoint.

    Returns (text, raw_response_json). The raw response includes whisper
    segments / per-token data when the server is asked for JSON; we capture
    the dict so downstream callers (e.g. confidence extraction stub) can
    operate on it. Raises RuntimeError on transport / parse failure so the
    caller can decide whether to fall back to subprocess.
    """
    fields: dict[str, str] = {
        # `json` returns {"text"} only. Per-word probabilities (the source of
        # the low-confidence overlay and /word-alternatives) are emitted
        # solely under `verbose_json`: segments[].words[] = {word, probability}.
        "response_format": "verbose_json",
        "temperature": "0.0",
    }
    if prompt:
        fields["prompt"] = prompt
    if language:
        fields["language"] = language
    body, boundary = _build_multipart_body(
        fields,
        file_field="file",  # whisper.cpp's whisper-server accepts `file`
        file_path=audio_path,
        file_content_type="audio/wav",
    )
    req = urllib.request.Request(
        config.WHISPER_SERVER_URL + "/inference",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=config.WHISPER_SERVER_REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # whisper-server returned an HTTP error — surface body for diagnosis.
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        raise RuntimeError(f"whisper-server HTTP {e.code}: {err_body}")
    except Exception as e:
        raise RuntimeError(f"whisper-server request failed: {e}")

    try:
        data = json.loads(raw)
    except Exception:
        # Some configurations return text/plain — accept that too.
        text_only = raw.decode("utf-8", errors="replace").strip()
        return re.sub(r"\s+", " ", text_only).strip(), {"text": text_only}
    text = ""
    if isinstance(data, dict):
        text = str(data.get("text", "")).strip()
        if not text and isinstance(data.get("segments"), list):
            text = " ".join(
                str(s.get("text", "")) for s in data["segments"] if isinstance(s, dict)
            ).strip()
    elif isinstance(data, str):
        text = data.strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text, data if isinstance(data, dict) else {"text": text}


def extract_word_confidences(response_json: dict) -> list[dict]:
    """Pull per-word/-token confidence data from whisper-server's response.

    Returns a list of {word, confidence, logprob} dicts. `confidence` is
    the linear probability (0.0–1.0); `logprob` is `math.log(confidence)`,
    which is what whisper.cpp natively threshold-checks. POL-3 uses the
    logprob form so the user-tunable threshold (-0.7 default) maps directly
    to whisper-cli's `--logprob-thold`. Empty list when the server didn't
    emit token-level data (e.g. `response_format=text`).
    """
    import math
    out: list[dict] = []
    if not isinstance(response_json, dict):
        return out
    segments = response_json.get("segments")
    if not isinstance(segments, list):
        return out

    def _attach_logprob(prob: float) -> float:
        """Linear prob → logprob, with a stable floor for prob<=0 to keep
        downstream comparisons well-defined."""
        if prob is None or prob <= 0.0:
            return -20.0
        return math.log(prob)

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        # whisper.cpp segments may carry `tokens` with logprobs, or `words`.
        words = seg.get("words")
        if isinstance(words, list):
            for w in words:
                if isinstance(w, dict) and "word" in w:
                    prob = float(w.get("probability", w.get("p", 0.0)) or 0.0)
                    out.append({
                        "word": str(w.get("word", "")),
                        "confidence": prob,
                        "logprob": _attach_logprob(prob),
                    })
            continue
        tokens = seg.get("tokens")
        if isinstance(tokens, list):
            for t in tokens:
                if isinstance(t, dict):
                    prob = float(t.get("p", t.get("prob", 0.0)) or 0.0)
                    out.append({
                        "word": str(t.get("text", t.get("token", ""))),
                        "confidence": prob,
                        "logprob": _attach_logprob(prob),
                    })
    return out


def _transcribe_subprocess_fallback(audio_path: Path, model: str = config.DEFAULT_MODEL,
                                     use_gpu: bool = True,
                                     prompt: str = "",
                                     language: str = "en") -> str:
    """Fallback to per-request `whisper-cli` subprocess when whisper-server
    is unreachable. Mirrors the original Phase 1 invocation but with the
    Phase 2 flag corrections (ADR-2) so the fallback is also flag-correct.
    """
    model_file = _safe_child(config.MODEL_DIR, f"ggml-{model}", ".bin")
    if model_file is None:
        raise ValueError(f"Invalid model name: {model!r}")
    if not model_file.exists():
        raise FileNotFoundError(f"Model not found: {model_file}")

    cmd = [
        config.WHISPER_BIN,
        "-m", str(model_file),
        "-f", str(audio_path),
        "--no-timestamps",
        "--no-prints",
        # MVP-9 / ADR-2 — match the persistent whisper-server flag set so the
        # fallback path doesn't silently lose accuracy on hallucination
        # suppression. `--carry-initial-prompt` is server-side only (single
        # request here, not chunked).
        "--suppress-nst",
        "--no-speech-thold", "0.80",
        "--logprob-thold", "-0.7",
        "--flash-attn",
    ]
    if language:
        cmd.extend(["--language", language])
    if prompt:
        cmd.extend(["--prompt", prompt])
    if not use_gpu:
        cmd.append("--no-gpu")

    if not _fallback_slot.acquire(timeout=_FALLBACK_TIMEOUT_S):
        raise RuntimeError(
            f"whisper-cli fallback busy for {_FALLBACK_TIMEOUT_S}s — another "
            "transcription is still running; retry shortly."
        )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_FALLBACK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Transcription timed out after {_FALLBACK_TIMEOUT_S}s — try a smaller model "
            "(medium/small/base) or a shorter clip."
        )
    finally:
        _fallback_slot.release()
    if result.returncode != 0:
        raise RuntimeError(f"whisper-cli failed: {result.stderr}")

    text = result.stdout.strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text


_UNIT = "whisper-server"


def _mark_transcribe_activity(*, shared: bool = True) -> None:
    """Update the idle-shutdown timer's last-active timestamp.

    `shared=False` only arms this process's timer (start-up arming) without
    claiming activity for other processes.
    """
    global _last_transcribe_ts
    with _last_transcribe_lock:
        _last_transcribe_ts = time.monotonic()
    if shared:
        config.touch_unit_activity(_UNIT, time.time())


def _begin_transcribe_request() -> None:
    """Count a request as in flight and refresh the idle timer at its start."""
    global _last_transcribe_ts, _transcribe_in_flight
    with _last_transcribe_lock:
        _transcribe_in_flight += 1
        _last_transcribe_ts = time.monotonic()
    config.touch_unit_activity(_UNIT, time.time())


def _end_transcribe_request() -> None:
    """Release an in-flight slot; the idle window restarts from now."""
    global _last_transcribe_ts, _transcribe_in_flight
    with _last_transcribe_lock:
        _transcribe_in_flight = max(0, _transcribe_in_flight - 1)
        _last_transcribe_ts = time.monotonic()
    config.touch_unit_activity(_UNIT, time.time())


def _idle_shutdown_tick() -> bool:
    """One idle-loop decision. Returns True when whisper-server was stopped.

    Holds `_whisper_lifecycle_lock` across the decision and the stop so a
    concurrent `_ensure_whisper_server_alive` either runs before (and its
    request is counted in flight) or after (and boots the server again).
    """
    global _last_transcribe_ts, _stop_retry_at, _stop_backoff
    with _whisper_lifecycle_lock:
        with _last_transcribe_lock:
            last = _last_transcribe_ts
            in_flight = _transcribe_in_flight
        if in_flight > 0 or last <= 0:
            return False  # busy, or never used in this process
        now = time.monotonic()
        idle_for = now - last
        shared_ts = config.unit_activity_mtime(_UNIT)
        if shared_ts > 0:
            # Another process's request counts too: never stop a unit whose
            # shared stamp is younger than the idle window.
            idle_for = min(idle_for, time.time() - shared_ts)
        if idle_for < config.IDLE_SHUTDOWN_SECONDS or now < _stop_retry_at:
            return False
        if not _whisper_server_probe(timeout=1.0):
            # Already down — clear marker so we don't keep spamming systemctl.
            with _last_transcribe_lock:
                _last_transcribe_ts = 0.0
            _drop_whisper_lease()
            return False
        log.info(
            "whisper-server idle for %.0fs — stopping to release VRAM", idle_for,
        )
        try:
            result = subprocess.run(
                ["systemctl", "--user", "stop", "whisper-server"],
                check=False, capture_output=True, timeout=10,
            )
            stopped = result.returncode == 0
            failure = f"exit {result.returncode}"
        except Exception as e:
            stopped = False
            failure = e.__class__.__name__
        if not stopped:
            # Keep the marker so a later tick retries; back off between tries.
            _stop_backoff = min(
                max(_stop_backoff * 2, config.IDLE_SHUTDOWN_CHECK_INTERVAL),
                config.IDLE_STOP_RETRY_MAX_SECONDS,
            )
            _stop_retry_at = time.monotonic() + _stop_backoff
            log.warning("systemctl stop whisper-server failed (%s); retrying in %.0fs",
                        failure, _stop_backoff)
            return False
        _stop_backoff = 0.0
        _stop_retry_at = 0.0
        _drop_whisper_lease()
        with _last_transcribe_lock:
            _last_transcribe_ts = 0.0
        return True


def _unit_is_active(unit: str) -> bool:
    """True when `systemctl --user is-active <unit>` reports active."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


def _idle_shutdown_loop() -> None:
    """Background thread: stop whisper-server after N seconds of inactivity.

    Daemon thread, started at server boot. Polls every IDLE_SHUTDOWN_CHECK_INTERVAL.
    Disabled entirely when IDLE_SHUTDOWN_DISABLED is set (users who keep the
    GPU-heavy ComfyUI pipeline off can leave whisper-server warm).
    """
    if config.IDLE_SHUTDOWN_DISABLED:
        log.info("Idle-shutdown disabled by env (WHISPER_IDLE_SHUTDOWN_DISABLED)")
        return
    log.info(
        "Idle-shutdown thread started; will stop whisper-server after %ds idle",
        config.IDLE_SHUTDOWN_SECONDS,
    )
    # A unit left running by a previous server process (or started outside
    # Calliope) would otherwise never be stopped: arm the timer for it.
    if _unit_is_active(_UNIT):
        _mark_transcribe_activity(shared=False)
    while True:
        time.sleep(config.IDLE_SHUTDOWN_CHECK_INTERVAL)
        _idle_shutdown_tick()


def _start_idle_shutdown_thread() -> None:
    """Start the idle-shutdown daemon thread once. Idempotent."""
    global _idle_shutdown_thread
    if _idle_shutdown_thread is not None and _idle_shutdown_thread.is_alive():
        return
    t = threading.Thread(
        target=_idle_shutdown_loop,
        name="whisper-idle-shutdown",
        daemon=True,
    )
    t.start()
    _idle_shutdown_thread = t


def transcribe(audio_path: Path, model: str = config.DEFAULT_MODEL, use_gpu: bool = True,
               prompt: str = "", language: str = "en") -> str:
    """Run whisper STT against `audio_path` and return cleaned text.

    Phase 2 (ADR-1): prefer the persistent `whisper-server` HTTP service; if
    that is unreachable and won't boot within the health timeout, fall back
    to a per-request `whisper-cli` subprocess invocation.

    `prompt` biases whisper toward terms (e.g. character names from vocab.yaml).
    `language` defaults to 'en'; pass 'auto' for whisper-detected language
    (full-Swahili utterances) or any other ISO-639-1 code for a hard hint.
    """
    text, _confs = transcribe_with_confidence(
        audio_path, model=model, use_gpu=use_gpu, prompt=prompt, language=language,
    )
    return text


def transcribe_with_confidence(
    audio_path: Path, model: str = config.DEFAULT_MODEL, use_gpu: bool = True,
    prompt: str = "", language: str = "en", timing: dict | None = None,
) -> tuple[str, list[dict]]:
    """Same as transcribe() but also returns per-word/-token confidence list.

    The confidence list is empty when:
    - whisper-server returned text only (no segments / tokens), or
    - the subprocess fallback path ran (whisper-cli stdout has no logprob).

    O-02: inside a live lease the liveness probe is skipped; if inference
    then fails, the lease is dropped and the server is re-ensured (probe or
    boot) and retried once before the whisper-cli fallback. `timing` receives
    `whisper_boot` when this call booted whisper-server.
    """
    _begin_transcribe_request()
    try:
        leased = _whisper_lease_valid()
        if leased or _ensure_whisper_server_alive(timing=timing):
            attempts = 2 if leased else 1
            for attempt in range(attempts):
                try:
                    text, raw = _transcribe_whisper_server(
                        audio_path, prompt=prompt, language=language,
                    )
                    _renew_whisper_lease()
                    return text, extract_word_confidences(raw)
                except RuntimeError as e:
                    _drop_whisper_lease()
                    if attempt + 1 < attempts and _ensure_whisper_server_alive(timing=timing):
                        continue
                    log.warning("whisper-server transcription failed: %s — falling back to subprocess", e)
                    break

        log.info("Using whisper-cli subprocess fallback")
        text = _transcribe_subprocess_fallback(
            audio_path, model=model, use_gpu=use_gpu, prompt=prompt, language=language,
        )
        return text, []
    finally:
        _end_transcribe_request()
