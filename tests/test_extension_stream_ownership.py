"""Extension: streamed dictation is owned by exactly one SillyTavern tab.

Runs the real ownership / streaming / undo / result functions from
`extension/index.js` in two Node `vm` contexts ("tabs") that share
localStorage, Web Locks and a BroadcastChannel bus, then replays the same SSE
token stream into both. Behavioural only; no source-text assertions.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "index.js"


def _slice(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _blocks() -> str:
    src = EXTENSION.read_text(encoding="utf-8")
    write_start = src.index("/** Write text into ST's chat input")
    write_end = src.index("\n}\n", write_start) + 3
    return "\n".join([
        _slice(src, "// Cross-tab side-effect ownership.", "function settings()"),
        _slice(src, "// ─── MVP-13: streaming formatter partials", "// ─── MVP-16"),
        _slice(src, "const UNDO_STACK_CAP", "/** Toast helper."),
        _slice(src, "function rememberAppliedDictationRequest", "async function buildPairedPhoneUrl"),
        src[write_start:write_end],
    ])


HARNESS = r"""
const vm = require('vm');
const BLOCK = __BLOCK__;
const SCENARIO = __SCENARIO__;
const APPEND_MODE = __APPEND__;
const USE_LOCKS = __LOCKS__;

// Shared fake clock (setTimeout / rAF are both driven manually).
let now = 1_000_000, nextId = 1;
const timers = new Map();
const clock = {
  setTimeout(fn, ms = 0) { const id = nextId++; timers.set(id, { at: now + ms, fn }); return id; },
  clearTimeout(id) { timers.delete(id); },
  advance(ms) {
    const target = now + ms;
    for (;;) {
      let due = null;
      for (const [id, t] of timers) if (t.at <= target && (!due || t.at < due[1].at)) due = [id, t];
      if (!due) break;
      timers.delete(due[0]); now = due[1].at; due[1].fn();
    }
    now = target;
  },
};

// Shared cross-tab primitives.
const store = new Map();
const localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};
const lockTails = new Map();
const locks = {
  request(name, cb) {
    const run = (lockTails.get(name) || Promise.resolve()).then(() => cb());
    lockTails.set(name, run.catch(() => {}));
    return run;
  },
};
const channels = [];
class BroadcastChannel {
  constructor(name) { this.name = name; this.listeners = []; channels.push(this); }
  addEventListener(_t, fn) { this.listeners.push(fn); }
  postMessage(data) {
    for (const ch of channels) if (ch !== this && ch.name === this.name) for (const fn of ch.listeners) fn({ data });
  }
}

function makeTab(name, initial) {
  const ta = { value: initial, inputs: 0, dispatchEvent() { this.inputs++; }, focus() {}, setSelectionRange() {} };
  let rafQueue = [];
  const ctx = vm.createContext({
    console, Date: { now: () => now }, Math, JSON, Map, Set, Promise, Error, String, Number, Array,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, queueMicrotask,
    requestAnimationFrame(fn) { rafQueue.push(fn); return rafQueue.length; },
    cancelAnimationFrame() { rafQueue = []; },
    localStorage, BroadcastChannel,
    navigator: USE_LOCKS ? { locks } : {},
    document: { getElementById: id => (id === 'send_textarea' ? ta : null) },
    Event: class { constructor(type) { this.type = type; } },
    window: {},
  });
  ctx.flushRaf = () => { const q = rafQueue; rafQueue = []; for (const fn of q) fn(); };
  vm.runInContext(`
    const LOG = () => {}; const WARN = () => {};
    const appliedDictationRequestIds = [];
    const pendingCanonicalResults = [];
    const PENDING_CANONICAL_RESULT_CAP = 16;
    let desktopCapture = null;
    function rememberChatIdentity() {}
    let lastDoneMode = '', lastDoneModel = '', lastModelFallback = false;
    const cfg = { appendMode: ${JSON.stringify(APPEND_MODE)}, autoSend: false };
    function settings() { return cfg; }
    function clearRepairTrace() {}
    function renderRepairTraceFromPayload() {}
    function renderLowConfBanner() {}
    function maybeReadDictatedPersonaText() {}
    function escapeHtml(s) { return s; }
    ${BLOCK}
    globalThis.api = {
      token: data => handleDictationToken(data),
      result: data => handleDictationResultEvent(data, ''),
      undo: () => popUndoSnapshot(),
      undoDepth: () => undoStack.length,
      hasSession: () => !!streamingSession,
    };
  `, ctx);
  return { name, ta, api: ctx.api, flushRaf: ctx.flushRaf };
}

