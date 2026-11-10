"""Behavioural coverage for the 2026-11-10 formatter audit fixes.

Synthetic data only: every upstream call goes to a fake `urlopen`, ST data
lives under tmp_path, and no real HOME/network/service is touched.
"""
from __future__ import annotations

import base64
import importlib
import io
import json
import logging
import pathlib
import socket
import sys
import time
import urllib.error

import pytest

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


# ─── helpers ─────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body: bytes = b"", lines: list[bytes] | None = None):
        self._body = body
        self._lines = lines or []

    def read(self, *_a):
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok(text: str, finish: str | None = "stop") -> bytes:
    choice = {"message": {"content": text}}
    if finish is not None:
        choice["finish_reason"] = finish
    return json.dumps({"choices": [choice]}).encode()


def _sse(*deltas: str, finish: str | None = "stop", done: bool = True) -> list[bytes]:
    lines = [
        b"data: " + json.dumps({"choices": [{"delta": {"content": d}}]}).encode() + b"\n"
        for d in deltas
    ]
    if finish:
        lines.append(b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}).encode() + b"\n")
    if done:
        lines.append(b"data: [DONE]\n")
    return lines


def _http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://stub/v1/chat/completions", code, "err", {}, io.BytesIO(body))


def _png_card(path: pathlib.Path, card: object) -> None:
    payload = b"chara\x00" + base64.b64encode(json.dumps(card).encode())
    chunk = len(payload).to_bytes(4, "big") + b"tEXt" + payload + b"\x00\x00\x00\x00"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)


def _mode(fmt, mode_id: str, **overrides) -> dict:
    mode = dict(next(m for m in fmt.DEFAULT_MODES if m["id"] == mode_id))
    mode.update(overrides)
    return mode


@pytest.fixture
def fmt(monkeypatch, tmp_path):
    module = importlib.import_module("calliope_server.formatter")
    cfg = module.config
    monkeypatch.setattr(cfg, "OMNIROUTE_API_KEY", "test")
    monkeypatch.setattr(cfg, "OMNIROUTE_RP_CHAIN", ["rp/one", "rp/two", "rp/three"])
    monkeypatch.setattr(cfg, "OMNIROUTE_CLEAN_CHAIN", ["clean/one", "clean/two"])
    monkeypatch.setattr(cfg, "OMNIROUTE_SELECTABLE_MODELS", ["pin/model"])
    for name in ("PERSONAS_DIR", "CHARACTERS_DIR", "ST_CHATS_DIR", "ST_GROUPS_DIR", "ST_GROUP_CHATS_DIR"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(cfg, name, d)
    monkeypatch.setattr(cfg, "ST_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(cfg, "VOICE_MACROS_FILE", tmp_path / "voice_macros.json")
    monkeypatch.setattr(cfg, "VOCAB_FILE", tmp_path / "vocab.json")
    module._invalidate_voice_macros_cache()
    module._invalidate_vocab_cache()
    module.reset_gateway_offline_cache()
    saved_state = dict(module.st_state)
    module.session_transcript.clear()
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(module.events, "broadcast_event", lambda name, data: events.append((name, data)))
    module._test_events = events
    yield module
    module.st_state.clear()
    module.st_state.update(saved_state)
    module.session_transcript.clear()
    module.reset_gateway_offline_cache()


def _capture(monkeypatch, fmt, respond):
    """Install a fake urlopen; `respond(payload, timeout)` returns a _Resp or raises."""
    calls: list[dict] = []

    def fake_urlopen(req, timeout=None):
        assert req.get_method() == "POST", "no preflight GET may run"
        payload = json.loads(req.data.decode())
        calls.append({"payload": payload, "timeout": timeout})
        return respond(payload, timeout)

    monkeypatch.setattr(fmt.urllib.request, "urlopen", fake_urlopen)
    return calls


def _done_tokens(fmt, request_id):
    return [d for n, d in fmt._test_events if n == "dictation-token" and d["requestId"] == request_id and d["done"]]


# ─── 1. FS-01: no dictated text in logs ─────────────────────────────

def test_command_dispatch_log_has_intent_and_length_only(fmt, caplog):
    caplog.set_level(logging.DEBUG, logger="dictation-server")
    out, skipped, reason, _ = fmt.run_pipeline("Computer, append PRIVATE_SECRET words", _mode(fmt, "plain"))
    assert out == "PRIVATE_SECRET words"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "intent=append" in logged and "residual_len=20" in logged
    assert "PRIVATE_SECRET" not in logged


def test_hallucination_drop_log_has_no_text(fmt, caplog):
    caplog.set_level(logging.DEBUG, logger="dictation-server")
    out, *_ = fmt.run_pipeline("Thanks for watching", _mode(fmt, "plain"))
    assert out == ""
    assert "watching" not in "\n".join(r.getMessage() for r in caplog.records).lower()


# ─── 2. chat-scoped transcript + reformat ───────────────────────────

def test_transcript_context_is_chat_scoped_and_bounded(fmt, monkeypatch):
    monkeypatch.setattr(fmt.config, "TRANSCRIPT_CONTEXT_LIMIT", 200)
    fmt.session_transcript.extend([
        {"role": "user", "text": "OTHER_CHAT_LINE", "chat_id": "chat-b"},
        *({"role": "user", "text": f"line {i} " + "x" * 40, "chat_id": "chat-a"} for i in range(50)),
    ])
    ctx = fmt.build_transcript_context("chat-a")
    assert "OTHER_CHAT_LINE" not in ctx
    assert "line 49" in ctx and "line 0 " not in ctx
    assert len(ctx) <= 200 + len("...\n")
    assert "OTHER_CHAT_LINE" in fmt.build_transcript_context("chat-b")
    assert fmt.build_transcript_context("") == ""


def test_run_pipeline_sends_only_current_chat_history(fmt, monkeypatch):
    fmt.session_transcript.extend([
        {"role": "user", "text": "BLEED_FROM_B", "chat_id": "chat-b"},
        {"role": "user", "text": "MINE_FROM_A", "chat_id": "chat-a"},
    ])
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("formatted")))
    out, skipped, _, _ = fmt.run_pipeline("I wave.", _mode(fmt, "rp_enhance"), chat_id="chat-a")
    assert (out, skipped) == ("formatted", False)
    sent = json.dumps(calls[0]["payload"])
    assert "MINE_FROM_A" in sent and "BLEED_FROM_B" not in sent


