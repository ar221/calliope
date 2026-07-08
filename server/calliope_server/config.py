"""Calliope server configuration — env vars, paths, model chains, tunables.

Every constant here is read from the environment (or derived from one that
is) AT IMPORT TIME. The executable `calliope-server` script purges cached
`calliope_server*` modules before importing this one, so each (re)execution
of the script — including test loads via SourceFileLoader with env
overrides — re-reads the environment fresh.
"""

import os
from pathlib import Path

try:
    import yaml  # type: ignore  # noqa: F401 — presence probe only
    _HAVE_YAML = True
except ImportError:  # pragma: no cover — fallback to JSON-only persistence
    _HAVE_YAML = False

# Top-N English frequency list for the vocab fuzzy gate (MVP-5).
# Optional dep; matches the PyYAML pattern. Hardcoded fallback below
# covers the load-bearing common words that the fuzzy pass must NEVER
# silently rewrite into character-name lookalikes.
try:
    from wordfreq import top_n_list as _wordfreq_top_n_list  # type: ignore
    COMMON_EN_WORDS = frozenset(w.lower() for w in _wordfreq_top_n_list("en", 5000))
except ImportError:  # pragma: no cover — fallback to hardcoded list
    _wordfreq_top_n_list = None  # type: ignore
    COMMON_EN_WORDS = frozenset({
        # Articles, pronouns, prepositions, conjunctions
        "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on",
        "at", "by", "for", "from", "with", "without", "about", "into",
        "onto", "over", "under", "after", "before", "between", "through",
        "during", "above", "below", "across", "behind", "beyond", "near",
        "off", "out", "up", "down", "than", "then", "as", "so", "yet",
        "nor", "because", "while", "since", "until", "though", "although",
        "whereas", "unless", "this", "that", "these", "those", "here",
        "there", "where", "when", "why", "how", "what", "who", "whom",
        "whose", "which", "all", "any", "some", "many", "much", "more",
        "most", "less", "least", "few", "several", "each", "every",
        "either", "neither", "both", "another", "other", "others", "such",
        # Personal/possessive pronouns
        "i", "me", "my", "mine", "myself", "you", "your", "yours",
        "yourself", "yourselves", "he", "him", "his", "himself", "she",
        "her", "hers", "herself", "it", "its", "itself", "we", "us", "our",
        "ours", "ourselves", "they", "them", "their", "theirs", "themselves",
        # Auxiliaries / common verbs
        "be", "am", "is", "are", "was", "were", "been", "being", "have",
        "has", "had", "having", "do", "does", "did", "doing", "done",
        "can", "could", "shall", "should", "will", "would", "may", "might",
        "must", "ought", "let", "go", "goes", "went", "gone", "going",
        "get", "gets", "got", "gotten", "getting", "make", "makes", "made",
        "making", "take", "takes", "took", "taken", "taking", "say", "says",
        "said", "saying", "see", "saw", "seen", "seeing", "come", "came",
        "coming", "want", "wanted", "wants", "use", "used", "uses",
        "find", "found", "give", "gave", "given", "tell", "told", "ask",
        "asked", "work", "works", "worked", "seem", "seemed", "feel",
        "felt", "try", "tried", "tries", "leave", "left", "call", "called",
        "know", "knew", "known", "think", "thought", "look", "looked",
        "looks", "need", "needed", "put", "puts", "putting",
        "mean", "means", "meant", "keep", "kept", "begin", "began",
        "begun", "show", "showed", "shown", "hear", "heard", "play",
        "played", "run", "ran", "move", "moved", "live", "lived", "believe",
        "believed", "hold", "held", "bring", "brought", "happen", "happened",
        "write", "wrote", "written", "provide", "provided", "sit", "sat",
        "stand", "stood", "lose", "lost", "pay", "paid", "meet", "met",
        "include", "set", "sets", "learn", "learned", "change", "changed",
        "lead", "led", "understand", "understood", "watch", "watched",
        "follow", "followed", "stop", "stopped", "create", "created",
        "speak", "spoke", "spoken", "read", "spend", "spent", "grow",
        "grew", "grown", "open", "opened", "walk", "walked", "win", "won",
        "offer", "offered", "remember", "remembered", "consider", "appear",
        "appeared", "buy", "bought", "wait", "waited", "serve", "served",
        "die", "died", "send", "sent", "expect", "build", "built", "stay",
        "stayed", "fall", "fell", "fallen", "cut", "reach", "reached",
        "kill", "killed", "remain", "remained",
        # Time / numbers / quantity
        "today", "tomorrow", "yesterday", "now", "later", "soon", "always",
        "never", "often", "sometimes", "usually", "rarely", "ever",
        "again", "still", "already", "almost", "just", "really",
        "very", "too", "also", "even", "only", "quite", "rather",
        "well", "no",
        "yes", "not", "one", "two", "three", "four", "five", "six",
        "seven", "eight", "nine", "ten", "first", "second", "last",
        "next", "year", "years", "day", "days", "week", "month", "months",
        "hour", "hours", "minute", "minutes", "time", "times",
        # Generic high-frequency content words
        "thing", "things", "person", "people", "way", "ways", "world",
        "life", "hand", "hands", "part", "place", "case", "case", "fact",
        "right", "good", "great", "small", "large", "long",
        "short", "high", "low", "old", "new", "young", "big", "little",
        "own", "same", "different", "able", "sure", "true", "real",
        "full", "empty", "early", "late", "easy", "hard", "free",
        "close", "far", "wear", "ear", "ears", "eye",
        "eyes", "face", "head", "back", "front", "side", "kind", "type",
        "form", "name", "names", "word", "words", "story", "stories",
        "house", "home", "room", "door", "wall", "floor", "table", "chair",
        # Common short words that look like names truncated
        "yaw", "yam", "yer", "yon",
        "owe",
        "via",
    })

