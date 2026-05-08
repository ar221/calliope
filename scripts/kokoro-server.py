#!/usr/bin/env python3
"""Kokoro-82M TTS server for Calliope.

Loopback-only HTTP server that loads the Kokoro ONNX model once at startup
and serves synthesis requests on 127.0.0.1:9002. Calliope's dictation-server
proxies its `/tts` endpoint here.

This script is a *runtime artifact* installed alongside a dedicated venv at
~/.local/share/calliope-tts/. It is NOT part of the calliope-server stdlib
contract — it imports kokoro-onnx, onnxruntime, numpy, and uses stdlib `wave`
for WAV encoding (no soundfile dep at runtime).

Endpoints
---------
- `GET  /health`      → {status, voices: <count>, model: <basename>}
- `GET  /voices`      → {voices: [{id, label}]}
- `POST /synthesize`  → audio/wav (16-bit PCM, 24kHz mono by default)
                        body: {text, voice?, speed?}

Env
---
- KOKORO_HOST    (default 127.0.0.1)
- KOKORO_PORT    (default 9002)
- KOKORO_MODEL   (default ~/.local/share/calliope-tts/models/onnx/model_fp16.onnx)
- KOKORO_VOICES  (default ~/.local/share/calliope-tts/models/voices-v1.0.bin)
- KOKORO_LANG    (default en-us)

Service unit: ../systemd/kokoro-server.service
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np  # type: ignore

# kokoro-onnx must be importable from the venv this script runs under.
from kokoro_onnx import Kokoro  # type: ignore

log = logging.getLogger("kokoro-server")

HOST = os.environ.get("KOKORO_HOST", "127.0.0.1")
PORT = int(os.environ.get("KOKORO_PORT", "9002"))
LANG = os.environ.get("KOKORO_LANG", "en-us")

_HOME = Path(os.path.expanduser("~"))
DEFAULT_MODEL = _HOME / ".local/share/calliope-tts/models/onnx/model_fp16.onnx"
DEFAULT_VOICES = _HOME / ".local/share/calliope-tts/models/voices-v1.0.bin"
MODEL_PATH = Path(os.environ.get("KOKORO_MODEL", str(DEFAULT_MODEL)))
VOICES_PATH = Path(os.environ.get("KOKORO_VOICES", str(DEFAULT_VOICES)))

MAX_TEXT_CHARS = 5000           # mirror dictation-server validation
MAX_BODY_BYTES = 64 * 1024      # 64 KiB JSON cap; text is the only big field


# ─── Model lifecycle ──────────────────────────────────────
# Kokoro is loaded once at startup. The `_voice_ids` cache mirrors
# `kokoro.get_voices()` so the /voices endpoint never re-queries the model.
_kokoro: Kokoro | None = None
_voice_ids: list[str] = []
_kokoro_lock = threading.Lock()


def _load_kokoro() -> Kokoro:
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Kokoro model not found: {MODEL_PATH}")
    if not VOICES_PATH.exists():
        raise FileNotFoundError(f"Kokoro voices file not found: {VOICES_PATH}")
    log.info("Loading Kokoro model: %s", MODEL_PATH)
    log.info("Loading voices: %s", VOICES_PATH)
    k = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    log.info("Kokoro ready")
    return k


def _ensure_loaded() -> Kokoro:
    """Lazy-init guard. Real bootstrap happens at startup; this is the
    safety net for callers that hit the server before main() finished
    constructing the model.
    """
    global _kokoro, _voice_ids
    if _kokoro is not None:
        return _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            _kokoro = _load_kokoro()
            try:
                _voice_ids = sorted(list(_kokoro.get_voices()))
            except Exception as e:
                log.warning("Could not enumerate voices: %s", e)
                _voice_ids = []
    return _kokoro


def _audio_to_wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
    """Encode a float32 mono waveform as 16-bit PCM WAV using stdlib `wave`.

    Avoids the soundfile/libsndfile runtime dep (kokoro-onnx ships fine
    without it). Clipping is intentional: kokoro outputs roughly [-1.0,
    1.0] but pathological prompts can spike slightly above 1.0; clamp
    rather than scale to keep amplitude consistent across requests.
    """
    if samples.ndim > 1:
        # Kokoro returns shape (1, N_samples). Squeeze the leading 1-dim.
        # mean(axis=-1) was a bug — it collapsed samples to a single value,
        # producing a 46-byte WAV. Fall back to channel-mean only if the
        # array is genuinely multi-channel after squeeze.
        samples = np.squeeze(samples)
        if samples.ndim > 1:
            samples = samples.mean(axis=0)
    samples = np.clip(samples, -1.0, 1.0)
    pcm16 = (samples * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


_RE = __import__('re')
_SENT_SPLIT_RE = _RE.compile(r'(?<=[.!?…])\s+|(?<=\n)')
_SOFT_SPLIT_RE = _RE.compile(r'(?<=[,;:])\s+')


def _split_long_fragment(fragment: str, max_chars: int) -> list[str]:
    """Split a single long sentence below Kokoro's internal splitter threshold."""
    fragment = fragment.strip()
    if len(fragment) <= max_chars:
        return [fragment] if fragment else []

    pieces: list[str] = []
    for soft in [p.strip() for p in _SOFT_SPLIT_RE.split(fragment) if p.strip()]:
        if len(soft) <= max_chars:
            pieces.append(soft)
            continue
        words = soft.split()
        cur = ''
        for word in words:
            if cur and len(cur) + 1 + len(word) > max_chars:
                pieces.append(cur)
                cur = word[:max_chars]
            else:
                cur = word if not cur else cur + ' ' + word
        if cur:
            pieces.append(cur)
    return pieces


