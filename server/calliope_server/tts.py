"""TTS layer — Kokoro proxy, audiobook assembly, voice casting.

Kokoro server lifecycle (probe / systemctl start / idle shutdown), /tts and
/tts/audiobook request validation, WAV post-processing, the voices cache,
and the voice-catalog suggestion/autocast scorer. Config values are read
via `calliope_server.config` AT CALL TIME (`config.X`) so tests can
monkeypatch `mod.config.<NAME>`. Extracted from the executable
`calliope-server` script (Stage 3 split).
"""

import io
import json
import logging
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import Counter
from pathlib import Path

from . import config

log = logging.getLogger("dictation-server")

# TTS — idle-shutdown state. `_last_tts_ts` (monotonic) is refreshed when
# Kokoro is claimed or booted and at the start and end of every synthesis
# call; `_tts_in_flight` counts synthesis calls currently talking to Kokoro.
# Both are guarded by `_last_tts_lock`. The idle loop never stops Kokoro
# while a synthesis is in flight or inside the idle window.
_last_tts_ts: float = 0.0
_tts_in_flight: int = 0
_last_tts_lock = threading.Lock()
# Serialises "is Kokoro up / start it" against the idle loop's "stop it".
_kokoro_lifecycle_lock = threading.Lock()
_kokoro_idle_thread: threading.Thread | None = None

# TTS — in-memory voices cache. Refreshed on TTL miss; protects the model
# server from a stampede of /tts/voices polls from the phone UI. Only one
# thread refreshes at a time (`_kokoro_voices_refresh_lock`); while it does,
# other callers read the stale cache instead of piling onto kokoro-server.
_kokoro_voices_cache: dict | None = None
_kokoro_voices_cache_ts: float = 0.0
_kokoro_voices_lock = threading.Lock()
_kokoro_voices_refresh_lock = threading.Lock()


# ─── TTS — Kokoro proxy helpers ──────────────────────────
# All callable as module functions so tests can mock urllib.request.urlopen
# and exercise validation without instantiating DictationHandler.


def _kokoro_probe(timeout: float = config.KOKORO_PROBE_TIMEOUT) -> bool:
    """True iff GET /health on kokoro-server returns within `timeout`s."""
    try:
        req = urllib.request.Request(config.KOKORO_SERVER_URL + "/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 600
    except urllib.error.HTTPError as e:
        return e.code < 600
    except Exception:
        return False


def _ensure_kokoro_alive(boot_timeout: float = config.KOKORO_BOOT_TIMEOUT, *,
                         claim: bool = True) -> bool:
    """Probe kokoro-server; if dead, `systemctl --user start kokoro-server`
    and poll up to `boot_timeout` seconds. Returns True on alive.

    `claim=True` (synthesis callers) refreshes the idle timer when Kokoro is
    already up, so the idle loop can't stop it between this check and the
    synthesis call. A boot always arms the timer, whatever `claim` says, so a
    unit started for a voice listing or a failed request still gets stopped.
    Runs under `_kokoro_lifecycle_lock`, which the idle loop holds while it
    decides and stops.
    """
    with _kokoro_lifecycle_lock:
        if _kokoro_probe(timeout=config.KOKORO_PROBE_TIMEOUT):
            if claim:
                _mark_tts_activity()
            return True
        log.info("kokoro-server not responding — attempting `systemctl --user start kokoro-server`")
        try:
            subprocess.run(
                ["systemctl", "--user", "start", "kokoro-server"],
                check=False, capture_output=True, timeout=10,
            )
        except Exception as e:
            log.warning(f"systemctl start kokoro-server failed: {e}")
            return False
        _mark_tts_activity()
        deadline = time.monotonic() + boot_timeout
        while time.monotonic() < deadline:
            if _kokoro_probe(timeout=config.KOKORO_PROBE_TIMEOUT):
                log.info("kokoro-server up")
                return True
            time.sleep(0.25)
        log.warning("kokoro-server did not become healthy within %.1fs", boot_timeout)
        return False


def _mark_tts_activity() -> None:
    """Update the TTS idle-shutdown timer's last-active timestamp."""
    global _last_tts_ts
    with _last_tts_lock:
        _last_tts_ts = time.monotonic()


def _begin_tts_synthesis() -> None:
    """Count a synthesis call as in flight and refresh the idle timer."""
    global _last_tts_ts, _tts_in_flight
    with _last_tts_lock:
        _tts_in_flight += 1
        _last_tts_ts = time.monotonic()


def _end_tts_synthesis() -> None:
    """Release an in-flight slot; the idle window restarts from now."""
    global _last_tts_ts, _tts_in_flight
    with _last_tts_lock:
        _tts_in_flight = max(0, _tts_in_flight - 1)
        _last_tts_ts = time.monotonic()


def _kokoro_idle_tick() -> bool:
    """One idle-loop decision. Returns True when kokoro-server was stopped."""
    global _last_tts_ts
    with _kokoro_lifecycle_lock:
        with _last_tts_lock:
            last = _last_tts_ts
            in_flight = _tts_in_flight
        if in_flight > 0 or last <= 0:
            return False
        idle_for = time.monotonic() - last
        if idle_for < config.KOKORO_IDLE_SHUTDOWN_SECONDS:
            return False
        if not _kokoro_probe(timeout=config.KOKORO_PROBE_TIMEOUT):
            with _last_tts_lock:
                _last_tts_ts = 0.0
            return False
        log.info("kokoro-server idle for %.0fs — stopping to release memory", idle_for)
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", "kokoro-server"],
                check=False, capture_output=True, timeout=10,
            )
        except Exception as e:
            log.warning(f"systemctl stop kokoro-server failed: {e}")
        with _last_tts_lock:
            _last_tts_ts = 0.0
        return True