VOCAB_FUZZY_CUTOFF = 0.84  # was 0.75 — see ADR-10 / Agent 3 §6
VOCAB_FUZZY_MIN_LEN = 4    # was 3 — kills 'yaz'/'Ayaz' (3-char tokens skipped)
VOCAB_FUZZY_MAX_HITS_PER_UTT = 2

# Phase 5 / POL-3 — "Did you mean?" word-confidence overlay (Agent 6 §3.3).
# Words with logprob below this threshold get tagged in the /transcribe
# response; the phone PWA + ST extension render them as tap-to-correct chips.
# -0.7 matches whisper-cli's existing --logprob-thold; tunable via env so
# the threshold can be lifted/lowered per-deployment without a rebuild.
WORD_CONFIDENCE_THRESHOLD = float(
    os.environ.get("DICTATION_LOW_CONFIDENCE_THRESHOLD", "-0.7"),
)
# Max number of low-confidence words flagged per utterance. Avoids flooding
# the UI on degraded audio; the rest pass through silently.
WORD_CONFIDENCE_MAX_SPANS = 12
# Min word length before flagging. Skips articles / prepositions where
# whisper logprobs are noisy and a "?" chip is more annoying than helpful.
WORD_CONFIDENCE_MIN_LEN = 3

# ─── Config ──────────────────────────────────────────────
DEFAULT_PORT = 8384
DEFAULT_HOST = os.environ.get("DICTATION_BIND_HOST", "127.0.0.1")
MODEL_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "whisper"
DATA_DIR = Path(
    os.environ.get(
        "CALLIOPE_DATA_DIR",
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "dictation-server",
    )
)
CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE = DATA_DIR / "key.pem"
TOKEN_FILE = DATA_DIR / "token"
WHISPER_BIN = os.environ.get("WHISPER_BIN", "whisper-cli")
CLAUDE_PROXY_URL = os.environ.get(
    "DICTATION_CLAUDE_PROXY_URL",
    os.environ.get("DICTATION_PROXY_URL", "http://localhost:42069"),
)
CLAUDE_RP_MODEL = os.environ.get(
    "DICTATION_CLAUDE_RP_MODEL",
    # was claude-sonnet-4-20250514; deprecated 2026-04-14, retires 2026-06-15
    os.environ.get("DICTATION_RP_MODEL", "claude-sonnet-4-6"),
)
OPENAI_PROXY_URL = os.environ.get("DICTATION_OPENAI_PROXY_URL", "http://127.0.0.1:10531/v1")
OPENAI_RP_MODEL = os.environ.get("DICTATION_OPENAI_RP_MODEL", "gpt-5.4")
OPENAI_CLEAN_MODEL = os.environ.get("DICTATION_OPENAI_CLEAN_MODEL", "gpt-5.4-mini")

# ─── OmniRoute provider (OpenAI-compatible aggregator) ─────
# Local aggregator that fronts the user's paid subs (Claude Code, Codex,
# Kimi Code, Super Grok, Nous Portal, NanoGPT) behind one OpenAI-shape
# endpoint. Speaks /v1/chat/completions, so it reuses the "openai" wire
# format. Distinct provider id keeps routing/audit legible and lets it
# carry a tiered *fallback chain* instead of a single model: a model that
# is transiently un-credentialed ("No credentials for provider: X") or
# errors is skipped and the next tier is tried, so a single expired sub
# never blocks dictation.
OMNIROUTE_PROXY_URL = os.environ.get("DICTATION_OMNIROUTE_URL", "http://127.0.0.1:20128/v1")