def _split_for_synth(text: str, max_chars: int = 100) -> list[str]:
    """Split text into chunks safe for kokoro.create().

    Upstream kokoro-onnx has a multi-batch concat bug: when input phonemes
    span multiple internal segments, the concat axis can mismatch
    (some segments produce shape (0,) for an unsupported phoneme cluster
    and others produce (N,)). The reliable workaround is to feed
    sentence-sized chunks one at a time and concatenate audio ourselves.
    """
    text = (text or '').strip()
    if not text:
        return []
    # Split by sentence-ending punctuation + newlines, then split long prose
    # sentences again. Kokoro's own splitter can still hit the concat bug on
    # long single sentences, so keep each model call deliberately small.
    sentence_parts = [p.strip() for p in _SENT_SPLIT_RE.split(text) if p and p.strip()]
    parts: list[str] = []
    for part in sentence_parts:
        parts.extend(_split_long_fragment(part, max_chars))
    if not parts:
        return [text]
    # Re-glue tiny adjacent fragments up to max_chars per chunk.
    chunks: list[str] = []
    cur = ''
    for p in parts:
        if cur and len(cur) + 1 + len(p) <= max_chars:
            cur = cur + ' ' + p
        else:
            if cur:
                chunks.append(cur)
            cur = p[:max_chars]  # hard-truncate any one absurdly long sentence
    if cur:
        chunks.append(cur)
    return chunks


def _synthesize(text: str, voice: str, speed: float) -> tuple[bytes, int]:
    """Run the model. Returns (wav_bytes, sample_rate).

    Synthesis is single-threaded via `_kokoro_lock` because kokoro-onnx
    is not internally thread-safe across overlapping `create()` calls
    on the same session. Calliope serialises its TTS calls anyway
    (one read-back at a time), so contention here is negligible.

    Long input is sentence-chunked (see _split_for_synth) to dodge an
    upstream multi-batch concat crash.
    """
    k = _ensure_loaded()
    chunks = _split_for_synth(text)
    if not chunks:
        raise ValueError("empty text after split")
    sr = 24000
    pieces: list[np.ndarray] = []
    with _kokoro_lock:
        for chunk in chunks:
            audio, sr = k.create(chunk, voice=voice, speed=speed, lang=LANG)
            arr = np.asarray(audio, dtype=np.float32)
            if arr.ndim > 1:
                arr = np.squeeze(arr)
            if arr.ndim > 1:
                arr = arr.mean(axis=0)
            if arr.size == 0:
                continue
            pieces.append(arr)
    if not pieces:
        raise RuntimeError("kokoro returned no audio for any chunk")
    full = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
    return _audio_to_wav_bytes(full, int(sr)), int(sr)