const settle = async () => { for (let i = 0; i < 25; i++) await new Promise(r => setImmediate(r)); };
// Let arbitration finish: Web Locks settle on microtasks, the localStorage
// fallback additionally waits on a 100 ms timer.
const pump = async () => { await settle(); clock.advance(100); await settle(); };

(async () => {
  const initial = APPEND_MODE === 'append' ? 'Earlier line  ' : 'draft';
  const tabs = [makeTab('a', initial), makeTab('b', initial)];
  const both = fn => tabs.forEach(fn);
  const out = { initial };

  // Two deltas arrive before arbitration can settle; the third after.
  both(t => t.api.token({ requestId: 'r1', delta: 'Hel', done: false }));
  both(t => t.api.token({ requestId: 'r1', delta: 'lo', done: false }));
  await pump();
  both(t => t.api.token({ requestId: 'r1', delta: '', done: true }));
  both(t => t.flushRaf());
  out.streamed = tabs.map(t => ({ value: t.ta.value, inputs: t.ta.inputs, session: t.api.hasSession(), undo: t.api.undoDepth() }));

  if (SCENARIO === 'timeout') {
    clock.advance(45_000);
    out.afterTimeout = tabs.map(t => ({ value: t.ta.value, session: t.api.hasSession(), undo: t.api.undoDepth() }));
    // Late deltas for the abandoned stream are ignored everywhere.
    both(t => t.api.token({ requestId: 'r1', delta: 'zombie', done: false }));
    both(t => t.flushRaf());
    out.afterLateToken = tabs.map(t => t.ta.value);
  }

  both(t => { t.api.result({ requestId: 'r1', text: 'Hello there.' }); });
  await pump();
  out.final = tabs.map(t => ({ value: t.ta.value, session: t.api.hasSession() }));
  out.undo = tabs.map(t => { const s = t.api.undo(); return s ? s.prevValue : null; });
  console.log(JSON.stringify(out));
})().catch(err => { console.error(err); process.exit(1); });
"""


def _run(scenario: str = "result", append_mode: str = "replace", locks: bool = True) -> dict:
    script = (
        HARNESS.replace("__BLOCK__", json.dumps(_blocks()))
        .replace("__SCENARIO__", json.dumps(scenario))
        .replace("__APPEND__", json.dumps(append_mode))
        .replace("__LOCKS__", "true" if locks else "false")
    )
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def _owner_index(out: dict) -> int:
    mutated = [i for i, tab in enumerate(out["streamed"]) if tab["inputs"]]
    assert len(mutated) == 1, out
    return mutated[0]


def _assert_loser_untouched(out: dict, loser: int) -> None:
    streamed = out["streamed"][loser]
    assert streamed == {"value": out["initial"], "inputs": 0, "session": False, "undo": 0}
    assert out["final"][loser] == {"value": out["initial"], "session": False}
    assert out["undo"][loser] is None


def test_only_one_tab_streams_and_applies_the_result():
    out = _run()
    owner = _owner_index(out)
    assert out["streamed"][owner]["value"] == "Hello"
    assert out["streamed"][owner]["session"] is True
    assert out["final"][owner] == {"value": "Hello there.", "session": False}
    _assert_loser_untouched(out, 1 - owner)


def test_single_undo_after_stream_restores_pre_stream_draft():
    out = _run()
    owner = _owner_index(out)
    assert out["streamed"][owner]["undo"] == 1
    assert out["undo"][owner] == "draft"


def test_append_mode_stream_keeps_base_and_undo_restores_it():
    out = _run(append_mode="append")
    owner = _owner_index(out)
    assert out["streamed"][owner]["value"] == "Earlier line\n\nHello"
    assert out["final"][owner]["value"] == "Earlier line\n\nHello there."
    assert out["undo"][owner] == "Earlier line  "
    _assert_loser_untouched(out, 1 - owner)


def test_stream_timeout_restores_base_only_on_owner():
    out = _run(scenario="timeout")
    owner = _owner_index(out)
    loser = 1 - owner
    assert out["afterTimeout"][owner] == {"value": "draft", "session": False, "undo": 0}
    assert out["afterTimeout"][loser] == {"value": "draft", "session": False, "undo": 0}
    assert out["afterLateToken"] == ["draft", "draft"]
    # The late canonical result still lands once, on the owner, via the batch path.
    assert out["final"][owner]["value"] == "Hello there."
    assert out["undo"][owner] == "draft"
    _assert_loser_untouched(out, loser)


def test_localstorage_fallback_also_elects_a_single_stream_owner():
    out = _run(locks=False)
    owner = _owner_index(out)
    assert out["final"][owner]["value"] == "Hello there."
    assert out["undo"][owner] == "draft"
    _assert_loser_untouched(out, 1 - owner)
