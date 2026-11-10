from __future__ import annotations

import pathlib

from conftest import load_wrapper


ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_UI_SRC = ROOT / "server" / "calliope_server" / "web_ui.py"
EXTENSION_SRC = ROOT / "extension" / "index.js"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    return load_wrapper("calliope_server_slice_a")


def test_save_char_mode_empty_mode_deletes_persisted_key(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)

    assert mod.save_char_mode("Camilla", "rp_enhance")["camilla"] == "rp_enhance"
    persisted = mod.save_char_mode("Camilla", "")

    assert "camilla" not in persisted
    assert mod.get_char_mode("Camilla") == ""
    assert "camilla" not in mod.CHAR_MODES_FILE.read_text()


def test_mode_memory_endpoint_returns_persisted_clear(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod.save_char_mode("Camilla", "rp_enhance")
    sent = {}

    class DummyHandler:
        path = "/state/mode-memory"
        headers = {"Content-Length": "2"}

        def require_auth(self):
            return True

        def _reject_oversized_body(self, limit):
            return False

        def read_json_body(self):
            return {"character": "Camilla", "mode": ""}

        def send_json(self, data, status=200):
            sent.update(status=status, data=data)

        def send_error_json(self, message, **kwargs):
            raise AssertionError((message, kwargs))

    mod.DictationHandler._dispatch_post(DummyHandler())

    assert sent["status"] == 200
    assert sent["data"]["mode"] == ""
    assert "camilla" not in sent["data"]["modes"]
    assert mod.get_char_mode("Camilla") == ""


def test_ad_hoc_reformat_preserves_exact_text_and_transcript(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    original = [
        {"id": "keep", "role": "user", "text": "existing", "starred": True},
        {"id": "context", "role": "context", "text": "reply"},
    ]
    sent = {}
    captured = {}

    class DummyHandler:
        def read_json_body(self):
            return {
                "text": "  exact source\n",
                "mode": "rp_enhance",
                "persist_transcript": False,
            }

        def send_json(self, data, status=200):
            sent.update(status=status, data=data)

        def send_error_json(self, message, **kwargs):
            raise AssertionError((message, kwargs))

        def _build_chat_context(self, chat_source):
            raise AssertionError("chat context should not be requested")

    def fake_run_pipeline(text, mode, **kwargs):
        captured["text"] = text
        return "\n  exact formatted output  \n", False, "", text

    monkeypatch.setattr(mod, "run_pipeline", fake_run_pipeline)
    mod.session_transcript[:] = original
    mod.DictationHandler._handle_reformat(DummyHandler(), {})

    assert captured["text"] == "  exact source\n"
    assert sent["data"]["text"] == "\n  exact formatted output  \n"
    assert sent["data"]["entry_id"] is None
    assert mod.session_transcript == original


def test_phone_language_propagates_and_defaults_to_auto(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    html = mod.DictationHandler._render_html_with_embed(
        object(), {"embed": ["1"], "language": ["sw"]}
    )
    assert '"language": "sw"' in html

    source = WEB_UI_SRC.read_text()
    assert "get('language') || 'auto'" in source
    assert "params.set('language', transcriptionLanguage)" in source

    extension = EXTENSION_SRC.read_text()
    assert "qp.set('language', lang)" in extension
    assert "lang && lang !== 'auto'" not in extension


def test_extension_slice_a_guards_context_and_text_safety():
    source = EXTENSION_SRC.read_text()

    assert "replace(/\\.png$/i, '')" in source
    assert "persist_transcript: false" in source
    assert "text: source" in source
    assert "ta.value !== source" in source
    assert "String(data?.text || '').trim()" not in source
    assert "refreshFormatterModeForContext()" in source
    assert "seq !== formatterModeRefreshSeq" in source
    assert "event_types.APP_READY" in source
    assert "event_types.CHAT_CHANGED" in source
    assert "event_types.CHAT_LOADED" in source
    assert "String(data?.mode || '') === String(modeId || '').trim()" in source
    assert "persistAddresseeChoice" not in source


def test_phone_regen_uses_a_formatter_mode_and_reports_http_errors():
    source = WEB_UI_SRC.read_text()

    assert "function resolveRegenMode()" in source
    # Main regen honours an OOC utterance; the transcript-entry regen keeps the plain resolver.
    assert "const regenMode = lastWasOoc ? 'grammar_clean' : resolveRegenMode();" in source
    assert source.count("const regenMode = resolveRegenMode();") == 1
    assert "if (!resp.ok || data.error)" in source
    assert "Regen failed (HTTP ${resp.status})" in source


def test_phone_microphone_selector_uses_permissive_bluetooth_constraints():
    source = WEB_UI_SRC.read_text()

    assert 'id="microphoneSelect"' in source
    assert "function recordingAudioConstraints()" in source
    assert "sampleRate: 16000" not in source
    assert "audio.deviceId = { exact: selectedMicrophoneId }" in source
    assert "Silent input detected" in source
    assert "if (!vadEnabled) return;" in source


def test_failed_legacy_regen_does_not_delete_transcript_entry(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    original = [{"id": "keep", "role": "user", "text": "existing", "starred": True}]
    sent = {}

    class DummyHandler:
        def read_json_body(self):
            return {"text": "existing", "mode": "rp_enhance"}

        def send_json(self, data, status=200):
            sent.update(status=status, data=data)

        def send_error_json(self, message, **kwargs):
            raise AssertionError((message, kwargs))

        def _build_chat_context(self, chat_source):
            return ""

    monkeypatch.setattr(
        mod,
        "run_pipeline",
        lambda *args, **kwargs: ("existing", True, "formatter offline", "existing"),
    )
    mod.session_transcript[:] = list(original)
    mod.DictationHandler._handle_reformat(DummyHandler(), {})

    assert sent["data"]["formatting_skipped"] is True
    assert mod.session_transcript == original


def test_reformat_target_deleted_during_formatting_does_not_pop_another_entry(tmp_path, monkeypatch):
    """The lock is released for the LLM call. If the requested entry is
    deleted meanwhile, the result must not be written over — or in place of —
    an unrelated entry (the old code reused a stale index, then fell through
    to 'pop the newest user entry')."""
    mod = _load_server(tmp_path, monkeypatch)
    sent = {}

    class DummyHandler:
        def read_json_body(self):
            return {"text": "second", "mode": "rp_enhance", "entry_id": "b"}

        def send_json(self, data, status=200):
            sent.update(status=status, data=data)

        def send_error_json(self, message, **kwargs):
            raise AssertionError((message, kwargs))

        def _build_chat_context(self, chat_source):
            return ""

    def pipeline_that_deletes_target(*args, **kwargs):
        with mod.transcript_lock:
            mod.session_transcript[:] = [e for e in mod.session_transcript if e["id"] != "b"]
        return ("*Second, polished.*", False, "", "")

    monkeypatch.setattr(mod, "run_pipeline", pipeline_that_deletes_target)
    mod.session_transcript[:] = [
        {"id": "a", "role": "user", "text": "first", "starred": True, "chat_id": ""},
        {"id": "b", "role": "user", "text": "second", "starred": False, "chat_id": ""},
        {"id": "c", "role": "user", "text": "third", "starred": False, "chat_id": ""},
    ]
    mod.DictationHandler._handle_reformat(DummyHandler(), {})

    assert sent["status"] == 200
    assert sent["data"]["text"] == "*Second, polished.*"
    remaining = [(e["id"], e["text"]) for e in mod.session_transcript]
    assert remaining == [("a", "first"), ("c", "third")]
    assert sent["data"]["entry_id"] is None


def test_reformat_into_a_mode_without_a_formatter_step_reports_skipped(tmp_path, monkeypatch):
    """'plain' has no LLM rewrite step, so a reformat into it must not claim success.

    Previously the handler ran the pipeline, got the text back unchanged, and
    replied formatting_skipped=False — so the client toasted a successful
    reformat that never reached a model. An unknown mode id resolves to 'plain',
    which made a typo look like a working regenerate.
    """
    mod = _load_server(tmp_path, monkeypatch)
    sent = {}
    ran = []

    class DummyHandler:
        def read_json_body(self):
            return {"text": "some dictated line", "mode": "plain"}

        def send_json(self, data, status=200):
            sent.update(status=status, data=data)

        def send_error_json(self, message, **kwargs):
            raise AssertionError((message, kwargs))

        def _build_chat_context(self, chat_source):
            return ""

    monkeypatch.setattr(
        mod, "run_pipeline",
        lambda *args, **kwargs: ran.append(1) or ("x", False, "", "x"),
    )
    mod.DictationHandler._handle_reformat(DummyHandler(), {})

    assert ran == [], "no formatter step means the pipeline should not run"
    assert sent["data"]["formatting_skipped"] is True
    assert "no formatter step" in sent["data"]["formatting_reason"]
    assert sent["data"]["text"] == "some dictated line"
    assert sent["data"]["model"] == ""


def test_mode_has_formatter_step_classifies_builtin_modes(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    for mode_id in ("grammar_clean", "rp_format", "rp_enhance", "persona_pov"):
        assert mod.mode_has_formatter_step(mod.resolve_mode(mode_id=mode_id)) is True, mode_id
    for mode_id in ("plain", "command"):
        assert mod.mode_has_formatter_step(mod.resolve_mode(mode_id=mode_id)) is False, mode_id
