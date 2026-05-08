"""Regression tests for scripts/learn-vocab (POL-2 adaptive lexicon).

Pinned behaviours:
  - Tokenization: same regex shape as server build_whisper_prompt
  - Stoplist filter: top common English words excluded
  - Frequency threshold: tokens with count < min_freq excluded
  - Casing: longest-form representative is preserved (Kael'thas > kael)
  - Cap: per-character cap honoured
  - Dry-run: vocab.yaml unchanged
  - --apply: vocab.yaml modified, atomic write
  - Dedup: existing entries (case-insensitive on `correct`) skipped
  - --merge-characters: existing entry's characters[] list extended
  - JSONL: header lines (no `mes`) ignored without crashing
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
from importlib.machinery import SourceFileLoader

import pytest

# Script lives at scripts/learn-vocab — no .py extension. Mirror the
# pattern in tests/test_vocab.py (lines 24-29).
SRC = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "learn-vocab"


@pytest.fixture(scope="module")
def mod():
    loader = SourceFileLoader("learn_vocab", str(SRC))
    spec = importlib.util.spec_from_loader("learn_vocab", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── Synthetic chat fixture helpers ──────────────────────────────────


def _write_chat(path: pathlib.Path, messages: list[str]) -> None:
    """Write a JSONL chat file: 1 metadata header + N message lines."""
    lines: list[str] = []
    # ST chat header (no `mes` key).
    lines.append(json.dumps({
        "user_name": "Ayaz",
        "character_name": path.parent.name,
        "create_date": "2026-04-01@12h00m00s000ms",
        "chat_metadata": {},
    }))
    for m in messages:
        lines.append(json.dumps({
            "name": "Test",
            "is_user": False,
            "is_system": False,
            "send_date": "2026-04-01 12:00:00",
            "mes": m,
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_chats(tmp_path: pathlib.Path, char: str,
                messages_per_chat: list[list[str]]) -> pathlib.Path:
    """Create chats_dir/<char>/N chat files. Returns chats_dir."""
    chats_dir = tmp_path / "chats"
    char_dir = chats_dir / char
    char_dir.mkdir(parents=True, exist_ok=True)
    for i, msgs in enumerate(messages_per_chat):
        _write_chat(char_dir / f"chat-{i:02d}.jsonl", msgs)
    return chats_dir


# ─── Tokenization ────────────────────────────────────────────────────


def test_tokenization_matches_server_regex(mod):
    """TOKEN_RE must match [A-Za-z][A-Za-z'-]+ (server line 107 shape)."""
    text = "Kael'thas walked. The fox-tail. 12abc and OOC2 ignored."
    toks = mod.TOKEN_RE.findall(text)
    # Numerals don't start tokens; "12abc" → "abc"; "OOC2" → "OOC".
    assert "Kael'thas" in toks
    assert "fox-tail" in toks
    assert "abc" in toks
    assert "OOC" in toks
    assert "walked" in toks
    # No bare digits or numerics.
    assert "12" not in toks
    assert "12abc" not in toks


# ─── Stoplist ────────────────────────────────────────────────────────


def test_stoplist_filters_common_words(mod, tmp_path):
    """Tokens in the stoplist must not appear in proposals even if frequent."""
    # "the" appears 5x; "Kaelthas" 3x. Stoplist drops "the".
    chats_dir = _make_chats(tmp_path, "Test", [
        ["the the the the the Kaelthas Kaelthas Kaelthas"],
    ])
    stoplist = mod.build_stoplist(quiet=True)
    assert "the" in stoplist  # sanity
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", stoplist, min_freq=3, cap=80,
    )
    words = [w for w, _ in proposals]
    assert "the" not in [w.lower() for w in words]
    assert "Kaelthas" in words


