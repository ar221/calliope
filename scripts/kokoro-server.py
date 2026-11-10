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
- KOKORO_LANG    (default en-us) — language for voices whose ID prefix isn't
                  in `_VOICE_PREFIX_LANG`; mapped prefixes (b→en-gb, f→fr-fr, …)
                  always use their own language

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
DEFAULT_LANG = os.environ.get("KOKORO_LANG", "en-us")

# Kokoro voice IDs are `<lang><gender>_<name>` (e.g. `bf_emma`); the first
# letter names the voice's native language. It is used for G2P only when the
# text is actually in that language (see _lang_for_chunk): espeak-ng's `ja`
# spells Latin words letter by letter and other languages inject switch
# markers, so English text read by a non-English voice must stay on
# KOKORO_LANG.
_VOICE_PREFIX_LANG = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}
FALLBACK_LANG = "en-us"
# lang -> whether the phonemizer accepted it, probed once per language.
# Guarded by _kokoro_lock (only touched during synthesis).
_lang_support: dict[str, bool] = {}


def _lang_for_voice(voice: str) -> str:
    """Native espeak-ng language of a Kokoro voice ID, by its one-letter prefix."""
    prefix = voice[:1].lower() if len(voice) > 2 and voice[2:3] == "_" else ""
    return _VOICE_PREFIX_LANG.get(prefix, DEFAULT_LANG)


# Unambiguous function words; words that are also common English words
# ("no", "come", "son", "pour", "do", single letters) are left out on purpose.
_FUNCTION_WORDS = {
    "en": frozenset("the and of to is was with that this for you her his she he they it are "
                    "were not but from have had at on in be what".split()),
    "es": frozenset("el la los las de que en un una es por con para se su lo como más pero "
                    "sus le ya este sí porque esta cuando muy".split()),
    "fr": frozenset("le la les de des du et est un une que qui dans pas sur ne se ce il elle "
                    "je vous nous au avec mais où ou".split()),
    "it": frozenset("il lo la gli le di che un una non con si da sono del della nel più anche "
                    "questo io tu lui lei".split()),
    "pt": frozenset("os de que em um uma para com não se por mais como mas foi ao ele ela dos "
                    "das seu sua você".split()),
}
_WORD_RE = __import__("re").compile(r"[^\W\d_]+")


def _lang_for_chunk(voice: str, chunk: str) -> str:
    """G2P language for `chunk` spoken by `voice`.

    The voice's native language applies only when the text is in it: by script
    for ja/cmn/hi, by function-word majority for Latin-script languages.
    Everything else uses DEFAULT_LANG, the operator's content language.
    """
    voice_lang = _lang_for_voice(voice)
    if voice_lang == DEFAULT_LANG:
        return DEFAULT_LANG
    voice_base, default_base = voice_lang.split("-")[0], DEFAULT_LANG.split("-")[0]
    if voice_base == default_base:
        return voice_lang  # same language, regional accent (en-gb vs en-us)

    kana = han = deva = latin = 0
    for ch in chunk:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF:
            kana += 1
        elif 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF:
            han += 1
        elif 0x0900 <= cp <= 0x097F:
            deva += 1
        elif cp < 0x250 and ch.isalpha():
            latin += 1
    native = {"ja": kana + han, "cmn": han, "hi": deva}.get(voice_base)
    if native is not None:
        return voice_lang if native > latin else DEFAULT_LANG

    words = _WORD_RE.findall(chunk.lower())
    voice_hits = sum(w in _FUNCTION_WORDS.get(voice_base, ()) for w in words)
    default_hits = sum(w in _FUNCTION_WORDS.get(default_base, ()) for w in words)
    return voice_lang if voice_hits > default_hits else DEFAULT_LANG


def _parse_speed(raw) -> float:
    """`speed` from a /synthesize body. Absent/null means 1.0; 0 is not absent.

    Raises ValueError for non-numbers and anything outside 0.5–2.0 (which
    includes every non-positive speed).
    """
    if raw is None:
        return 1.0
    try:
        speed = float(raw)
    except (TypeError, ValueError):
        raise ValueError("'speed' must be a number")
    # Kokoro accepts a wide range; clamp to a sane band to avoid
    # pathological prosody.
    if not (0.5 <= speed <= 2.0):
        raise ValueError("'speed' must be between 0.5 and 2.0")
    return speed


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
_SENT_SPLIT_RE = _RE.compile(r'(?<=[.!?…])\s+|(?<=[。！？])\s*|(?<=\n)')
_SOFT_SPLIT_RE = _RE.compile(r'(?<=[,;:])\s+|(?<=[，、；：])\s*')


