"""SillyTavern reader layer — personas, character cards, chats, groups.

Read-only view over the SillyTavern data tree. All directory locations are
read from `calliope_server.config` AT CALL TIME (`config.X`), so tests can
monkeypatch `mod.config.<DIR>` and every function here sees the override.
Extracted from the executable `calliope-server` script (Stage 2 split).
"""

import json
import logging
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

from . import config
from .config import _safe_child

log = logging.getLogger("dictation-server")

# ST avatars arrive as image filenames (`Elara.png`); learn-vocab scopes by
# chat directory (`elara`); the phone may send a display stem. All collapse to
# one key via `canonical_character_key`.
_CHARACTER_IMAGE_EXT_RE = re.compile(r"\.(?:png|webp|jpe?g|gif)$", re.IGNORECASE)


def canonical_character_key(raw: object) -> str:
    """Return the one canonical key for a character reference.

    Strips directory parts and a trailing image extension, NFC-normalises,
    casefolds and collapses whitespace: `Elara.png`, `elara` and ` ELARA `
    all map to `elara`. Used for vocab scoping and mode memory; card files
    on disk are resolved separately by `_resolve_character_card_path`.
    """
    text = unicodedata.normalize("NFC", str(raw or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = " ".join(text.split())
    text = _CHARACTER_IMAGE_EXT_RE.sub("", text).strip()
    return unicodedata.normalize("NFC", " ".join(text.casefold().split()))


def _expand_persona_macros(text: str, display_name: str) -> str:
    """Resolve the user-identity macros Calliope can safely know."""
    if not text:
        return ""
    replacements = {
        "{{user}}": display_name,
        "{{persona}}": display_name,
    }
    out = text
    for macro, value in replacements.items():
        out = re.sub(re.escape(macro), value, out, flags=re.IGNORECASE)
    return out


# ─── Parsed-file cache ───────────────────────────────────
# Cards, the persona catalog, persona markdown and group definitions are
# re-read on every dictation/state poll. Cache the PARSED value by
# (mtime_ns, size) so a changed file is re-parsed and an unchanged one is not.
# Values are shared: callers must treat them as read-only. Chat text is never
# cached here.
_PARSE_CACHE_MAX = 128
_parse_cache: dict[tuple[str, str], tuple[tuple[int, int], object]] = {}
_parse_cache_lock = threading.Lock()


def _cached_parse(kind: str, path: Path, parse):
    """Return `parse(path)`, reusing the last result while the file is unchanged.

    Returns None when the file cannot be stat'ed; `parse` must not raise.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    sig = (st.st_mtime_ns, st.st_size)
    key = (kind, str(path))
    with _parse_cache_lock:
        hit = _parse_cache.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    value = parse(path)
    with _parse_cache_lock:
        _parse_cache.pop(key, None)
        while len(_parse_cache) >= _PARSE_CACHE_MAX:
            _parse_cache.pop(next(iter(_parse_cache)))
        _parse_cache[key] = (sig, value)
    return value


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _read_text_cached(path: Path) -> str | None:
    """UTF-8 text of a small local file (persona markdown), cached by signature."""
    return _cached_parse("text", path, _read_text)


# ─── Persona discovery & loading ─────────────────────────
def _load_st_persona_catalog() -> dict[str, dict]:
    """Read only persona names/descriptions from SillyTavern settings (cached)."""
    catalog = _cached_parse("persona-catalog", config.ST_SETTINGS_FILE, _parse_st_persona_catalog)
    return catalog if isinstance(catalog, dict) else {}


def _parse_st_persona_catalog(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    power_user = data.get("power_user", {})
    if not isinstance(power_user, dict):
        return {}
    names = power_user.get("personas", {})
    descriptions = power_user.get("persona_descriptions", {})
    if not isinstance(names, dict):
        return {}
    if not isinstance(descriptions, dict):
        descriptions = {}
    out: dict[str, dict] = {}
    for persona_id, display_name in names.items():
        if not isinstance(persona_id, str) or not persona_id.strip():
            continue
        descriptor = descriptions.get(persona_id, {})
        description = descriptor.get("description", "") if isinstance(descriptor, dict) else ""
        name = str(display_name or persona_id).strip()
        out[persona_id] = {
            "id": persona_id,
            "name": name,
            "description": _expand_persona_macros(str(description or "").strip(), name),
            "source": "sillytavern",
        }
    return out


def discover_personas() -> list[dict]:
    """Return all SillyTavern personas plus optional local markdown personas."""
    merged = {
        persona_id: {"id": p["id"], "name": p["name"], "source": p["source"]}
        for persona_id, p in _load_st_persona_catalog().items()
    }
    if config.PERSONAS_DIR.is_dir():
        for f in sorted(config.PERSONAS_DIR.glob("*.md")):
            if f.name.endswith(".voice.md"):
                continue
            name = f.stem.replace("-", " ").replace("_", " ").title()
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("# "):
                        raw = line.lstrip("# ").strip()
                        raw = re.sub(r"\s*[—\-]\s*PERSONA.*$", "", raw, flags=re.IGNORECASE).strip()
                        name = raw
                        break
            except Exception:
                pass
            merged[f.stem] = {"id": f.stem, "name": name, "source": "local"}
    return sorted(merged.values(), key=lambda p: (p["name"].casefold(), p["id"].casefold()))


def load_persona_voice(persona_id: str) -> str:
    """Load condensed voice guide for a persona. Prefers .voice.md sidecar."""
    if not persona_id or persona_id == "none":
        return ""

    # Try sidecar first
    voice_file = _safe_child(config.PERSONAS_DIR, persona_id, ".voice.md")
    if voice_file is None:
        return ""
    text = (_read_text_cached(voice_file) or "").strip() if voice_file.is_file() else ""
    if text:
        return f"PERSONA VOICE GUIDE (you are writing AS this persona):\n{text}"

    # Fallback: auto-extract from full persona file
    full_file = _safe_child(config.PERSONAS_DIR, persona_id, ".md")
    if full_file is None or not full_file.exists():
        st_persona = _load_st_persona_catalog().get(persona_id)
        if st_persona and st_persona.get("description"):
            return (
                "PERSONA VOICE GUIDE (you are writing AS this SillyTavern persona):\n"
                + st_persona["description"][:4000].rstrip()
            )
        return ""

    content = _read_text_cached(full_file)
    try:
        if content is None:
            return ""
        sections = []
        # Extract key sections by header
        for header in ("QUICK REFERENCE", "PHYSICAL PRESENCE", "COMMUNICATION PATTERNS", "BEHAVIORAL TELLS"):
            pattern = rf"##\s+{header}\s*\n(.*?)(?=\n##|\Z)"
            match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
            if match:
                section_text = match.group(1).strip()
                # Truncate long sections
                if len(section_text) > 500:
                    section_text = section_text[:500] + "..."
                sections.append(section_text)
        if sections:
            return "PERSONA VOICE GUIDE (you are writing AS this persona):\n" + "\n\n".join(sections)
    except Exception:
        pass

    return ""


def load_persona_full(persona_id: str, max_chars: int = 2000) -> dict:
    """Return the full persona card: {id, name, description}.

    Prefers the full .md file (name + description up to ~max_chars).
    Falls back to the voice sidecar or the ID itself if nothing else is available.
    Used by persona_pov mode which needs a richer narrator description than voice guide.
    """
    if not persona_id or persona_id == "none":
        return {}

    full_file = _safe_child(config.PERSONAS_DIR, persona_id, ".md")
    voice_file = _safe_child(config.PERSONAS_DIR, persona_id, ".voice.md")
    if full_file is None or voice_file is None:
        return {}

    name = persona_id.replace("-", " ").replace("_", " ").title()
    description = ""

    content = _read_text_cached(full_file) if full_file.is_file() else None
    if content is not None:
        try:
            # Extract display name from first heading if present
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    raw = stripped.lstrip("# ").strip()
                    raw = re.sub(r"\s*[—\-]\s*PERSONA.*$", "", raw, flags=re.IGNORECASE).strip()
                    if raw:
                        name = raw
                    break
            description = content.strip()
            if len(description) > max_chars:
                description = description[:max_chars].rstrip() + "\n\n[...truncated]"
        except Exception as e:
            log.warning(f"Failed to read persona {persona_id}: {type(e).__name__}")

    if not description and voice_file.is_file():
        description = (_read_text_cached(voice_file) or "").strip()
        if len(description) > max_chars:
            description = description[:max_chars].rstrip() + "\n\n[...truncated]"

    if not description:
        st_persona = _load_st_persona_catalog().get(persona_id)
        if st_persona:
            name = st_persona["name"]
            full_description = st_persona["description"]
            description = full_description[:max_chars].rstrip()
            if len(full_description) > max_chars:
                description += "\n\n[...truncated]"

    return {"id": persona_id, "name": name, "description": description}


# ─── Character card discovery & loading ───────────────────
def discover_characters() -> list[dict]:
    """Scan config.CHARACTERS_DIR for .png character cards, return sorted name list."""
    characters = []
    if not config.CHARACTERS_DIR.is_dir():
        return characters
    for f in sorted(config.CHARACTERS_DIR.glob("*.png")):
        characters.append({"id": f.stem, "name": f.stem})
    return characters


def discover_group_characters(group_id: str) -> list[dict]:
    """POL-6 — return only the members of `group_id` (subset of full list).

    `id` is the member's avatar filename (the stable card identity, e.g.
    `FileStem.png`); `name` is the card's display name, a label only. Display
    names can differ from file stems or repeat, so they never identify a card.
    Empty list when the group id is unknown.
    """
    members = ChatReader.group_member_objects(group_id)
    return [{"id": m["avatar"], "name": m["name"]} for m in members]


def _resolve_character_card_path(char_id: str) -> Path | None:
    """Find the card PNG in CHARACTERS_DIR for any form of character reference.

    Accepts the raw ST avatar (`Elara.png`), a stem in any case (`Elara`) or
    the canonical key (`elara`). Tries the exact-case `<stem>.png` first, then
    matches stems case-insensitively via `canonical_character_key`. Only
    files directly inside CHARACTERS_DIR are ever returned.
    """
    key = canonical_character_key(char_id)
    if not key or key in (".", ".."):
        return None
    base = config.CHARACTERS_DIR
    stem = _CHARACTER_IMAGE_EXT_RE.sub("", str(char_id or "").strip())
    exact = _safe_child(base, stem, ".png")
    if exact is not None and exact.is_file():
        return exact
    if not base.is_dir():
        return None
    for f in sorted(base.glob("*.png")):
        if canonical_character_key(f.stem) == key:
            candidate = _safe_child(base, f.name)
            if candidate is not None and candidate.is_file():
                return candidate
    return None



_CARD_TEXT_FIELDS = ("name", "description", "personality", "scenario")


def _parse_character_png(path: Path) -> dict:
    """Extract and validate the chara card JSON from a PNG tEXt chunk.

    Returns {} for unreadable/malformed cards: a non-object root or `data`,
    and non-string text fields are dropped rather than crashing callers.
    """
    import struct
    import base64

    try:
        data = path.read_bytes()
        pos = 8  # skip PNG signature
        while pos + 8 <= len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            chunk_type = data[pos + 4:pos + 8]
            if chunk_type == b"tEXt":
                parts = data[pos + 8:pos + 8 + length].split(b"\x00", 1)
                if parts[0] == b"chara" and len(parts) > 1:
                    card_json = json.loads(base64.b64decode(parts[1]))
                    if not isinstance(card_json, dict):
                        return {}
                    # Normalize: some cards nest under "data", some don't
                    card = card_json.get("data", card_json)
                    if not isinstance(card, dict):
                        return {}
                    return {
                        k: v for k, v in card.items()
                        if k not in _CARD_TEXT_FIELDS or isinstance(v, str)
                    }
            pos += 12 + length
    except Exception as e:
        log.warning("Failed to parse character card %s: %s", path.name, type(e).__name__)
    return {}


def load_character_card(char_id: str) -> dict:
    """Extract chara_card_v2 JSON from a character PNG's tEXt chunk.

    Parsed cards are cached by file signature; the returned dict is shared
    and must not be mutated.

    POL-6: when `char_id` is a group id (e.g. the ST extension still
    forwarding `selected_group` while the addressee picker rolls out),
    return an explicit error envelope instead of an empty dict so the
    caller can branch deliberately. Existing valid character cards are
    unaffected.
    """
    card_path = _resolve_character_card_path(char_id)
    if card_path is None:
        # Disambiguate: a UUID-ish id with no PNG → could be a group id
        # the bridge sent before the addressee picker shipped.
        key = canonical_character_key(char_id)
        try:
            if key and key in {canonical_character_key(g) for g in ChatReader.get_group_id_set()}:
                return {"error": "group_id_not_a_character", "group_id": char_id}
        except Exception:  # pragma: no cover — defensive: never let group probe break card load
            pass
        return {}
    card = _cached_parse("card", card_path, _parse_character_png)
    return card if isinstance(card, dict) else {}


def character_context_from_card(card: dict, char_id: str = "") -> str:
    """Condensed character context for prompts from an already-loaded card."""
    if not isinstance(card, dict) or not card or card.get("error"):
        return ""
    parts = [f"CHARACTER: {card.get('name') or char_id}"]

    desc = str(card.get("description") or "").strip()
    if desc:
        # Truncate very long descriptions to save tokens
        if len(desc) > 1500:
            desc = desc[:1500] + "..."
        parts.append(desc)

    personality = str(card.get("personality") or "").strip()
    if personality:
        parts.append(f"Personality: {personality}")

    scenario = str(card.get("scenario") or "").strip()
    if scenario:
        parts.append(f"Scenario: {scenario}")

    return "\n\n".join(parts)


def build_character_context(char_id: str) -> str:
    """Build a condensed character context string for RP+ prompts."""
    if not char_id or char_id == "none":
        return ""
    # POL-6: a group-id surfaces as {error: 'group_id_not_a_character'} and
    # yields no character context rather than crashing on a `name` lookup.
    return character_context_from_card(load_character_card(char_id), char_id)


def _id_text(value: object) -> str:
    """ST ids are strings or numbers; anything else is not an id."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def _parse_group_def(path: Path) -> dict | None:
    """Parse one group definition; None unless it is a JSON object.

    `id`/`chat_id` are normalized to strings, `name` to a string, and
    `members`/`chats` keep only string/number entries.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    group = dict(data)
    group["id"] = _id_text(data.get("id"))
    group["chat_id"] = _id_text(data.get("chat_id"))
    name = data.get("name")
    group["name"] = name if isinstance(name, str) else ""
    members = data.get("members")
    group["members"] = [m for m in members if isinstance(m, str) and m.strip()] if isinstance(members, list) else []
    chats = data.get("chats")
    group["chats"] = [c for c in (_id_text(x) for x in chats) if c] if isinstance(chats, list) else []
    return group


# ─── ST Chat Reader ──────────────────────────────────────
_recent_chats_cache: dict = {"data": [], "ts": 0.0}
# Guards _recent_chats_cache under ThreadingHTTPServer (audit fix 6). The
# filesystem scan itself runs unlocked (slow, idempotent); only cache
# read/write is serialized.
_recent_chats_lock = threading.Lock()


class ChatReader:
    """Reads SillyTavern chat files (JSONL) and group definitions."""

    @staticmethod
    def _tail_lines(filepath: Path, n: int) -> list[str]:
        """Read last n lines from a file efficiently."""
        try:
            with open(filepath, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return []
                block_size = min(size, 8192)
                lines: list[bytes] = []
                pos = size
                while len(lines) < n + 1 and pos > 0:
                    read_size = min(block_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    chunk = f.read(read_size)
                    new_lines = chunk.split(b"\n")
                    if lines:
                        # Merge the last fragment of chunk with first fragment of lines
                        new_lines[-1] = new_lines[-1] + lines[0]
                        lines = new_lines + lines[1:]
                    else:
                        lines = new_lines
                return [
                    line.decode("utf-8", errors="replace")
                    for line in lines[-n:]
                    if line.strip()
                ]
        except Exception as e:
            log.warning(f"Failed to tail {filepath}: {e}")
            return []

    @staticmethod
    def _find_newest_jsonl(directory: Path) -> Path | None:
        """Most recently modified .jsonl file directly inside `directory`.

        Files that resolve outside the directory (symlinks) or vanish/fail
        to stat mid-scan are skipped.
        """
        if not directory.is_dir():
            return None
        newest: Path | None = None
        newest_mtime = -1
        for f in directory.glob("*.jsonl"):
            safe = _safe_child(directory, f.name)
            if safe is None:
                continue
            try:
                st = safe.stat()
            except OSError:
                continue
            if st.st_mtime_ns > newest_mtime and safe.is_file():
                newest, newest_mtime = safe, st.st_mtime_ns
        return newest

    @staticmethod
    def _load_group_defs() -> list[dict]:
        """Load all valid group definitions (parsed files cached by signature)."""
        groups = []
        if not config.ST_GROUPS_DIR.is_dir():
            return groups
        for f in sorted(config.ST_GROUPS_DIR.glob("*.json")):
            group = _cached_parse("group", f, _parse_group_def)
            if group:
                groups.append(group)
        return groups

    @staticmethod
    def _find_group_chat_file(group: dict) -> Path | None:
        """Find the chat file for a group definition."""
        chat_id = group.get("chat_id", "")
        if not chat_id:
            return None
        chat_file = _safe_child(config.ST_GROUP_CHATS_DIR, str(chat_id), ".jsonl")
        if chat_file is None:
            return None
        if chat_file.exists():
            return chat_file
        return None

    @staticmethod
    def get_recent_chats(limit: int = 20) -> list[dict]:
        """Return recently active chats sorted by last modified time.

        Uses a 5-second cache to avoid rescanning the filesystem on every call.
        """
        import time

        now = time.time()
        with _recent_chats_lock:
            if _recent_chats_cache["data"] and (now - _recent_chats_cache["ts"]) < 5.0:
                return list(_recent_chats_cache["data"][:limit])

        chats: list[dict] = []

        # Individual chats
        if config.ST_CHATS_DIR.is_dir():
            for char_dir in config.ST_CHATS_DIR.iterdir():
                if not char_dir.is_dir():
                    continue
                newest = ChatReader._find_newest_jsonl(char_dir)
                if newest is None:
                    continue
                try:
                    stat = newest.stat()
                    # Count lines (approximate message count)
                    line_count = sum(1 for _ in open(newest, "rb")) - 1  # minus metadata line
                    chats.append({
                        "name": char_dir.name,
                        "type": "individual",
                        "message_count": max(0, line_count),
                        "last_active": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        "file": str(newest),
                        "_mtime": stat.st_mtime,
                    })
                except Exception:
                    continue

        # Group chats
        for group in ChatReader._load_group_defs():
            chat_file = ChatReader._find_group_chat_file(group)
            if chat_file is None:
                continue
            try:
                stat = chat_file.stat()
                line_count = sum(1 for _ in open(chat_file, "rb")) - 1
                chats.append({
                    "name": group.get("name", "Unknown Group"),
                    "type": "group",
                    "message_count": max(0, line_count),
                    "last_active": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "file": str(chat_file),
                    "_mtime": stat.st_mtime,
                })
            except Exception:
                continue

        # Sort by modification time, newest first
        chats.sort(key=lambda c: c.get("_mtime", 0), reverse=True)
        # Strip internal fields
        for c in chats:
            c.pop("_mtime", None)

        with _recent_chats_lock:
            _recent_chats_cache["data"] = chats
            _recent_chats_cache["ts"] = now
        return chats[:limit]

    @staticmethod
    def get_active_chat() -> dict | None:
        """Return the single most recently modified chat (individual or group)."""
        chats = ChatReader.get_recent_chats(limit=1)
        return chats[0] if chats else None

    @staticmethod
    def read_chat_messages(
        chat_name: str, chat_type: str = "individual", last_n: int = 10
    ) -> list[dict]:
        """Read the last N messages from a chat.

        Reads from the END of the file efficiently.
        """
        chat_file: Path | None = None

        if chat_type == "individual":
            chat_dir = _safe_child(config.ST_CHATS_DIR, chat_name)
            if chat_dir is None:
                return []
            chat_file = ChatReader._find_newest_jsonl(chat_dir)
        elif chat_type == "group":
            for group in ChatReader._load_group_defs():
                if group.get("name", "") == chat_name:
                    chat_file = ChatReader._find_group_chat_file(group)
                    break

        if chat_file is None:
            return []
        return ChatReader._read_chat_file(chat_file, last_n)

    @staticmethod
    def _read_chat_file(chat_file: Path, last_n: int) -> list[dict]:
        """Parse the last `last_n` messages of a JSONL chat file.

        Lines that are not JSON objects are skipped; non-string `name`/`mes`
        become defaults instead of failing the whole read.
        """
        # Read extra lines to account for metadata line and ensure we get enough
        raw_lines = ChatReader._tail_lines(chat_file, last_n + 2)

        messages = []
        for line in raw_lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Skip metadata line and malformed records
            if not isinstance(data, dict) or "chat_metadata" in data:
                continue
            extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
            name = data.get("name")
            mes = data.get("mes")
            send_date = data.get("send_date")
            messages.append({
                "name": name if isinstance(name, str) and name else "Unknown",
                "is_user": bool(data.get("is_user", False)),
                "is_system": (
                    bool(data.get("is_system", False))
                    or str(extra.get("type", "")).lower() == "system"
                ),
                "text": mes if isinstance(mes, str) else "",
                "send_date": send_date if isinstance(send_date, (str, int, float)) else "",
            })

        # Return only the last N actual messages
        return messages[-last_n:]

    @staticmethod
    def read_state_chat(state: dict, last_n: int) -> tuple[list[dict], bool] | None:
        """Read the EXACT chat an ST state snapshot names.

        `chatId` is the chat file stem. Group chats (`chatType == "group"`)
        live in the group-chats dir; when the snapshot's group (`groupId`, or
        `characterId` which ST sets to the group id) is known, the chat must
        belong to it. Solo/individual chats live in `chats/<avatar stem>/`
        (`characterId` is the avatar filename), with `characterName` as a
        fallback directory. Every path is `_safe_child`-contained.

        Returns (messages, is_group), or None when the chat cannot be
        resolved so the caller can fall back to its own lookup.
        """
        if not isinstance(state, dict):
            return None
        chat_id = state.get("chatId")
        if not isinstance(chat_id, str) or not chat_id.strip():
            return None
        chat_id = chat_id.strip()
        if chat_id.lower().endswith(".jsonl"):
            chat_id = chat_id[:-len(".jsonl")]
        chat_type = str(state.get("chatType") or "").strip().lower()

        if chat_type == "group":
            chat_file = _safe_child(config.ST_GROUP_CHATS_DIR, chat_id, ".jsonl")
            if chat_file is None or not chat_file.is_file():
                return None
            group_id = _id_text(state.get("groupId")) or _id_text(state.get("characterId"))
            group = ChatReader.find_group_by_id(group_id) if group_id else None
            if group is not None and chat_id not in {group["chat_id"], *group["chats"]}:
                return None
            return ChatReader._read_chat_file(chat_file, last_n), True

        if chat_type not in ("solo", "individual", ""):
            return None
        candidates: list[str] = []
        avatar = state.get("characterId")
        if isinstance(avatar, str) and avatar.strip():
            candidates.append(_CHARACTER_IMAGE_EXT_RE.sub("", avatar.strip()))
        name = state.get("characterName")
        if isinstance(name, str) and name.strip() and name.strip() not in candidates:
            candidates.append(name.strip())
        for candidate in candidates:
            chat_dir = _safe_child(config.ST_CHATS_DIR, candidate)
            if chat_dir is None or not chat_dir.is_dir():
                continue
            chat_file = _safe_child(chat_dir, chat_id, ".jsonl")
            if chat_file is not None and chat_file.is_file():
                return ChatReader._read_chat_file(chat_file, last_n), False
        return None

    @staticmethod
    def _group_member_name_from_avatar(avatar: str) -> str:
        """Resolve an ST group member avatar filename to display name.

        Group defs store member avatar filenames. Prefer the character card's
        declared name when the matching synthetic/real card exists; if the
        avatar/card was deleted, fall back to the filename stem instead of
        dropping the member or crashing.
        """
        stem = Path(str(avatar or "")).stem
        if not stem:
            return ""
        try:
            card = load_character_card(stem)
        except Exception:  # pragma: no cover — defensive fallback only
            card = {}
        if isinstance(card, dict):
            name = str(card.get("name") or "").strip()
            if name:
                return name
        return stem

    @staticmethod
    def get_group_members(group_name: str) -> list[str]:
        """Return member display names for a group chat."""
        for group in ChatReader._load_group_defs():
            if group.get("name", "") == group_name:
                names: list[str] = []
                for member in group.get("members", []) or []:
                    name = ChatReader._group_member_name_from_avatar(str(member))
                    if name:
                        names.append(name)
                return names
        return []

    # ─── POL-6 — group-id helpers + last-speaker tracking ─────
    @staticmethod
    def find_group_by_id(group_id: str) -> dict | None:
        """Return the group definition matching `group_id`, or None.

        Matches against the group def's `id` field. Used by
        `/characters?group=…` and the addressee-picker plumbing.
        """
        if not group_id:
            return None
        for group in ChatReader._load_group_defs():
            if str(group.get("id", "")) == group_id:
                return group
        return None

    @staticmethod
    def get_group_id_set() -> set[str]:
        """Return the set of all known group ids on disk.

        Used by `load_character_card` to disambiguate a group-id
        accidentally passed where a character-id is expected (the bridge
        used to do this; addressee picker fixes it forward).
        """
        return {
            str(g.get("id", ""))
            for g in ChatReader._load_group_defs()
            if g.get("id")
        }

    @staticmethod
    def group_member_objects(group_id: str) -> list[dict]:
        """Return per-member `[{name, avatar}]` dicts for a group.

        `avatar` is the on-disk filename (`Hana.png`); `name` is the
        stem (`Hana`). Used by `/characters?group=…` and the
        `/state` + `/active-chat` group payloads.
        """
        group = ChatReader.find_group_by_id(group_id)
        if not group:
            return []
        out: list[dict] = []
        for raw in group.get("members", []) or []:
            avatar = str(raw)
            name = ChatReader._group_member_name_from_avatar(avatar)
            if name:
                out.append({"name": name, "avatar": avatar})
        return out

    @staticmethod
    def get_group_last_speaker(group_id: str) -> str | None:
        """Return the most recent non-user speaker in a group chat.

        Reads the tail of the group chat (existing chat-tail helpers)
        and finds the rightmost `is_user: false` message. Returns the
        speaker `name` field, or None when the chat has no AI messages
        yet (cold-start / brand-new group).

        Cached per `(group_id, mtime)` to avoid re-tailing the chat
        file on every /state poll.
        """
        global _group_last_speaker_cache
        if not group_id:
            return None
        group = ChatReader.find_group_by_id(group_id)
        if not group:
            return None
        chat_file = ChatReader._find_group_chat_file(group)
        if chat_file is None:
            return None
        try:
            mtime = chat_file.stat().st_mtime
        except OSError:
            return None
        cache_key = (group_id, mtime)
        with _group_last_speaker_lock:
            cached = _group_last_speaker_cache.get(cache_key)
            if cached is not None:
                return cached or None  # may be sentinel ""
        messages = ChatReader.read_chat_messages(
            group.get("name", ""), "group", last_n=20,
        )
        last_ai_name: str | None = None
        for msg in reversed(messages):
            if msg.get("is_user") or msg.get("is_system"):
                continue
            name = str(msg.get("name", "") or "").strip()
            if name and name.lower() not in {"you", "system"}:
                last_ai_name = name
                break
        with _group_last_speaker_lock:
            # Trim cache to prevent unbounded growth (keep ~32 entries).
            if len(_group_last_speaker_cache) > 64:
                _group_last_speaker_cache.clear()
            _group_last_speaker_cache[cache_key] = last_ai_name or ""
        return last_ai_name


# POL-6 — last-speaker cache (`(group_id, mtime) -> name|""`).
# A sentinel "" means "we checked, no AI message yet" — distinct from
# cache miss. Lock protects both read + write paths.
_group_last_speaker_cache: dict = {}
_group_last_speaker_lock = threading.Lock()


class ContextBuilder:
    """Assembles context strings from chat history for RP+ prompts."""

    @staticmethod
    def format_messages(messages: list[dict], is_group: bool = False) -> str:
        """Format messages into a context string.

        Individual: [You]: ... / [Partner]: ...
        Group: [CharName]: ... / [You]: ...
        """
        lines = []
        for msg in messages:
            if msg["is_user"]:
                label = "You"
            elif is_group:
                label = msg.get("name", "Partner")
            else:
                label = "Partner"
            text = msg.get("text", "").strip()
            if text:
                lines.append(f"[{label}]: {text}")
        return "\n\n".join(lines)

    @staticmethod
    def build_context(
        chat_name: str, chat_type: str, window_size: int = config.CHAT_CONTEXT_WINDOW
    ) -> str:
        """Build context string from chat history."""
        messages = ChatReader.read_chat_messages(chat_name, chat_type, last_n=window_size)
        if not messages:
            return ""
        is_group = chat_type == "group"
        return ContextBuilder.format_messages(messages, is_group=is_group)