def _parse_model_chain(raw: str, default: list[str]) -> list[str]:
    """Parse a comma-separated model chain env override; fall back to default."""
    items = [m.strip() for m in (raw or "").split(",") if m.strip()]
    return items or list(default)


# RP/enhance chain (creative-quality first). Live-verified 2026-07-01.
OMNIROUTE_RP_CHAIN = _parse_model_chain(
    os.environ.get("DICTATION_OMNIROUTE_RP_CHAIN", ""),
    ["claude/claude-opus-4-8", "codex/gpt-5.5", "claude/claude-sonnet-4-6", "nous/x-ai/grok-4.3"],
)
# Cleanup/grammar chain (cheaper + faster first).
OMNIROUTE_CLEAN_CHAIN = _parse_model_chain(
    os.environ.get("DICTATION_OMNIROUTE_CLEAN_CHAIN", ""),
    ["codex/gpt-5.4", "claude/claude-sonnet-4-6"],
)

_VALID_PROVIDERS = {"claude", "openai", "omniroute"}
DEFAULT_FORMATTER_PROVIDER = os.environ.get("DICTATION_FORMATTER_PROVIDER", "omniroute").strip().lower()
if DEFAULT_FORMATTER_PROVIDER not in _VALID_PROVIDERS:
    DEFAULT_FORMATTER_PROVIDER = "omniroute"
DEFAULT_MODEL = "large-v3-turbo"
TRANSCRIPT_CONTEXT_LIMIT = 3000  # max chars of transcript to include in RP+ prompt
PERSONAS_DIR = Path(os.environ.get("DICTATION_PERSONAS_DIR", Path.home() / "STWork/personas"))
RULES_DIR = Path(os.environ.get("DICTATION_RULES_DIR", Path.home() / "STWork/rules"))
# SillyTavern data root. `DICTATION_ST_DATA_ROOT` relocates the whole
# default-user tree in one shot; the per-directory env vars below still win
# individually when set. Default preserves the historical hardcoded path.
ST_DATA_ROOT = Path(os.environ.get(
    "DICTATION_ST_DATA_ROOT",
    "/mnt/hdd/AI/SillyTavern/data/default-user",
))
CHARACTERS_DIR = Path(os.environ.get(
    "DICTATION_CHARACTERS_DIR",
    str(ST_DATA_ROOT / "characters"),
))
ST_CHATS_DIR = Path(os.environ.get(
    "DICTATION_ST_CHATS_DIR", str(ST_DATA_ROOT / "chats")))
ST_GROUPS_DIR = Path(os.environ.get(
    "DICTATION_ST_GROUPS_DIR", str(ST_DATA_ROOT / "groups")))
ST_GROUP_CHATS_DIR = Path(os.environ.get(
    "DICTATION_ST_GROUP_CHATS_DIR", str(ST_DATA_ROOT / "group chats")))
CHAT_CONTEXT_WINDOW = 8  # number of recent messages to include

# Request body caps (2026-07 security audit). Oversized Content-Length is
# rejected with HTTP 413 BEFORE the body is read; malformed/negative values
# get a 400. Env-overridable for unusual deployments.
MAX_JSON_BODY_BYTES = int(os.environ.get(
    "DICTATION_MAX_JSON_BODY_BYTES", str(1 * 1024 * 1024)))     # 1 MB
MAX_AUDIO_BODY_BYTES = int(os.environ.get(
    "DICTATION_MAX_AUDIO_BODY_BYTES", str(25 * 1024 * 1024)))   # 25 MB

# Phase 2 — Modes + Vocab persistence. YAML preferred, JSON fallback if PyYAML missing.
_CONFIG_EXT = "yaml" if _HAVE_YAML else "json"
MODES_FILE = DATA_DIR / f"modes.{_CONFIG_EXT}"
VOCAB_FILE = DATA_DIR / f"vocab.{_CONFIG_EXT}"
CHAR_MODES_FILE = DATA_DIR / f"char-modes.{_CONFIG_EXT}"
# Phase 5 / POL-1 — voice macro grammar persistence. Optional user file;
# absent → hardcoded defaults (see DEFAULT_VOICE_SENTINEL / DEFAULT_VOICE_INTENTS).
VOICE_MACROS_FILE = DATA_DIR / f"voice_macros.{_CONFIG_EXT}"
WHISPER_PROMPT_TOKEN_CAP = 200  # approx; we truncate by word count