def test_reformat_skips_command_dispatch_and_hallucination_filter(fmt, monkeypatch):
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("Computer, send it now.")))
    out, skipped, _, _ = fmt.run_pipeline("Computer, send.", _mode(fmt, "grammar_clean"), reformat=True)
    assert (out, skipped) == ("Computer, send it now.", False)
    assert len(calls) == 1
    assert not [n for n, _ in fmt._test_events if n == "dictation-command"]
    # Without reformat the same text is a Send command and never reaches a model.
    out, *_ = fmt.run_pipeline("Computer, send.", _mode(fmt, "grammar_clean"))
    assert out == "" and len(calls) == 1


# ─── 3. command grammar ─────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Computer, send me the files",
    "Computer, delete that photo from the trip",
    "computer swipe leftover cake onto the plate",
    "Computer, regenerate the whole idea, she said.",
])
def test_destructive_command_prefix_is_ordinary_dictation(fmt, text):
    assert fmt.command_dispatch(text) == (text, None)


@pytest.mark.parametrize("text,intent,args", [
    ("Computer, send.", "send", {}),
    ("Computer, swipe, left.", "swipe", {"direction": "left"}),
    ("Hey, computer: swipe right!", "swipe", {"direction": "right"}),
    ("computer delete-that", "delete_last", {}),
])
def test_whole_utterance_commands_match_punctuation_tolerantly(fmt, text, intent, args):
    residual, cmd = fmt.command_dispatch(text)
    assert residual == ""
    assert cmd["intent"] == intent and cmd["args"] == args


def test_mixed_intents_keep_residual(fmt):
    residual, cmd = fmt.command_dispatch("Computer, replace: she smiles.")
    assert cmd["intent"] == "replace" and residual == "she smiles."


def test_ooc_with_comma_is_recognised(fmt):
    text, mode, is_ooc = fmt.resolve_ooc_override("OOC, pause the scene", _mode(fmt, "rp_enhance"))
    assert is_ooc and text == "pause the scene" and mode["id"] == "grammar_clean"


# ─── 4. upstream failure classification / stream completion ────────

