"""POL-3 — low-confidence word tagging + alternatives.

`compute_low_confidence_spans` ingests text + whisper word-confidence
list and returns spans suitable for the "did you mean?" overlay.

`word_alternatives` is the public-endpoint backing helper (vocab tier).
"""
from __future__ import annotations

import importlib.util
import math
import pathlib
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


def _wc(word: str, prob: float) -> dict:
    """Build a {word, confidence, logprob} entry like extract_word_confidences."""
    lp = math.log(prob) if prob > 0 else -20.0
    return {"word": word, "confidence": prob, "logprob": lp}


# ─── Threshold gating ─────────────────────────────────────────


def test_high_confidence_words_skipped(mod):
    """Words above threshold should not produce spans."""
    text = "she walked into the room"
    confs = [_wc(w, 0.95) for w in text.split()]
    spans = mod.compute_low_confidence_spans(text, confs)
    assert spans == []


def test_low_confidence_words_flagged(mod):
    """A word below the -0.7 threshold should appear in spans."""
    text = "she met Suzy at noon"
    confs = [
        _wc("she", 0.99),
        _wc("met", 0.92),
        _wc("Suzy", 0.30),  # logprob ≈ -1.2 → flagged
        _wc("at", 0.94),
        _wc("noon", 0.88),
    ]
    spans = mod.compute_low_confidence_spans(text, confs)
    assert len(spans) == 1
    span = spans[0]
    assert span["word"] == "Suzy"
    assert span["start_idx"] == text.index("Suzy")
    assert span["end_idx"] == span["start_idx"] + len("Suzy")
    assert span["logprob"] < mod.WORD_CONFIDENCE_THRESHOLD


def test_short_words_skipped_even_if_low_confidence(mod):
    """Tokens shorter than WORD_CONFIDENCE_MIN_LEN are not flagged."""
    text = "I am at home"
    confs = [_wc(w, 0.10) for w in text.split()]
    spans = mod.compute_low_confidence_spans(text, confs)
    # all tokens shorter than 3 chars → "home" is the only candidate
    assert all(s["word"].lower() == "home" for s in spans)


def test_max_spans_cap(mod):
    """No more than WORD_CONFIDENCE_MAX_SPANS entries returned."""
    long_text = " ".join([f"strangeword{i}" for i in range(40)])
    confs = [_wc(w, 0.10) for w in long_text.split()]
    spans = mod.compute_low_confidence_spans(long_text, confs)
    assert len(spans) <= mod.WORD_CONFIDENCE_MAX_SPANS


def test_threshold_override(mod):
    """Caller-supplied threshold takes precedence."""
    text = "she met Suzy at noon"
    confs = [_wc("Suzy", 0.30)]
    # Tighter threshold (closer to 0) → "Suzy" no longer flagged.
    spans = mod.compute_low_confidence_spans(text, confs, threshold=-2.0)
    assert spans == []


def test_empty_inputs(mod):
    assert mod.compute_low_confidence_spans("", []) == []
    assert mod.compute_low_confidence_spans("hello world", []) == []


# ─── Alternatives ─────────────────────────────────────────────


def test_alternatives_pull_from_vocab(mod, tmp_path, monkeypatch):
    """When user vocab has a similar word, it surfaces as an alternative."""
    vocab_file = tmp_path / "vocab.yaml"
    vocab_file.write_text(
        "- correct: Suzy\n"
        "  aliases: [Suzie]\n"
        "- correct: Hana\n"
        "  aliases: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod.config, "VOCAB_FILE", vocab_file)
    mod._invalidate_vocab_cache()
    try:
        # Whisper hears "Suzie" instead of "Suzy" (close difflib ratio).
        confs = [_wc("Suzie", 0.20)]
        spans = mod.compute_low_confidence_spans("she met Suzie at noon", confs)
        assert len(spans) == 1
        # Vocab alt should propose "Suzy" within the top-3.
        assert "Suzy" in spans[0]["alternatives"]

        # Direct API surface used by /word-alternatives endpoint.
        alts = mod.word_alternatives("Suzie")
        assert any(a["text"] == "Suzy" for a in alts)
        assert all(a["source"] == "vocab" for a in alts)
        for a in alts:
            assert 0.0 <= a["score"] <= 1.0
    finally:
        mod._invalidate_vocab_cache()


def test_word_alternatives_empty_vocab_returns_empty(mod, tmp_path, monkeypatch):
    vocab_file = tmp_path / "vocab.yaml"
    vocab_file.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(mod.config, "VOCAB_FILE", vocab_file)
    mod._invalidate_vocab_cache()
    try:
        assert mod.word_alternatives("Sushi") == []
    finally:
        mod._invalidate_vocab_cache()


# ─── extract_word_confidences logprob attachment ────────────


def test_extract_word_confidences_attaches_logprob(mod):
    response = {
        "segments": [
            {"words": [
                {"word": "hello", "probability": 0.9},
                {"word": "world", "p": 0.6},
            ]}
        ]
    }
    out = mod.extract_word_confidences(response)
    assert len(out) == 2
    for entry in out:
        assert "logprob" in entry
        assert entry["logprob"] == pytest.approx(math.log(entry["confidence"]))


def test_extract_word_confidences_zero_probability_safe(mod):
    """A 0.0 probability must not throw — falls back to a stable floor."""
    response = {"segments": [{"words": [{"word": "x", "probability": 0.0}]}]}
    out = mod.extract_word_confidences(response)
    assert out[0]["logprob"] == -20.0  # documented floor
