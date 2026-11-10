"""One canonical character key across vocab, mode memory and card lookup.

ST sends the raw avatar filename (`Elara.png`), learn-vocab scopes by the
chat directory (`elara`) and the phone sends a card stem. The server
canonicalises every form to one key, while card lookup still finds the
on-disk PNG regardless of which form arrives.
"""
from __future__ import annotations

import base64
import json
import pathlib

import pytest

from conftest import load_script, load_wrapper

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEARN_VOCAB_SRC = ROOT / "scripts" / "learn-vocab"


@pytest.fixture
def mod(tmp_path, monkeypatch):
    module = load_wrapper("calliope_server_charkey")
    monkeypatch.setattr(module.config, "CHAR_MODES_FILE", tmp_path / "char-modes.yaml")
    monkeypatch.setattr(module.formatter, "_char_modes_cache", {"data": {}, "mtime": 0.0})
    return module


@pytest.fixture(scope="module")
def learn_vocab():
    return load_script(LEARN_VOCAB_SRC, "learn_vocab_charkey")


def _write_card(chars_dir: pathlib.Path, stem: str, name: str) -> pathlib.Path:
    """Minimal synthetic PNG with a chara tEXt chunk."""
    payload = b"chara\x00" + base64.b64encode(
        json.dumps({"data": {"name": name, "description": f"{name} card"}}).encode("utf-8"),
    )
    chunk = len(payload).to_bytes(4, "big") + b"tEXt" + payload + b"\x00\x00\x00\x00"
    path = chars_dir / f"{stem}.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)
    return path


# ─── canonical_character_key ─────────────────────────────────────────


def test_canonical_key_collapses_surface_forms(mod):
    key = mod.canonical_character_key
    assert key("Elara.png") == key("elara") == key(" ELARA ") == "elara"
    assert key("chars/Elara.WEBP") == "elara"
    assert key("C:\\st\\Elara.jpeg") == "elara"
    assert key("Hana   Nakamura.gif") == "hana nakamura"
    assert key("Straße") == "strasse"
    # NFD input normalises to the same key as NFC input.
    assert key("Rene\u0301e.png") == key("Ren\u00e9e") == "ren\u00e9e"
    assert key("") == key(None) == ""
    # Only a trailing image extension is stripped.
    assert key("Dr. Mira") == "dr. mira"


def test_learn_vocab_key_matches_server(mod, learn_vocab):
    for raw in (
        "Elara.png", "elara", " ELARA ", "chars/Elara.WEBP", "Hana   Nakamura",
        "Straße.jpg", "Rene\u0301e", "Dr. Mira", "", "x.png.png",
    ):
        assert learn_vocab.canonical_character_key(raw) == mod.canonical_character_key(raw), raw


# ─── Vocab scope ─────────────────────────────────────────────────────


def _inject_vocab(monkeypatch, mod, entries):
    normalized = [n for n in (mod._normalize_vocab_entry(e) for e in entries) if n]
    monkeypatch.setattr(mod.formatter, "load_vocab", lambda: normalized)


def test_vocab_scoped_to_stem_applies_to_avatar_filename(mod, monkeypatch):
    _inject_vocab(monkeypatch, mod, [
        {"correct": "Kaelthas", "aliases": ["kale thus"], "characters": ["elara"]},
    ])
    assert "Kaelthas" in mod.build_whisper_prompt("Elara.png")
    assert mod.vocab_correct("hello kale thus", character_id="Elara.png") == "hello Kaelthas"
    assert mod.build_whisper_prompt("Mira.png") == ""


def test_vocab_scoped_to_avatar_filename_applies_to_stem(mod, monkeypatch):
    _inject_vocab(monkeypatch, mod, [
        {"correct": "Kaelthas", "aliases": [], "characters": ["Elara.png"]},
    ])
    assert "Kaelthas" in mod.build_whisper_prompt("elara")
    assert mod._normalize_vocab_entry(
        {"correct": "Kaelthas", "characters": ["Elara.png", "elara"]},
    )["characters"] == ["elara"]