def test_http_429_and_503_advance_but_400_aborts(fmt, monkeypatch):
    script = {"rp/one": _http_error(429), "rp/two": _http_error(503)}

    def respond(p, t):
        if p["model"] in script:
            raise script[p["model"]]
        return _Resp(_ok("third tier"))

    calls = _capture(monkeypatch, fmt, respond)
    out, skipped, _ = fmt.format_rp("raw")
    assert (out, skipped) == ("third tier", False)
    assert [c["payload"]["model"] for c in calls] == ["rp/one", "rp/two", "rp/three"]
    assert fmt.get_model_attribution()["tier"] == 2

    body = json.dumps({"error": {"message": "max_tokens invalid"}}).encode()
    calls = _capture(monkeypatch, fmt, lambda p, t: (_ for _ in ()).throw(_http_error(400, body)))
    out, skipped, reason = fmt.format_rp("raw")
    assert (out, skipped) == ("raw", True)
    assert len(calls) == 1 and "HTTP 400" in reason and "max_tokens invalid" in reason


def test_malformed_and_truncated_responses_advance(fmt, monkeypatch):
    replies = {"rp/one": _Resp(b"<html>oops"), "rp/two": _Resp(_ok("half", finish="length"))}
    calls = _capture(monkeypatch, fmt, lambda p, t: replies.get(p["model"]) or _Resp(_ok("whole")))
    assert fmt.format_rp("raw")[:2] == ("whole", False)
    assert len(calls) == 3

    monkeypatch.setattr(fmt.config, "OMNIROUTE_RP_CHAIN", ["rp/one"])
    _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("cut off", finish="content_filter")))
    out, skipped, reason = fmt.format_rp("raw")
    assert (out, skipped) == ("raw", True) and "truncated" in reason


def test_refused_connection_marks_gateway_offline_without_retrying_tiers(fmt, monkeypatch):
    calls = _capture(monkeypatch, fmt, lambda p, t: (_ for _ in ()).throw(
        urllib.error.URLError(ConnectionRefusedError(111, "refused"))))
    out, skipped, reason = fmt.format_rp("raw")
    assert skipped and "offline" in reason and len(calls) == 1
    # Negative cache: the next request does not touch the network.
    assert fmt.format_rp("raw")[1] is True and len(calls) == 1
    fmt.reset_gateway_offline_cache()
    _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("back")))
    assert fmt.format_rp("raw")[:2] == ("back", False)


def test_timeout_advances_without_marking_offline(fmt, monkeypatch):
    def respond(p, t):
        if p["model"] == "rp/one":
            raise urllib.error.URLError(socket.timeout("timed out"))
        return _Resp(_ok("tier two"))

    _capture(monkeypatch, fmt, respond)
    assert fmt.format_rp("raw")[:2] == ("tier two", False)
    assert not fmt._gateway_known_offline()


def test_stream_without_completion_marker_is_not_success(fmt, monkeypatch):
    monkeypatch.setattr(fmt.config, "OMNIROUTE_RP_CHAIN", ["rp/one"])
    _capture(monkeypatch, fmt, lambda p, t: _Resp(lines=_sse("partial prose", finish=None, done=False)))
    out, skipped, reason = fmt.format_rp("raw", mode=2, request_id="r1")
    assert (out, skipped) == ("raw", True) and "truncated" in reason
    assert len(_done_tokens(fmt, "r1")) == 1


def test_stream_length_finish_with_no_visible_text_advances(fmt, monkeypatch):
    replies = {"rp/one": _sse(finish="length", done=False)}
    _capture(monkeypatch, fmt, lambda p, t: _Resp(lines=replies.get(p["model"]) or _sse("full ", "answer")))
    out, skipped, _ = fmt.format_rp("raw", mode=2, request_id="r2")
    assert (out, skipped) == ("full answer", False)
    assert len(_done_tokens(fmt, "r2")) == 1


def test_budget_exhaustion_still_emits_done_token(fmt, monkeypatch):
    monkeypatch.setattr(fmt.config, "FORMATTER_CHAIN_BUDGET_SECONDS", 0.0)
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(lines=_sse("x")))
    out, skipped, reason = fmt.format_rp("raw", mode=2, request_id="r3")
    assert skipped and "timed out" in reason and not calls
    assert len(_done_tokens(fmt, "r3")) == 1


