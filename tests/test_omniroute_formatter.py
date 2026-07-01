"""OmniRoute formatter provider + model-chain fallback coverage.

Verifies:
- provider normalization + OpenAI-shape routing for omniroute,
- URL/model/payload builders treat omniroute as OpenAI-compatible,
- the model chain skips credential/routing errors and falls through,
- a hard (non-skippable) error stops the chain and returns raw text,
- disfluency_clean shares the same chain-fallback behavior.

No real network: urllib.request.urlopen is monkeypatched with a fake that
serves scripted per-model responses.
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
    monkeypatch.setenv(
        "DICTATION_OMNIROUTE_CLEAN_CHAIN",
        "codex/gpt-5.4, claude/claude-sonnet-4-6",
    )
    name = f"calliope_server_omni_{uuid.uuid4().hex}"
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


def _install_scripted_urlopen(monkeypatch, mod, script: dict[str, bytes], calls: list):
    """Route urlopen by the request payload's `model` field. `script` maps
    model -> response body bytes. Health probes (GET) return an empty 200."""

    def fake_urlopen(req, timeout=None):
        method = getattr(req, "method", "GET") or "GET"
        if method == "GET":
            return _FakeResp(b"{}")
        payload = json.loads(req.data.decode())
        model = payload.get("model", "")
        calls.append(model)
        body = script.get(model)
        if body is None:
            body = _err_body("model not found")
        return _FakeResp(body)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


# ── provider abstraction ──────────────────────────────────────────

def test_omniroute_is_default_and_openai_shape(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod.DEFAULT_FORMATTER_PROVIDER == "omniroute"
    assert mod.normalize_formatter_provider("omniroute") == "omniroute"
    assert mod.normalize_formatter_provider("garbage") == "omniroute"
    assert mod._is_openai_shape("omniroute") is True
    assert mod.formatter_request_url("omniroute").endswith("/v1/chat/completions")


def test_omniroute_model_chain(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    chain = mod.formatter_model_chain("omniroute")
    assert chain[0] == "claude/claude-opus-4-8"
    assert chain == [
        "claude/claude-opus-4-8",
        "codex/gpt-5.5",
        "claude/claude-sonnet-4-6",
    ]
    clean = mod.formatter_model_chain("omniroute", cleanup=True)
    assert clean == ["codex/gpt-5.4", "claude/claude-sonnet-4-6"]
    # Non-omniroute providers collapse to a single-element chain.
    assert len(mod.formatter_model_chain("claude")) == 1


def test_omniroute_payload_is_openai_messages(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    payload = mod.formatter_payload(
        "omniroute",
        system_prompt="SYS",
        user_content="hello",
        model="codex/gpt-5.5",
        max_tokens=128,
        temperature=0.3,
    )
    assert payload["model"] == "codex/gpt-5.5"
    assert payload["messages"][0] == {"role": "system", "content": "SYS"}
    assert payload["messages"][1] == {"role": "user", "content": "hello"}
    assert payload["temperature"] == 0.3


# ── chain fallback ────────────────────────────────────────────────

def test_chain_skips_credential_error_to_next_tier(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_scripted_urlopen(
        monkeypatch,
        mod,
        {
            "claude/claude-opus-4-8": _err_body("no credentials for provider"),
            "codex/gpt-5.5": _ok_body("polished prose"),
        },
        calls,
    )
    out, skipped, reason = mod.format_rp("raw text here", provider="omniroute")
    assert out == "polished prose"
    assert skipped is False
    assert calls == ["claude/claude-opus-4-8", "codex/gpt-5.5"]


def test_chain_falls_through_to_last_tier(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_scripted_urlopen(
        monkeypatch,
        mod,
        {
            "claude/claude-opus-4-8": _err_body("model not found"),
            "codex/gpt-5.5": _err_body("rate limit exceeded"),
            "claude/claude-sonnet-4-6": _ok_body("sonnet output"),
        },
        calls,
    )
    out, skipped, _ = mod.format_rp("raw text here", provider="omniroute")
    assert out == "sonnet output"
    assert skipped is False
    assert calls == [
        "claude/claude-opus-4-8",
        "codex/gpt-5.5",
        "claude/claude-sonnet-4-6",
    ]


def test_hard_error_stops_chain(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_scripted_urlopen(
        monkeypatch,
        mod,
        {
            # A non-skippable error (e.g. a 500-style bad-request) should abort
            # rather than burn the rest of the chain.
            "claude/claude-opus-4-8": _err_body("internal formatting explosion"),
            "codex/gpt-5.5": _ok_body("should not reach here"),
        },
        calls,
    )
    out, skipped, reason = mod.format_rp("raw text here", provider="omniroute")
    assert skipped is True
    assert out == "raw text here"
    assert calls == ["claude/claude-opus-4-8"]


def test_disfluency_clean_uses_cleanup_chain(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_scripted_urlopen(
        monkeypatch,
        mod,
        {
            "codex/gpt-5.4": _err_body("no credentials"),
            "claude/claude-sonnet-4-6": _ok_body(
                "This is the cleaned up sentence with plenty of words to pass the length guard."
            ),
        },
        calls,
    )
    text = "so um like this is the uh the sentence i want cleaned up you know"
    out, cleaned, _ = mod.disfluency_clean(text, provider="omniroute")
    assert cleaned is True
    assert "cleaned up sentence" in out
    assert calls == ["codex/gpt-5.4", "claude/claude-sonnet-4-6"]
