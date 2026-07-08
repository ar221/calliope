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
from datetime import datetime
from pathlib import Path

from . import config
from .config import _safe_child

log = logging.getLogger("dictation-server")


# ─── Persona discovery & loading ─────────────────────────
def discover_personas() -> list[dict]:
    """Scan config.PERSONAS_DIR for .md files (excluding .voice.md sidecars)."""
    personas = []
    if not config.PERSONAS_DIR.is_dir():
        return personas
    for f in sorted(config.PERSONAS_DIR.glob("*.md")):
        if f.name.endswith(".voice.md"):
            continue
        # Extract display name from first heading
        name = f.stem.replace("-", " ").replace("_", " ").title()
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("# "):
                    raw = line.lstrip("# ").strip()
                    # Clean up common suffixes and version tags
                    raw = re.sub(r"\s*[—\-]\s*PERSONA.*$", "", raw, flags=re.IGNORECASE).strip()
                    name = raw
                    break
        except Exception:
            pass
        personas.append({"id": f.stem, "name": name})
    return personas


def load_persona_voice(persona_id: str) -> str:
    """Load condensed voice guide for a persona. Prefers .voice.md sidecar."""
    if not persona_id or persona_id == "none":
        return ""

    # Try sidecar first
    voice_file = _safe_child(config.PERSONAS_DIR, persona_id, ".voice.md")
    if voice_file is None:
        return ""
    if voice_file.exists():
        try:
            text = voice_file.read_text(encoding="utf-8").strip()
            if text:
                return f"PERSONA VOICE GUIDE (you are writing AS this persona):\n{text}"
        except Exception:
            pass

    # Fallback: auto-extract from full persona file
    full_file = _safe_child(config.PERSONAS_DIR, persona_id, ".md")
    if full_file is None or not full_file.exists():
        return ""

    try:
        content = full_file.read_text(encoding="utf-8")
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

    if full_file.exists():
        try:
            content = full_file.read_text(encoding="utf-8")
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
            log.warning(f"Failed to read persona {persona_id}: {e}")

    if not description and voice_file.exists():
        try:
            description = voice_file.read_text(encoding="utf-8").strip()
            if len(description) > max_chars:
                description = description[:max_chars].rstrip() + "\n\n[...truncated]"
        except Exception:
            pass

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

    Each entry mirrors `discover_characters` shape (`{id, name}`) so the
    addressee picker can drop in the same chip rendering. Empty list when
    the group id is unknown.
    """
    members = ChatReader.group_member_objects(group_id)
    return [{"id": m["name"], "name": m["name"]} for m in members]


def load_character_card(char_id: str) -> dict:
    """Extract chara_card_v2 JSON from a character PNG's tEXt chunk.

    POL-6: when `char_id` is a group id (e.g. the ST extension still
    forwarding `selected_group` while the addressee picker rolls out),
    return an explicit error envelope instead of an empty dict so the
    caller can branch deliberately. Existing valid character cards are
    unaffected.
    """
    import struct
    import base64

    card_path = _safe_child(config.CHARACTERS_DIR, char_id, ".png")
    if card_path is None:
        return {}
    if not card_path.exists():
        # Disambiguate: a UUID-ish id with no PNG → could be a group id
        # the bridge sent before the addressee picker shipped.
        try:
            if char_id and char_id in ChatReader.get_group_id_set():
                return {"error": "group_id_not_a_character", "group_id": char_id}
        except Exception:  # pragma: no cover — defensive: never let group probe break card load
            pass
        return {}

    try:
        data = card_path.read_bytes()
        pos = 8  # skip PNG signature
        while pos < len(data):
            length = struct.unpack(">I", data[pos:pos + 4])[0]
            chunk_type = data[pos + 4:pos + 8]
            chunk_data = data[pos + 8:pos + 8 + length]

            if chunk_type == b"tEXt":
                parts = chunk_data.split(b"\x00", 1)
                if parts[0] == b"chara" and len(parts) > 1:
                    card_json = json.loads(base64.b64decode(parts[1]))
                    # Normalize: some cards nest under "data", some don't
                    return card_json.get("data", card_json)

            pos += 12 + length
    except Exception as e:
        log.warning(f"Failed to load character card {char_id}: {e}")

    return {}


def build_character_context(char_id: str) -> str:
    """Build a condensed character context string for RP+ prompts."""
    if not char_id or char_id == "none":
        return ""

    card = load_character_card(char_id)
    if not card or card.get("error"):
        # POL-6: a group-id surfaces as {error: 'group_id_not_a_character'}.
        # Treat as "no character context" rather than crashing on a `name`
        # lookup — the addressee picker is the right surface to resolve this.
        return ""

    parts = []
    name = card.get("name", char_id)
    parts.append(f"CHARACTER: {name}")

    desc = card.get("description", "").strip()
    if desc:
        # Truncate very long descriptions to save tokens
        if len(desc) > 1500:
            desc = desc[:1500] + "..."
        parts.append(desc)

    personality = card.get("personality", "").strip()
    if personality:
        parts.append(f"Personality: {personality}")

    scenario = card.get("scenario", "").strip()
    if scenario:
        parts.append(f"Scenario: {scenario}")

    return "\n\n".join(parts)


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
        """Find the most recently modified .jsonl file in a directory."""
        if not directory.is_dir():
            return None
        jsonl_files = list(directory.glob("*.jsonl"))
        if not jsonl_files:
            return None
        return max(jsonl_files, key=lambda f: f.stat().st_mtime)

    @staticmethod
    def _load_group_defs() -> list[dict]:
        """Load all group definition JSON files."""
        groups = []
        if not config.ST_GROUPS_DIR.is_dir():
            return groups
        for f in config.ST_GROUPS_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                groups.append(data)
            except Exception:
                continue
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
            # Skip metadata line
            if "chat_metadata" in data:
                continue
            extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
            messages.append({
                "name": data.get("name", "Unknown"),
                "is_user": bool(data.get("is_user", False)),
                "is_system": (
                    bool(data.get("is_system", False))
                    or str(extra.get("type", "")).lower() == "system"
                ),
                "text": data.get("mes", ""),
                "send_date": data.get("send_date", ""),
            })

        # Return only the last N actual messages
        return messages[-last_n:]

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

