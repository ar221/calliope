"""POL-1 — voice command dispatcher unit tests.

`command_dispatch` is a regex pre-pass on raw whisper output that detects
sentinel-prefixed voice commands (`computer: send`, `hey computer swipe right`,
…) and returns (residual_text, command_dict_or_None).

Pure-command intents short-circuit the pipeline; mixed intents (`append`,
`replace`) preserve the trailing residual as content for downstream steps.

OOC handling lives upstream in `_handle_transcribe`; this file exercises the
in-pipeline dispatcher only.
"""
from __future__ import annotations

import importlib.util
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


# ─── Pure-command intents: residual must be empty ────────────────────


@pytest.mark.parametrize("text,expected_intent", [
    ("computer: send", "send"),
    ("Computer, send.", "send"),
    ("hey computer send", "send"),
    ("computer regenerate", "regenerate"),
    ("computer regen", "regenerate"),
    ("computer: stop", "stop"),
    ("computer cancel", "stop"),
    ("computer scratch that", "undo"),
    ("computer: new paragraph", "new_paragraph"),
    ("computer scene break", "scene_break"),
    ("computer clear", "clear"),
])
def test_pure_command_short_circuits(mod, text, expected_intent):
    residual, cmd = mod.command_dispatch(text)
    assert cmd is not None, f"expected a command from {text!r}"
    assert cmd["intent"] == expected_intent
    assert residual == ""
    assert cmd["source_text"] == text


# ─── Swipe direction args ────────────────────────────────────────────


@pytest.mark.parametrize("text,direction", [
    ("computer: swipe right", "right"),
    ("computer swipe left", "left"),
    ("computer next swipe", "right"),
    ("computer swipe prev", "left"),
    ("computer swipe", "right"),  # bare swipe → default right
])
def test_swipe_direction(mod, text, direction):
    residual, cmd = mod.command_dispatch(text)
    assert cmd is not None
    assert cmd["intent"] == "swipe"
    assert cmd["args"].get("direction") == direction
    assert residual == ""


# ─── Mixed-intent residual passes through ────────────────────────────


def test_append_with_residual(mod):
    residual, cmd = mod.command_dispatch("computer: append hello world")
    assert cmd is not None
    assert cmd["intent"] == "append"
    # Residual is the trailing content (no leading whitespace).
    assert residual == "hello world"


def test_replace_with_residual(mod):
    residual, cmd = mod.command_dispatch("computer replace this is the new text")
    assert cmd is not None
    assert cmd["intent"] == "replace"
    assert residual == "this is the new text"


# ─── No sentinel ⇒ no dispatch ───────────────────────────────────────


@pytest.mark.parametrize("text", [
    "send him a message",
    "swipe right on the screen",
    "she walked into the room",
    "regenerate the trade ticket",
    "",
    "   ",
    "the computer was off",   # sentinel must be at start
])
def test_no_sentinel_passes_through(mod, text):
    residual, cmd = mod.command_dispatch(text)
    assert cmd is None
    assert residual == text


# ─── Case insensitivity + punctuation tolerance ──────────────────────


def test_case_insensitive_and_punctuation(mod):
    for text in ("COMPUTER: SEND", "Computer. Send", "computer , send"):
        residual, cmd = mod.command_dispatch(text)
        assert cmd is not None and cmd["intent"] == "send", text
        assert residual == ""


# ─── OOC regex (separate constant) ───────────────────────────────────


@pytest.mark.parametrize("text", [
    "OOC: ping me later",
    "ooc: paragraph break",
    "out-of-character note",
    "out of character: hello",
])
def test_ooc_prefix_regex_matches(mod, text):
    assert mod.OOC_PREFIX_RE.match(text) is not None


def test_ooc_does_not_match_inline(mod):
    # OOC must be at the start of the utterance, with delimiter.
    assert mod.OOC_PREFIX_RE.match("we are out of character now") is None


# ─── User override via voice_macros file ─────────────────────────────


def test_voice_macros_override(mod, tmp_path, monkeypatch):
    """User-supplied voice_macros file changes sentinel + intents."""
    macros_path = tmp_path / "voice_macros.yaml"
    macros_path.write_text(
        "sentinel: agent\n"
        "intents:\n"
        "  - phrase: ['punt']\n"
        "    intent: send\n"
        "  - phrase: ['polish']\n"
        "    intent: regenerate\n"
        "    args_after: hint\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod.config, "VOICE_MACROS_FILE", macros_path)
    mod._invalidate_voice_macros_cache()
    try:
        residual, cmd = mod.command_dispatch("agent: punt")
        assert cmd is not None and cmd["intent"] == "send"
        assert residual == ""

        residual, cmd = mod.command_dispatch("agent polish for clarity")
        assert cmd is not None and cmd["intent"] == "regenerate"
        # `args_after: hint` consumes the trailing residual.
        assert cmd["args"].get("hint") == "for clarity"
        assert residual == ""

        # Old `computer` sentinel no longer matches when user override is in place.
        _, cmd = mod.command_dispatch("computer: send")
        assert cmd is None
    finally:
        mod._invalidate_voice_macros_cache()


def test_pure_command_intents_set_includes_new(mod):
    """Sanity: the short-circuit set has the documented intents."""
    expected = {"send", "swipe", "regenerate", "delete_last", "undo",
                "new_paragraph", "scene_break", "stop", "clear"}
    assert expected.issubset(mod.PURE_COMMAND_INTENTS)
    # `append` and `replace` flow residual; must NOT be in the pure set.
    assert "append" not in mod.PURE_COMMAND_INTENTS
    assert "replace" not in mod.PURE_COMMAND_INTENTS
