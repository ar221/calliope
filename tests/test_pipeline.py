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
import io
import json
import pathlib
from importlib.machinery import SourceFileLoader

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"
WEB_UI_SRC = SRC.parent / "calliope_server" / "web_ui.py"


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


def test_rp_enhance_uses_low_temperature_for_faithful_rewrite(mod):
    mode = next(m for m in mod.DEFAULT_MODES if m["id"] == "rp_enhance")
    assert mode["temperature"] == 0.45


def test_narrator_past_mode_registered(mod):
    mode = next(m for m in mod.DEFAULT_MODES if m["id"] == "narrator_past")
    assert mode["pipeline"] == [
        "whisper", "hallucination_filter", "command_dispatch",
        "vocab_correct", "disfluency_clean", "rp_enhance",
    ]
    assert mode["temperature"] == 0.4
    assert "third-person past-tense" in mode["system_prompt"]


def test_narrator_present_mode_registered(mod):
    mode = next(m for m in mod.DEFAULT_MODES if m["id"] == "narrator_present")
    assert mode["pipeline"] == [
        "whisper", "hallucination_filter", "command_dispatch",
        "vocab_correct", "disfluency_clean", "rp_enhance",
    ]
    assert mode["temperature"] == 0.4
    assert "third-person present-tense" in mode["system_prompt"]
    assert "present tense" in mode["system_prompt"].lower()


def test_persona_pov_prompt_includes_few_shot_tuning(mod):
    system, _ = mod._build_persona_pov_prompt(
        {"name": "Ayaz", "description": "Direct, intense."},
        {"name": "Camilla", "card": "A careful listener."},
        "",
    )
    assert "Voice tuning examples" in system
    assert "Do not add extra actions" in system
    assert "OUTPUT CONTRACT" in system
    assert "Do NOT add a lead-in, preamble" in system


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


# ─── POL-17 repair trace ─────────────────────────────────────────────


def test_repair_trace_exposes_raw_cleaned_final_without_persistence(mod):
    trace = mod.build_repair_trace(
        raw="um suzie steps closer",
        cleaned="Suzie steps closer.",
        final="*Suzy steps closer.*",
    )

    assert trace == {
        "raw": "um suzie steps closer",
        "cleaned": "Suzie steps closer.",
        "final": "*Suzy steps closer.*",
        "stages": ["raw", "cleaned", "final"],
        "has_changes": True,
        "persistence": "in_ram_only",
    }


def test_repair_trace_drops_duplicate_empty_stages(mod):
    trace = mod.build_repair_trace(raw="hello", cleaned="", final="hello")

    assert trace["cleaned"] == ""
    assert trace["stages"] == ["raw"]
    assert trace["has_changes"] is False
    assert trace["persistence"] == "in_ram_only"


def test_reformat_endpoint_returns_repair_trace_without_storing_trace(monkeypatch, mod):
    sent = {}
    original_transcript = list(mod.session_transcript)
    original_vocab_cache = dict(mod._vocab_cache)

    class DummyHandler:
        def read_json_body(self):
            return {"text": "um suzie steps closer", "mode": "grammar_clean"}

        def send_json(self, data, status=200):
            sent["status"] = status
            sent["data"] = data

        def send_error_json(self, message, **kwargs):  # pragma: no cover - failure path
            raise AssertionError((message, kwargs))

        def _build_chat_context(self, chat_source):  # pragma: no cover - unused here
            raise AssertionError("chat context should not be requested")

    def fake_run_pipeline(text, mode, **kwargs):
        assert text == "um suzie steps closer"
        assert mode["id"] == "grammar_clean"
        return "Suzie steps closer.", False, "", "Suzie steps closer."

    monkeypatch.setattr(mod, "run_pipeline", fake_run_pipeline)
    try:
        mod.session_transcript[:] = []
        mod.DictationHandler._handle_reformat(DummyHandler(), {})
    finally:
        mod.session_transcript[:] = original_transcript
        mod._vocab_cache.clear()
        mod._vocab_cache.update(original_vocab_cache)

    payload = sent["data"]
    assert sent["status"] == 200
    assert payload["raw"] == "um suzie steps closer"
    assert payload["cleaned"] == "Suzie steps closer."
    assert payload["repair_trace"] == {
        "raw": "um suzie steps closer",
        "cleaned": "Suzie steps closer.",
        "final": "Suzie steps closer.",
        "stages": ["raw", "cleaned"],
        "has_changes": True,
        "persistence": "in_ram_only",
    }
    assert mod.session_transcript == []
    assert "repair_trace" not in json.dumps(mod.session_transcript)
    assert mod._vocab_cache == original_vocab_cache