def test_cleanup_chain_shares_one_deadline(fmt, monkeypatch):
    clock = {"t": 100.0}
    monkeypatch.setattr(fmt.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(fmt.config, "DISFLUENCY_CLEAN_TIMEOUT", 3.0)
    monkeypatch.setattr(fmt.config, "DISFLUENCY_CLEAN_CHAIN_BUDGET_SECONDS", 4.0)

    def respond(p, t):
        clock["t"] += t
        raise TimeoutError("timed out")

    calls = _capture(monkeypatch, fmt, respond)
    text = "um so like I walk over and uh pick up the cup and then I put it down again"
    out, cleaned, reason = fmt.disfluency_clean(text)
    assert (out, cleaned) == (text, False)
    assert [c["timeout"] for c in calls] == [3.0, 1.0]


# ─── 5. context-flag enforcement ────────────────────────────────────

def test_rp_format_omits_last_ai_without_chat_context_flag(fmt, monkeypatch):
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("ok")))
    fmt.run_pipeline("I nod.", _mode(fmt, "rp_format"), context="LAST_AI_SECRET")
    assert "LAST_AI_SECRET" not in json.dumps(calls[0]["payload"])
    fmt.run_pipeline("I nod.", _mode(fmt, "rp_format", use_chat_context=True), context="LAST_AI_SECRET")
    assert "LAST_AI_SECRET" in json.dumps(calls[1]["payload"])


def _persona_and_card(fmt):
    (fmt.config.PERSONAS_DIR / "rashid.md").write_text("# Lord Rashid\nPERSONA_DESC", encoding="utf-8")
    _png_card(fmt.config.CHARACTERS_DIR / "Elara.png", {"data": {"name": "Elara", "description": "CARD_DESC"}})


def test_persona_pov_honours_flags_and_parses_card_once(fmt, monkeypatch):
    _persona_and_card(fmt)
    st = importlib.import_module("calliope_server.sillytavern")
    parses = {"n": 0}
    real = st._parse_character_png

    def counting(path):
        parses["n"] += 1
        return real(path)

    monkeypatch.setattr(st, "_parse_character_png", counting)
    st._parse_cache.clear()
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("rewritten")))
    mode = _mode(fmt, "persona_pov", pipeline=["persona_pov"])
    out, skipped, _, _ = fmt.run_pipeline(
        "I bow.", mode, persona_id="rashid", character_id="elara",
        chat_context="CHAT_HISTORY", context="LAST_AI",
    )
    assert (out, skipped) == ("rewritten", False)
    sent = json.dumps(calls[0]["payload"])
    assert "PERSONA_DESC" in sent and "CARD_DESC" in sent and "CHAT_HISTORY" in sent
    assert parses["n"] == 1

    no_chat = dict(mode, use_chat_context=False)
    fmt.run_pipeline("I bow.", no_chat, persona_id="rashid", character_id="elara",
                     chat_context="CHAT_HISTORY", context="LAST_AI")
    sent = json.dumps(calls[1]["payload"])
    assert "CHAT_HISTORY" not in sent and "LAST_AI" not in sent

    no_persona = dict(mode, use_persona=False, use_character=False)
    out, skipped, _, _ = fmt.run_pipeline("I bow.", no_persona, persona_id="rashid", character_id="elara")
    sent = json.dumps(calls[2]["payload"])
    assert "PERSONA_DESC" not in sent and "CARD_DESC" not in sent
    # A2: a successful honest fallback is not reported as skipped.
    assert (out, skipped) == ("rewritten", False)


def _fresh_state(fmt, **fields):
    fmt.st_state.update({
        "chatId": "chat-a", "chatType": "individual", "characterId": "Alice.png",
        "characterName": "Alice", "personaId": "", "sceneContinuity": "SECRET_SCENE",
        "lastUpdated": time.time(), **fields,
    })