def _hard_split(text: str, max_chars: int) -> list[str]:
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def _split_long_fragment(fragment: str, max_chars: int) -> list[str]:
    """Split a single long sentence below Kokoro's internal splitter threshold.

    Lossless: unspaced runs (CJK, long tokens) are hard-split, never dropped.
    """
    fragment = fragment.strip()
    if len(fragment) <= max_chars:
        return [fragment] if fragment else []

    pieces: list[str] = []
    for soft in [p.strip() for p in _SOFT_SPLIT_RE.split(fragment) if p.strip()]:
        if len(soft) <= max_chars:
            pieces.append(soft)
            continue
        cur = ''
        for word in soft.split():
            if len(word) > max_chars:
                if cur:
                    pieces.append(cur)
                slices = _hard_split(word, max_chars)
                pieces.extend(slices[:-1])
                cur = slices[-1]
            elif cur and len(cur) + 1 + len(word) > max_chars:
                pieces.append(cur)
                cur = word
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
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


def _effective_lang(k, voice: str, chunk: str) -> str:
    """Language to synthesize `chunk` with. Each mapped language is checked
    once against the phonemizer; one it can't handle (missing espeak-ng data
    or G2P extras) falls back to FALLBACK_LANG, logged once. Caller holds
    _kokoro_lock."""
    lang = _lang_for_chunk(voice, chunk)
    if lang == FALLBACK_LANG:
        return lang
    supported = _lang_support.get(lang)
    if supported is None:
        tokenizer = getattr(k, "tokenizer", None)
        try:
            if tokenizer is not None:
                tokenizer.phonemize("a", lang)
            supported = True
        except Exception as e:
            supported = False
            log.warning("language %r unavailable for G2P (%s); voices using it fall back to %s",
                        lang, e, FALLBACK_LANG)
        _lang_support[lang] = supported
    return lang if supported else FALLBACK_LANG


def _synth_one(k, chunk: str, voice: str, speed: float):
    """Mono float32 array for `chunk` (may be size 0). Caller holds _kokoro_lock.

    Kokoro indexes the voice style by phoneme count and raises IndexError past
    its limit; verbose G2P output can exceed it for a chunk that is short in
    characters, so halve and retry instead of failing the request.
    """
    try:
        audio, sr = k.create(chunk, voice=voice, speed=speed,
                             lang=_effective_lang(k, voice, chunk))
    except IndexError:
        if len(chunk) < 2:
            raise
        cut = chunk.rfind(" ", 1, len(chunk) // 2 + 1)
        if cut <= 0:
            cut = len(chunk) // 2
        halves = [part for part in (chunk[:cut].strip(), chunk[cut:].strip()) if part]
        pieces, sr = [], 24000
        for part in halves:
            arr, sr = _synth_one(k, part, voice, speed)
            if arr.size:
                pieces.append(arr)
        return (np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)), sr
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim > 1:
        arr = np.squeeze(arr)
    if arr.ndim > 1:
        arr = arr.mean(axis=0)
    return arr, sr


def _synth_recover(k, chunk: str, voice: str, speed: float):
    """Synthesize a chunk, recovering from the kokoro-onnx 0.5.0 phonemizer bug:
    certain word *combinations* return zero-length audio while their sub-spans
    synthesize fine. On empty output, re-split smaller and retry. Returns
    (list_of_pieces, sample_rate); list is empty only when nothing in the chunk
    is synthesizable (e.g. CJK under en-us)."""
    arr, sr = _synth_one(k, chunk, voice, speed)
    if arr.size:
        return [arr], sr
    subs = _split_long_fragment(chunk, max_chars=max(8, len(chunk) // 2))
    if len(subs) <= 1:
        subs = chunk.split()  # clause re-split made no progress -> word level
    pieces: list[np.ndarray] = []
    for sub in subs:
        if not sub.strip():
            continue
        sub_arr, sr = _synth_one(k, sub, voice, speed)
        if sub_arr.size:
            pieces.append(sub_arr)
    if not pieces:
        log.warning("kokoro empty audio, unrecoverable chunk (len=%d voice=%s lang=%s)",
                    len(chunk), voice, _lang_for_chunk(voice, chunk))
    return pieces, sr


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
            recovered, sr = _synth_recover(k, chunk, voice, speed)
            pieces.extend(recovered)
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
            speed = _parse_speed(body.get("speed"))
        except ValueError as e:
            self._err(str(e), status=400, code="bad_input")
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
            # Exception text from the phonemizer/model can quote the input.
            log.warning("synthesis failed: %s (text_len=%d voice=%s)",
                        e.__class__.__name__, len(text), voice)
            self._err(f"Synthesis failed ({e.__class__.__name__})", status=500, code="synth_failed")
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