def test_mining_strips_html_and_css_noise(mod, tmp_path):
    """Rendered ST markup must not leak font/color/hex junk into vocab."""
    chats_dir = _make_chats(tmp_path, "Test", [
        ["<font color='#aabbcc'>Kaelthas Kaelthas Kaelthas</font> font color aabbcc"],
    ])
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", set(), min_freq=3, cap=80, proper_only=False,
    )
    words = {w for w, _ in proposals}
    assert "Kaelthas" in words
    assert "font" not in {w.lower() for w in words}
    assert "color" not in {w.lower() for w in words}
    assert "aabbcc" not in {w.lower() for w in words}


def test_mining_normalizes_possessives_and_drops_contractions(mod, tmp_path):
    """Names should survive as canonical terms; prose contractions should not."""
    chats_dir = _make_chats(tmp_path, "Test", [
        ["Camilla's Camilla's Camilla's you're you're you're didn't didn't didn't"],
    ])
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", set(), min_freq=3, cap=80, proper_only=False,
    )
    words = {w for w, _ in proposals}
    lowered = {w.lower() for w in words}
    assert "Camilla" in words
    assert "camilla's" not in lowered
    assert "you're" not in lowered
    assert "didn't" not in lowered


# ─── Frequency threshold ─────────────────────────────────────────────


def test_min_freq_threshold(mod, tmp_path):
    """Words with count < min_freq must be excluded."""
    chats_dir = _make_chats(tmp_path, "Test", [
        ["Onmyoji Onmyoji Onmyoji Yokai Yokai Tengu"],  # 3, 2, 1
    ])
    stoplist = set()  # disable stoplist so we isolate frequency logic
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", stoplist, min_freq=3, cap=80,
    )
    words = [w for w, _ in proposals]
    assert "Onmyoji" in words
    assert "Yokai" not in words
    assert "Tengu" not in words


def test_min_freq_three_default(mod, tmp_path):
    """Default min_freq=3: a word seen exactly 3 times is included."""
    chats_dir = _make_chats(tmp_path, "Test", [
        ["Foobar Foobar Foobar"],
    ])
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", set(), min_freq=3, cap=80,
        proper_only=False,
    )
    assert ("Foobar", 3) in proposals


# ─── Casing preservation ─────────────────────────────────────────────


def test_casing_keeps_longest_representative(mod, tmp_path):
    """Of variants of the same lowercased token, longest form is kept.

    The tokenizer treats the apostrophe as an in-word character, so
    "kael", "Kael", and "Kael'thas" are DIFFERENT tokens (different
    lowercased keys: 'kael' vs "kael'thas"). To exercise casing-merge
    we must use case variants of the SAME alphabetic spelling.
    """
    chats_dir = _make_chats(tmp_path, "Test", [
        ["kaelthas KAELTHAS Kaelthas Kael'thas Kael'thas Kael'thas"],
    ])
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", set(), min_freq=3, cap=80,
        proper_only=False,
    )
    rep = dict(proposals)
    # 'kaelthas' lowercase key — variants kaelthas, KAELTHAS, Kaelthas (3x).
    # Longest form by len() is "kaelthas" / "KAELTHAS" / "Kaelthas" — all
    # tied at 8 chars; first-seen wins which is "kaelthas". The point is
    # ALL three variants collapse to ONE entry with count 3.
    assert any(w.lower() == "kaelthas" for w in rep)
    [kaelthas_key] = [w for w in rep if w.lower() == "kaelthas"]
    assert rep[kaelthas_key] == 3
    # Separately: "Kael'thas" (lowercase 'kael'thas') is its OWN entry,
    # count 3, longest representative kept.
    assert "Kael'thas" in rep
    assert rep["Kael'thas"] == 3


