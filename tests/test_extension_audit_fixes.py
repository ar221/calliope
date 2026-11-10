"""Extension: behavioural tests for the 2026-11-10 audit fixes.

The whole `extension/index.js` (ST imports stripped, self-bootstrap removed)
is evaluated in Node `vm` contexts that act as SillyTavern tabs. Tabs share a
fake clock, localStorage, a BroadcastChannel bus and a Web Locks manager whose
locks are released when a tab dies. Payloads are shaped like the real server
responses. Synthetic data only; no network, no source-text assertions.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "index.js"


def _module_source() -> str:
    src = EXTENSION.read_text(encoding="utf-8")
    src = src[: src.index("// Self-bootstrap for robustness")]
    lines = [ln for ln in src.splitlines() if not ln.startswith("import ")]
    return "\n".join(lines).replace("export async function init", "async function init")


HARNESS = r"""
const vm = require('vm');
const SOURCE = __SOURCE__;
const SCENARIO = __SCENARIO__;

// ── shared clock ────────────────────────────────────────────────────────────
let now = 1_000_000, nextId = 1;
const timers = new Map();
const clock = {
  setTimeout(fn, ms = 0) { const id = nextId++; timers.set(id, { at: now + ms, fn }); return id; },
  clearTimeout(id) { timers.delete(id); },
  setInterval() { return nextId++; },
  clearInterval() {},
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
const settle = async () => { for (let i = 0; i < 40; i++) await new Promise(r => setImmediate(r)); };
const pump = async () => { await settle(); clock.advance(100); await settle(); };

// ── shared cross-tab primitives ─────────────────────────────────────────────
const store = new Map();
const localStorage = {
  getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: k => store.delete(k),
};

// Web Locks with tab death: a dead tab's held locks are released and its
// queued requests dropped, like a closed browser tab.
const lockQueues = new Map();
function pumpLock(name) {
  const q = lockQueues.get(name);
  if (!q || !q.length) return;
  const head = q[0];
  if (head.granted) return;
  if (head.tab.dead) { q.shift(); pumpLock(name); return; }
  head.granted = true;
  head.release = () => {
    if (head.released) return;
    head.released = true;
    const i = q.indexOf(head);
    if (i >= 0) q.splice(i, 1);
    pumpLock(name);
  };
  Promise.resolve().then(() => head.cb()).then(
    v => { head.release(); head.resolve(v); },
    e => { head.release(); head.reject(e); },
  );
}
function makeLocks(tab) {
  return {
    request(name, cb) {
      return new Promise((resolve, reject) => {
        if (!lockQueues.has(name)) lockQueues.set(name, []);
        lockQueues.get(name).push({ tab, cb, resolve, reject, granted: false });
        pumpLock(name);
      });
    },
  };
}
function killTab(tab) {
  tab.dead = true;
  for (const [name, q] of lockQueues) {
    for (const e of [...q]) {
      if (e.tab !== tab) continue;
      if (e.granted) e.release(); else q.splice(q.indexOf(e), 1);
    }
    pumpLock(name);
  }
}

const channels = [];
function makeBroadcastChannel(tab) {
  return class BroadcastChannel {
    constructor(name) { this.name = name; this.listeners = []; this.tab = tab; channels.push(this); }
    addEventListener(_t, fn) { this.listeners.push(fn); }
    postMessage(data) {
      for (const ch of channels) {
        if (ch !== this && ch.name === this.name && !ch.tab.dead) for (const fn of ch.listeners) fn({ data });
      }
    }
  };
}

// ── fake browser bits ───────────────────────────────────────────────────────
function fakeEl(extra = {}) {
  return Object.assign({
    style: {}, dataset: {}, children: [], textContent: '', innerHTML: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    setAttribute(k, v) { this[k] = v; }, getAttribute(k) { return this[k] ?? null; },
    removeAttribute() {}, hasAttribute() { return false; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    insertBefore(c) { return c; }, addEventListener() {}, remove() {}, focus() {},
  }, extra);
}

function response(status, body = {}, blob = null) {
  return {
    ok: status >= 200 && status < 300, status,
    json: async () => body, blob: async () => blob,
    clone() { return this; }, headers: { get: () => null },
  };
}

let blobSeq = 0;
function makeTab(name, initial = 'draft', opts = {}) {
  const tab = { name, dead: false, fetchLog: [], toasts: [], plays: [], revoked: [], audios: [], intervals: [] };
  tab.route = () => response(404, { error: 'not_found' });
  const ta = fakeEl({
    value: initial, inputs: 0, parentElement: null,
    dispatchEvent() { this.inputs++; }, setSelectionRange() {},
  });
  const sendBut = fakeEl({ clicks: 0, click() { this.clicks++; } });
  let rafQueue = [];

  class FakeURL extends URL {}
  FakeURL.createObjectURL = () => `blob:${name}-${++blobSeq}`;
  FakeURL.revokeObjectURL = (u) => tab.revoked.push(u);

  class FakeAudio {
    constructor() { this.paused = true; this.readyState = 0; this.listeners = {}; this.src = ''; tab.audios.push(this); }
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
    removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter(f => f !== fn); }
    load() { this.readyState = 1; }
    play() { this.paused = false; tab.plays.push(this.src); return Promise.resolve(); }
    pause() { this.paused = true; }
    end() { this.paused = true; for (const fn of [...(this.listeners.ended || [])]) fn(); }
  }

  class FakeRecorder {
    static isTypeSupported(m) { return m === 'audio/webm;codecs=opus'; }
    constructor(_stream, { mimeType }) { this.mimeType = mimeType; this.state = 'inactive'; this.listeners = {}; }
    addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      for (const fn of this.listeners.dataavailable || []) fn({ data: new Blob(['opus-bytes']) });
      for (const fn of this.listeners.stop || []) fn();
    }
  }

  const listeners = {};
  const ctx = vm.createContext({
    console, Date: { now: () => now }, Math, JSON, Map, Set, Promise, Error, String, Number, Array, Object,
    RegExp, Symbol, Boolean, encodeURIComponent, decodeURIComponent, URLSearchParams, structuredClone,
    Uint8Array, btoa, crypto: require('crypto').webcrypto,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval(fn) { tab.intervals.push(fn); return nextId++; }, clearInterval() {}, queueMicrotask,
    requestAnimationFrame(fn) { rafQueue.push(fn); return rafQueue.length; },
    cancelAnimationFrame() { rafQueue = []; },
    AbortController, Blob, URL: FakeURL, Audio: FakeAudio, MediaRecorder: FakeRecorder,
    HTMLMediaElement: { HAVE_METADATA: 1 }, HTMLElement: function () {},
    Event: class { constructor(type) { this.type = type; } },
    localStorage, BroadcastChannel: makeBroadcastChannel(tab),
    navigator: {
      locks: makeLocks(tab),
      mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
    },
    fetch: (url, init = {}) => new Promise((resolve, reject) => {
      const entry = { url: String(url), init, resolve, reject, aborted: false };
      tab.fetchLog.push(entry);
      const fail = () => { entry.aborted = true; const e = new Error('aborted'); e.name = 'AbortError'; reject(e); };
      if (init.signal?.aborted) return fail();
      init.signal?.addEventListener('abort', fail);
      const routed = tab.route(entry);
      if (routed !== 'pending') resolve(routed);
    }),
    document: {
      hidden: !!opts.hidden, visibilityState: opts.hidden ? 'hidden' : 'visible', readyState: 'complete', body: fakeEl(),
      getElementById: id => (id === 'send_textarea' ? ta : id === 'send_but' ? sendBut : null),
      querySelector: () => null, querySelectorAll: () => [],
      createElement: () => fakeEl(), addEventListener() {}, hasFocus: () => opts.focused !== false,
    },
    window: {
      location: { hostname: 'calliope.test' },
      matchMedia: () => ({ matches: false }), addEventListener() {},
      toastr: {
        success: (m) => tab.toasts.push(['success', m]), warning: (m) => tab.toasts.push(['warning', m]),
        error: (m) => tab.toasts.push(['error', m]), info: (m) => tab.toasts.push(['info', m]),
      },
    },
    // SillyTavern imports.
    eventSource: { on(evt, fn) { (listeners[evt] ||= []).push(fn); } },
    event_types: {},
    name1: 'Persona', this_chid: 0, user_avatar: 'persona.png', selected_group: null, groups: [],
    characters: [{ name: 'Alice', avatar: 'Alice.png', chat: 'alice-chat-1' }],
    chat: opts.chat || [],
    extension_settings: {
      'dictation-bridge': {
        serverUrl: 'https://calliope.test:8384', serverToken: 'synthetic-token',
        appendMode: opts.appendMode || 'replace', captureMode: 'desktop-toggle', openStyle: 'iframe',
        sseEnabled: false, broadcastState: true, autoSend: false,
      },
    },
    getContext: () => ({ saveSettingsDebounced() {}, chatMetadata: {} }),
    power_user: { personas: {} },
    qrcodegen: {},
  });
  ctx.globalThis = ctx;
  vm.runInContext(SOURCE, ctx, { filename: 'index.js' });
  tab.ta = ta;
  tab.sendBut = sendBut;
  tab.ctx = ctx;
  tab.run = code => vm.runInContext(code, ctx);
  tab.setVisibility = (hidden, focused) => {
    ctx.document.hidden = hidden;
    ctx.document.visibilityState = hidden ? 'hidden' : 'visible';
    ctx.document.hasFocus = () => focused;
  };
  tab.flushRaf = () => { const q = rafQueue; rafQueue = []; for (const fn of q) fn(); };
  return tab;
}

