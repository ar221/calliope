"""POL-1 `OOC:` convention and request-context privacy.

Plain mode must never send text to a provider, so an `OOC:` aside in plain
mode stays verbatim; in a formatter mode it is rerouted to grammar_clean and
the result carries `is_ooc` so clients re-prefix it.
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid
from importlib.machinery import SourceFileLoader

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIROUTE_API_KEY", "test-key")
    name = f"calliope_ooc_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_ooc_in_formatter_mode_reroutes_to_grammar_clean(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    text, mode, is_ooc = mod.resolve_ooc_override("OOC: brb two minutes", mod.get_mode("rp_enhance"))
    assert is_ooc is True
    assert mode["id"] == "grammar_clean"
    assert text == "brb two minutes"


def test_ooc_in_plain_mode_is_left_alone(tmp_path, monkeypatch):
    """Rerouting plain → grammar_clean would send text to OmniRoute from a
    mode whose contract is "no provider ever sees this"."""
    mod = _load_server(tmp_path, monkeypatch)
    plain = mod.get_mode("plain")
    text, mode, is_ooc = mod.resolve_ooc_override("OOC: brb two minutes", plain)
    assert is_ooc is False
    assert mode is plain
    assert text == "OOC: brb two minutes"
    assert not mod.mode_has_formatter_step(mode)


def test_no_ooc_prefix_is_a_noop(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    rp = mod.get_mode("rp_enhance")
    assert mod.resolve_ooc_override("we are out of character now", rp) == (
        "we are out of character now", rp, False,
    )


def test_context_query_param_is_redacted_from_request_lines(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    line = 'POST /transcribe?mode=rp_enhance&context=She%20whispered%20his%20name&language=en HTTP/1.1'
    assert mod._redact_sensitive_url_params(line) == (
        'POST /transcribe?mode=rp_enhance&context=[REDACTED]&language=en HTTP/1.1'
    )


def test_request_context_is_clamped_to_the_tail(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    limit = mod.REQUEST_CONTEXT_LIMIT
    text = "x" * limit + "TAIL"
    out = mod.clamp_request_context(text)
    assert len(out) == limit
    assert out.endswith("TAIL")
    assert mod.clamp_request_context("short") == "short"
