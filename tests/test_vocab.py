"""Vocab fuzzy-collision regression tests.

Empirical corpus from Agent 3 §6 / ADR-10. Phase 1 MVP-5 raised
VOCAB_FUZZY_CUTOFF from 0.75 to 0.84, raised VOCAB_FUZZY_MIN_LEN from 3
to 4, gated on COMMON_EN_WORDS, and capped fuzzy hits per utterance.

These tests pin down the regressions so a future loosening of any of the
four gates fails CI loudly instead of silently corrupting dictation.
"""
from __future__ import annotations

import pytest

from conftest import load_wrapper


@pytest.fixture(scope="module")
def mod():
    return load_wrapper("calliope_vocab")


def _inject_vocab(monkeypatch, mod, entries):
    """Replace load_vocab() with a stub returning the given entries."""
    normalized = []
    for e in entries:
        norm = mod._normalize_vocab_entry(e)
        if norm:
            normalized.append(norm)
    monkeypatch.setattr(mod.formatter, "load_vocab", lambda: normalized)


def _names(monkeypatch, mod, *names):
    _inject_vocab(monkeypatch, mod, [{"correct": n, "aliases": []} for n in names])


# ─── Empirical regression corpus (Agent 3 §6) ────────────────────────
# Each case loads the colliding name, proves the fuzzy pass is live with a
# positive control in the same utterance, and proves the named gate is what
# protects the trap token by loosening that gate alone.


def test_yaz_not_rewritten_to_ayaz(mod, monkeypatch):
    """'yaz' vs 'Ayaz' ratio 0.857 clears the cutoff; only the length gate saves it."""
    _names(monkeypatch, mod, "Ayaz")
    assert mod.vocab_correct("she said yaz to Ayazz", character_id="") == \
        "she said yaz to Ayaz"

    monkeypatch.setattr(mod.formatter.config, "VOCAB_FUZZY_MIN_LEN", 3)
    assert mod.vocab_correct("she said yaz", character_id="") == "she said Ayaz"


def test_yari_not_rewritten_to_yuri(mod, monkeypatch):
    """'Yari' vs 'Yuri' ratio 0.750 sits below the 0.84 cutoff (was 0.75)."""
    _names(monkeypatch, mod, "Yuri")
    assert mod.vocab_correct("Yari ran to Yurii", character_id="") == "Yari ran to Yuri"

    monkeypatch.setattr(mod.formatter.config, "VOCAB_FUZZY_CUTOFF", 0.75)
    assert mod.vocab_correct("Yari ran", character_id="") == "Yuri ran"


def test_hane_not_rewritten_to_hana(mod, monkeypatch):
    """'Hane' must not collapse to 'Hana'. Same ratio = 0.750 trap as above."""
    _names(monkeypatch, mod, "Hana")
    assert mod.vocab_correct("she called Hane and Hanaa", character_id="") == \
        "she called Hane and Hana"

    monkeypatch.setattr(mod.formatter.config, "VOCAB_FUZZY_CUTOFF", 0.75)
    assert mod.vocab_correct("she called Hane", character_id="") == "she called Hana"


# ─── Common-word frequency gate (Agent 3 §6 third gate) ──────────────


@pytest.mark.parametrize("word", ["Wear", "Near", "House", "Here", "Your"])
def test_common_word_gate_skips_top_5k(mod, monkeypatch, word):
    """A name one letter off a common word (ratio ≥ 0.88) must not eat the word."""
    name = word + "a"
    _names(monkeypatch, mod, name)
    assert word.lower() in mod.formatter.config.COMMON_EN_WORDS
    assert mod.vocab_correct(f"{word} and {name}h", character_id="") == f"{word} and {name}"

    monkeypatch.setattr(mod.formatter.config, "COMMON_EN_WORDS", frozenset())
    assert mod.vocab_correct(f"{word} broke", character_id="") == f"{name} broke"


# ─── Pass 1 — exact-alias replacement still works ────────────────────


def test_alias_replacement_works(mod, monkeypatch):
    """An explicit alias must still be replaced regardless of fuzzy gates.

    Pass 1 (exact word-boundary alias substitution) runs before Pass 2
    (fuzzy), so this should land even though 'ayaz' is a 4-char common-ish
    token that Pass 2 alone might skip.
    """
    _inject_vocab(monkeypatch, mod, [
        {"correct": "Ayaz", "aliases": ["ayaz", "ayaaz"]},
    ])
    result = mod.vocab_correct("hello ayaz how are you", character_id="")
    assert result == "hello Ayaz how are you"


def test_alias_case_insensitive(mod, monkeypatch):
    """Aliases match case-insensitively but emit the canonical 'correct' form."""
    _inject_vocab(monkeypatch, mod, [
        {"correct": "Ayaz", "aliases": ["AYAZ"]},
    ])
    assert mod.vocab_correct("said ayaz again", character_id="") == "said Ayaz again"


# ─── Fuzzy hit cap (Agent 3 §6 fourth gate) ──────────────────────────


def test_fuzzy_hits_capped_per_utterance(mod, monkeypatch):
    """Exactly VOCAB_FUZZY_MAX_HITS_PER_UTT (2) earliest near-misses are rewritten.

    Every token below clears the cutoff on its own (ratio 0.933); the third
    survives only because of the cap.
    """
    _names(monkeypatch, mod, "Calliope")
    assert mod.formatter.config.VOCAB_FUZZY_MAX_HITS_PER_UTT == 2
    result = mod.vocab_correct("Caliope Caliope Caliope walked", character_id="")
    assert result == "Calliope Calliope Caliope walked"

    assert mod.vocab_correct("Caliope walked", character_id="") == "Calliope walked"


# ─── Empty / no-op paths ─────────────────────────────────────────────


def test_empty_text_returns_empty(mod):
    assert mod.vocab_correct("", character_id="") == ""


def test_no_vocab_entries_passthrough(mod, monkeypatch):
    monkeypatch.setattr(mod.formatter, "load_vocab", lambda: [])
    text = "yaz Yari Hane wear near"
    assert mod.vocab_correct(text, character_id="") == text