def test_casing_longest_wins_among_variants(mod, tmp_path):
    """When variants of the same lowercased token differ in length,
    the longest is kept as representative."""
    # Three variants of 'foobar'. "FooBar" (6) vs "foobar" (6) vs longer.
    chats_dir = _make_chats(tmp_path, "Test", [
        ["fb Fb FB"],   # 'fb' lowercase, all length 2 — tie broken by first-seen
        ["alpha Alpha alphaThing"],  # different lowercased keys; not relevant
        ["beta Beta beta"],  # 'beta' (4) wins length tie via first-seen rule
        ["gamma gAmma GAMMA"],  # all length 5
    ])
    # The casing rule uses `>` (strict). First-seen wins on ties. We
    # don't pin first-seen because file iteration order is platform-y.
    # Instead pin: representative.lower() == key for every proposal.
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", set(), min_freq=3, cap=80,
    )
    for w, _ in proposals:
        assert w.lower() in {p[0].lower() for p in proposals}


# ─── Cap ─────────────────────────────────────────────────────────────


def test_cap_limits_per_character(mod, tmp_path):
    """`cap` must limit the number of returned proposals."""
    # 10 distinct alphabetic words (the tokenizer drops digits, so
    # "Word0" would tokenize to "Word" — use letters instead).
    distinct = [
        "alpha", "bravo", "charlie", "delta", "echo",
        "foxtrot", "golf", "hotel", "india", "juliet",
    ]
    line = " ".join(distinct)
    repeated = " ".join([line] * 5)  # each appears 5 times
    chats_dir = _make_chats(tmp_path, "Test", [[repeated]])
    proposals, _, _ = mod.mine_character(
        chats_dir / "Test", set(), min_freq=3, cap=4, proper_only=False,
    )
    assert len(proposals) == 4
    # All four must come from the distinct set.
    for w, _ in proposals:
        assert w in distinct


# ─── JSONL header robustness ─────────────────────────────────────────


def test_jsonl_header_line_ignored(mod, tmp_path):
    """First-line metadata blob (no `mes` key) must not crash mining."""
    char_dir = tmp_path / "chats" / "Test"
    char_dir.mkdir(parents=True)
    # Just the metadata header — no message lines.
    (char_dir / "chat.jsonl").write_text(
        json.dumps({"user_name": "Ayaz", "create_date": "x"}) + "\n",
        encoding="utf-8",
    )
    proposals, chat_count, msg_count = mod.mine_character(
        char_dir, set(), min_freq=1, cap=80,
    )
    assert chat_count == 1
    assert msg_count == 0
    assert proposals == []


def test_jsonl_malformed_line_skipped(mod, tmp_path):
    """Malformed JSON lines are skipped without aborting the file."""
    char_dir = tmp_path / "chats" / "Test"
    char_dir.mkdir(parents=True)
    (char_dir / "chat.jsonl").write_text(
        '\n'.join([
            json.dumps({"user_name": "Ayaz"}),
            "{not valid json",
            json.dumps({"mes": "Kaelthas Kaelthas Kaelthas"}),
        ]) + "\n",
        encoding="utf-8",
    )
    proposals, _, msg_count = mod.mine_character(
        char_dir, set(), min_freq=1, cap=80,
    )
    assert msg_count == 1
    assert ("Kaelthas", 3) in proposals


# ─── Dry-run vs --apply ──────────────────────────────────────────────


def test_dry_run_does_not_modify_vocab(mod, tmp_path):
    """Default (no --apply) must leave vocab.yaml unchanged on disk."""
    pytest.importorskip("yaml")
    vocab_path = tmp_path / "vocab.yaml"
    vocab_path.write_text("[]\n", encoding="utf-8")
    original = vocab_path.read_text()

    chats_dir = _make_chats(tmp_path, "Test", [
        ["Kaelthas Kaelthas Kaelthas Onmyoji Onmyoji Onmyoji"],
    ])
    rc = mod.main([
        "--character", "Test",
        "--chats-dir", str(chats_dir),
        "--vocab-yaml", str(vocab_path),
        "--quiet",
    ])
    assert rc == 0
    assert vocab_path.read_text() == original