def test_learned_scope_applies_on_server(mod, learn_vocab, tmp_path, monkeypatch):
    """learn-vocab's scope key for chats/Elara/ matches ST's `Elara.png`."""
    chats = tmp_path / "chats" / "Elara"
    chats.mkdir(parents=True)
    (chats / "c.jsonl").write_text(
        json.dumps({"chat_metadata": {}}) + "\n"
        + "\n".join(json.dumps({"mes": "Kaelthas waits", "is_user": False}) for _ in range(4))
        + "\n",
        encoding="utf-8",
    )
    vocab: list[dict] = []
    learn_vocab.process_character(
        character="Elara", chats_dir=tmp_path / "chats", vocab=vocab,
        stoplist=set(), min_freq=3, cap=80, apply=True,
        merge_characters=False, proper_only=True,
    )
    assert vocab and vocab[0]["characters"] == ["elara"]
    _inject_vocab(monkeypatch, mod, vocab)
    assert "Kaelthas" in mod.build_whisper_prompt("Elara.png")


# ─── Mode memory ─────────────────────────────────────────────────────


def test_char_mode_saved_under_avatar_reads_back_under_stem(mod):
    mod.save_char_mode("Elara.png", "rp_enhance")
    assert mod.get_char_mode("elara") == "rp_enhance"
    assert mod.get_char_mode(" ELARA ") == "rp_enhance"
    assert mod.load_char_modes() == {"elara": "rp_enhance"}


def test_char_mode_saved_under_stem_reads_back_under_avatar(mod):
    mod.save_char_mode("elara", "persona_pov")
    assert mod.get_char_mode("Elara.png") == "persona_pov"
    mod.save_char_mode("Elara.png", "")
    assert mod.get_char_mode("elara") == ""


def test_legacy_char_modes_file_merges_duplicates_and_writes_back(mod):
    path = mod.config.CHAR_MODES_FILE
    path.write_text(json.dumps({"Elara.png": "rp_format", "elara": "rp_enhance", "Mira": "grammar_clean"}))

    assert mod.load_char_modes() == {"elara": "rp_enhance", "mira": "grammar_clean"}
    assert mod.get_char_mode("Elara.png") == "rp_enhance"

    persisted = path.read_text()
    assert "Elara.png" not in persisted and "Mira" not in persisted
    assert "elara" in persisted and "mira" in persisted
    mtime = path.stat().st_mtime_ns
    mod.formatter._char_modes_cache.update(data={}, mtime=0.0)
    assert mod.load_char_modes() == {"elara": "rp_enhance", "mira": "grammar_clean"}
    assert path.stat().st_mtime_ns == mtime  # already canonical: no second write


def test_mode_memory_endpoints_canonicalise_character(mod):
    sent: dict = {}

    class Handler:
        headers = {"Content-Length": "2"}

        def __init__(self, path, body=None):
            self.path = path
            self._body = body

        def require_auth(self):
            return True

        def _reject_oversized_body(self, limit):
            return False

        def read_json_body(self):
            return self._body

        def send_json(self, data, status=200):
            sent.clear()
            sent.update(status=status, data=data)

        def send_error_json(self, message, **kwargs):
            raise AssertionError((message, kwargs))

    mod.DictationHandler._dispatch_post(
        Handler("/state/mode-memory", {"character": "Elara.png", "mode": "rp_enhance"}),
    )
    assert sent["data"]["character"] == "elara"
    assert sent["data"]["mode"] == "rp_enhance"

    mod.DictationHandler._dispatch_get(Handler("/state/mode-memory?character=elara"))
    assert sent["data"] == {"character": "elara", "mode": "rp_enhance"}


# ─── Card lookup ─────────────────────────────────────────────────────


def test_card_lookup_resolves_avatar_and_canonical_key(mod, tmp_path, monkeypatch):
    chars = tmp_path / "characters"
    chars.mkdir()
    _write_card(chars, "Elara", "Elara Vance")
    _write_card(chars, "Mira", "Mira Sol")
    monkeypatch.setattr(mod.config, "CHARACTERS_DIR", chars)

    for ref in ("Elara.png", "elara", " ELARA ", "Elara"):
        assert mod.load_character_card(ref)["name"] == "Elara Vance", ref
    assert "CHARACTER: Elara Vance" in mod.build_character_context("elara")
    assert mod.load_character_card("nobody") == {}
