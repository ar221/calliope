"""POL-6 — group-chat addressee picker server-side helpers.

Covers:
  - ChatReader.find_group_by_id / get_group_id_set / group_member_objects
  - ChatReader.get_group_last_speaker (cached by mtime)
  - load_character_card returns explicit error envelope on group-id input
  - discover_group_characters maps group members → chip-friendly shape
  - snapshot_state normalizes chatType + injects group payload
"""
from __future__ import annotations

import base64
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
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_synthetic_character_card(
    chars_dir: pathlib.Path, stem: str, name: str,
) -> None:
    """Write a minimal synthetic PNG tEXt card; no real ST card data."""
    payload = b"chara\x00" + base64.b64encode(
        json.dumps({"data": {"name": name}}).encode("utf-8"),
    )
    chunk = (
        len(payload).to_bytes(4, "big")
        + b"tEXt" + payload
        + b"\x00\x00\x00\x00"
    )
    (chars_dir / f"{stem}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)


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

    monkeypatch.setattr(mod.config, "ST_GROUPS_DIR", groups_dir)
    monkeypatch.setattr(mod.config, "ST_GROUP_CHATS_DIR", group_chats_dir)
    monkeypatch.setattr(mod.config, "CHARACTERS_DIR", chars_dir)
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


def test_group_members_resolve_avatar_filenames_to_character_names(mod, group_fixture):
    """Avatar filenames map through synthetic character cards to display names."""
    for stem, display_name in (
        ("Hana", "Hana Mori"),
        ("Yuri", "Yuri Vale"),
        ("Suzy", "Suzy Park"),
    ):
        write_synthetic_character_card(group_fixture["chars_dir"], stem, display_name)

    assert mod.ChatReader.get_group_members(group_fixture["group_name"]) == [
        "Hana Mori", "Yuri Vale", "Suzy Park",
    ]
    members = mod.ChatReader.group_member_objects(group_fixture["group_id"])
    assert [m["name"] for m in members] == ["Hana Mori", "Yuri Vale", "Suzy Park"]
    assert [m["avatar"] for m in members] == ["Hana.png", "Yuri.png", "Suzy.png"]


def test_group_member_missing_avatar_falls_back_to_filename_stem(mod, group_fixture):
    """Deleted/missing character avatars stay selectable by stem instead of crashing."""
    assert mod.ChatReader.get_group_members(group_fixture["group_name"]) == [
        "Hana", "Yuri", "Suzy",
    ]
    members = mod.ChatReader.group_member_objects(group_fixture["group_id"])
    assert [m["name"] for m in members] == ["Hana", "Yuri", "Suzy"]


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


def test_last_speaker_none_when_no_ai_messages(mod, group_fixture):
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


def test_last_speaker_skips_later_user_and_system_messages(mod, group_fixture):
    """Latest non-user/non-system message wins even when newer noise exists."""
    chat_file = group_fixture["group_chats_dir"] / "asylum-chat-1.jsonl"
    chat_file.write_text(
        "\n".join([
            json.dumps({"chat_metadata": {}}),
            json.dumps({
                "name": "Yuri", "is_user": False,
                "mes": "synthetic character line",
            }),
            json.dumps({
                "name": "You", "is_user": True,
                "mes": "synthetic user line",
            }),
            json.dumps({
                "name": "System", "is_user": False, "is_system": True,
                "mes": "synthetic system note",
            }),
        ]) + "\n",
        encoding="utf-8",
    )
    with mod._group_last_speaker_lock:
        mod._group_last_speaker_cache.clear()

    assert mod.ChatReader.get_group_last_speaker(group_fixture["group_id"]) == "Yuri"


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
    mod.update_state({
        "chatType": "", "characterId": "", "characterName": "", "chatId": "",
    })


def test_all_members_addressee_round_trips(mod, group_fixture):
    """`*all` is a joint-context addressee, not an empty/solo-character fallback."""
    gid = group_fixture["group_id"]
    mod.update_state({
        "chatId": "asylum-chat-1",
        "chatType": "group",
        "characterId": gid,
        "characterName": "*all",
        "personaId": "synthetic-persona",
    })
    snap = mod.snapshot_state()
    contract = mod.build_scene_contract(snap)

    assert snap["chatType"] == "group"
    assert snap["characterName"] == "*all"
    assert contract["chat_type"] == "group"
    assert contract["addressee"] == "*all"
    assert "group: *all" in contract["facts"]
    assert "group: *all" in contract["prompt"]
    mod.update_state({
        "chatType": "", "characterId": "", "characterName": "", "chatId": "",
    })


def test_scene_contract_preserves_group_identity_not_solo_character(mod, group_fixture):
    contract = mod.build_scene_contract({
        "chatType": "group",
        "characterId": group_fixture["group_id"],
        "characterName": group_fixture["group_name"],
        "personaId": "synthetic-persona",
        "groupMembers": [
            {"name": "Hana", "avatar": "Hana.png"},
            {"name": "Yuri", "avatar": "Yuri.png"},
            {"name": "Suzy", "avatar": "Suzy.png"},
        ],
        "lastSpeaker": "Yuri",
    })

    assert contract["chat_type"] == "group"
    assert contract["character_id"] == group_fixture["group_id"]
    assert contract["addressee"] == group_fixture["group_name"]
    assert "group: Asylum Crew" in contract["facts"]
    assert "group_members: Hana, Yuri, Suzy" in contract["facts"]
    expected_rule = (
        "In group chat, do not assume the last speaker is the addressee "
        "unless dictated."
    )
    assert expected_rule in contract["rules"]
    assert "chat_type: solo" not in contract["prompt"]
    assert "addressee: Hana" not in contract["prompt"]