def _kokoro_idle_shutdown_loop() -> None:
    """Background daemon: stop kokoro-server after N seconds of TTS inactivity.

    Mirrors `_idle_shutdown_loop` but for the Kokoro process. Disabled
    entirely when config.KOKORO_IDLE_SHUTDOWN_DISABLED is set.
    """
    if config.KOKORO_IDLE_SHUTDOWN_DISABLED:
        log.info("Kokoro idle-shutdown disabled by env (config.KOKORO_IDLE_SHUTDOWN_DISABLED)")
        return
    log.info(
        "Kokoro idle-shutdown thread started; will stop kokoro-server after %ds idle",
        config.KOKORO_IDLE_SHUTDOWN_SECONDS,
    )
    while True:
        time.sleep(config.IDLE_SHUTDOWN_CHECK_INTERVAL)
        _kokoro_idle_tick()


def _start_kokoro_idle_shutdown_thread() -> None:
    """Idempotent starter for the Kokoro idle-shutdown daemon."""
    global _kokoro_idle_thread
    if _kokoro_idle_thread is not None and _kokoro_idle_thread.is_alive():
        return
    t = threading.Thread(
        target=_kokoro_idle_shutdown_loop,
        name="kokoro-idle-shutdown",
        daemon=True,
    )
    t.start()
    _kokoro_idle_thread = t


def _validate_tts_request(body: object) -> tuple[str, str, float, dict | None]:
    """Validate a /tts request body. Returns (text, voice, speed, error_or_None).

    `error_or_None` is a dict with {error, code, status} when validation fails;
    None on success. Pure function for unit testing.
    """
    if not isinstance(body, dict):
        return "", "", 1.0, {
            "error": "Expected a JSON object",
            "code": "bad_input",
            "status": 400,
        }
    text = str(body.get("text", "") or "").strip()
    if not text:
        return "", "", 1.0, {
            "error": "Missing 'text'",
            "code": "missing_param",
            "status": 400,
        }
    if len(text) > config.TTS_MAX_TEXT_CHARS:
        return text, "", 1.0, {
            "error": f"'text' exceeds {config.TTS_MAX_TEXT_CHARS} chars",
            "code": "text_too_long",
            "status": 400,
        }
    voice = str(body.get("voice") or config.KOKORO_DEFAULT_VOICE).strip()
    if not voice:
        voice = config.KOKORO_DEFAULT_VOICE
    raw_speed = body.get("speed", 1.0)
    try:
        speed = float(raw_speed) if raw_speed is not None else 1.0
    except (TypeError, ValueError):
        return text, voice, 1.0, {
            "error": "'speed' must be a number",
            "code": "bad_input",
            "status": 400,
        }
    if not (0.5 <= speed <= 2.0):
        return text, voice, speed, {
            "error": "'speed' must be between 0.5 and 2.0",
            "code": "bad_input",
            "status": 400,
        }
    return text, voice, speed, None