const pendingFetches = (tab, part) => tab.fetchLog.filter(e => e.url.includes(part));

// ── scenarios ───────────────────────────────────────────────────────────────
const scenarios = {
  // E1: the stream owner dies before the result; the surviving tab delivers it.
  async ownerDisappears() {
    const tabs = [makeTab('a'), makeTab('b')];
    const token = d => tabs.forEach(t => t.run(`handleDictationToken(${JSON.stringify(d)})`));
    token({ requestId: 'r1', delta: 'Hel', done: false });
    await pump();
    token({ requestId: 'r1', delta: 'lo', done: false });
    tabs.forEach(t => t.flushRaf());
    const owner = tabs.find(t => t.ta.inputs > 0);
    const survivor = tabs.find(t => t !== owner);
    const streamedOnOwner = owner.ta.value;
    const survivorBefore = survivor.ta.value;
    killTab(owner);
    survivor.run(`handleDictationResultEvent(${JSON.stringify({ requestId: 'r1', text: 'Hello there.' })}, '')`);
    await pump();
    return { streamedOnOwner, survivorBefore, survivorAfter: survivor.ta.value };
  },

  // E1: a tab that joined after the tokens must not win while the owner lives.
  async lateSubscriber() {
    const tabs = [makeTab('a'), makeTab('b')];
    tabs.forEach(t => t.run(`handleDictationToken({ requestId: 'r2', delta: 'Hi', done: false })`));
    await pump();
    tabs.forEach(t => t.flushRaf());
    const late = makeTab('late');
    const result = JSON.stringify({ requestId: 'r2', text: 'Hi there.' });
    late.run(`handleDictationResultEvent(${result}, '')`);
    await pump();
    const lateWhileOwnerLive = late.ta.value;
    tabs.forEach(t => t.run(`handleDictationResultEvent(${result}, '')`));
    await pump();
    return {
      lateWhileOwnerLive,
      lateFinal: late.ta.value,
      lateInputs: late.ta.inputs,
      others: tabs.map(t => t.ta.value).sort(),
    };
  },

  // E2: chat switch mid-stream: no restore of the old draft, result dropped everywhere.
  async chatSwitchDuringStream() {
    const tabs = [makeTab('a'), makeTab('b')];
    tabs.forEach(t => t.run(`handleDictationToken({ requestId: 'r3', delta: 'Partial', done: false })`));
    await pump();
    tabs.forEach(t => t.flushRaf());
    const owner = tabs.find(t => t.ta.inputs > 0);
    const other = tabs.find(t => t !== owner);
    const streamed = owner.ta.value;
    owner.run(`characters[0].chat = 'alice-chat-2'`);
    owner.run('onChatGenerationChanged()');
    const afterSwitch = owner.ta.value;
    tabs.forEach(t => t.run(`handleDictationToken({ requestId: 'r3', delta: ' late', done: false })`));
    tabs.forEach(t => t.flushRaf());
    tabs.forEach(t => t.run(`handleDictationResultEvent({ requestId: 'r3', text: 'Partial result.' }, '')`));
    await pump();
    return {
      streamed, afterSwitch,
      ownerFinal: owner.ta.value, otherFinal: other.ta.value,
      ownerUndo: owner.run('popUndoSnapshot()'),
    };
  },

  // E2/M4: chat switch while the desktop upload is in flight cancels it and
  // the request's later SSE result (server echoes client_request_id as the
  // request id) never lands in either tab.
  async chatSwitchDuringUpload() {
    const a = makeTab('a', 'draft');
    const b = makeTab('b', 'draft');
    a.route = (e) => (e.url.includes('/transcribe') ? 'pending' : response(404, {}));
    await a.run(`startDesktopRecording('toggle')`);
    a.run(`stopDesktopRecording('toggle')`);
    await settle();
    const upload = pendingFetches(a, '/transcribe')[0];
    const contentType = upload?.init?.headers?.['Content-Type'];
    const clientId = new URL(upload.url).searchParams.get('client_request_id');
    [a, b].forEach(t => t.run(`handleDictationStateEvent({ requestId: ${JSON.stringify(clientId)}, state: 'transcribing' })`));
    a.run(`characters[0].chat = 'alice-chat-2'`);
    a.run('onChatGenerationChanged()');
    await settle();
    [a, b].forEach(t => t.run(`handleDictationResultEvent({ requestId: ${JSON.stringify(clientId)}, text: 'Late words.' }, '')`));
    await pump();
    return {
      uploadStarted: !!upload, contentType, uploadAborted: !!upload?.aborted,
      clientIdOk: /^[A-Za-z0-9_-]{16,32}$/.test(clientId || ''),
      warmed: pendingFetches(a, '/warm').length,
      captureCleared: a.run('desktopCapture === null'),
      values: [a.ta.value, b.ta.value],
    };
  },

  // M4: cancelling an upload suppresses exactly its own request id; an
  // unrelated phone request seen during the upload still lands.
  async cancelUploadKeepsUnrelated() {
    const a = makeTab('a', '');
    const b = makeTab('b', '');
    a.route = (e) => (e.url.includes('/transcribe') ? 'pending' : response(404, {}));
    await a.run(`startDesktopRecording('toggle')`);
    a.run(`stopDesktopRecording('toggle')`);
    await settle();
    const upload = pendingFetches(a, '/transcribe')[0];
    const clientId = new URL(upload.url).searchParams.get('client_request_id');
    [a, b].forEach(t => {
      t.run(`handleDictationStateEvent({ requestId: ${JSON.stringify(clientId)}, state: 'transcribing' })`);
      t.run(`handleDictationStateEvent({ requestId: 'phone-1', state: 'transcribing' })`);
    });
    a.run(`stopDesktopRecording('toggle')`); // second press during upload = cancel
    await settle();
    clock.advance(5000);
    [a, b].forEach(t => {
      t.run(`handleDictationResultEvent({ requestId: ${JSON.stringify(clientId)}, text: 'Cancelled take.' }, '')`);
      t.run(`handleDictationResultEvent({ requestId: 'phone-1', text: 'Phone words.' }, '')`);
    });
    await pump();
    return { uploadAborted: !!upload.aborted, values: [a.ta.value, b.ta.value].sort() };
  },

  // ST fires CHAT_CHANGED for a reload of the same chat: in-flight work stays.
  async sameChatReload() {
    const t = makeTab('a', 'draft');
    t.run(`handleDictationToken({ requestId: 'r7', delta: 'Kept', done: false })`);
    await pump();
    t.flushRaf();
    t.run('onChatGenerationChanged()'); // same identity
    const generationAfterReload = t.run('chatGeneration');
    t.run(`handleDictationResultEvent({ requestId: 'r7', text: 'Kept text.' }, '')`);
    await pump();
    const afterReload = t.ta.value;
    t.run(`characters[0].chat = 'alice-chat-2'`);
    t.run('onChatGenerationChanged()');
    return { generationAfterReload, afterReload, generationAfterSwitch: t.run('chatGeneration') };
  },

  // M13: a lone hidden tab keeps the heartbeat; with two tabs, the hidden
  // owner keeps it and a visible-but-unfocused tab only takes over once the
  // owner's lease expires.
  async heartbeatLease() {
    const okState = (t) => { t.route = (e) => (e.url.endsWith('/state') ? response(200, {}) : response(404, {})); };
    const statePosts = (t) => pendingFetches(t, '/state').filter(e => e.init.method === 'POST').length;
    const chat = [{ mes: 'Hello.', is_user: false }];
    const lone = makeTab('lone', '', { chat, hidden: true, focused: false });
    okState(lone);
    lone.run('startStateHeartbeat()');
    lone.intervals[0]();
    await settle();
    const lonePosts = statePosts(lone);
    killTab(lone);
    store.clear();

    const owner = makeTab('owner', '', { chat, hidden: false, focused: true });
    const other = makeTab('other', '', { chat, hidden: false, focused: false });
    [owner, other].forEach(okState);
    owner.run('startStateHeartbeat()'); // visible: claims the lease
    other.run('startStateHeartbeat()');
    owner.setVisibility(true, false);   // ST minimised during phone dictation
    owner.intervals[0](); other.intervals[0]();
    await settle();
    const ownerWhileHidden = statePosts(owner);
    const otherWhileOwnerLive = statePosts(other);
    killTab(owner);
    clock.advance(80_000);              // lease expires without renewal
    other.intervals[0]();
    await settle();
    return { lonePosts, ownerWhileHidden, otherWhileOwnerLive, otherAfterExpiry: statePosts(other) };
  },

  // E3/E4/M5: SSE result first, then the richer HTTP response for the same
  // request: text inserted once, spans/entry id attached with offsets.
  async httpAfterSse() {
    const t = makeTab('a', 'Earlier line', { appendMode: 'append' });
    const banners = [];
    t.ctx.renderLowConfBanner = (spans, placement) => banners.push({ spans, placement });
    const sse = {
      requestId: 'r5', text: 'I wear the crown.', has_repair_trace: true, mode: 'rp_enhance',
      formatting_skipped: false, formatting_reason: '', model: 'synthetic/model', model_fallback: false,
      model_tier: 0, is_ooc: false, mode_override: '', auto_send: false, ts: 1,
    };
    const http = {
      text: 'I wear the crown.', raw: 'i wear the crown', cleaned: 'I wear the crown.',
      repair_trace: { raw: 'i wear the crown', cleaned: 'I wear the crown.', final: 'I wear the crown.', has_changes: true },
      mode: 'rp_enhance', transcript_count: 1, formatting_skipped: false, formatting_reason: '',
      model: 'synthetic/model', model_fallback: false, model_tier: 0, chat_id: '', is_ooc: false,
      mode_override: '', word_confidences: [],
      low_confidence_spans: [
        { word: 'wear', start_idx: 2, end_idx: 6, confidence: 0.31, logprob: -1.2, alternatives: ['where'] },
        { word: 'crown', start_idx: 11, end_idx: 16, confidence: 0.4, logprob: -0.9, alternatives: [] },
      ],
      request_id: 'r5', entry_id: 'entry-77',
    };
    await t.run(`handleDictationResultEvent(${JSON.stringify(sse)}, '')`);
    await settle();
    const afterSse = { value: t.ta.value, inputs: t.ta.inputs };
    await t.run(`deliverDesktopResult({ cancelled: false, generation: chatGeneration }, ${JSON.stringify(http)})`);
    await settle();
    const banner = banners[banners.length - 1];
    t.ctx.__banner = banner;
    const chips = t.run('normalizeLowConfSpans(__banner.spans, __banner.placement)');
    t.ctx.__chips = chips;
    const first = t.run('applyLowConfReplacement(document.getElementById("send_textarea").value, __chips, 0, "where")');
    const second = t.run('applyLowConfReplacement(' + JSON.stringify(first.value) + ', __chips, 1, "crowd")');
    return {
      afterSse,
      afterHttp: { value: t.ta.value, inputs: t.ta.inputs },
      bannerCalls: banners.length,
      entryId: t.run('lastAppliedDictation.entryId'),
      chips: JSON.parse(JSON.stringify(chips.map(c => ({ word: c.word, alts: c.alts })))),
      first: first.value, second: second.value,
    };
  },

  // M5: HTTP first, SSE afterwards: still exactly one insertion.
  async httpBeforeSse() {
    const t = makeTab('a', '');
    const http = { text: 'Only once.', request_id: 'r6', low_confidence_spans: [], entry_id: '' };
    await t.run(`deliverDesktopResult({ cancelled: false, generation: chatGeneration }, ${JSON.stringify(http)})`);
    await t.run(`handleDictationResultEvent({ requestId: 'r6', text: 'Only once.' }, '')`);
    await settle();
    return { value: t.ta.value, inputs: t.ta.inputs };
  },

  // /word-alternatives returns objects; the popover needs their text.
  async wordAlternatives() {
    const t = makeTab('a');
    t.route = () => response(200, { alternatives: [
      { text: 'where', score: 0.9, source: 'vocab' }, { text: 'ware', score: 0.5, source: 'phonetic' },
    ] });
    return { alts: await t.run(`fetchWordAlternatives('wear', '')`) };
  },

  // E5: Stop while a single-message TTS fetch is pending: nothing plays.
  async ttsStopDuringFetch() {
    const t = makeTab('a', '', { chat: [{ mes: 'Hello there, traveller.', is_user: false }] });
    t.route = () => 'pending';
    t.ctx.__mes = fakeEl({ mesid: '0', getAttribute(k) { return k === 'mesid' ? '0' : null; } });
    t.ctx.__btn = fakeEl();
    const reading = t.run('readMessageAloud(__mes, __btn)');
    await settle();
    t.run('stopTts()');
    const req = pendingFetches(t, '/tts')[0];
    req.resolve(response(200, {}, new Blob(['wav'], { type: 'audio/wav' })));
    await reading;
    await settle();
    return { aborted: req.aborted, plays: t.plays.length, audios: t.audios.length };
  },

  // E6: skip while a chunk is being fetched discards that chunk; pause holds
  // a finished fetch and resume plays exactly one chunk.
  async ttsSkipAndPause() {
    const t = makeTab('a', '', { chat: [{ mes: 'x', is_user: false }] });
    t.route = () => 'pending';
    t.ctx.__mes = fakeEl({ getAttribute: () => '0' });
    t.ctx.__btn = fakeEl();
    t.run(`ttsStreamingSession = createTtsStreamSession(__mes, __btn, { queue: ['One.', 'Two.', 'Three.'] })`);
    t.run('playNextTtsStreamChunk(ttsStreamingSession)');
    await settle();
    t.run('skipCurrentTtsChunk()');
    await settle();
    const [one, two] = pendingFetches(t, '/tts');
    one.resolve(response(200, {}, new Blob(['1'], { type: 'audio/wav' })));
    await settle();
    const playsAfterSkippedCompletes = t.plays.length;
    t.run('toggleTtsStreamPause()'); // pause while chunk two is in flight
    two.resolve(response(200, {}, new Blob(['2'], { type: 'audio/wav' })));
    await settle();
    const playsWhilePaused = t.plays.length;
    t.run('toggleTtsStreamPause()'); // resume
    await settle();
    const playsAfterResume = t.plays.length;
    const fetchesBeforeEnd = pendingFetches(t, '/tts').length;
    t.audios[t.audios.length - 1].end();
    await settle();
    return {
      skippedAborted: one.aborted, playsAfterSkippedCompletes, playsWhilePaused, playsAfterResume,
      fetchesBeforeEnd, fetchesAfterEnd: pendingFetches(t, '/tts').length,
      bodies: pendingFetches(t, '/tts').map(e => JSON.parse(e.init.body).text),
      revokedAfterEnd: t.revoked.length,
    };
  },

  // E11: a sample playback releases its own object URL when it ends.
  async ttsSampleCleanup() {
    const t = makeTab('a');
    t.route = () => response(200, {}, new Blob(['wav'], { type: 'audio/wav' }));
    await t.run(`playTtsSample('Sample text.', 'af_heart')`);
    const src = t.audios[0].src;
    t.audios[0].end();
    await settle();
    return { played: t.plays, revoked: t.revoked, src, current: t.run('currentTtsPlayback === null') };
  },

  // E9: /state rejected -> false, no fresh timestamp, retry not deduped,
  // keepalive body capped; phone launch warns instead of proceeding silently.
  async postStateFailure() {
    const hugeAi = 'A'.repeat(100_000);
    const t = makeTab('a', '', { chat: [{ mes: hugeAi, is_user: false }] });
    let stateStatus = 500;
    t.route = (e) => {
      if (e.url.endsWith('/state') && e.init.method === 'POST') return response(stateStatus, {});
      if (e.url.endsWith('/health')) return response(200, {});
      if (e.url.endsWith('/state')) return response(200, {});
      if (e.url.endsWith('/pair/bootstrap')) return response(200, { code: 'one-time' });
      return response(404, {});
    };
    const rejected = await t.run(`postState('test')`);
    const post = pendingFetches(t, '/state').filter(e => e.init.method === 'POST');
    const body = post[0].init.body;
    const afterReject = t.run('({ success: lastStateSuccess.t, attempt: lastStateAttemptAt, error: lastStateError })');
    await t.run(`launchPhoneDictation(null)`);
    const launchWarning = t.toasts.some(([lvl, m]) => lvl === 'warning' && m.includes('did not accept'));
    stateStatus = 200;
    const accepted = await t.run(`postState('retry')`);
    return {
      rejected, accepted, afterReject,
      success: t.run('lastStateSuccess.t'),
      bodyBytes: Buffer.byteLength(body), lastAiLen: JSON.parse(body).lastAiMessage.length,
      keepalive: post[0].init.keepalive,
      posts: pendingFetches(t, '/state').filter(e => e.init.method === 'POST').length,
      launchWarning, warmed: pendingFetches(t, '/warm').length,
    };
  },
};