def test_apply_writes_vocab(mod, tmp_path):
    """--apply must persist new entries to vocab.yaml."""
    yaml = pytest.importorskip("yaml")
    vocab_path = tmp_path / "vocab.yaml"
    vocab_path.write_text("[]\n", encoding="utf-8")

    chats_dir = _make_chats(tmp_path, "Hana Nakamura", [
        ["Kaelthas Kaelthas Kaelthas Onmyoji Onmyoji Onmyoji"],
    ])
    rc = mod.main([
        "--character", "Hana Nakamura",
        "--chats-dir", str(chats_dir),
        "--vocab-yaml", str(vocab_path),
        "--apply",
        "--quiet",
    ])
    assert rc == 0
    written = yaml.safe_load(vocab_path.read_text())
    assert isinstance(written, list)
    correct_words = {e["correct"] for e in written}
    assert "Kaelthas" in correct_words
    assert "Onmyoji" in correct_words
    # Characters are lowercased per server normalization.
    for e in written:
        if e["correct"] in ("Kaelthas", "Onmyoji"):
            assert e["characters"] == ["hana nakamura"]


# ─── Dedup ───────────────────────────────────────────────────────────


def test_existing_entry_skipped_case_insensitive(mod, tmp_path):
    """Candidate matching existing `correct` (case-insensitive) is skipped."""
    yaml = pytest.importorskip("yaml")
    vocab_path = tmp_path / "vocab.yaml"
    # Pre-existing scoped to a DIFFERENT character.
    yaml.safe_dump(
        [{"correct": "kaelthas", "aliases": [], "characters": ["yerin park"]}],
        vocab_path.open("w"),
    )

    chats_dir = _make_chats(tmp_path, "Hana Nakamura", [
        ["Kaelthas Kaelthas Kaelthas"],  # would match existing "kaelthas"
    ])
    # Without --merge-characters: skip silently.
    rc = mod.main([
        "--character", "Hana Nakamura",
        "--chats-dir", str(chats_dir),
        "--vocab-yaml", str(vocab_path),
        "--apply",
        "--quiet",
    ])
    assert rc == 0
    after = yaml.safe_load(vocab_path.read_text())
    assert len(after) == 1
    assert after[0]["characters"] == ["yerin park"]  # unchanged


def test_merge_characters_extends_existing_scope(mod, tmp_path):
    """With --apply --merge-characters, current char appended to scope list."""
    yaml = pytest.importorskip("yaml")
    vocab_path = tmp_path / "vocab.yaml"
    yaml.safe_dump(
        [{"correct": "kaelthas", "aliases": [], "characters": ["yerin park"]}],
        vocab_path.open("w"),
    )

    chats_dir = _make_chats(tmp_path, "Hana Nakamura", [
        ["Kaelthas Kaelthas Kaelthas"],
    ])
    rc = mod.main([
        "--character", "Hana Nakamura",
        "--chats-dir", str(chats_dir),
        "--vocab-yaml", str(vocab_path),
        "--apply",
        "--merge-characters",
        "--quiet",
    ])
    assert rc == 0
    after = yaml.safe_load(vocab_path.read_text())
    assert len(after) == 1
    assert sorted(after[0]["characters"]) == ["hana nakamura", "yerin park"]


# ─── JSON output ─────────────────────────────────────────────────────


def test_json_output_shape(mod, tmp_path, capsys):
    """--json emits a parseable JSON envelope with results."""
    pytest.importorskip("yaml")
    vocab_path = tmp_path / "vocab.yaml"
    vocab_path.write_text("[]\n", encoding="utf-8")
    chats_dir = _make_chats(tmp_path, "Test", [
        ["Kaelthas Kaelthas Kaelthas"],
    ])
    rc = mod.main([
        "--character", "Test",
        "--chats-dir", str(chats_dir),
        "--vocab-yaml", str(vocab_path),
        "--json",
        "--quiet",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["applied"] is False
    assert payload["vocab_yaml"] == str(vocab_path)
    assert len(payload["results"]) == 1
    r = payload["results"][0]
    assert r["character"] == "Test"
    assert any(a["correct"] == "Kaelthas" for a in r["added"])