def _synthesize_via_kokoro(text: str, voice: str, speed: float = 1.0) -> tuple[bytes, str]:
    """POST to kokoro-server's /synthesize. Returns (audio_bytes, content_type).

    Raises RuntimeError on transport / HTTP error so the handler can map
    to a 502/503. Caller is responsible for liveness probe (`_ensure_kokoro_alive`).
    The call counts as in flight for the idle loop, and refreshes the idle
    timer at start and end, so a long audiobook (one call per segment) keeps
    Kokoro alive for its whole render.
    """
    body = json.dumps({
        "text": text,
        "voice": voice,
        "speed": speed,
    }).encode("utf-8")
    req = urllib.request.Request(
        config.KOKORO_SERVER_URL + "/synthesize",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    _begin_tts_synthesis()
    try:
        with urllib.request.urlopen(req, timeout=config.KOKORO_REQUEST_TIMEOUT) as resp:
            audio = resp.read()
            ctype = resp.headers.get("Content-Type", "audio/wav")
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            err_body = ""
        raise RuntimeError(f"kokoro-server HTTP {e.code}: {err_body}")
    except Exception as e:
        raise RuntimeError(f"kokoro-server request failed: {e}")
    finally:
        _end_tts_synthesis()
    return audio, ctype


def _normalize_voice_key(name: object) -> str:
    return str(name or "").strip().lower()


def _validate_audiobook_request(body: object) -> tuple[list[dict], str, dict, str, dict | None]:
    """Validate /tts/audiobook input.

    Returns (messages, narrator_voice, voice_map, title, error_or_None).
    messages are normalized to {name, text, is_user, voice?}.
    """
    if not isinstance(body, dict):
        return [], "", {}, "", {
            "error": "Expected a JSON object",
            "code": "bad_input",
            "status": 400,
        }
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        return [], "", {}, "", {
            "error": "Missing non-empty 'messages' list",
            "code": "missing_param",
            "status": 400,
        }
    if len(raw_messages) > config.TTS_AUDIOBOOK_MAX_MESSAGES:
        return [], "", {}, "", {
            "error": f"'messages' exceeds {config.TTS_AUDIOBOOK_MAX_MESSAGES} entries",
            "code": "too_many_messages",
            "status": 400,
        }

    messages: list[dict] = []
    total_chars = 0
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "") or "").strip()
        if not text:
            continue
        total_chars += len(text)
        if total_chars > config.TTS_AUDIOBOOK_MAX_TOTAL_CHARS:
            return [], "", {}, "", {
                "error": f"Audiobook text exceeds {config.TTS_AUDIOBOOK_MAX_TOTAL_CHARS} chars",
                "code": "text_too_long",
                "status": 400,
            }
        messages.append({
            "name": str(item.get("name", "") or "").strip(),
            "text": text[:config.TTS_MAX_TEXT_CHARS],
            "is_user": bool(item.get("is_user", False)),
            "voice": str(item.get("voice", "") or "").strip(),
        })
    if not messages:
        return [], "", {}, "", {
            "error": "No readable message text",
            "code": "missing_param",
            "status": 400,
        }

    narrator_voice = str(body.get("narratorVoice") or config.KOKORO_DEFAULT_VOICE).strip() or config.KOKORO_DEFAULT_VOICE
    raw_voice_map = body.get("voiceMap") or {}
    voice_map = {
        _normalize_voice_key(k): str(v).strip()
        for k, v in raw_voice_map.items()
        if str(k).strip() and str(v).strip()
    } if isinstance(raw_voice_map, dict) else {}
    title = str(body.get("title") or "calliope-audiobook").strip()[:80] or "calliope-audiobook"
    return messages, narrator_voice, voice_map, title, None