scenarios[SCENARIO]().then(out => console.log(JSON.stringify(out)))
  .catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


def _run(scenario: str) -> dict:
    script = HARNESS.replace("__SOURCE__", json.dumps(_module_source())).replace(
        "__SCENARIO__", json.dumps(scenario)
    )
    result = subprocess.run(["node", "-"], input=script, cwd=ROOT, text=True, capture_output=True, check=False, timeout=60)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_surviving_tab_delivers_result_when_stream_owner_dies():
    out = _run("ownerDisappears")
    assert out["streamedOnOwner"] == "Hello"
    assert out["survivorBefore"] == "draft"
    assert out["survivorAfter"] == "Hello there."


def test_late_subscriber_cannot_win_while_owner_is_alive():
    out = _run("lateSubscriber")
    assert out["lateWhileOwnerLive"] == "draft"
    assert out["lateFinal"] == "draft"
    assert out["lateInputs"] == 0
    assert out["others"] == ["Hi there.", "draft"]


def test_chat_switch_mid_stream_never_restores_old_draft_or_lands_result():
    out = _run("chatSwitchDuringStream")
    assert out["streamed"] == "Partial"
    # Only the streamed partial is removed; chat A's draft is not written back.
    assert out["afterSwitch"] == ""
    assert out["ownerFinal"] == ""
    assert out["otherFinal"] == "draft"
    assert out["ownerUndo"] is None


