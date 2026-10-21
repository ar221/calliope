"""OmniRoute formatter model-chain fallback coverage.

Verifies:
- authenticated OpenAI-shape routing for OmniRoute,
- URL/model/payload builders use catalog-qualified model IDs,
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
    monkeypatch.setenv("OMNIROUTE_API_KEY", "test-key")
    monkeypatch.setenv(
        "DICTATION_OMNIROUTE_RP_CHAIN",
        "codex/gpt-5.6-sol-high, cc/claude-sonnet-5, claude/claude-opus-4-8",
    )
    monkeypatch.setenv(
        "DICTATION_OMNIROUTE_CLEAN_CHAIN",
        "codex/gpt-5.6-sol-low, claude/claude-sonnet-4-6",
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


# ── routing ───────────────────────────────────────────────────────

def test_requests_target_openai_chat_completions(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod.formatter_request_url().endswith("/v1/chat/completions")

def test_omniroute_requests_require_bearer_auth(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    requests = []

    def fake_urlopen(req, timeout=None):
        requests.append(req)
        return _FakeResp(_ok_body("formatted"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    out, skipped, reason = mod.format_rp("raw text here")
    assert skipped is False, reason
    assert out == "formatted"
    assert requests
    assert all(req.get_header("Authorization") == "Bearer test-key" for req in requests)


def test_missing_omniroute_key_fails_closed_before_network(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.config, "OMNIROUTE_API_KEY", "")
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network should not run")),
    )
    out, skipped, reason = mod.format_rp("raw text here")
    assert out == "raw text here"
    assert skipped is True
    assert "not reachable" in reason


def test_omniroute_model_chain(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    chain = mod.formatter_model_chain()
    assert chain[0] == "codex/gpt-5.6-sol-high"
    assert chain == [
        "codex/gpt-5.6-sol-high",
        "cc/claude-sonnet-5",
        "claude/claude-opus-4-8",
    ]
    clean = mod.formatter_model_chain(cleanup=True)
    assert clean == ["codex/gpt-5.6-sol-low", "claude/claude-sonnet-4-6"]


def test_selectable_model_override_is_single_omniroute_lane(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(
        mod.config,
        "OMNIROUTE_SELECTABLE_MODELS",
        ["claude/claude-opus-4-8", "codex/gpt-5.6-sol-high"],
    )
    assert mod.formatter_model_chain(
        model_override="claude/claude-opus-4-8"
    ) == ["claude/claude-opus-4-8"]
    assert mod.formatter_model_chain(
        model_override="codex/gpt-5.6-sol-high"
    ) == ["codex/gpt-5.6-sol-high"]
    assert mod.formatter_model_chain(
        model_override="untrusted/arbitrary-model"
    ) == []


def test_omniroute_payload_is_openai_messages(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    payload = mod.formatter_payload(
        system_prompt="SYS",
        user_content="hello",
        model="codex/gpt-5.6-sol-high",
        max_tokens=128,
        temperature=0.3,
    )
    assert payload["model"] == "codex/gpt-5.6-sol-high"
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
            "codex/gpt-5.6-sol-high": _err_body("no credentials for provider"),
            "cc/claude-sonnet-5": _ok_body("polished prose"),
        },
        calls,
    )
    out, skipped, reason = mod.format_rp("raw text here")
    assert out == "polished prose"
    assert skipped is False
    assert calls == ["codex/gpt-5.6-sol-high", "cc/claude-sonnet-5"]


def test_chain_falls_through_to_last_tier(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_scripted_urlopen(
        monkeypatch,
        mod,
        {
            "codex/gpt-5.6-sol-high": _err_body("model not found"),
            "cc/claude-sonnet-5": _err_body("rate limit exceeded"),
            "claude/claude-opus-4-8": _ok_body("opus output"),
        },
        calls,
    )
    out, skipped, _ = mod.format_rp("raw text here")
    assert out == "opus output"
    assert skipped is False
    assert calls == [
        "codex/gpt-5.6-sol-high",
        "cc/claude-sonnet-5",
        "claude/claude-opus-4-8",
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
            "codex/gpt-5.6-sol-high": _err_body("internal formatting explosion"),
            "cc/claude-sonnet-5": _ok_body("should not reach here"),
        },
        calls,
    )
    out, skipped, reason = mod.format_rp("raw text here")
    assert skipped is True
    assert out == "raw text here"
    assert calls == ["codex/gpt-5.6-sol-high"]


def test_disfluency_clean_uses_cleanup_chain(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []
    _install_scripted_urlopen(
        monkeypatch,
        mod,
        {
            "codex/gpt-5.6-sol-low": _err_body("no credentials"),
            "claude/claude-sonnet-4-6": _ok_body(
                "This is the cleaned up sentence with plenty of words to pass the length guard."
            ),
        },
        calls,
    )
    text = "so um like this is the uh the sentence i want cleaned up you know"
    out, cleaned, _ = mod.disfluency_clean(text)
    assert cleaned is True
    assert "cleaned up sentence" in out
    assert calls == ["codex/gpt-5.6-sol-low", "claude/claude-sonnet-4-6"]


def test_timeout_on_first_tier_falls_through_instead_of_returning_raw(tmp_path, monkeypatch):
    """A tier that times out must advance the chain.

    Regression: `socket.timeout` was not a chain-skip marker, so a slow
    reasoning-tier first model aborted the whole chain and /reformat returned
    unformatted text even on "OmniRoute Auto".
    """
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        method = getattr(req, "method", "GET") or "GET"
        if method == "GET":
            return _FakeResp(b"{}")
        model = json.loads(req.data.decode()).get("model", "")
        calls.append(model)
        if model == "codex/gpt-5.6-sol-high":
            raise TimeoutError("timed out")
        return _FakeResp(_ok_body("sonnet rescued the regenerate"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    out, skipped, reason = mod.format_rp("raw text here")
    assert skipped is False, reason
    assert out == "sonnet rescued the regenerate"
    assert calls == ["codex/gpt-5.6-sol-high", "cc/claude-sonnet-5"]


def test_timeout_markers_are_chain_skippable(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    for message in (
        "timed out",
        "The read operation timed out",
        "<urlopen error timed out>",
        "HTTP Error 504: Gateway Timeout",
    ):
        assert mod._is_chain_skippable_error(message) is True, message
    assert mod._is_chain_skippable_error("internal formatting explosion") is False


def test_chain_budget_stops_retrying_once_exhausted(tmp_path, monkeypatch):
    """Timeout fallback must not stack one full timeout per tier."""
    mod = _load_server(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.config, "FORMATTER_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(mod.config, "FORMATTER_CHAIN_BUDGET_SECONDS", 6.0)

    clock = {"t": 0.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        method = getattr(req, "method", "GET") or "GET"
        if method == "GET":
            return _FakeResp(b"{}")
        calls.append(json.loads(req.data.decode()).get("model", ""))
        # Every tier burns its whole per-attempt allowance, then times out.
        clock["t"] += timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    out, skipped, reason = mod.format_rp("raw text here")
    assert skipped is True
    assert out == "raw text here"
    # Tier 0 uses 5s of the 6s budget; tier 1 is clamped to the 1s remainder;
    # tier 2 never runs because the budget is gone.
    assert calls == ["codex/gpt-5.6-sol-high", "cc/claude-sonnet-5"]
    assert "budget" in reason


def test_explicit_model_override_reports_timeout_without_silent_fallback(tmp_path, monkeypatch):
    """An explicitly chosen model is a one-element chain: no substitution."""
    mod = _load_server(tmp_path, monkeypatch)
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        method = getattr(req, "method", "GET") or "GET"
        if method == "GET":
            return _FakeResp(b"{}")
        calls.append(json.loads(req.data.decode()).get("model", ""))
        raise TimeoutError("timed out")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    out, skipped, reason = mod.format_rp(
        "raw text here",
        model_override="claude/claude-opus-4-8",
    )
    assert skipped is True
    assert out == "raw text here"
    assert calls == ["claude/claude-opus-4-8"]
    assert "timed out" in reason


def _formatter_module(mod):
    """The real formatter module. `calliope-server` only re-exports its names,
    so patching the server module would not affect format_rp's own globals."""
    import sys
    return sys.modules[mod.format_rp.__module__]


