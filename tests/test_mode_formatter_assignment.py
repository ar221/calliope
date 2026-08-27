"""Per-mode LLM assignment + per-provider fallback chains.

Covers:
- every provider now exposes a fallback chain (the OpenAI lane used to be a
  single pinned model with nowhere to fall back to),
- explicit model pins are allow-listed per provider rather than OmniRoute-only,
- a mode can pin its own model/provider, honoured by run_pipeline,
- a request-level model still wins over a mode's pin,
- save_mode_formatter validates and persists without deadlocking.

No real network: urllib.request.urlopen is monkeypatched throughout.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import uuid
from importlib.machinery import SourceFileLoader

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DICTATION_FORMATTER_PROVIDER", "omniroute")
    name = f"calliope_server_modefmt_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


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


# ── per-provider chains ───────────────────────────────────────────

def test_openai_lane_has_a_fallback_chain(tmp_path, monkeypatch):
    """Regression: the OpenAI lane was one pinned model with no fallback."""
    mod = _load_server(tmp_path, monkeypatch)
    chain = mod.formatter_model_chain("openai")
    assert len(chain) > 1, chain
    assert chain[0] == mod.config.OPENAI_RP_MODEL
    clean = mod.formatter_model_chain("openai", cleanup=True)
    assert len(clean) > 1, clean
    assert clean[0] == mod.config.OPENAI_CLEAN_MODEL


def test_openai_chain_keeps_an_operator_pin_first(tmp_path, monkeypatch):
    """A pinned DICTATION_OPENAI_RP_MODEL stays primary but gains fallbacks."""
    monkeypatch.setenv("DICTATION_OPENAI_RP_MODEL", "gpt-5.4")
    mod = _load_server(tmp_path, monkeypatch)
    chain = mod.formatter_model_chain("openai")
    assert chain[0] == "gpt-5.4"
    assert len(chain) > 1
    assert len(chain) == len(set(chain)), f"chain has duplicates: {chain}"


def test_openai_chain_falls_through_on_error(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    chain = mod.formatter_model_chain("openai")
    calls: list[str] = []
    _script(monkeypatch, mod, {
        chain[0]: _err("rate limit exceeded"),
        chain[1]: _ok("openai fallback output"),
    }, calls)

    out, skipped, reason = mod.format_rp("raw text here", provider="openai")
    assert skipped is False, reason
    assert out == "openai fallback output"
    assert calls == [chain[0], chain[1]]
    assert mod.get_model_attribution()["fallback"] is True


def test_claude_lane_is_still_a_single_model(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod.formatter_model_chain("claude") == [mod.config.CLAUDE_RP_MODEL]


def test_selectable_models_are_per_provider(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert "gpt-5.5" in mod.selectable_models("openai")
    assert "gpt-5.5" not in mod.selectable_models("omniroute")
    # An OpenAI model may now be pinned on its own lane...
    assert mod.formatter_model_chain("openai", model_override="gpt-5.5") == ["gpt-5.5"]
    # ...but not smuggled onto another provider's lane.
    assert mod.formatter_model_chain("omniroute", model_override="gpt-5.5") == []


def test_retired_antigravity_sonnet_id_is_gone(tmp_path, monkeypatch):
    """The antigravity lane only carries sonnet-4-6; that id was not in the catalog."""
    mod = _load_server(tmp_path, monkeypatch)
    assert "no-think/antigravity/claude-sonnet-5" not in mod.config.OMNIROUTE_RP_CHAIN
    assert "no-think/antigravity/claude-sonnet-5" not in mod.config.OMNIROUTE_SELECTABLE_MODELS
    assert "no-think/cc/claude-sonnet-5" in mod.config.OMNIROUTE_RP_CHAIN


# ── per-mode assignment resolution ────────────────────────────────

def test_mode_pin_is_used_when_request_has_no_model(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mode = {"id": "rp_enhance", "formatter_model": "no-think/cc/claude-opus-4-8"}
    assert mod.resolve_formatter_target(mode, "omniroute", "") == (
        "omniroute", "no-think/cc/claude-opus-4-8",
    )


def test_request_model_beats_mode_pin(tmp_path, monkeypatch):
    """The picker is a direct user choice, so it outranks stored config."""
    mod = _load_server(tmp_path, monkeypatch)
    mode = {"id": "rp_enhance", "formatter_model": "no-think/cc/claude-opus-4-8"}
    assert mod.resolve_formatter_target(mode, "omniroute", "codex/gpt-5.6-sol-high") == (
        "omniroute", "codex/gpt-5.6-sol-high",
    )


def test_mode_provider_pin_overrides_the_hardcoded_client_provider(tmp_path, monkeypatch):
    """Both clients send provider=omniroute, so a mode must be able to override it.

    Without this, a per-mode assignment could only ever name OmniRoute models.
    """
    mod = _load_server(tmp_path, monkeypatch)
    mode = {
        "id": "rp_enhance",
        "formatter_model": "gpt-5.5",
        "formatter_provider": "openai",
    }
    assert mod.resolve_formatter_target(mode, "omniroute", "") == ("openai", "gpt-5.5")


def test_unpinned_mode_leaves_provider_and_chain_alone(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod.resolve_formatter_target({"id": "rp_enhance"}, "omniroute", "") == (
        "omniroute", "",
    )


def test_run_pipeline_routes_a_pinned_mode_to_its_own_provider(tmp_path, monkeypatch):
    """End to end: a mode pinned to an OpenAI model must reach that model."""
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _script(monkeypatch, mod, {"gpt-5.5": _ok("routed to the pinned openai model")}, calls)

    mode = mod._normalize_mode({
        "id": "rp_enhance",
        "pipeline": ["whisper", "rp_enhance"],
        "formatter_model": "gpt-5.5",
        "formatter_provider": "openai",
    })
    out, skipped, reason, _cleaned = mod.run_pipeline(
        "she walks in and i say hello", mode, provider="omniroute",
    )
    assert skipped is False, reason
    assert out == "routed to the pinned openai model"
    assert calls == ["gpt-5.5"]
    assert mod.get_model_attribution()["provider"] == "openai"


# ── persistence ───────────────────────────────────────────────────

def test_save_mode_formatter_persists_and_survives_reload(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    view = mod.save_mode_formatter("rp_format", "no-think/cc/claude-opus-4-8", "omniroute")
    assert view["formatter_model"] == "no-think/cc/claude-opus-4-8"
    assert view["formatter_provider"] == "omniroute"

    mod._invalidate_modes_cache()
    reloaded = mod.get_mode("rp_format")
    assert reloaded["formatter_model"] == "no-think/cc/claude-opus-4-8"
    assert reloaded["formatter_provider"] == "omniroute"



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
    mod.run_pipeline("she walks in and i say hello", enhance, provider="omniroute")
    assert mod.get_model_attribution()["model"] == "codex/gpt-5.6-sol-high"

    # Same thread, now a mode with no formatter step.
    plain = mod._normalize_mode({"id": "plain", "pipeline": ["whisper"]})
    out, skipped, _reason, _cleaned = mod.run_pipeline("untouched", plain, provider="omniroute")
    assert out == "untouched"
    assert skipped is False
    assert mod.get_model_attribution()["model"] == "", "stale model leaked into a plain run"
    assert mod.get_model_attribution()["provider"] == ""


def test_save_mode_formatter_clears_the_pin(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod.save_mode_formatter("rp_format", "no-think/cc/claude-opus-4-8", "omniroute")
    view = mod.save_mode_formatter("rp_format", "", "")
    assert view["formatter_model"] == ""
    assert view["formatter_provider"] == ""
    mod._invalidate_modes_cache()
    assert mod.get_mode("rp_format")["formatter_model"] == ""


def test_save_mode_formatter_rejects_bad_input(tmp_path, monkeypatch):
    """A typo must not silently route a mode to the wrong LLM."""
    mod = _load_server(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="unknown mode"):
        mod.save_mode_formatter("rp_fromat", "no-think/cc/claude-opus-4-8", "omniroute")
    with pytest.raises(ValueError, match="not selectable"):
        mod.save_mode_formatter("rp_format", "totally/made-up", "omniroute")
    # gpt-5.5 is real, but not on the OmniRoute lane.
    with pytest.raises(ValueError, match="not selectable"):
        mod.save_mode_formatter("rp_format", "gpt-5.5", "omniroute")
    # 'plain' has no formatter step, so a pin could never apply.
    with pytest.raises(ValueError, match="no formatter step"):
        mod.save_mode_formatter("plain", "no-think/cc/claude-opus-4-8", "omniroute")


def test_save_mode_formatter_does_not_deadlock(tmp_path, monkeypatch):
    """It holds modes_lock while calling load_modes(), which retakes it."""
    import threading
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    assert isinstance(fmt.modes_lock, type(threading.RLock()))

    done = threading.Event()

    def run():
        mod.save_mode_formatter("rp_format", "codex/gpt-5.6-sol-high", "omniroute")
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
    # Option values carry the provider, since a model is meaningless without it.
    assert "`${provider}|${model}`" in source
    # The stale antigravity label must not linger in the picker.
    assert "no-think/antigravity/claude-sonnet-5" not in source
    assert "'no-think/cc/claude-sonnet-5': 'Sonnet 5'" in source


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
    assert "'no-think/cc/claude-sonnet-5': 'Sonnet 5'" in source


def test_modes_endpoint_exposes_the_pin(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod.save_mode_formatter("rp_enhance", "no-think/cc/claude-opus-4-6", "omniroute")
    view = mod.mode_public_view(mod.get_mode("rp_enhance"))
    assert view["formatter_model"] == "no-think/cc/claude-opus-4-6"
    assert view["formatter_provider"] == "omniroute"
    # Prompt internals still must not leak.
    assert "system_prompt" not in view