def _voice_for_audiobook_message(message: dict, narrator_voice: str, voice_map: dict) -> str:
    explicit = str(message.get("voice") or "").strip()
    if explicit:
        return explicit
    name = _normalize_voice_key(message.get("name"))
    if name and name in voice_map:
        return voice_map[name]
    return narrator_voice


def _read_wav_payload(audio: bytes) -> tuple[tuple[int, int, int], bytes]:
    with wave.open(io.BytesIO(audio), "rb") as wav:
        params = (wav.getnchannels(), wav.getsampwidth(), wav.getframerate())
        frames = wav.readframes(wav.getnframes())
    return params, frames


def _combine_wav_segments(segments: list[bytes], silence_ms: int = config.TTS_AUDIOBOOK_SILENCE_MS) -> bytes:
    if not segments:
        raise ValueError("No audio segments")
    first_params, first_frames = _read_wav_payload(segments[0])
    channels, sample_width, frame_rate = first_params
    silence_frames = max(0, int(frame_rate * silence_ms / 1000))
    silence = b"\x00" * silence_frames * channels * sample_width
    all_frames = [first_frames]
    for segment in segments[1:]:
        params, frames = _read_wav_payload(segment)
        if params != first_params:
            raise ValueError("TTS segments have incompatible WAV formats")
        if silence:
            all_frames.append(silence)
        all_frames.append(frames)
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(frame_rate)
        wav.writeframes(b"".join(all_frames))
    return out.getvalue()