def test_repair_trace_ui_escapes_html_and_does_not_post_full_trace():
    source = WEB_UI_SRC.read_text()
    render_start = source.index("function renderRepairTrace()")
    render_end = source.index("async function acceptRepairAsVocab()")
    render_src = source[render_start:render_end]
    show_start = source.index("function showResult(text, meta)")
    show_end = source.index("function clearResult()", show_start)
    show_src = source[show_start:show_end]

    assert "escapeHtml(value)" in render_src
    assert "escapeHtml(repairStageLabel(stage))" in render_src
    assert "innerHTML = trace.stages.map" in render_src
    assert "postToEmbedParent" not in show_src
    assert "postMessage(payload, embedParentOrigin)" in source
    assert "postMessage({ type: 'dictation-ready' }, '*')" not in source


def test_embed_postmessage_target_is_derived_from_referrer_not_query(mod):
    html = mod.DictationHandler._render_html_with_embed(
        object(),
        {"embed": ["1"], "parent_origin": ["https://evil.example"]},
    )

    assert '"parentOrigin"' not in html
    assert "new URL(document.referrer).origin" in html
    assert "postMessage(payload, embedParentOrigin)" in html


def test_transcribe_sse_result_source_includes_repair_trace():
    source = SRC.read_text()
    marker = "# MVP-13: emit canonical `dictation-result` after streaming."
    start = source.index(marker)
    end = source.index("# MVP-16 — pipeline finished.", start)
    block = source[start:end]

    assert "broadcast_event(\"dictation-result\"" in block
    assert "\"has_repair_trace\": repair_trace.get(\"has_changes\", False)" in block
    assert "\"raw\": raw_text" not in block
    assert "\"cleaned\": cleaned_text" not in block
    assert "\"repair_trace\": repair_trace" not in block
    assert "repair_trace = build_repair_trace(raw_text, cleaned_text, output_text)" in source[:start]