# ─── HTTP handler ─────────────────────────────────────────
class KokoroHandler(BaseHTTPRequestHandler):
    server_version = "kokoro-server/1.0"

    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _err(self, msg: str, status: int = 400, code: str = "error") -> None:
        self._json({"error": msg, "code": code}, status=status)

    def _read_json_body(self) -> dict | None:
        n = int(self.headers.get("Content-Length", "0") or "0")
        if n <= 0:
            return {}
        if n > MAX_BODY_BYTES:
            self._err("Request body too large", status=413, code="body_too_large")
            return None
        try:
            raw = self.rfile.read(n)
            data = json.loads(raw)
        except Exception:
            self._err("Invalid JSON", status=400, code="bad_json")
            return None
        if not isinstance(data, dict):
            self._err("Expected a JSON object", status=400, code="bad_input")
            return None
        return data

    def do_GET(self):  # noqa: N802 — http.server contract
        if self.path == "/health":
            try:
                _ensure_loaded()
                self._json({
                    "status": "ok",
                    "voices": len(_voice_ids),
                    "model": MODEL_PATH.name,
                })
            except Exception as e:
                log.exception("health probe failed during model load")
                self._err(f"Model not loaded: {e}", status=503, code="not_ready")
            return
        if self.path == "/voices":
            try:
                _ensure_loaded()
            except Exception as e:
                self._err(f"Model not loaded: {e}", status=503, code="not_ready")
                return
            voices = [{"id": v, "label": v} for v in _voice_ids]
            self._json({"voices": voices})
            return
        self._err("Not found", status=404, code="not_found")

    def do_POST(self):  # noqa: N802
        if self.path != "/synthesize":
            self._err("Not found", status=404, code="not_found")
            return
        body = self._read_json_body()
        if body is None:
            return  # error already sent
        text = str(body.get("text", "")).strip()
        if not text:
            self._err("Missing 'text'", status=400, code="missing_param")
            return
        if len(text) > MAX_TEXT_CHARS:
            self._err(
                f"'text' exceeds {MAX_TEXT_CHARS} chars",
                status=400, code="text_too_long",
            )
            return
        voice = str(body.get("voice") or "af_heart").strip()
        try:
            speed = float(body.get("speed") or 1.0)
        except (TypeError, ValueError):
            self._err("'speed' must be a number", status=400, code="bad_input")
            return
        # Kokoro accepts a wide range; clamp to a sane band to avoid
        # pathological prosody.
        if not (0.5 <= speed <= 2.0):
            self._err("'speed' must be between 0.5 and 2.0",
                      status=400, code="bad_input")
            return
        # Validate voice (after we know the catalog is loaded).
        try:
            _ensure_loaded()
        except Exception as e:
            log.exception("model load failed during synth")
            self._err(f"Model not loaded: {e}", status=503, code="not_ready")
            return
        if _voice_ids and voice not in _voice_ids:
            self._err(
                f"Unknown voice '{voice}'",
                status=400, code="unknown_voice",
            )
            return
        try:
            wav_bytes, _sr = _synthesize(text, voice, speed)
        except Exception as e:
            log.exception("synthesis failed")
            self._err(f"Synthesis failed: {e}", status=500, code="synth_failed")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav_bytes)))
        self.end_headers()
        try:
            self.wfile.write(wav_bytes)
        except Exception:
            # Client disconnected mid-stream — not actionable.
            pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Eagerly load the model so /health reflects readiness, not lazy-warmup.
    try:
        _ensure_loaded()
    except Exception as e:
        log.error("Failed to load Kokoro at startup: %s", e)
        return 1
    server = ThreadingHTTPServer((HOST, PORT), KokoroHandler)
    server.daemon_threads = True
    log.info("Kokoro server listening on http://%s:%d", HOST, PORT)
    log.info("Voices loaded: %d", len(_voice_ids))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