def test_scene_contract_only_from_matching_fresh_state(fmt, monkeypatch):
    _fresh_state(fmt)
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("ok")))
    mode = _mode(fmt, "rp_enhance")
    fmt.run_pipeline("I wave.", mode, character_id="alice", chat_id="chat-a")
    sent = json.dumps(calls[-1]["payload"])
    assert sent.count("SECRET_SCENE") == 1  # P5: contract sent once
    assert sent.count("SCENE CONTRACT") == 1

    fmt.run_pipeline("I wave.", mode, character_id="bob")  # other character
    fmt.run_pipeline("I wave.", mode)  # manual request, no identity
    fmt.run_pipeline("I wave.", mode, character_id="alice", chat_id="chat-zzz")  # other chat
    for call in calls[1:]:
        assert "SECRET_SCENE" not in json.dumps(call["payload"])
        assert "Alice" not in json.dumps(call["payload"])

    _fresh_state(fmt, lastUpdated=time.time() - 3600)  # dead state
    fmt.run_pipeline("I wave.", mode, character_id="alice", chat_id="chat-a")
    assert "SECRET_SCENE" not in json.dumps(calls[-1]["payload"])


def test_disfluency_clean_counts_as_provider_step(fmt):
    cleanup_only = {"id": "c", "pipeline": ["whisper", "disfluency_clean"]}
    assert fmt.mode_has_formatter_step(cleanup_only)
    out, skipped, reason, _ = fmt.run_pipeline("too short", cleanup_only)
    assert skipped and reason == "input too short"


# ─── 6. grammar_clean chain ─────────────────────────────────────────

def test_grammar_clean_uses_cleanup_chain_unless_pinned(fmt, monkeypatch):
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("Clean.")))
    fmt.run_pipeline("clean", _mode(fmt, "grammar_clean"))
    fmt.run_pipeline("clean", _mode(fmt, "grammar_clean"), formatter_model="pin/model")
    assert [c["payload"]["model"] for c in calls] == ["clean/one", "pin/model"]


# ─── 7. reasoning filter ────────────────────────────────────────────

def test_stream_filter_drops_think_and_thinking_any_case(fmt):
    f = fmt._ThinkingTagFilter()
    chunks = ["Hi <TH", "INK>secret", " stuff</th", "ink> there <thinking>more</THINKING>!"]
    assert "".join(f.feed(c) for c in chunks) + f.flush() == "Hi  there !"
    g = fmt._ThinkingTagFilter()
    assert g.feed("ok <think>never closed") + g.flush() == "ok "


def test_batch_strip_handles_think_tags_and_unterminated_block(fmt):
    assert fmt.strip_formatter_preamble("<Think>hidden</Think>Answer") == "Answer"
    assert fmt.strip_formatter_preamble("Answer <thinking>dangling plan") == "Answer"


# ─── 8. completion-first timing ─────────────────────────────────────

def test_timing_stamps_ttft_without_probe(fmt, monkeypatch):
    calls = _capture(monkeypatch, fmt, lambda p, t: _Resp(lines=_sse("streamed")))
    timing: dict = {}
    out, skipped, _, _ = fmt.run_pipeline("I wave.", _mode(fmt, "rp_enhance"), request_id="r9", timing=timing)
    assert (out, skipped) == ("streamed", False)
    assert "ttft" in timing and "probe" not in timing
    assert timing["ttft"] <= timing["step_rp_enhance"]
    assert len(calls) == 1


# ─── 9. word-confidence alignment ───────────────────────────────────

def test_repeated_word_anchors_to_its_own_occurrence(fmt):
    text = "hello world hello"
    words = [
        {"word": " hello", "logprob": -0.01},
        {"word": " world", "logprob": -0.01},
        {"word": " hello", "logprob": -5.0},
    ]
    spans = fmt.compute_low_confidence_spans(text, words, threshold=-1.0)
    assert [(s["start_idx"], s["end_idx"]) for s in spans] == [(12, 17)]


# ─── 11. caches ─────────────────────────────────────────────────────

def test_empty_vocab_is_a_cache_hit(fmt, monkeypatch):
    fmt.config.VOCAB_FILE.write_text("[]", encoding="utf-8")
    parses = {"n": 0}
    real = fmt._parse_config

    def counting(raw):
        parses["n"] += 1
        return real(raw)

    monkeypatch.setattr(fmt, "_parse_config", counting)
    assert fmt.load_vocab() == [] and fmt.load_vocab() == []
    assert parses["n"] == 1


# ─── 12. rp_eval ────────────────────────────────────────────────────

