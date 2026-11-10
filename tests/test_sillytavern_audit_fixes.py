"""Behavioural coverage for the 2026-11-10 SillyTavern reader audit fixes.

All ST data is synthetic and lives under tmp_path.
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import pathlib
import sys

import pytest

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _png_card(path: pathlib.Path, card: object) -> None:
    payload = b"chara\x00" + base64.b64encode(json.dumps(card).encode())
    chunk = len(payload).to_bytes(4, "big") + b"tEXt" + payload + b"\x00\x00\x00\x00"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)


def _jsonl(path: pathlib.Path, *records: object) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


@pytest.fixture
def st(monkeypatch, tmp_path):
    module = importlib.import_module("calliope_server.sillytavern")
    root = tmp_path / "st"
    for name in ("CHARACTERS_DIR", "ST_CHATS_DIR", "ST_GROUPS_DIR", "ST_GROUP_CHATS_DIR", "PERSONAS_DIR"):
        d = root / name.lower()
        d.mkdir(parents=True)
        monkeypatch.setattr(module.config, name, d)
    monkeypatch.setattr(module.config, "ST_SETTINGS_FILE", root / "settings.json")
    module._parse_cache.clear()
    yield module
    module._parse_cache.clear()


def _cfg(st, name):
    return getattr(st.config, name)


# ─── contract 4: read_state_chat ────────────────────────────────────

def test_read_state_chat_solo_reads_exact_chat_not_newest(st):
    chat_dir = _cfg(st, "ST_CHATS_DIR") / "FileStem"
    chat_dir.mkdir()
    _jsonl(chat_dir / "Chat A.jsonl", {"chat_metadata": {}}, {"name": "Elara", "mes": "SELECTED", "is_user": False})
    _jsonl(chat_dir / "Chat B.jsonl", {"name": "Elara", "mes": "NEWER_OTHER", "is_user": False})
    os.utime(chat_dir / "Chat B.jsonl", (2_000_000_000, 2_000_000_000))
    state = {"chatId": "Chat A", "chatType": "solo", "characterId": "FileStem.png", "characterName": "Elara"}
    messages, is_group = st.ChatReader.read_state_chat(state, 8)
    assert is_group is False
    assert [m["text"] for m in messages] == ["SELECTED"]
    # characterName works as the directory fallback, `individual` is accepted.
    by_name = _cfg(st, "ST_CHATS_DIR") / "Mira"
    by_name.mkdir()
    _jsonl(by_name / "c1.jsonl", {"name": "Mira", "mes": "hi"})
    got = st.ChatReader.read_state_chat({"chatId": "c1", "chatType": "individual", "characterName": "Mira"}, 8)
    assert got is not None and got[0][0]["text"] == "hi"


def test_read_state_chat_group_validates_membership(st):
    (_cfg(st, "ST_GROUPS_DIR") / "g.json").write_text(json.dumps(
        {"id": "g1", "name": "Party", "members": ["A.png"], "chat_id": "gc-2", "chats": ["gc-1", "gc-2"]}
    ))
    _jsonl(_cfg(st, "ST_GROUP_CHATS_DIR") / "gc-1.jsonl", {"name": "A", "mes": "older group chat"})
    _jsonl(_cfg(st, "ST_GROUP_CHATS_DIR") / "stranger.jsonl", {"name": "A", "mes": "other group"})
    messages, is_group = st.ChatReader.read_state_chat(
        {"chatId": "gc-1", "chatType": "group", "characterId": "g1"}, 8)
    assert is_group is True and messages[0]["text"] == "older group chat"
    assert st.ChatReader.read_state_chat({"chatId": "stranger", "chatType": "group", "groupId": "g1"}, 8) is None


@pytest.mark.parametrize("state", [
    {},
    {"chatId": "", "chatType": "solo", "characterId": "X.png"},
    {"chatId": "../../etc/passwd", "chatType": "solo", "characterId": "X.png"},
    {"chatId": "c", "chatType": "solo", "characterId": "../outside"},
    {"chatId": "missing", "chatType": "solo", "characterId": "X.png"},
    {"chatId": "c", "chatType": "weird", "characterId": "X.png"},
    {"chatId": ["c"], "chatType": "group"},
])
def test_read_state_chat_unresolvable_returns_none(st, state):
    (_cfg(st, "ST_CHATS_DIR") / "X").mkdir()
    assert st.ChatReader.read_state_chat(state, 8) is None


def test_read_state_chat_rejects_symlink_escape(st, tmp_path):
    outside = tmp_path / "outside.jsonl"
    _jsonl(outside, {"name": "x", "mes": "LEAK"})
    chat_dir = _cfg(st, "ST_CHATS_DIR") / "X"
    chat_dir.mkdir()
    (chat_dir / "c.jsonl").symlink_to(outside)
    assert st.ChatReader.read_state_chat({"chatId": "c", "chatType": "solo", "characterId": "X.png"}, 8) is None
    assert st.ChatReader.read_chat_messages("X", "individual") == []


# ─── FS-05 / F25: malformed local data ──────────────────────────────

def test_malformed_chat_lines_are_skipped(st):
    chat_dir = _cfg(st, "ST_CHATS_DIR") / "X"
    chat_dir.mkdir()
    (chat_dir / "c.jsonl").write_text(
        '["list"]\n42\n"str"\n{"name": "X", "mes": null}\n{"name": 7, "mes": "ok"}\n', encoding="utf-8")
    messages = st.ChatReader.read_chat_messages("X", "individual")
    assert [(m["name"], m["text"]) for m in messages] == [("X", ""), ("Unknown", "ok")]


def test_newest_chat_ignores_unstatable_entries(st):
    chat_dir = _cfg(st, "ST_CHATS_DIR") / "X"
    chat_dir.mkdir()
    (chat_dir / "dangling.jsonl").symlink_to(chat_dir / "nowhere.jsonl")
    _jsonl(chat_dir / "real.jsonl", {"name": "X", "mes": "real"})
    assert st.ChatReader._find_newest_jsonl(chat_dir).name == "real.jsonl"


@pytest.mark.parametrize("card", [[1, 2], {"data": [1, 2]}, "just a string"])
def test_malformed_card_roots_load_as_empty(st, card):
    _png_card(_cfg(st, "CHARACTERS_DIR") / "Bad.png", card)
    assert st.load_character_card("Bad") == {}
    assert st.build_character_context("Bad") == ""


def test_non_string_card_fields_are_dropped(st):
    _png_card(_cfg(st, "CHARACTERS_DIR") / "Odd.png", {"data": {"name": "Odd", "description": ["x"], "scenario": "S"}})
    assert st.build_character_context("Odd") == "CHARACTER: Odd\n\nScenario: S"


def test_malformed_group_defs_are_skipped(st):
    groups = _cfg(st, "ST_GROUPS_DIR")
    (groups / "a.json").write_text("[1, 2]")
    (groups / "b.json").write_text('"nope"')
    (groups / "c.json").write_text(json.dumps({"id": 5, "name": ["x"], "members": ["A.png", 3, None]}))
    defs = st.ChatReader._load_group_defs()
    assert len(defs) == 1
    assert defs[0]["id"] == "5" and defs[0]["name"] == "" and defs[0]["members"] == ["A.png"]
    assert st.ChatReader.group_member_objects("5") == [{"name": "A", "avatar": "A.png"}]


def test_malformed_settings_root_yields_no_personas(st):
    st.config.ST_SETTINGS_FILE.write_text("[]")
    assert st.discover_personas() == []


# ─── FS-03: group member identity ───────────────────────────────────

def test_group_member_id_is_avatar_and_name_is_label(st):
    _png_card(_cfg(st, "CHARACTERS_DIR") / "FileStem.png", {"data": {"name": "Display Name", "description": "D"}})
    (_cfg(st, "ST_GROUPS_DIR") / "g.json").write_text(json.dumps({"id": "g", "members": ["FileStem.png"]}))
    chars = st.discover_group_characters("g")
    assert chars == [{"id": "FileStem.png", "name": "Display Name"}]
    # The id resolves the card; the display name does not.
    assert st.load_character_card(chars[0]["id"])["name"] == "Display Name"
    assert st.load_character_card(chars[0]["name"]) == {}


# ─── O-03/O-05: parsed-file caching ─────────────────────────────────

def test_card_parsed_once_until_file_changes(st, monkeypatch):
    path = _cfg(st, "CHARACTERS_DIR") / "Elara.png"
    _png_card(path, {"data": {"name": "Elara"}})
    parses = {"n": 0}
    real = st._parse_character_png

    def counting(p):
        parses["n"] += 1
        return real(p)

    monkeypatch.setattr(st, "_parse_character_png", counting)
    for _ in range(3):
        assert st.load_character_card("Elara")["name"] == "Elara"
    assert parses["n"] == 1
    _png_card(path, {"data": {"name": "Elara Renamed"}})
    assert st.load_character_card("Elara")["name"] == "Elara Renamed"
    assert parses["n"] == 2


def test_parse_cache_is_bounded(st, monkeypatch):
    monkeypatch.setattr(st, "_PARSE_CACHE_MAX", 4)
    for i in range(10):
        _png_card(_cfg(st, "CHARACTERS_DIR") / f"C{i}.png", {"data": {"name": f"C{i}"}})
        st.load_character_card(f"C{i}")
    assert len(st._parse_cache) <= 4
