"""POL-6 — group-chat addressee picker server-side helpers.

Covers:
  - ChatReader.find_group_by_id / get_group_id_set / group_member_objects
  - ChatReader.get_group_last_speaker (cached by mtime)
  - load_character_card returns explicit error envelope on group-id input
  - discover_group_characters maps group members → chip-friendly shape
  - snapshot_state normalizes chatType + injects group payload
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import time
from importlib.machinery import SourceFileLoader

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


@pytest.fixture(scope="module")
def mod():
    loader = SourceFileLoader("calliope_server", str(SRC))
    spec = importlib.util.spec_from_loader("calliope_server", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def group_fixture(mod, tmp_path, monkeypatch):
    """Lay down a group def + group chat file under tmp_path and rebind paths."""
    groups_dir = tmp_path / "groups"
    group_chats_dir = tmp_path / "group_chats"
    chars_dir = tmp_path / "characters"
    groups_dir.mkdir()
    group_chats_dir.mkdir()
    chars_dir.mkdir()

    group_id = "group-uuid-1234"
    group_def = {
        "id": group_id,
        "name": "Asylum Crew",
        "members": ["Hana.png", "Yuri.png", "Suzy.png"],
        "chat_id": "asylum-chat-1",
    }
    (groups_dir / "asylum.json").write_text(
        json.dumps(group_def), encoding="utf-8",
    )

    # Group chat tail with a final AI message from Hana.
    chat_lines = [
        json.dumps({"chat_metadata": {}}),
        json.dumps({"name": "You", "is_user": True, "mes": "hello"}),
        json.dumps({"name": "Yuri", "is_user": False, "mes": "tense room"}),
        json.dumps({"name": "Hana", "is_user": False, "mes": "hi back"}),
    ]
    (group_chats_dir / "asylum-chat-1.jsonl").write_text(
        "\n".join(chat_lines) + "\n", encoding="utf-8",
    )

    monkeypatch.setattr(mod, "ST_GROUPS_DIR", groups_dir)
    monkeypatch.setattr(mod, "ST_GROUP_CHATS_DIR", group_chats_dir)
    monkeypatch.setattr(mod, "CHARACTERS_DIR", chars_dir)
    # Clear last-speaker cache between tests.
    with mod._group_last_speaker_lock:
        mod._group_last_speaker_cache.clear()
    return {
        "group_id": group_id,
        "group_name": "Asylum Crew",
        "members": ["Hana", "Yuri", "Suzy"],
        "groups_dir": groups_dir,
        "group_chats_dir": group_chats_dir,
        "chars_dir": chars_dir,
    }


# ─── Group-id helpers ─────────────────────────────────────────


def test_find_group_by_id(mod, group_fixture):
    g = mod.ChatReader.find_group_by_id(group_fixture["group_id"])
    assert g is not None
    assert g["name"] == group_fixture["group_name"]


def test_find_group_by_id_unknown_returns_none(mod, group_fixture):
    assert mod.ChatReader.find_group_by_id("does-not-exist") is None
    assert mod.ChatReader.find_group_by_id("") is None


def test_group_id_set(mod, group_fixture):
    ids = mod.ChatReader.get_group_id_set()
    assert group_fixture["group_id"] in ids


def test_group_member_objects(mod, group_fixture):
    members = mod.ChatReader.group_member_objects(group_fixture["group_id"])
    names = [m["name"] for m in members]
    avatars = [m["avatar"] for m in members]
    assert names == group_fixture["members"]
    assert avatars == ["Hana.png", "Yuri.png", "Suzy.png"]


# ─── Last speaker tracking ────────────────────────────────────


def test_last_speaker_returns_most_recent_ai(mod, group_fixture):
    speaker = mod.ChatReader.get_group_last_speaker(group_fixture["group_id"])
    assert speaker == "Hana"  # last is_user:false in the chat


def test_last_speaker_cached_by_mtime(mod, group_fixture):
    """Second call hits the cache; mtime invalidates."""
    gid = group_fixture["group_id"]
    chat_file = group_fixture["group_chats_dir"] / "asylum-chat-1.jsonl"

    s1 = mod.ChatReader.get_group_last_speaker(gid)
    assert s1 == "Hana"

    # Append a new AI message under a different name; mtime moves.
    new_line = json.dumps({"name": "Suzy", "is_user": False, "mes": "yo"})
    with open(chat_file, "a", encoding="utf-8") as f:
        f.write(new_line + "\n")
    # Force a different mtime so the cache key differs.
    later = time.time() + 5
    import os
    os.utime(chat_file, (later, later))

    s2 = mod.ChatReader.get_group_last_speaker(gid)
    assert s2 == "Suzy"


def test_last_speaker_none_when_no_ai_messages(mod, group_fixture, tmp_path):
    """Brand-new group chat → None (cold start)."""
    chat_file = group_fixture["group_chats_dir"] / "asylum-chat-1.jsonl"
    chat_file.write_text(
        json.dumps({"chat_metadata": {}}) + "\n"
        + json.dumps({"name": "You", "is_user": True, "mes": "hi"}) + "\n",
        encoding="utf-8",
    )
    # Bust the cache.
    with mod._group_last_speaker_lock:
        mod._group_last_speaker_cache.clear()
    speaker = mod.ChatReader.get_group_last_speaker(group_fixture["group_id"])
    assert speaker is None


# ─── load_character_card group-id error envelope ──────────────


def test_load_character_card_group_id_returns_error(mod, group_fixture):
    card = mod.load_character_card(group_fixture["group_id"])
    assert card.get("error") == "group_id_not_a_character"
    assert card.get("group_id") == group_fixture["group_id"]


def test_load_character_card_unknown_id_returns_empty(mod, group_fixture):
    """Non-existent + non-group → preserve historical empty-dict behavior."""
    assert mod.load_character_card("totally-unknown-character") == {}


def test_build_character_context_handles_group_id(mod, group_fixture):
    # Must NOT crash on the error envelope (build_character_context guards).
    assert mod.build_character_context(group_fixture["group_id"]) == ""


# ─── discover_group_characters ────────────────────────────────


def test_discover_group_characters(mod, group_fixture):
    chars = mod.discover_group_characters(group_fixture["group_id"])
    assert [c["id"] for c in chars] == group_fixture["members"]
    assert [c["name"] for c in chars] == group_fixture["members"]


def test_discover_group_characters_unknown_group(mod, group_fixture):
    assert mod.discover_group_characters("nope") == []


# ─── snapshot_state normalization ─────────────────────────────


def test_snapshot_state_solo_normalized(mod, group_fixture):
    """`individual` → `solo` on the normalized chatType field."""
    mod.update_state({
        "chatId": "solo-1",
        "chatType": "individual",
        "characterId": "Vera",
        "personaId": "ayaz",
    })
    snap = mod.snapshot_state()
    assert snap["chatType"] == "solo"
    # Reset for next test.
    mod.update_state({"chatType": "", "characterId": "", "chatId": ""})


def test_snapshot_state_group_includes_payload(mod, group_fixture):
    """`group` → injects groupId/groupMembers/lastSpeaker."""
    gid = group_fixture["group_id"]
    mod.update_state({
        "chatId": "asylum-chat-1",
        "chatType": "group",
        "characterId": gid,  # bridge sometimes posts group_id here
        "personaId": "ayaz",
    })
    snap = mod.snapshot_state()
    assert snap["chatType"] == "group"
    assert snap["groupId"] == gid
    members = snap["groupMembers"]
    assert [m["name"] for m in members] == group_fixture["members"]
    assert snap["lastSpeaker"] == "Hana"
    # Reset.
    mod.update_state({"chatType": "", "characterId": "", "chatId": ""})