def _rp_eval():
    from conftest import load_script
    return load_script(SERVER_DIR / "rp_eval.py", "rp_eval_audit")


def test_rp_eval_mixed_pov_heuristic():
    ev = _rp_eval()
    ok = ev.RPEvalCase(id="ok", raw="I watch her laugh and smile", output="I watch as she laughs, and I smile.")
    assert "mixed_pov" not in ev.score_case(ok).flags
    flipped = ev.RPEvalCase(id="bad", raw="I watch her laugh", output="He watches as she laughs.")
    assert "mixed_pov" in ev.score_case(flipped).flags
    named = ev.RPEvalCase(id="named", raw="I nod", output="Rashid nods, and I wait.", speaker="Rashid")
    assert "mixed_pov" in ev.score_case(named).flags


def test_rp_eval_counts_cjk_words():
    ev = _rp_eval()
    case = ev.RPEvalCase(id="cjk", raw="我想你", output="我真的非常非常非常想念你，我的朋友，你知道吗", max_expansion_ratio=2.8)
    result = ev.score_case(case)
    assert result.metrics["raw_words"] == 3
    assert "over_expanded" in result.flags
    assert ev.score_case(ev.RPEvalCase(id="en", raw="I don't know", output="I don't know.")).metrics["raw_words"] == 3


# ─── review regressions ─────────────────────────────────────────────

def test_cleanup_keeps_first_paragraph_that_looks_like_preamble(fmt, monkeypatch):
    cleaned_reply = (
        "This is the letter I wrote to you yesterday, and I want you to read it now.\n\n"
        "\"Dear Elara, I have missed you every single day since you left the city.\""
    )
    _capture(monkeypatch, fmt, lambda p, t: _Resp(_ok("<think>plan</think>" + cleaned_reply)))
    text = ("um this is the uh letter I wrote to you yesterday and I want you to read it now "
            "dear Elara I have missed you every single day since you left the city")
    out, cleaned, _ = fmt.disfluency_clean(text)
    assert cleaned is True
    assert out == cleaned_reply


def test_append_keeps_leading_minus_in_residual(fmt):
    residual, cmd = fmt.command_dispatch("Computer, append -5 degrees.")
    assert cmd["intent"] == "append" and residual == "-5 degrees."
    assert fmt.command_dispatch("Computer, send -")[1]["intent"] == "send"


def test_rp_eval_ignores_dialogue_in_raw_for_pov():
    ev = _rp_eval()
    case = ev.RPEvalCase(id="dlg", raw='"I love you," she says.', output='"I love you," she says.')
    assert "mixed_pov" not in ev.score_case(case).flags


def test_builtin_modes_inherit_whisper_model(fmt, monkeypatch, tmp_path):
    modes_file = tmp_path / "modes.json"
    monkeypatch.setattr(fmt.config, "MODES_FILE", modes_file)
    monkeypatch.setattr(fmt.config, "DEFAULT_MODEL", "chosen-model")
    modes_file.write_text(json.dumps([
        {"id": "plain", "pipeline": ["whisper"], "whisper_model": "large-v3-turbo"},
        {"id": "rp_enhance", "pipeline": ["whisper", "rp_enhance"], "whisper_model": "small"},
        {"id": "custom", "pipeline": ["whisper", "rp_format"], "whisper_model": "large-v3-turbo"},
        {"id": "blank", "pipeline": ["whisper"], "whisper_model": ""},
    ]))
    fmt._invalidate_modes_cache()
    try:
        got = {m["id"]: m["whisper_model"] for m in fmt.load_modes()}
        assert got["plain"] == "chosen-model"      # legacy seeded value → inherit
        assert got["rp_enhance"] == "small"        # deliberate pin kept
        assert got["custom"] == "large-v3-turbo"   # user mode pin kept
        assert got["blank"] == "chosen-model"
        assert got["grammar_clean"] == "chosen-model"  # merged built-in
        fmt.save_mode_formatter("custom", "pin/model")
        saved = {m["id"]: m["whisper_model"] for m in fmt._parse_config(modes_file.read_bytes())}
        assert saved["plain"] == "" and saved["blank"] == ""
        assert saved["rp_enhance"] == "small" and saved["custom"] == "large-v3-turbo"
    finally:
        fmt._invalidate_modes_cache()