def _safe_download_stem(title: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", title.strip()).strip("-._")
    return stem[:80] or "calliope-audiobook"


def _fetch_kokoro_voices(force: bool = False) -> dict:
    """Return the cached voices payload, refreshing after config.KOKORO_VOICES_TTL_SECONDS.

    Shape mirrors kokoro-server's /voices: {"voices": [{id, label}, ...]}.
    Raises RuntimeError on transport failure when no cached value exists.
    The refresh uses the short probe timeout and is single-flight: while one
    thread refreshes, callers that hold a stale cache get it immediately and
    cold callers wait for that refresh rather than starting their own.
    """
    global _kokoro_voices_cache, _kokoro_voices_cache_ts
    with _kokoro_voices_lock:
        cached = _kokoro_voices_cache
        ts = _kokoro_voices_cache_ts
    if not force and cached is not None and (time.monotonic() - ts) < config.KOKORO_VOICES_TTL_SECONDS:
        return cached
    if not _kokoro_voices_refresh_lock.acquire(blocking=cached is None):
        return cached  # another thread is refreshing; serve stale meanwhile
    try:
        if cached is None:
            with _kokoro_voices_lock:
                if _kokoro_voices_cache is not None and not force:
                    return _kokoro_voices_cache  # filled while we waited
        try:
            req = urllib.request.Request(config.KOKORO_SERVER_URL + "/voices", method="GET")
            with urllib.request.urlopen(req, timeout=config.KOKORO_PROBE_TIMEOUT) as resp:
                raw = resp.read()
            data = json.loads(raw)
        except urllib.error.HTTPError as e:
            if cached is not None:
                return cached  # serve stale on upstream error
            raise RuntimeError(f"kokoro-server /voices HTTP {e.code}")
        except Exception as e:
            if cached is not None:
                return cached
            raise RuntimeError(f"kokoro-server /voices failed: {e}")
        if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
            if cached is not None:
                return cached
            raise RuntimeError("kokoro-server /voices: malformed response")
        with _kokoro_voices_lock:
            _kokoro_voices_cache = data
            _kokoro_voices_cache_ts = time.monotonic()
        return data
    finally:
        _kokoro_voices_refresh_lock.release()


def _list_kokoro_voices() -> dict | None:
    """Voices payload for GET /tts/voices without booting Kokoro needlessly.

    Any cached list (fresh or stale) is served through `_fetch_kokoro_voices`,
    which refreshes it when Kokoro answers and falls back to the cache when it
    doesn't — a voice listing never starts the unit once a list is known.
    Only a cold cache boots Kokoro (`claim=False`: listing voices is not TTS
    use, though a boot still arms the idle timer). Returns None when Kokoro is
    unreachable and nothing is cached; raises RuntimeError on upstream failure.
    """
    with _kokoro_voices_lock:
        have_cache = _kokoro_voices_cache is not None
    if not have_cache and not _ensure_kokoro_alive(claim=False):
        return None
    return _fetch_kokoro_voices()


def validate_voices(ids) -> list[str]:
    """Return the voice IDs in `ids` that kokoro-server doesn't know, sorted.

    Lets /tts/audiobook reject a stale voiceMap before synthesizing the first
    segment. An unavailable or empty voice list returns [] (kokoro-server
    itself skips voice validation when it couldn't enumerate voices), so
    synthesis surfaces the real upstream error instead of a false
    "unknown voice".
    """
    try:
        payload = _fetch_kokoro_voices()
    except RuntimeError as e:
        log.warning("voice validation skipped: %s", e)
        return []
    known = {
        str(v.get("id") if isinstance(v, dict) else v)
        for v in payload.get("voices", [])
    }
    if not known:
        return []
    return sorted({str(i) for i in ids if str(i) not in known})


def _invalidate_kokoro_voices_cache() -> None:
    """Test hook — drop the voices cache."""
    global _kokoro_voices_cache, _kokoro_voices_cache_ts
    with _kokoro_voices_lock:
        _kokoro_voices_cache = None
        _kokoro_voices_cache_ts = 0.0


# ─── Voice Casting — catalog + suggestion scorer ─────────────────────────────

_VOICE_CATALOG: dict | None = None
# voice_catalog.json lives next to the executable script (server/), one
# level above this package directory.
_VOICE_CATALOG_PATH = Path(__file__).resolve().parent.parent / "voice_catalog.json"

_SUGGEST_KEYWORD_TONES: dict[str, list[str]] = {
    "warm":         ["warm"],              "kind":          ["warm"],
    "sweet":        ["warm"],              "gentle":        ["warm", "soft"],
    "caring":       ["warm"],              "nurturing":     ["warm"],
    "loving":       ["warm"],              "tender":        ["warm", "soft"],
    "dark":         ["dark"],              "evil":          ["dark", "cold"],
    "cruel":        ["cold"],              "villain":       ["dark", "cold", "authoritative"],
    "sinister":     ["dark", "cold"],      "cold":          ["cold"],
    "icy":          ["cold"],              "menacing":      ["dark", "cold"],
    "soft":         ["soft"],              "quiet":         ["soft"],
    "shy":          ["soft"],              "timid":         ["soft"],
    "delicate":     ["soft"],              "meek":          ["soft"],
    "seductive":    ["sultry", "intimate"],"flirtatious":   ["sultry"],
    "sensual":      ["sultry", "intimate"],"alluring":      ["sultry"],
    "intimate":     ["intimate"],          "sultry":        ["sultry"],
    "cheerful":     ["bright", "energetic"],"bubbly":       ["bright", "playful"],
    "enthusiastic": ["energetic"],         "lively":        ["bright", "energetic"],
    "excited":      ["energetic"],         "happy":         ["bright", "warm"],
    "perky":        ["bright", "playful"], "energetic":     ["energetic"],
    "playful":      ["playful"],           "mischievous":   ["playful"],
    "witty":        ["playful"],           "comedic":       ["playful"],
    "trickster":    ["playful"],           "funny":         ["playful", "bright"],
    "sarcastic":    ["playful"],
    "mysterious":   ["mysterious", "ethereal", "cold"],
    "enigmatic":    ["mysterious", "ethereal"],
    "supernatural": ["ethereal"],          "otherworldly":  ["ethereal"],
    "mystical":     ["ethereal"],          "ethereal":      ["ethereal"],
    "ghost":        ["ethereal", "cold"],  "spirit":        ["ethereal"],
    "authoritative":["authoritative"],     "commanding":    ["authoritative", "deep"],
    "dominant":     ["authoritative"],     "powerful":      ["authoritative", "deep"],
    "strict":       ["authoritative"],     "leader":        ["authoritative"],
    "elegant":      ["elegant", "smooth", "formal"],
    "refined":      ["smooth", "formal"],  "sophisticated": ["elegant", "smooth"],
    "aristocrat":   ["elegant", "formal"], "formal":        ["formal"],
    "professional": ["professional", "neutral"],
    "narrator":     ["narrative", "clear", "expressive"],
    "stoic":        ["neutral", "calm"],   "calm":          ["calm"],
    "composed":     ["calm", "smooth"],    "deep":          ["deep"],
    "gruff":        ["deep", "raspy"],     "raspy":         ["raspy"],
    "wise":         ["calm", "authoritative"],
    "motherly":     ["warm", "gentle"],    "fatherly":      ["warm", "deep"],
    "paternal":     ["warm", "deep"],      "jolly":         ["jolly", "warm"],
    "grumpy":       ["deep", "cold"],      "brooding":      ["dark", "calm"],
    "regal":        ["elegant", "authoritative", "formal"],
    "royal":        ["elegant", "formal"], "noble":         ["elegant", "formal"],
    "queen":        ["elegant", "authoritative"],
    "king":         ["authoritative", "deep"],
    "princess":     ["elegant", "bright"], "knight":        ["authoritative", "clear"],
    "soldier":      ["authoritative", "clear"],
    "military":     ["authoritative", "formal"],
    "commander":    ["authoritative", "deep"],
    "scholar":      ["professional", "clear"],
    "professor":    ["professional", "authoritative"],
    "intellectual": ["professional", "clear"],
    "bard":         ["narrative", "expressive"],
    "storyteller":  ["narrative", "expressive"],
    "robot":        ["neutral", "cold"],   "monotone":      ["neutral"],
    "angelic":      ["ethereal", "soft"],  "demon":         ["dark", "cold"],
    "nervous":      ["soft"],              "anxious":       ["soft"],
    "confident":    ["authoritative", "smooth"],
    "sassy":        ["playful", "bright"], "cheeky":        ["playful"],
    "smug":         ["smooth", "playful"], "melanchol":     ["calm", "soft"],
    "somber":       ["calm", "soft"],      "sad":           ["soft"],
}

# Accent detection word sets, checked in order — first hit wins (matches the
# historical if/elif precedence). Tokens, not substrings: "uk." and "spain,"
# now count. Bare "us" is deliberately absent: the pronoun ("let us") would
# lock every cast to American.
_SUGGEST_ACCENT_WORDS: dict[str, frozenset[str]] = {
    "british":    frozenset({"british", "britain", "london", "uk"}),
    "american":   frozenset({"american", "america", "usa"}),
    "french":     frozenset({"french", "france", "paris", "parisian"}),
    "italian":    frozenset({"italian", "italy"}),
    "japanese":   frozenset({"japanese", "japan"}),
    "chinese":    frozenset({"chinese", "china", "mandarin"}),
    "spanish":    frozenset({"spanish", "spain", "español", "espanol", "mexican",
                             "mexico", "hispanic", "latina", "latino"}),
    "portuguese": frozenset({"portuguese", "portugal", "brazilian", "brazil"}),
    "hindi":      frozenset({"hindi", "indian", "india"}),
}

_SUGGEST_AGE_YOUNG = frozenset({"young", "youthful", "teen", "teenager", "teenage", "girlish", "boyish"})
_SUGGEST_AGE_MATURE = frozenset({"elderly", "older", "elder", "aged", "mature", "veteran",
                                 "grandmother", "grandma", "grandfather", "grandpa"})
_SUGGEST_ENERGY_HIGH = frozenset({"energetic", "hyper", "hyperactive", "bubbly", "lively",
                                  "exuberant", "boisterous", "peppy", "spirited", "excitable"})
_SUGGEST_ENERGY_LOW = frozenset({"calm", "quiet", "serene", "tired", "lazy", "weary",
                                 "reserved", "subdued", "mellow", "soothing"})

# Allowed continuations for stem-prefix keyword matches ("" = exact already
# handled, kept for uniformity). Blocks compound-word false positives:
# "gentleman" must not match "gentle", while "gently"/"coldest"/"demonic" do.
_SUGGEST_STEM_SUFFIXES = frozenset({
    "", "s", "es", "d", "ed", "ing", "ly", "ness", "ful", "ish",
    "ic", "ical", "y", "er", "est", "ier", "iest", "ous", "ian", "al",
})


def _load_voice_catalog() -> dict:
    global _VOICE_CATALOG
    if _VOICE_CATALOG is not None:
        return _VOICE_CATALOG
    try:
        with open(_VOICE_CATALOG_PATH, encoding="utf-8") as fh:
            _VOICE_CATALOG = json.load(fh)
    except Exception as exc:
        log.warning("voice_catalog.json load failed: %s", exc)
        _VOICE_CATALOG = {}
    return _VOICE_CATALOG


def _suggest_voices(
    name: str,
    description: str,
    personality: str,
    recent_messages: list[str],
    existing_voices: dict[str, str],
    narrator: str,
    n: int = 3,
) -> list[dict]:
    catalog = _load_voice_catalog()
    if not catalog:
        return []

    text = " ".join(filter(None, [name, description, personality] + list(recent_messages)[-8:])).lower()
    # Word tokens, not raw substrings: "woman," and "uk." count, "scolded"
    # no longer triggers "cold". Accents in words (ñ, é) stay intact.
    token_counts = Counter(re.findall(r"[a-zà-ÿ]+", text))
    tokens = set(token_counts)

    fem = sum(token_counts[w] for w in ("she", "her", "hers", "woman", "girl", "female", "lady"))
    masc = sum(token_counts[w] for w in ("he", "him", "his", "man", "boy", "male", "gentleman"))
    gender_pref: str | None = None
    if fem > masc * 1.5:
        gender_pref = "feminine"
    elif masc > fem * 1.5:
        gender_pref = "masculine"

    desired_tones: set[str] = set()
    for keyword, tones in _SUGGEST_KEYWORD_TONES.items():
        # Exact token hit, or stem + morphological suffix for stems >= 4 chars:
        # "deeply" -> deep, "aristocratic" -> aristocrat, "melancholic" ->
        # melanchol — but never arbitrary continuations ("gentleman" != gentle).
        if keyword in tokens or (
            len(keyword) >= 4
            and any(
                t.startswith(keyword) and t[len(keyword):] in _SUGGEST_STEM_SUFFIXES
                for t in tokens
            )
        ):
            desired_tones.update(tones)

    accent_pref: str | None = None
    for accent, words in _SUGGEST_ACCENT_WORDS.items():
        if tokens & words:
            accent_pref = accent
            break

    age_pref: str | None = None
    if tokens & _SUGGEST_AGE_YOUNG:
        age_pref = "young adult"
    elif tokens & _SUGGEST_AGE_MATURE:
        age_pref = "mature"

    energy_pref: str | None = None
    if tokens & _SUGGEST_ENERGY_HIGH:
        energy_pref = "high"
    elif tokens & _SUGGEST_ENERGY_LOW:
        energy_pref = "low"

    used_voices: set[str] = set(existing_voices.values())
    if narrator:
        used_voices.add(narrator)

    scored: list[tuple[float, str, str]] = []
    for voice_id, meta in catalog.items():
        score = 0.0
        reason_parts: list[str] = []

        if gender_pref:
            if meta["gender"] == gender_pref:
                score += 4.0
                reason_parts.append(meta["gender"])
            else:
                score -= 3.0

        voice_tones = set(meta.get("tone", []))
        matched = voice_tones & desired_tones
        if matched:
            score += len(matched) * 1.5
            reason_parts.extend(sorted(matched)[:2])

        if accent_pref:
            # An explicit nationality mention is close to a hard constraint:
            # the ±6.0 swing outweighs typical tone/age/energy stacks on the
            # wrong accent pack (only a near-perfect 3-tone+age+energy fit ties).
            if meta.get("accent") == accent_pref:
                score += 4.0
                reason_parts.append(meta["accent"])
            else:
                score -= 2.0
        elif meta.get("accent") in ("american", "british"):
            score += 0.3

        if age_pref:
            voice_age = meta.get("age", "adult")
            if voice_age == age_pref:
                score += 1.2
                reason_parts.append(voice_age)
            elif {voice_age, age_pref} == {"young adult", "mature"}:
                score -= 0.8

        if energy_pref:
            voice_energy = meta.get("energy", "medium")
            if voice_energy == energy_pref:
                score += 0.8
                reason_parts.append(f"{voice_energy} energy")
            elif {voice_energy, energy_pref} == {"high", "low"}:
                score -= 0.4

        if voice_id in used_voices:
            score -= 1.5

        reason = ", ".join(dict.fromkeys(reason_parts))[:64] or meta.get("notes", "")[:64]
        scored.append((score, voice_id, reason))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"voice": vid, "score": round(sc, 2), "reason": rsn} for sc, vid, rsn in scored[:n]]