def test_chat_switch_during_upload_cancels_and_drops_late_result():
    out = _run("chatSwitchDuringUpload")
    assert out["uploadStarted"] is True
    assert out["contentType"] == "audio/webm;codecs=opus"
    assert out["warmed"] == 1
    assert out["uploadAborted"] is True
    assert out["captureCleared"] is True
    assert out["values"] == ["draft", "draft"]
    assert out["clientIdOk"] is True


def test_cancelled_upload_suppresses_only_its_own_request():
    out = _run("cancelUploadKeepsUnrelated")
    assert out["uploadAborted"] is True
    # The phone result seen during the upload still lands (in one tab).
    assert out["values"] == ["", "Phone words."]


def test_same_chat_reload_keeps_in_flight_work():
    out = _run("sameChatReload")
    assert out["generationAfterReload"] == 0
    assert out["afterReload"] == "Kept text."
    assert out["generationAfterSwitch"] == 1


def test_heartbeat_lease_survives_hidden_owner_and_expires_for_takeover():
    out = _run("heartbeatLease")
    assert out["lonePosts"] == 1
    assert out["ownerWhileHidden"] == 1
    assert out["otherWhileOwnerLive"] == 0
    assert out["otherAfterExpiry"] == 1


def test_http_after_sse_adds_metadata_without_reinserting_text():
    out = _run("httpAfterSse")
    assert out["afterSse"]["value"] == "Earlier line\n\nI wear the crown."
    assert out["afterHttp"] == out["afterSse"]
    assert out["bannerCalls"] == 1
    assert out["entryId"] == "entry-77"
    assert out["chips"] == [{"word": "wear", "alts": ["where"]}, {"word": "crown", "alts": []}]
    # Offset-aware: both chips stay usable and shift with earlier edits.
    assert out["first"] == "Earlier line\n\nI where the crown."
    assert out["second"] == "Earlier line\n\nI where the crowd."


