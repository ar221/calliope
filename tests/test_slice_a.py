from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "server" / "calliope-server"
WEB_UI_SRC = ROOT / "server" / "calliope_server" / "web_ui.py"
EXTENSION_SRC = ROOT / "extension" / "index.js"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    name = f"calliope_server_slice_a_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_save_char_mode_empty_mode_deletes_persisted_key(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)

    assert mod.save_char_mode("Camilla", "rp_enhance")["Camilla"] == "rp_enhance"
    persisted = mod.save_char_mode("Camilla", "")

    assert "Camilla" not in persisted
    assert mod.get_char_mode("Camilla") == ""
    assert "Camilla" not in mod.CHAR_MODES_FILE.read_text()


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

    mod.DictationHandler.do_POST(DummyHandler())

    assert sent["status"] == 200
    assert sent["data"]["mode"] == ""
    assert "Camilla" not in sent["data"]["modes"]
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