def test_transcribe_endpoint_keeps_repair_trace_out_of_sse_and_persistence(monkeypatch, mod):
    sent = {}
    events = []
    original_transcript = list(mod.session_transcript)
    original_vocab_cache = dict(mod._vocab_cache)

    class DummyHandler:
        headers = {"Content-Length": "4", "Content-Type": "audio/wav"}
        rfile = io.BytesIO(b"RIFF")

        def send_json(self, data, status=200):
            sent["status"] = status
            sent["data"] = data

        def send_error_json(self, message, **kwargs):  # pragma: no cover - failure path
            raise AssertionError((message, kwargs))

        def _build_chat_context(self, chat_source):  # pragma: no cover - unused here
            raise AssertionError("chat context should not be requested")

        def _emit_dictation_state(self, *args, **kwargs):
            pass

        def _emit_dictation_transcript(self, *args, **kwargs):
            pass

    def fake_transcribe(path, **kwargs):
        assert pathlib.Path(path).exists()
        return "um suzie steps closer", []

    def fake_run_pipeline(text, mode, **kwargs):
        assert text == "um suzie steps closer"
        assert mode["id"] == "rp_enhance"
        return "*Suzy steps closer.*", False, "", "Suzie steps closer."

    def fake_broadcast(event, payload):
        events.append((event, payload))
        return 1

    monkeypatch.setattr(mod, "transcribe_with_confidence", fake_transcribe)
    monkeypatch.setattr(mod, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(mod, "broadcast_event", fake_broadcast)
    try:
        mod.session_transcript[:] = []
        mod.DictationHandler._handle_transcribe(
            DummyHandler(),
            {"mode": ["rp_enhance"], "provider": ["local"], "use_state": ["0"]},
        )
    finally:
        mod.session_transcript[:] = original_transcript
        mod._vocab_cache.clear()
        mod._vocab_cache.update(original_vocab_cache)

    payload = sent["data"]
    assert sent["status"] == 200
    assert payload["repair_trace"]["persistence"] == "in_ram_only"
    assert payload["repair_trace"]["stages"] == ["raw", "cleaned", "final"]

    result_events = [payload for event, payload in events if event == "dictation-result"]
    assert len(result_events) == 1
    sse_payload = result_events[0]
    assert sse_payload["text"] == "*Suzy steps closer.*"
    assert sse_payload["has_repair_trace"] is True
    assert "raw" not in sse_payload
    assert "cleaned" not in sse_payload
    assert "repair_trace" not in sse_payload
    assert "repair_trace" not in json.dumps(mod.session_transcript)
    assert mod._vocab_cache == original_vocab_cache


def test_dictation_transcript_sse_omits_raw_text(monkeypatch, mod):
    events = []
    monkeypatch.setattr(mod, "broadcast_event", lambda event, payload: events.append((event, payload)))

    mod.DictationHandler._emit_dictation_transcript(object(), "req1", "final", "raw secret text", latency_ms=12)

    assert events == [("dictation-transcript", {
        "requestId": "req1",
        "phase": "final",
        "has_text": True,
        "source": "whisper",
        "ts": events[0][1]["ts"],
        "latency_ms": 12,
    })]
    assert "text" not in events[0][1]
    assert "raw secret text" not in json.dumps(events[0][1])


def test_extension_ignores_dictation_transcript_text_preview():
    source = (SRC.parents[1] / "extension" / "index.js").read_text()
    start = source.index("sseSource.addEventListener('dictation-transcript'")
    end = source.index("sseSource.addEventListener('dictation-result'", start)
    block = source[start:end]

    assert "data.text" not in block
    assert "const preview = 'speech'" in block


def test_vocab_accept_path_persists_alias_only_without_repair_trace(monkeypatch, mod):
    sent = {}
    writes = []

    class DummyHandler:
        def read_json_body(self):
            return {"correct": "*Suzy steps closer.*", "aliases": ["um suzie steps closer"]}

        def send_json(self, data, status=200):
            sent["status"] = status
            sent["data"] = data

        def send_error_json(self, message, **kwargs):  # pragma: no cover - failure path
            raise AssertionError((message, kwargs))

    def fake_atomic_write(path, data):
        writes.append((path, data))

    monkeypatch.setattr(mod, "load_vocab", lambda: [])
    monkeypatch.setattr(mod, "_atomic_write", fake_atomic_write)
    monkeypatch.setattr(mod, "_invalidate_vocab_cache", lambda: None)

    mod.DictationHandler._handle_vocab_add(DummyHandler())

    assert sent["status"] == 200
    assert sent["data"]["added"] == {
        "correct": "*Suzy steps closer.*",
        "aliases": ["um suzie steps closer"],
    }
    assert len(writes) == 1
    persisted = writes[0][1].decode("utf-8")
    assert "*Suzy steps closer.*" in persisted
    assert "um suzie steps closer" in persisted
    assert "repair_trace" not in persisted
    assert "in_ram_only" not in persisted


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

    monkeypatch.setattr(mod.formatter, "probe_formatter", lambda *a, **k: (True, ""))
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
    assert "OUTPUT CONTRACT" in user_content
    assert "Do NOT add a lead-in, preamble" in user_content


def test_build_scene_contract_is_in_memory_and_prompt_ready(mod):
    contract = mod.build_scene_contract(
        {
            "personaId": "lord-rashid",
            "characterId": "elara",
            "characterName": "Elara",
            "chatType": "individual",
            "sceneContinuity": "location: library; clothing: black dress",
        },
        chat_context="Elara smiled.",
    )

    assert contract["persistence"] == "in_memory_only"
    assert contract["chat_type"] == "solo"
    assert contract["addressee"] == "Elara"
    assert "location: library" in contract["prompt"]
    assert "do not persist" in contract["prompt"]
    assert "Do not invent new scene facts" in contract["prompt"]


def test_rp_enhance_payload_uses_scene_contract(monkeypatch, mod):
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

    contract = mod.build_scene_contract({
        "personaId": "lord-rashid",
        "characterId": "elara",
        "characterName": "Elara",
        "chatType": "group",
        "lastSpeaker": "Mira",
    })
    monkeypatch.setattr(mod.formatter, "probe_formatter", lambda *a, **k: (True, ""))
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    out, skipped, reason = mod.format_rp(
        "I step closer.", mode=2, scene_contract=contract,
    )

    assert out == "formatted"
    assert not skipped
    assert reason == ""
    payload = json.dumps(captured["payload"])
    assert "SCENE CONTRACT" in payload
    assert "last_speaker: Mira" in payload
    assert "do not persist" in payload


def test_persona_pov_prompt_accepts_scene_contract(mod):
    contract = mod.build_scene_contract({
        "personaId": "lord-rashid",
        "characterId": "elara",
        "characterName": "Elara",
        "chatType": "individual",
    })
    system, _ = mod._build_persona_pov_prompt(
        {"name": "Ayaz", "description": "Direct."},
        {"name": "Elara", "card": "A careful listener."},
        "",
        scene_contract=contract,
    )

    assert "SCENE CONTRACT" in system
    assert "Preserve the dictated speaker" in system
    assert "Do not invent new scene facts" in system


def test_strip_formatter_preamble_removes_pyrite_meta_paragraph(mod):
    leaked = (
        "Oh YES this is the pivot moment — I need to capture the controlled choice. "
        "The question is a gambit. Let's go.\n\n"
        "*He stands at the threshold, letting the quiet settle before he speaks.*\n\n"
        '"Coffee," he says. "Sounds good."'
    )

    cleaned = mod.strip_formatter_preamble(leaked)

    assert cleaned.startswith("*He stands at the threshold")
    assert "pivot moment" not in cleaned
    assert "Let's go" not in cleaned


def test_strip_formatter_preamble_keeps_legitimate_plain_output(mod):
    output = "I step closer and ask if she wants coffee."
    assert mod.strip_formatter_preamble(output) == output


def _load_isolated_server(tmp_path, monkeypatch):
    """Load a fresh server module bound to an isolated CALLIOPE_DATA_DIR."""
    import uuid
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    name = f"calliope_server_modes_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_load_modes_merges_new_builtin_into_legacy_file(tmp_path, monkeypatch):
    """A pre-existing modes file that predates a new built-in mode should still
    surface that mode, while preserving the user's customizations."""
    m = _load_isolated_server(tmp_path, monkeypatch)
    # Build a legacy modes file: all defaults EXCEPT narrator_present, with a
    # customized rp_enhance (simulating the user's real edits).
    legacy = [dict(d) for d in m.DEFAULT_MODES if d["id"] != "narrator_present"]
    for mode in legacy:
        if mode["id"] == "rp_enhance":
            mode["temperature"] = 0.99  # user customization
    m._atomic_write(m.MODES_FILE, m._serialize_config(legacy))
    m._modes_cache["data"] = None  # bust cache

    loaded = m.load_modes()
    ids = [x["id"] for x in loaded]
    assert "narrator_present" in ids, "new built-in mode must be merged in"
    # Customization preserved, not overwritten by default merge.
    rp = next(x for x in loaded if x["id"] == "rp_enhance")
    assert rp["temperature"] == 0.99
    # No duplicates.
    assert len(ids) == len(set(ids))


def test_load_modes_does_not_duplicate_when_file_current(tmp_path, monkeypatch):
    m = _load_isolated_server(tmp_path, monkeypatch)
    m._atomic_write(m.MODES_FILE, m._serialize_config([dict(d) for d in m.DEFAULT_MODES]))
    m._modes_cache["data"] = None
    loaded = m.load_modes()
    ids = [x["id"] for x in loaded]
    assert ids.count("narrator_present") == 1
    assert len(ids) == len(m.DEFAULT_MODES)
