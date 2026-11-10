"""Per-mode LLM assignment + OmniRoute fallback chains.

Covers:
- OmniRoute tiered fallback chains,
- explicit model pins are allow-listed catalog-qualified OmniRoute IDs,
- per-mode pins are honoured by run_pipeline,
- a request-level model still wins over a mode's pin,
- save_mode_formatter validates and persists without deadlocking.

No real network: urllib.request.urlopen is monkeypatched throughout.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from conftest import load_wrapper


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIROUTE_API_KEY", "test-key")
    return load_wrapper("calliope_server_modefmt")


def _formatter_module(mod):
    import sys
    return sys.modules[mod.format_rp.__module__]


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def _err(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}).encode()


def _script(monkeypatch, mod, script: dict[str, bytes], calls: list):
    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            return _FakeResp(b'{"data":[]}')
        model = json.loads(req.data.decode()).get("model", "")
        calls.append(model)
        return _FakeResp(script.get(model) or _err("model not found"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


# ── OmniRoute model chains ────────────────────────────────────────

def test_auto_chain_is_the_rp_chain(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod.formatter_model_chain() == mod.config.OMNIROUTE_RP_CHAIN
    assert mod.formatter_model_chain(cleanup=True) == mod.config.OMNIROUTE_CLEAN_CHAIN


def test_selectable_models_include_openrouter_and_xai_lanes(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    models = mod.selectable_models()
    for model in (
        "openrouter/google/gemini-3.5-flash",
        "openrouter/google/gemini-3.6-flash",
        "openrouter/moonshotai/kimi-k3",
        "xao/grok-4.6",
    ):
        assert model in models
        assert mod.formatter_model_chain(model_override=model) == [model]


def test_selectable_models_require_gateway_qualified_ids(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert "codex/gpt-5.6-luna" in mod.selectable_models()
    assert "gpt-5.5" not in mod.selectable_models()
    assert mod.formatter_model_chain(
        model_override="codex/gpt-5.6-luna"
    ) == ["codex/gpt-5.6-luna"]
    assert mod.formatter_model_chain(model_override="gpt-5.5") == []


def test_default_chain_uses_catalog_qualified_ids(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod.config.OMNIROUTE_RP_CHAIN == [
        "codex/gpt-5.6-sol-high",
        "cc/claude-sonnet-5",
        "claude/claude-opus-4-8",
    ]
    assert mod.config.OMNIROUTE_CLEAN_CHAIN == [
        "codex/gpt-5.6-sol-low",
        "claude/claude-sonnet-4-6",
    ]

# ── per-mode assignment resolution ────────────────────────────────

def test_mode_pin_is_used_when_request_has_no_model(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mode = {"id": "rp_enhance", "formatter_model": "claude/claude-opus-4-8"}
    assert mod.resolve_formatter_model(mode, "") == "claude/claude-opus-4-8"


def test_request_model_beats_mode_pin(tmp_path, monkeypatch):
    """The picker is a direct user choice, so it outranks stored config."""
    mod = _load_server(tmp_path, monkeypatch)
    mode = {"id": "rp_enhance", "formatter_model": "claude/claude-opus-4-8"}
    assert mod.resolve_formatter_model(mode, "codex/gpt-5.6-sol-high") == "codex/gpt-5.6-sol-high"


def test_unpinned_mode_uses_the_chain(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod.resolve_formatter_model({"id": "rp_enhance"}, "") == ""


def test_retired_mode_keys_are_ignored_and_not_rewritten(tmp_path, monkeypatch):
    """Modes files from earlier versions may carry `formatter_provider` and
    `preset`. They load fine, are dropped, and a save does not write them back."""
    mod = _load_server(tmp_path, monkeypatch)
    legacy = [dict(m) for m in mod.DEFAULT_MODES]
    for m in legacy:
        m["preset"] = "default"
        m["formatter_provider"] = "omniroute"
    mod.config.MODES_FILE.write_bytes(mod._serialize_config(legacy))
    mod._invalidate_modes_cache()

    mode = mod.get_mode("rp_format")
    assert "preset" not in mode
    assert "formatter_provider" not in mode

    mod.save_mode_formatter("rp_format", "claude/claude-opus-4-8")
    raw = mod.config.MODES_FILE.read_text()
    assert "formatter_provider" not in raw
    assert "preset" not in raw


def test_run_pipeline_routes_a_pinned_mode_through_omniroute(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _script(monkeypatch, mod, {
        "codex/gpt-5.6-luna": _ok("routed to the pinned OmniRoute model"),
    }, calls)

    mode = mod._normalize_mode({
        "id": "rp_enhance",
        "pipeline": ["whisper", "rp_enhance"],
        "formatter_model": "codex/gpt-5.6-luna",
    })
    out, skipped, reason, _cleaned = mod.run_pipeline(
        "she walks in and i say hello", mode,
    )
    assert skipped is False, reason
    assert out == "routed to the pinned OmniRoute model"
    assert calls == ["codex/gpt-5.6-luna"]
    assert mod.get_model_attribution()["model"] == "codex/gpt-5.6-luna"


# ── persistence ───────────────────────────────────────────────────

def test_save_mode_formatter_persists_and_survives_reload(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    view = mod.save_mode_formatter("rp_format", "claude/claude-opus-4-8")
    assert view["formatter_model"] == "claude/claude-opus-4-8"

    mod._invalidate_modes_cache()
    reloaded = mod.get_mode("rp_format")
    assert reloaded["formatter_model"] == "claude/claude-opus-4-8"


def test_plain_mode_does_not_report_a_previous_runs_model(tmp_path, monkeypatch):
    """Attribution is reset before the plain-mode early return.

    Regression: reset_model_attribution() ran *after* the `if not pipeline`
    return, so a plain run on a reused thread reported whatever model the
    previous run had recorded.
    """
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _script(monkeypatch, mod, {
        "codex/gpt-5.6-sol-high": _ok("formatted by the first run"),
    }, calls)

    enhance = mod._normalize_mode({"id": "rp_enhance", "pipeline": ["whisper", "rp_enhance"]})
    mod.run_pipeline("she walks in and i say hello", enhance)
    assert mod.get_model_attribution()["model"] == "codex/gpt-5.6-sol-high"

    # Same thread, now a mode with no formatter step.
    plain = mod._normalize_mode({"id": "plain", "pipeline": ["whisper"]})
    out, skipped, _reason, _cleaned = mod.run_pipeline("untouched", plain)
    assert out == "untouched"
    assert skipped is False
    assert mod.get_model_attribution()["model"] == "", "stale model leaked into a plain run"


def test_save_mode_formatter_clears_the_pin(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod.save_mode_formatter("rp_format", "claude/claude-opus-4-8")
    view = mod.save_mode_formatter("rp_format", "")
    assert view["formatter_model"] == ""
    mod._invalidate_modes_cache()
    assert mod.get_mode("rp_format")["formatter_model"] == ""


def test_save_mode_formatter_rejects_bad_input(tmp_path, monkeypatch):
    """A typo must not silently route a mode to the wrong LLM."""
    mod = _load_server(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="unknown mode"):
        mod.save_mode_formatter("rp_fromat", "claude/claude-opus-4-8")
    with pytest.raises(ValueError, match="not selectable"):
        mod.save_mode_formatter("rp_format", "totally/made-up")
    with pytest.raises(ValueError, match="not selectable"):
        mod.save_mode_formatter("rp_format", "gpt-5.5")
    # 'plain' has no formatter step, so a pin could never apply.
    with pytest.raises(ValueError, match="no formatter step"):
        mod.save_mode_formatter("plain", "claude/claude-opus-4-8")


def test_save_mode_formatter_does_not_deadlock(tmp_path, monkeypatch):
    """It holds modes_lock while calling load_modes(), which retakes it."""
    import threading
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    assert isinstance(fmt.modes_lock, type(threading.RLock()))

    done = threading.Event()

    def run():
        mod.save_mode_formatter("rp_format", "codex/gpt-5.6-sol-high")
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(timeout=5), "save_mode_formatter deadlocked"


EXT_SRC = pathlib.Path(__file__).resolve().parents[1] / "extension" / "index.js"


def test_extension_exposes_a_per_mode_model_control():
    source = EXT_SRC.read_text()

    assert 'id="dictation_bridge_mode_model"' in source
    assert "/modes/formatter" in source
    assert "function reflectModeModel()" in source
    assert "function populateModeModelOptions()" in source
    # Option values are bare model IDs; there is no provider grouping.
    assert "omniroute|" not in source
    assert "for (const [provider, models]" not in source
    assert "'cc/claude-sonnet-5': 'Sonnet 5'" in source


def test_extension_discloses_that_the_global_override_beats_mode_pins():
    """The global picker is persisted and sent every request, so a stale
    selection silently disables every per-mode assignment. That has to be
    visible in the UI, not just true in the server."""
    source = EXT_SRC.read_text()

    # Label must not imply the override is per-request.
    assert "Global model override (every mode)" in source
    assert "Formatter model (this request)" not in source
    assert "dictation_bridge_formatter_model_hint" in source
    assert "overrideHintEl" in source
    # The mode hint calls the pin inactive while an override is set.
    assert "Inactive: the global override" in source
    # Changing the override refreshes the hints instead of going stale.
    assert source.count("reflectModeModel();") >= 4
    assert "'cc/claude-sonnet-5': 'Sonnet 5'" in source


def test_modes_endpoint_exposes_the_pin(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod.save_mode_formatter("rp_enhance", "claude/claude-opus-4-8")
    view = mod.mode_public_view(mod.get_mode("rp_enhance"))
    assert view["formatter_model"] == "claude/claude-opus-4-8"
    assert "formatter_provider" not in view
    # Prompt internals still must not leak.
    assert "system_prompt" not in view