def _stub_health_only(monkeypatch, mod):
    """Serve the health probe; make any real POST an explicit failure."""

    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            return _FakeResp(b"{}")
        raise AssertionError("test leaked a real formatter POST")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


def test_streaming_timeout_after_partial_tokens_is_not_reported_as_success(tmp_path, monkeypatch):
    """Partial stream + error must not pass truncated prose off as clean output.

    The error is checked before the buffered text, so a mid-answer timeout
    keeps the raw text canonical and flags the truncation instead of silently
    accepting half a rewrite.
    """
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    _stub_health_only(monkeypatch, mod)
    emitted: list[dict] = []
    monkeypatch.setattr(
        fmt.events, "broadcast_event", lambda name, data: emitted.append({name: data})
    )
    monkeypatch.setattr(
        fmt, "_stream_formatter",
        lambda url, payload, on_delta, timeout=None, deadline=None: (
            "half a rewri", "timed out"
        ),
    )

    out, skipped, reason = mod.format_rp(
        "raw text here", request_id="req-1"
    )
    assert skipped is True
    assert out == "raw text here"
    assert "truncated" in reason
    # The client must still be told the token stream ended.
    assert any(
        "dictation-token" in e and e["dictation-token"].get("done") is True for e in emitted
    )


def test_streaming_timeout_with_no_tokens_falls_through_to_next_tier(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    _stub_health_only(monkeypatch, mod)
    monkeypatch.setattr(fmt.events, "broadcast_event", lambda name, data: None)
    seen: list[str] = []

    def fake_stream(url, payload, on_delta, timeout=None, deadline=None):
        model = json.loads(payload.decode()).get("model", "")
        seen.append(model)
        if model == "codex/gpt-5.6-sol-high":
            return "", "timed out"
        return "streamed sonnet output", ""

    monkeypatch.setattr(fmt, "_stream_formatter", fake_stream)

    out, skipped, reason = mod.format_rp(
        "raw text here", request_id="req-2"
    )
    assert skipped is False, reason
    assert out == "streamed sonnet output"
    assert seen == ["codex/gpt-5.6-sol-high", "cc/claude-sonnet-5"]


def test_probe_tolerates_cold_health_endpoint_latency(tmp_path, monkeypatch):
    """A 3s cold /v1/models must not be reported as an offline proxy.

    Regression: format_rp probed with a hard-coded timeout=2 while OmniRoute's
    catalog endpoint takes >3s cold, so the first regenerate after an idle
    period returned raw text without trying a single model.
    """
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    fmt.reset_probe_cache()
    assert mod.config.FORMATTER_PROBE_TIMEOUT_SECONDS >= 3.0

    probes: list[float] = []

    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            probes.append(timeout)
            if timeout < 3.0:                      # emulate the cold catalog build
                raise TimeoutError("timed out")
            return _FakeResp(b'{"data":[]}')
        return _FakeResp(_ok_body("formatted output"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    out, skipped, reason = mod.format_rp("raw text here")
    assert skipped is False, reason
    assert out == "formatted output"
    assert probes and probes[0] >= 3.0, f"probe used too short a timeout: {probes}"


def test_probe_result_is_cached_across_calls(tmp_path, monkeypatch):
    """One dictation must not pay for a fresh catalog probe per formatter step."""
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    fmt.reset_probe_cache()
    monkeypatch.setattr(mod.config, "FORMATTER_PROBE_TTL_SECONDS", 30.0)
    probes = {"n": 0}

    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            probes["n"] += 1
            return _FakeResp(b'{"data":[]}')
        return _FakeResp(_ok_body("formatted output"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    for _ in range(3):
        assert mod.format_rp("raw text here")[1] is False
    assert probes["n"] == 1, f"expected one cached probe, got {probes['n']}"


def test_probe_failure_is_retried_quickly(tmp_path, monkeypatch):
    """A failed probe must not be cached for the full success TTL."""
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    fmt.reset_probe_cache()
    assert (
        mod.config.FORMATTER_PROBE_FAIL_TTL_SECONDS
        < mod.config.FORMATTER_PROBE_TTL_SECONDS
    )

    clock = {"t": 0.0}
    monkeypatch.setattr(fmt.time, "monotonic", lambda: clock["t"])
    state = {"up": False}

    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            if not state["up"]:
                raise ConnectionRefusedError("connection refused")
            return _FakeResp(b'{"data":[]}')
        return _FakeResp(_ok_body("formatted output"))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    assert mod.format_rp("raw text here")[1] is True
    state["up"] = True
    clock["t"] += mod.config.FORMATTER_PROBE_FAIL_TTL_SECONDS + 0.1
    assert mod.format_rp("raw text here")[1] is False


def test_failed_probe_verdict_is_cached_even_though_the_probe_burns_its_timeout(
    tmp_path, monkeypatch
):
    """A failing probe must not be re-run on every request.

    Regression: expiry was computed from the time the probe STARTED. Since a
    failing probe burns its full timeout (6s) and the failure TTL is shorter
    (3s), the stored verdict was already expired, so a down proxy made every
    single request pay another full probe.
    """
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    fmt.reset_probe_cache()

    clock = {"t": 1000.0}
    monkeypatch.setattr(fmt.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(mod.config, "FORMATTER_PROBE_TIMEOUT_SECONDS", 6.0)
    monkeypatch.setattr(mod.config, "FORMATTER_PROBE_FAIL_TTL_SECONDS", 3.0)
    probes = {"n": 0}

    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            probes["n"] += 1
            clock["t"] += timeout      # a timing-out probe consumes its budget
            raise TimeoutError("timed out")
        raise AssertionError("proxy is down; no formatter POST expected")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    assert mod.format_rp("raw text here")[1] is True
    assert probes["n"] == 1

    # Immediately afterwards the cached failure must still be honoured.
    assert mod.format_rp("raw text here")[1] is True
    assert probes["n"] == 1, "failed verdict expired on arrival — cache is useless"

    # Once the failure TTL really elapses, probing resumes.
    clock["t"] += 3.1
    assert mod.format_rp("raw text here")[1] is True
    assert probes["n"] == 2


def test_stream_deadline_stops_consuming_a_trickling_stream(tmp_path, monkeypatch):
    """A stream that stays alive but never finishes must not be consumed forever.

    Each SSE line arrives inside the socket timeout, so `timeout` alone cannot
    stop it. `_stream_formatter` checks the chain deadline between events.
    """
    mod = _load_server(tmp_path, monkeypatch)
    fmt = _formatter_module(mod)
    fmt.reset_probe_cache()
    clock = {"t": 500.0}
    monkeypatch.setattr(fmt.time, "monotonic", lambda: clock["t"])

    class _TricklingResp:
        """Heartbeats indefinitely, advancing the clock on each line.

        Hard-capped so a regression in the deadline check fails the test instead
        of hanging the suite forever.
        """

        def __init__(self):
            self.lines = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.lines += 1
            if self.lines > 1000:
                raise AssertionError("deadline was never enforced; stream ran away")
            clock["t"] += 1.0
            return b": heartbeat\n"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        if (getattr(req, "method", "GET") or "GET") == "GET":
            return _FakeResp(b'{"data":[]}')
        return _TricklingResp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    text, err = fmt._stream_formatter(
        "http://stub/v1/chat/completions", b"{}",
        lambda d: None, timeout=18.0, deadline=clock["t"] + 5.0,
    )
    assert text == ""
    assert "time budget" in err
    # Without the deadline check this loop would never terminate.
    assert clock["t"] <= 512.0, f"kept reading past the deadline: {clock['t']}"


def test_rp_enhance_prompt_locks_charged_and_comic_wording(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    prompt = mod.RP_SYSTEM_ENHANCE
    assert "CHARGED WORDS ARE LOCKED" in prompt
    assert "appear verbatim" in prompt
    assert "profanity, slurs, nicknames, insults, punchlines" in prompt
    assert "lexical fidelity wins" in prompt
