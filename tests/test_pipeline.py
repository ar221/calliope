"""Pipeline-step unit tests — currently focused on the Phase 1 MVP-12
hallucination filter.

The filter exists because Whisper was trained on YouTube subtitles and
hallucinates end-credits text on silent / near-silent audio. Confirmed
failure modes (Agent 3 §4 / ADR-12):

  - Pure stock phrase → drop entirely
  - Repeated single common token (e.g. 'you you you you') → drop
  - Real speech → pass through unchanged
"""
from __future__ import annotations

import importlib.util
import json
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


# ─── Stock-phrase drops ──────────────────────────────────────────────


@pytest.mark.parametrize("stock", [
    "thanks for watching",
    "thank you for watching",
    "please subscribe to my channel",
    "like and subscribe",
])
def test_stock_phrase_dropped(mod, stock):
    text, dropped, reason = mod.hallucination_filter(stock)
    assert dropped, f"{stock!r} should have been dropped, got {reason!r}"
    assert text == ""
    assert "stock hallucination" in reason or "stock phrase" in reason


def test_stock_phrase_with_punctuation_dropped(mod):
    """Filter normalises punctuation before matching, so a trailing '!' or
    capitalisation must not let the hallucination through."""
    text, dropped, _ = mod.hallucination_filter("Thanks for watching!")
    assert dropped
    assert text == ""


# ─── Single-token repetition ─────────────────────────────────────────


@pytest.mark.parametrize("token", ["you", "the", "bye"])
def test_repeated_single_token_dropped(mod, token):
    text, dropped, reason = mod.hallucination_filter(" ".join([token] * 4))
    assert dropped, f"repeated {token!r} should have been dropped"
    assert text == ""
    assert "degenerate" in reason


def test_two_repetitions_not_dropped(mod):
    """Threshold is len>=3; 'you you' should pass."""
    text, dropped, _ = mod.hallucination_filter("you you")
    assert not dropped
    assert text == "you you"


# ─── Real speech passes through ──────────────────────────────────────


@pytest.mark.parametrize("real", [
    "she walked into the room and looked at me",
    "I'm trying to figure out what comes next",
    "the trade closed at three forty-five",
    "Lord Rashid, the package has arrived",
])
def test_real_speech_passes(mod, real):
    text, dropped, reason = mod.hallucination_filter(real)
    assert not dropped, f"real speech wrongly dropped: {real!r} ({reason})"
    assert text == real


# ─── Edge cases ──────────────────────────────────────────────────────


def test_empty_input_passes(mod):
    text, dropped, _ = mod.hallucination_filter("")
    assert not dropped
    assert text == ""


def test_punctuation_only_passes(mod):
    """All-punctuation input has no normalised token to match against;
    the filter must not crash and must not drop it."""
    text, dropped, _ = mod.hallucination_filter("...!?")
    assert not dropped


def test_stock_phrase_embedded_in_long_real_speech_passes(mod):
    """Substring containment requires the stock phrase to be >=80% of the
    utterance's tokens. A short stock phrase buried in real speech must
    pass through, otherwise we'd nuke legit dictation that mentions
    subscribing.
    """
    real = (
        "I asked her to remind me to thank you for watching the kids "
        "while I was at the trading desk"
    )
    text, dropped, _ = mod.hallucination_filter(real)
    assert not dropped
    assert text == real


# ─── Roadmap modes / prompt tuning ───────────────────────────────────


def test_persona_pov_uses_lower_temperature(mod):
    mode = next(m for m in mod.DEFAULT_MODES if m["id"] == "persona_pov")
    assert mode["temperature"] == 0.4


def test_narrator_past_mode_registered(mod):
    mode = next(m for m in mod.DEFAULT_MODES if m["id"] == "narrator_past")
    assert mode["pipeline"] == [
        "whisper", "hallucination_filter", "command_dispatch",
        "vocab_correct", "disfluency_clean", "rp_enhance",
    ]
    assert mode["temperature"] == 0.4
    assert "third-person past-tense" in mode["system_prompt"]


def test_persona_pov_prompt_includes_few_shot_tuning(mod):
    system, _ = mod._build_persona_pov_prompt(
        {"name": "Ayaz", "description": "Direct, intense."},
        {"name": "Camilla", "card": "A careful listener."},
        "",
    )
    assert "Voice tuning examples" in system
    assert "Do not add extra actions" in system


def test_persona_pov_prompt_includes_scene_continuity(mod):
    system, _ = mod._build_persona_pov_prompt(
        {"name": "Ayaz", "description": "Direct."},
        {"name": "Camilla", "card": "A careful listener."},
        "",
        "location: bedroom; clothing: black dress; position: pinned to wall",
    )
    assert "Current scene continuity" in system
    assert "black dress" in system
    assert "pinned to wall" in system


def test_update_state_accepts_and_truncates_scene_continuity(mod):
    long_scene = "location: bedroom " * 300
    snap = mod.update_state({"sceneContinuity": long_scene})
    assert snap["sceneContinuity"].startswith("location: bedroom")
    assert len(snap["sceneContinuity"]) == 2000
    mod.update_state({"sceneContinuity": ""})


def test_update_state_strips_tracker_template_noise(mod):
    snap = mod.update_state({
        "sceneContinuity": (
            "scene: Location: apartment. Camilla: on sofa.\n"
            "scene: version: 1; location: place: apartment; characters: raw duplicate\n"
            "tracker: enabled: false; mesTrackerTemplate: <div>junk</div>; "
            "generateContextTemplate: huge prompt"
        ),
    })
    assert snap["sceneContinuity"] == "scene: Location: apartment. Camilla: on sofa."
    mod.update_state({"sceneContinuity": ""})


def test_rp_enhance_payload_includes_scene_continuity(monkeypatch, mod):
    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"content": "formatted"}).encode()

    def fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode())
        return FakeResponse()

    monkeypatch.setattr(mod, "probe_formatter", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    out, skipped, reason = mod.format_rp(
        "I step closer.", mode=2,
        scene_continuity="location: hallway; clothing: robe",
    )
    assert out == "formatted"
    assert not skipped
    assert reason == ""
    user_content = json.dumps(captured["payload"])
    assert "SCENE CONTINUITY" in user_content
    assert "location: hallway" in user_content