def test_http_before_sse_inserts_once():
    out = _run("httpBeforeSse")
    assert out == {"value": "Only once.", "inputs": 1}


def test_word_alternatives_objects_become_text():
    assert _run("wordAlternatives") == {"alts": ["where", "ware"]}


def test_stop_during_pending_tts_fetch_prevents_playback():
    out = _run("ttsStopDuringFetch")
    assert out == {"aborted": True, "plays": 0, "audios": 0}


def test_skip_discards_pending_chunk_and_pause_holds_one_chunk():
    out = _run("ttsSkipAndPause")
    assert out["skippedAborted"] is True
    assert out["playsAfterSkippedCompletes"] == 0
    assert out["playsWhilePaused"] == 0
    assert out["playsAfterResume"] == 1
    assert out["fetchesBeforeEnd"] == 2
    assert out["fetchesAfterEnd"] == 3
    assert out["bodies"] == ["One.", "Two.", "Three."]
    assert out["revokedAfterEnd"] >= 1


def test_sample_playback_revokes_its_object_url_on_end():
    out = _run("ttsSampleCleanup")
    assert out["played"] == [out["src"]]
    assert out["src"] in out["revoked"]
    assert out["current"] is True


def test_rejected_state_post_is_not_fresh_and_launch_warns():
    out = _run("postStateFailure")
    assert out["rejected"] is False
    assert out["afterReject"]["success"] == 0
    assert out["afterReject"]["attempt"] > 0
    assert out["afterReject"]["error"] == "state_http_500"
    assert out["lastAiLen"] == 4000
    assert out["bodyBytes"] < 16_000
    assert out["keepalive"] is True
    assert out["launchWarning"] is True
    assert out["warmed"] == 1
    assert out["accepted"] is True
    assert out["success"] > 0
    # rejected + launch + retry: a failed post never dedupes the next one.
    assert out["posts"] == 3