# Phase 3 — ST state sync
STATE_FRESH_SECONDS = 60    # < this = fresh, banner shows "Following ST"
STATE_STALE_SECONDS = 120   # between fresh and this = stale, banner warns
# > STATE_STALE_SECONDS = dead, server UI reverts to manual
DISFLUENCY_CLEAN_MODEL = os.environ.get(
    "DICTATION_CLEANUP_MODEL", "claude-haiku-4-5-20251001"
)
DISFLUENCY_CLEAN_TIMEOUT = 3.0  # seconds; falls through to raw on timeout
DISFLUENCY_CLEAN_MAX_WORDS = 500  # skip cleanup if input exceeds this
DISFLUENCY_CLEAN_MIN_WORDS = 12  # skip cleanup below this — LLM round-trip
                                  # is more disruptive than the disfluencies
                                  # it removes for quick-fire RP responses.
                                  # See ADR-9 / Agent 3 §10 recommendation 5.
FORMATTER_TIMEOUT_SECONDS = 5.0  # was 60s — see ADR-9 / Agent 3 §1
                                  # 5s is the real upper bound; a hung
                                  # formatter must not freeze the
                                  # conversation for a full minute.

# Phase 2 — persistent whisper-server (ADR-1).
# A user systemd unit `whisper-server.service` runs the long-lived
# whisper.cpp HTTP server. The dictation-server posts WAV uploads at
# /inference and falls back to a per-request subprocess `whisper-cli`
# invocation when the server is dead and won't start within the
# bootstrap timeout. Idle-shutdown thread reclaims VRAM after N
# minutes of inactivity (5 min default) so ComfyUI / ST-Extras can
# coexist on the same GPU.
WHISPER_SERVER_URL = os.environ.get("WHISPER_SERVER_URL", "http://127.0.0.1:9001")
WHISPER_SERVER_HEALTH_TIMEOUT = 10.0  # seconds to wait for boot after `systemctl start`
WHISPER_SERVER_REQUEST_TIMEOUT = 120.0  # match old whisper-cli timeout
IDLE_SHUTDOWN_SECONDS = int(os.environ.get("WHISPER_IDLE_SHUTDOWN_SECONDS", "300"))
IDLE_SHUTDOWN_DISABLED = bool(os.environ.get("WHISPER_IDLE_SHUTDOWN_DISABLED"))
IDLE_SHUTDOWN_CHECK_INTERVAL = 60.0  # seconds between idle checks

# ─── TTS — Kokoro-82M proxy (read-back UX) ───────────────
# Lifecycle mirrors whisper-server: a sibling user unit (`kokoro-server.service`)
# runs the long-lived Kokoro process on 127.0.0.1:9002. The dictation-server
# proxies POST /tts and GET /tts/voices through to it, with on-demand boot
# and idle-shutdown to keep the model out of memory between uses.
KOKORO_SERVER_URL = os.environ.get("KOKORO_SERVER_URL", "http://127.0.0.1:9002")
KOKORO_DEFAULT_VOICE = os.environ.get("KOKORO_DEFAULT_VOICE", "af_heart")
KOKORO_IDLE_SHUTDOWN_SECONDS = int(os.environ.get("KOKORO_IDLE_SHUTDOWN_SECONDS", "600"))
KOKORO_IDLE_SHUTDOWN_DISABLED = bool(os.environ.get("KOKORO_IDLE_SHUTDOWN_DISABLED"))
KOKORO_PROBE_TIMEOUT = 1.0
KOKORO_BOOT_TIMEOUT = 10.0
KOKORO_REQUEST_TIMEOUT = float(os.environ.get("KOKORO_REQUEST_TIMEOUT", "240"))
KOKORO_VOICES_TTL_SECONDS = 60.0
TTS_MAX_TEXT_CHARS = 5000
TTS_AUDIOBOOK_MAX_MESSAGES = int(os.environ.get("TTS_AUDIOBOOK_MAX_MESSAGES", "200"))
TTS_AUDIOBOOK_MAX_TOTAL_CHARS = int(os.environ.get("TTS_AUDIOBOOK_MAX_TOTAL_CHARS", "60000"))
TTS_AUDIOBOOK_SILENCE_MS = int(os.environ.get("TTS_AUDIOBOOK_SILENCE_MS", "350"))

# ─── SSL cert constants ───────────────────────────────────
CERT_RENEW_THRESHOLD_DAYS = 7   # auto-regenerate if less than this remaining
CERT_WARN_THRESHOLD_DAYS = 30   # log a warning below this
CERT_RENEW_CHECK_INTERVAL_SECONDS = 12 * 3600  # in-run periodic cert check
CERT_FINGERPRINT_FILE = DATA_DIR / "cert.fingerprint"
CERT_VALIDITY_DAYS = 90  # was 3650 — see ADR-7 / Agent 5 §7.
                          # Lower validity exercises the auto-renew code
                          # path quarterly instead of in 2036.