def _autocast_voices(
    members: list[dict],
    existing_voices: dict[str, str],
    narrator: str,
    overwrite: bool = False,
) -> dict:
    """Assign a distinct Kokoro voice to each group member in one pass.

    `members` is an ordered list of {name, description?, personality?,
    recent_messages?} dicts. Assignment is greedy: each member's top
    suggestion is chosen while accumulating already-picked voices into the
    used set, so the per-member used-voice penalty in `_suggest_voices`
    steers the cast toward distinct voices. When distinct voices run out
    (more members than catalog entries) it falls back to the best-scoring
    voice even if reused, so every member still gets a voice.

    `overwrite=False` preserves any name already present in
    `existing_voices`; `overwrite=True` reassigns every member.

    Returns {assignments: {name: voice}, assigned: [...], skipped: [...],
    reused: [...]} where names are echoed as provided (caller owns
    normalization for storage).
    """
    catalog = _load_voice_catalog()
    assignments: dict[str, str] = {}
    assigned: list[dict] = []
    skipped: list[str] = []
    reused: list[str] = []

    # Seed the used set from existing profiles + narrator so the cast avoids
    # colliding with voices the user already locked in.
    used: set[str] = {v for v in existing_voices.values() if v}
    if narrator:
        used.add(narrator)

    existing_lower = {str(k).strip().lower() for k in existing_voices}

    for member in members:
        name = str(member.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        if not overwrite and key in existing_lower:
            skipped.append(name)
            continue
        if not catalog:
            continue

        description = str(member.get("description") or "")
        personality = str(member.get("personality") or "")
        recent = member.get("recent_messages")
        recent = [str(m) for m in (recent if isinstance(recent, list) else [])]

        # Pass the running used-set as existing_voices so distinct voices win.
        ranked = _suggest_voices(
            name, description, personality, recent,
            {f"_used_{i}": v for i, v in enumerate(used)}, narrator, n=len(catalog),
        )
        if not ranked:
            continue

        # Prefer the top unused suggestion; fall back to the top pick overall.
        pick = next((r for r in ranked if r["voice"] not in used), ranked[0])
        voice = pick["voice"]
        if voice in used:
            reused.append(name)
        used.add(voice)
        assignments[name] = voice
        assigned.append({"name": name, "voice": voice, "reason": pick.get("reason", "")})

    return {
        "assignments": assignments,
        "assigned": assigned,
        "skipped": skipped,
        "reused": reused,
    }
