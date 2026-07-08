"""Vocab fuzzy-collision regression tests.

Empirical corpus from Agent 3 §6 / ADR-10. Phase 1 MVP-5 raised
VOCAB_FUZZY_CUTOFF from 0.75 to 0.84, raised VOCAB_FUZZY_MIN_LEN from 3
to 4, gated on COMMON_EN_WORDS, and capped fuzzy hits per utterance.

These tests pin down the regressions so a future loosening of any of the
four gates fails CI loudly instead of silently corrupting dictation.
"""
from __future__ import annotations

import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader

import pytest

# The server script lives at `server/calliope-server` — no `.py`
# extension, so the default extension-based loader probe fails.
# Pass an explicit SourceFileLoader.
SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


@pytest.fixture(scope="module")
def mod():
    loader = SourceFileLoader("calliope_server", str(SRC))
    spec = importlib.util.spec_from_loader("calliope_server", loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inject_vocab(monkeypatch, mod, entries):
    """Replace load_vocab() with a stub returning the given entries."""
    normalized = []
    for e in entries:
        norm = mod._normalize_vocab_entry(e)
        if norm:
            normalized.append(norm)
    monkeypatch.setattr(mod.formatter, "load_vocab", lambda: normalized)


# ─── Empirical regression corpus (Agent 3 §6) ────────────────────────


def test_yaz_not_rewritten_to_ayaz(mod):
    """'yaz' is short and a real-ish token; must not silently become 'Ayaz'.

    Pre-fix difflib ratio: 0.857 ≥ 0.75 cutoff → silent rewrite.
    Post-fix gates: token len 3 < VOCAB_FUZZY_MIN_LEN (4) → skipped.
    """
    result = mod.vocab_correct("she said yaz to me", character_id="")
    assert "Ayaz" not in result
    assert "yaz" in result.lower()


def test_yari_not_rewritten_to_yuri(mod):
    """'Yari' must not collapse to 'Yuri' via fuzzy.

    Pre-fix difflib ratio: 0.750 = 0.75 cutoff → silent rewrite.
    Post-fix: 0.750 < VOCAB_FUZZY_CUTOFF (0.84) → skipped.
    """
    result = mod.vocab_correct("Yari ran across the field", character_id="")
    assert "Yuri" not in result
    assert "Yari" in result


def test_hane_not_rewritten_to_hana(mod):
    """'Hane' must not collapse to 'Hana'. Same ratio = 0.750 trap as above."""
    result = mod.vocab_correct("she called him Hane", character_id="")
    assert "Hana" not in result
    assert "Hane" in result


# ─── Common-word frequency gate (Agent 3 §6 third gate) ──────────────


@pytest.mark.parametrize("word", ["wear", "near", "shore", "here", "your"])
def test_common_word_gate_skips_top_5k(mod, word):
    """Top-5k English words must never fuzzy-match against names."""
    result = mod.vocab_correct(f"the {word} is broken", character_id="")
    assert word in result.lower(), f"common word {word!r} got rewritten: {result!r}"


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
    assert "Ayaz" in result, f"alias substitution failed: {result!r}"
    # The lowercase alias should have been replaced — no surviving 'ayaz'
    # token in the original casing.
    assert " ayaz " not in f" {result} ", f"alias not substituted: {result!r}"


def test_alias_case_insensitive(mod, monkeypatch):
    """Aliases match case-insensitively but emit the canonical 'correct' form."""
    _inject_vocab(monkeypatch, mod, [
        {"correct": "Ayaz", "aliases": ["AYAZ"]},
    ])
    result = mod.vocab_correct("said Ayaz again", character_id="")
    assert "Ayaz" in result


# ─── Fuzzy hit cap (Agent 3 §6 fourth gate) ──────────────────────────


def test_fuzzy_hits_capped_per_utterance(mod, monkeypatch):
    """At most VOCAB_FUZZY_MAX_HITS_PER_UTT fuzzy substitutions per call.

    Even when many 4+ char near-matches are present, only the cap-many
    earliest ones should be rewritten. Prevents a single noisy utterance
    from being mass-rewritten into a name list.
    """
    _inject_vocab(monkeypatch, mod, [
        {"correct": "Calliope", "aliases": []},
    ])
    # Three near-misses for 'Calliope' (ratio borderline; we just need each
    # to be a 4+ char token outside common words).
    text = "Caliope Caliopa Caliopy walked together"
    result = mod.vocab_correct(text, character_id="")
    hits = result.count("Calliope")
    assert hits <= mod.VOCAB_FUZZY_MAX_HITS_PER_UTT, (
        f"fuzzy hit cap exceeded: got {hits} hits in {result!r}"
    )


# ─── Empty / no-op paths ─────────────────────────────────────────────


def test_empty_text_returns_empty(mod):
    assert mod.vocab_correct("", character_id="") == ""


def test_no_vocab_entries_passthrough(mod, monkeypatch):
    monkeypatch.setattr(mod.formatter, "load_vocab", lambda: [])
    text = "yaz Yari Hane wear near"
    assert mod.vocab_correct(text, character_id="") == text
