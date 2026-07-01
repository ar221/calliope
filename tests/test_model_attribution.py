"""Model attribution coverage — verifies the thread-local slot records which
formatter model actually produced the final text, including chain fallback.

Shares the scripted-urlopen fake from the omniroute suite's style.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid
from importlib.machinery import SourceFileLoader

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DICTATION_FORMATTER_PROVIDER", "omniroute")
    monkeypatch.setenv(
        "DICTATION_OMNIROUTE_RP_CHAIN",
        "claude/claude-opus-4-8, codex/gpt-5.5, claude/claude-sonnet-4-6",
    )
    name = f"calliope_attr_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_body(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def _err_body(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}).encode()


def _install(monkeypatch, mod, script):
    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            return _FakeResp(b"{}")
        model = json.loads(req.data.decode()).get("model", "")
        return _FakeResp(script.get(model, _err_body("model not found")))
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


def test_attribution_defaults_empty(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod.reset_model_attribution()
    attr = mod.get_model_attribution()
    assert attr == {"provider": "", "model": "", "fallback": False, "tier": 0}


def test_attribution_records_primary_model(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    _install(monkeypatch, mod, {"claude/claude-opus-4-8": _ok_body("prose")})
    mod.reset_model_attribution()
    out, skipped, _ = mod.format_rp("hello there friend", provider="omniroute")
    assert skipped is False
    attr = mod.get_model_attribution()
    assert attr["model"] == "claude/claude-opus-4-8"
    assert attr["tier"] == 0
    assert attr["fallback"] is False
    assert attr["provider"] == "omniroute"


def test_attribution_records_fallback_tier(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    _install(
        monkeypatch,
        mod,
        {
            "claude/claude-opus-4-8": _err_body("no credentials"),
            "codex/gpt-5.5": _ok_body("prose from gpt"),
        },
    )
    mod.reset_model_attribution()
    out, skipped, _ = mod.format_rp("hello there friend", provider="omniroute")
    assert skipped is False
    attr = mod.get_model_attribution()
    assert attr["model"] == "codex/gpt-5.5"
    assert attr["tier"] == 1
    assert attr["fallback"] is True


def test_attribution_empty_on_total_failure(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    _install(
        monkeypatch,
        mod,
        {"claude/claude-opus-4-8": _err_body("internal explosion")},
    )
    mod.reset_model_attribution()
    out, skipped, _ = mod.format_rp("hello there friend", provider="omniroute")
    assert skipped is True
    attr = mod.get_model_attribution()
    # Hard failure never records a winning model.
    assert attr["model"] == ""


def test_run_pipeline_resets_stale_attribution(tmp_path, monkeypatch):
    """A pipeline run with no successful LLM step must not leak a model that a
    prior run recorded on this thread. vocab_correct is a pure pre-processor,
    so the slot stays empty after the run."""
    mod = _load_server(tmp_path, monkeypatch)
    mod.record_model_attribution("omniroute", "stale/model", tier=2)
    mode = {"id": "vocab_only", "pipeline": ["whisper", "vocab_correct"]}
    mod.run_pipeline("just some words", mode, provider="omniroute")
    assert mod.get_model_attribution()["model"] == ""
