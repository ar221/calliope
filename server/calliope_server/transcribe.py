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
from pathlib import Path

from . import config
from .config import _safe_child

log = logging.getLogger("dictation-server")

# Phase 2 — last-transcribe timestamp (monotonic) drives the idle-shutdown
# thread. Updated after every successful whisper-server transcription.
_last_transcribe_ts: float = 0.0
_last_transcribe_lock = threading.Lock()
_idle_shutdown_thread: threading.Thread | None = None


def _whisper_server_probe(timeout: float = 1.0) -> bool:
    """Best-effort liveness probe against whisper-server.

    whisper.cpp's whisper-server serves an index page at GET / — any non-error
    response means the process is up and the HIP context is allocated. We
    don't care about response body, only that the socket accepts the request
    within `timeout` seconds.
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
    except Exception:
        return False


def _ensure_whisper_server_alive(boot_timeout: float | None = None) -> bool:
    """Probe whisper-server; if dead, try `systemctl --user start whisper-server`
    and poll up to `boot_timeout` seconds for liveness. Returns True on alive.
    """
    if boot_timeout is None:
        boot_timeout = config.WHISPER_SERVER_HEALTH_TIMEOUT
    if _whisper_server_probe(timeout=1.0):
        return True
    log.info("whisper-server not responding — attempting `systemctl --user start whisper-server`")
    try:
        subprocess.run(
            ["systemctl", "--user", "start", "whisper-server"],
            check=False, capture_output=True, timeout=10,
        )
    except Exception as e:
        log.warning(f"systemctl start whisper-server failed: {e}")
        return False

    deadline = time.monotonic() + boot_timeout
    while time.monotonic() < deadline:
        if _whisper_server_probe(timeout=1.0):
            log.info("whisper-server up")
            return True
        time.sleep(0.25)
    log.warning("whisper-server did not become healthy within %.1fs", boot_timeout)
    return False


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
        "response_format": "json",
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

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Transcription timed out after 120s — try a smaller model (medium/small/base) "
            "or a shorter clip."
        )
    if result.returncode != 0:
        raise RuntimeError(f"whisper-cli failed: {result.stderr}")

    text = result.stdout.strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _mark_transcribe_activity() -> None:
    """Update the idle-shutdown timer's last-active timestamp."""
    global _last_transcribe_ts
    with _last_transcribe_lock:
        _last_transcribe_ts = time.monotonic()


def _idle_shutdown_loop() -> None:
    """Background thread: stop whisper-server after N seconds of inactivity.

    Daemon thread, started at server boot. Polls every IDLE_SHUTDOWN_CHECK_INTERVAL.
    Disabled entirely when IDLE_SHUTDOWN_DISABLED is set (users who keep the
    GPU-heavy ComfyUI pipeline off can leave whisper-server warm).
    """
    global _last_transcribe_ts
    if config.IDLE_SHUTDOWN_DISABLED:
        log.info("Idle-shutdown disabled by env (WHISPER_IDLE_SHUTDOWN_DISABLED)")
        return
    log.info(
        "Idle-shutdown thread started; will stop whisper-server after %ds idle",
        config.IDLE_SHUTDOWN_SECONDS,
    )
    while True:
        time.sleep(config.IDLE_SHUTDOWN_CHECK_INTERVAL)
        with _last_transcribe_lock:
            last = _last_transcribe_ts
        if last <= 0:
            continue  # never been used in this process
        idle_for = time.monotonic() - last
        if idle_for < config.IDLE_SHUTDOWN_SECONDS:
            continue
        if not _whisper_server_probe(timeout=1.0):
            # Already down — clear marker so we don't keep spamming systemctl.
            with _last_transcribe_lock:
                _last_transcribe_ts = 0.0
            continue
        log.info(
            "whisper-server idle for %.0fs — stopping to release VRAM", idle_for,
        )
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", "whisper-server"],
                check=False, capture_output=True, timeout=10,
            )
        except Exception as e:
            log.warning(f"systemctl stop whisper-server failed: {e}")
        with _last_transcribe_lock:
            _last_transcribe_ts = 0.0


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
    prompt: str = "", language: str = "en",
) -> tuple[str, list[dict]]:
    """Same as transcribe() but also returns per-word/-token confidence list.

    The confidence list is empty when:
    - whisper-server returned text only (no segments / tokens), or
    - the subprocess fallback path ran (whisper-cli stdout has no logprob).

    MVP-9 (ADR-2): exposes the JSON-mode confidence stream the persistent
    server can produce, so downstream UI ('did you mean?', risk gating) can
    consume it without re-running inference. No consumer wired yet —
    pass-through only.
    """
    if _ensure_whisper_server_alive():
        try:
            text, raw = _transcribe_whisper_server(
                audio_path, prompt=prompt, language=language,
            )
            _mark_transcribe_activity()
            return text, extract_word_confidences(raw)
        except RuntimeError as e:
            log.warning("whisper-server transcription failed: %s — falling back to subprocess", e)
            # Fall through to subprocess fallback.

    log.info("Using whisper-cli subprocess fallback")
    text = _transcribe_subprocess_fallback(
        audio_path, model=model, use_gpu=use_gpu, prompt=prompt, language=language,
    )
    return text, []
