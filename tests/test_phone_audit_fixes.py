"""Phone PWA audit fixes (2026-11-10), exercised behaviourally under Node.

The served phone script (WEB_UI) and the pairing bootstrap script are loaded
from `calliope_server.web_ui` and run inside Node `vm` contexts with a small
fake DOM, fetch, MediaRecorder and getUserMedia. Synthetic data only; no
network, no services, no source-text assertions.
"""
from __future__ import annotations

import html
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_UI_PATH = ROOT / "server" / "calliope_server" / "web_ui.py"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _pages() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("_phone_audit_web_ui", WEB_UI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    script_re = re.compile(r"<script>(.*?)</script>", re.S)
    (main,) = script_re.findall(module.WEB_UI)
    (boot,) = script_re.findall(module.PAIRING_BOOTSTRAP_HTML)
    return {"main": main, "boot": boot}


HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const SCRIPTS = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); };

function makeStorage(init) {
  const m = new Map(Object.entries(init || {}));
  return {
    getItem: k => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: k => m.delete(k),
    has: k => m.has(k),
  };
}

class El {
  constructor(id, tag) {
    this.id = id || ''; this.tagName = (tag || 'div').toUpperCase();
    this.children = []; this.listeners = {}; this.attrs = {}; this.dataset = {};
    this._classes = new Set(); this._text = ''; this.innerHTML = '';
    this.style = { setProperty() {} }; this.value = ''; this.checked = false;
    this.disabled = false; this.hidden = false; this.scrollTop = 0; this.scrollHeight = 0;
    this.width = 160; this.height = 32; this.offsetWidth = 0;
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get classList() {
    const s = this._classes;
    return {
      add: (...c) => c.forEach(x => s.add(x)),
      remove: (...c) => c.forEach(x => s.delete(x)),
      toggle: (c, f) => { const on = f === undefined ? !s.has(c) : !!f; if (on) s.add(c); else s.delete(c); return on; },
      contains: c => s.has(c),
    };
  }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(' '); }
  get options() { return this.children; }
  get selectedOptions() { return []; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener() {}
  dispatch(t, extra) {
    for (const fn of this.listeners[t] || []) {
      fn(Object.assign({ type: t, target: this, preventDefault() {}, stopPropagation() {} }, extra || {}));
    }
  }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c) { return this.appendChild(c); }
  remove() {}
  querySelectorAll() { return []; }
  querySelector() { return null; }
  closest() { return null; }
  focus() {} select() {} setSelectionRange() {} scrollIntoView() {}
  click() { this.clicked = true; this.dispatch('click'); }
  getContext() { return { clearRect() {}, fillRect() {}, fillStyle: '' }; }
}

function makeTrack() {
  return { label: 'Synthetic mic', stopped: false, stop() { this.stopped = true; },
           addEventListener(t, fn) { this['on' + t] = fn; } };
}
function makeStream() {
  const track = makeTrack();
  return { track, getTracks: () => [track], getAudioTracks: () => [track] };
}

function makePage(opts) {
  opts = opts || {};
  const env = {
    fetches: [], recorders: [], gumCalls: 0, posted: [], opened: [], wrote: null,
    state: opts.state || null, stateMode: 'ok', health: opts.health || { status: 'ok' },
    supported: opts.supported || ['audio/webm;codecs=opus'],
    recorderThrows: !!opts.recorderThrows, gum: opts.gum || null,
  };
  const elements = new Map();
  const body = new El('body');
  const document = {
    readyState: 'complete', hidden: false, referrer: opts.referrer || '', body,
    documentElement: new El('html'), listeners: {},
    getElementById(id) { if (!elements.has(id)) elements.set(id, new El(id)); return elements.get(id); },
    querySelector(sel) { return sel.includes('dictation-embed') && opts.embed ? { content: '1' } : null; },
    createElement: tag => new El('', tag),
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener() {},
    open() {}, write(h) { env.wrote = h; }, close() {},
  };
  const json = (data, status, headers) => ({
    ok: (status || 200) < 400, status: status || 200,
    headers: { get: k => (headers || {})[k.toLowerCase()] || null },
    json: async () => data, text: async () => (typeof data === 'string' ? data : JSON.stringify(data)),
    blob: async () => new Blob([typeof data === 'string' ? data : JSON.stringify(data)]),
  });
  async function fakeFetch(input, init) {
    init = init || {};
    const headers = init.headers && typeof init.headers.entries === 'function'
      ? Object.fromEntries(init.headers.entries()) : Object.assign({}, init.headers || {});
    const url = String(input);
    const method = init.method || 'GET';
    env.fetches.push({ url, method, headers, body: typeof init.body === 'string' ? init.body : null });
    const path = url.split('?')[0];
    if (path === '/state') {
      if (env.stateMode === 'down') throw new TypeError('Failed to fetch');
      if (env.stateMode === '503') return json({ error: 'busy' }, 503);
      if (env.stateMode === '401') return json({ error: 'unauthorized' }, 401);
      return json(env.state || {});
    }
    if (path === '/modes' && opts.modesGate) await opts.modesGate;
    if (path === '/modes') return json({ modes: [
      { id: 'rp_enhance', label: 'RP+', icon: 'sparkles', use_persona: true, use_character: true,
        use_chat_context: true, pipeline: ['whisper', 'rp_enhance'] },
      { id: 'plain', label: 'Plain', icon: 'mic', pipeline: ['whisper'] },
    ] });
    if (path === '/personas') return json({ personas: [{ id: 'p1', name: 'P1' }] });
    if (path === '/characters') return json({ characters: [] });
    if (path === '/transcribe') return json({ text: 'hello there', raw: 'hello there', mode: 'rp_enhance', entry_id: 'e42' });
    if (path === '/reformat') return json({ text: 'Hello there.', mode: 'rp_enhance' });
    if (path === '/transcript' && method === 'DELETE') return json({ cleared: 1, kept_starred: 0 });
    if (path === '/transcript') return json({ transcript: opts.transcript || [] });
    if (path === '/transcript/export.md') {
      return json('# synthetic', 200, { 'content-disposition': 'attachment; filename="session.md"' });
    }
    if (path === '/health') return json(env.health);
    return json({});
  }
  class FakeRecorder {
    static isTypeSupported(t) { return env.supported.includes(t); }
    constructor(stream, options) {
      if (env.recorderThrows) { const e = new Error('no'); e.name = 'NotSupportedError'; throw e; }
      this.stream = stream; this.options = options; this.mimeType = options.mimeType || 'audio/webm';
      this.state = 'inactive'; env.recorders.push(this);
    }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      if (this.ondataavailable) this.ondataavailable({ data: new Blob(['synthetic-audio']) });
      if (this.onstop) this.onstop();
    }
  }
  const parent = { postMessage(data, origin) { env.posted.push({ data, origin }); } };
  const ctx = {
    console, JSON, Math, Promise, Error, TypeError, Map, Set, Array, Object, String, Number, Date,
    URL, URLSearchParams, Headers, Blob, Uint8Array, Symbol, RegExp, Boolean, Infinity, NaN,
    setTimeout: (fn, ms) => 1, clearTimeout() {}, setInterval: () => 7, clearInterval() {},
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    document, env,
    location: { search: opts.search || '', origin: 'https://phone.test', pathname: '/', hash: '' },
    history: { replaceState() {} },
    sessionStorage: makeStorage(opts.noToken ? {} : { dictationToken: 'synthetic-token' }),
    localStorage: makeStorage(opts.local || {}),
    navigator: {
      vibrate() {}, clipboard: { writeText: async () => {} },
      mediaDevices: {
        enumerateDevices: async () => [],
        getUserMedia: (c) => { env.gumCalls++; return env.gum ? env.gum(c) : Promise.resolve(makeStream()); },
      },
    },
    MediaRecorder: FakeRecorder,
    matchMedia: () => ({ matches: true }),
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
    confirm: () => true, alert() {},
    DICTATION_EMBED_CONFIG: opts.embedConfig,
    fetch: opts.fetchImpl || fakeFetch,
    addEventListener() {}, removeEventListener() {},
    isSecureContext: true,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.parent = opts.embed ? parent : ctx;
  ctx.AudioContext = opts.audio ? class {
    constructor() { this.state = 'running'; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() {
      return { fftSize: 0, frequencyBinCount: 256, disconnect() {},
               getByteTimeDomainData(b) { b.fill(200); }, getByteFrequencyData(b) { b.fill(0); } };
    }
    close() {}
  } : undefined;
  vm.createContext(ctx);
  vm.runInContext(opts.script || SCRIPTS.main, ctx);
  const run = code => vm.runInContext(code, ctx);
  return { ctx, env, run, el: id => document.getElementById(id), parent };
}

const FRESH = { fresh: true, lastUpdated: 1, chatId: 'chat-1', chatType: 'solo', characterId: 'Alice',
                characterName: 'Alice', personaId: 'p1', mode: '' };

const scenarios = {
  async xss() {
    const p = makePage();
    await settle();
    const hostile = "x&#39;);globalThis.pwned=true;//\\\"><img src=x onerror=pwned=1>";
    p.run(`allCharacters = [{ id: ${JSON.stringify(hostile)}, name: ${JSON.stringify(hostile)} }];`);
    p.run('filterCharacters()');
    const dd = p.el('charDropdown');
    const opt = dd.children[1];
    opt.click();
    p.run(`renderVocab([{ correct: 'Say "hi" & <b>\\'yo\\'</b>', aliases: ['a"b'] }])`);
    return {
      dropdownMarkup: dd.innerHTML,
      optionText: opt.textContent,
      selected: p.run('selectedCharacter'),
      pwned: p.run('typeof pwned'),
      vocabHtml: p.el('vocabList').innerHTML,
    };
  },

  async followParams() {
    const p = makePage({ state: FRESH });
    await settle();
    await p.run('loadStateAndApply()');
    await settle();
    const out = {
      params: p.run('buildTranscribeParams().toString()'),
      regenSource: p.run('regenChatSource()'),
      previewUrls: p.env.fetches.filter(f => f.url.startsWith('/active-chat')).map(f => f.url),
    };
    // Full take: warm-up, recorder mime as Content-Type, entry_id used on Regenerate.
    await p.run('startRecording()');
    p.run('stopRecording()');
    await settle();
    await p.run('regenerate()');
    await settle();
    const transcribe = p.env.fetches.find(f => f.url.startsWith('/transcribe'));
    const reformat = p.env.fetches.find(f => f.url === '/reformat');
    out.warm = p.env.fetches.filter(f => f.url === '/warm').map(f => f.method);
    out.transcribeUrl = transcribe.url;
    out.contentType = transcribe.headers['content-type'];
    out.recorderOptions = p.env.recorders[0].options;
    out.reformatBody = JSON.parse(reformat.body);
    out.stateFetchesBeforeUpload = p.env.fetches.slice(0, p.env.fetches.indexOf(transcribe))
      .filter(f => f.url === '/state').length;
    out.finalState = p.run('captureState');
    return out;
  },

  async explicitChatNotMixedWithState() {
    const p = makePage({ state: FRESH });
    await settle();
    await p.run('loadStateAndApply()');
    p.run("setChatSource('individual:Bea')");
    const picked = p.run('buildTranscribeParams().toString()');
    p.run("setChatSource('manual')");
    const manual = p.run('buildTranscribeParams().toString()');
    p.run("setChatSource('auto')");
    const followed = p.run('buildTranscribeParams().toString()');
    return { picked, manual, followed };
  },

  async modeAdoptedWhenStateBeatsModes() {
    let openModes;
    const modesGate = new Promise(r => { openModes = r; });
    const p = makePage({ state: Object.assign({}, FRESH, { mode: 'plain' }), modesGate });
    await settle();
    await p.run('loadStateAndApply()');
    const beforeModes = p.run('currentMode');
    openModes();
    await settle();
    const afterModes = p.run('currentMode');
    const followParams = p.run('buildTranscribeParams().toString()');
    p.run("setMode('rp_enhance')");          // user chip
    await p.run('loadStateAndApply()');
    return { beforeModes, afterModes, followParams, afterChip: p.run('currentMode'),
             chipParams: p.run('buildTranscribeParams().toString()') };
  },

  async overrideSurvivesRecord() {
    const p = makePage({ state: FRESH });
    await settle();
    await p.run('loadStateAndApply()');
    p.run("pickCharacter('Bob', 'Bob')");
    await p.run('startRecording()');
    await settle();
    const duringRecording = p.run('selectedCharacter');
    const params = p.run('buildTranscribeParams().toString()');
    p.run('stopRecording()');
    await settle();
    const afterSubmit = p.run('selectedCharacter');
    await p.run('resyncWithST()');
    return { duringRecording, afterSubmit, params, afterResync: p.run('selectedCharacter'),
             follow: p.run('stFollow') };
  },

  async staleStateNotAdopted() {
    const p = makePage({ state: Object.assign({}, FRESH, { fresh: false }) });
    await settle();
    p.run("selectCharacter('Bob', 'Bob')");
    await p.run('startRecording()');
    await settle();
    return { selected: p.run('selectedCharacter'),
             params: p.run('buildTranscribeParams().toString()') };
  },

  async explicitEmptyClears() {
    const p = makePage({ state: FRESH });
    await settle();
    await p.run('loadStateAndApply()');
    const before = [p.run('selectedCharacter'), p.run('selectedPersona')];
    p.env.state = Object.assign({}, FRESH, { characterId: '', characterName: '', personaId: '' });
    await p.run('loadStateAndApply()');
    return { before, after: [p.run('selectedCharacter'), p.run('selectedPersona')] };
  },

  async touchCancelDuringPermission() {
    let resolveGum;
    const stream = makeStream();
    const p = makePage({ gum: () => new Promise(r => { resolveGum = r; }) });
    await settle();
    p.run('onRecordTouchStart({ touches: [1] })');
    const during = p.run('captureState');
    p.run('onRecordTouchCancel()');
    const afterCancel = p.run('captureState');
    resolveGum(stream);
    await settle();
    return { during, afterCancel, final: p.run('captureState'), trackStopped: stream.track.stopped,
             recorders: p.env.recorders.length };
  },

  async doubleStartRejected() {
    let resolveGum;
    const p = makePage({ gum: () => new Promise(r => { resolveGum = r; }) });
    await settle();
    const first = p.run('startRecording()');
    p.run('toggleRecord()');
    const second = await p.run('startRecording()');
    resolveGum(makeStream());
    const firstResult = await first;
    await settle();
    return { gumCalls: p.env.gumCalls, second, firstResult, state: p.run('captureState'),
             recorders: p.env.recorders.length };
  },

  async recorderThrowsReleasesMic() {
    const stream = makeStream();
    const p = makePage({ recorderThrows: true, gum: () => Promise.resolve(stream) });
    await settle();
    const started = await p.run('startRecording()');
    return { started, trackStopped: stream.track.stopped, state: p.run('captureState'),
             label: p.el('recordLabel').textContent };
  },

  async unsupportedCodec() {
    const p = makePage({ supported: [] });
    await settle();
    const started = await p.run('startRecording()');
    return { started, gumCalls: p.env.gumCalls, label: p.el('recordLabel').textContent };
  },

  async mimeFallback() {
    const p = makePage({ supported: ['audio/mp4'] });
    await settle();
    await p.run('startRecording()');
    p.run('stopRecording()');
    await settle();
    const transcribe = p.env.fetches.find(f => f.url.startsWith('/transcribe'));
    return { contentType: transcribe.headers['content-type'] };
  },

  async browserStopResetsUi() {
    const p = makePage();
    await settle();
    await p.run('startRecording()');
    p.env.recorders[0].stop();   // browser-initiated stop, not stopRecording()
    await settle();
    return { state: p.run('captureState'), uploads: p.env.fetches.filter(f => f.url.startsWith('/transcribe')).length,
             pressed: p.el('recordBtn').getAttribute('aria-pressed') };
  },

  async vadToggleKeepsLevelPolling() {
    const p = makePage({ audio: true });
    await settle();
    await p.run('startRecording()');
    const before = p.run('vadCheckInterval');
    const box = p.el('vadEnabled');
    box.checked = false; box.dispatch('change');
    const afterOff = p.run('vadCheckInterval');
    p.run('checkSilence()');
    const peak = p.run('peakInputDb');
    return { before, afterOff, peakFinite: Number.isFinite(peak) };
  },

  async transportFailureKeepsToken() {
    const p = makePage({ state: FRESH });
    await settle();
    p.env.stateMode = 'down';
    await p.run('loadStateAndApply()');
    const down = { token: p.ctx.sessionStorage.getItem('dictationToken'),
                   banner: p.el('stFollowText').textContent, status: p.el('statusText').textContent };
    p.env.stateMode = '401';
    await p.run('loadStateAndApply()');
    return { down, tokenAfter401: p.ctx.sessionStorage.getItem('dictationToken'),
             bannerAfter401: p.el('stFollowText').textContent };
  },

  async bootstrap() {
    const results = {};
    for (const mode of ['down', '503', '401', 'ok']) {
      const calls = [];
      const fetchImpl = async (url, init) => {
        calls.push({ url, headers: init.headers });
        if (mode === 'down') throw new TypeError('Failed to fetch');
        if (url === '/state') return { ok: mode === 'ok', status: { '503': 503, '401': 401, ok: 200 }[mode] };
        return { ok: true, status: 200, text: async () => '<button id="recordBtn"></button>' };
      };
      const p = makePage({ script: SCRIPTS.boot, fetchImpl });
      p.env.fetches = calls;
      await settle();
      results[mode] = {
        token: p.ctx.sessionStorage.getItem('dictationToken'),
        unpairedHidden: p.el('unpaired').hidden, retryHidden: p.el('bootRetry').hidden,
        wrote: p.env.wrote, auth: calls[0] && calls[0].headers.Authorization,
      };
    }
    return results;
  },

  async embedOrigins() {
    const p = makePage({ embed: true, referrer: 'https://st.example:8000/chat',
                         embedConfig: { embed: true, allowedOrigins: ['https://st.example'] } });
    await settle();
    const msg = (origin, source, mode) => p.run(
      `handleEmbedMessage({ origin: ${JSON.stringify(origin)}, source: ${source},
                            data: { type: 'dictation-set-mode', mode: ${JSON.stringify(mode)} } })`);
    p.run("setMode('rp_enhance', { persist: false })");
    msg('https://evil.example', 'window.parent', 'plain');
    const afterEvil = p.run('currentMode');
    msg('https://st.example:9999', '({})', 'plain');
    const afterWrongSource = p.run('currentMode');
    msg('https://st.example:9999', 'window.parent', 'plain');
    const afterTrusted = p.run('currentMode');
    p.run("postToEmbedParent({ type: 'dictation-edit', text: 'x' })");
    return { afterEvil, afterWrongSource, afterTrusted, posted: p.env.posted };
  },

  async exportClearCert() {
    const p = makePage({ state: FRESH, health: { status: 'ok', cert_expires_days: 3 },
                         transcript: [{ id: 'a', role: 'user', text: 't', chat_id: 'chat-1' }] });
    await settle();
    await p.run('loadStateAndApply()');
    await p.run('downloadSession()');
    await p.run('clearSession({ shiftKey: false })');
    await p.run('clearSession({ shiftKey: true })');
    await p.run('checkCertHealth()');
    const exportFetch = p.env.fetches.find(f => f.url === '/transcript/export.md');
    const anchor = p.ctx.document.body.children[0];
    return {
      exportAuth: exportFetch.headers.authorization,
      anchorDownload: anchor && anchor.download, anchorClicked: !!(anchor && anchor.clicked),
      deletes: p.env.fetches.filter(f => f.method === 'DELETE').map(f => f.url),
      certShown: p.el('certBanner').classList.contains('show'),
      certUrgent: p.el('certBanner').classList.contains('urgent'),
      certText: p.el('certBanner').textContent,
      farCert: p.run('certWarning(90)'),
    };
  },
};

(async () => {
  const out = {};
  for (const [name, fn] of Object.entries(scenarios)) out[name] = await fn();
  console.log(JSON.stringify(out));
})().catch(err => { console.error(err && err.stack || err); process.exit(1); });
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory) -> dict:
    scripts = tmp_path_factory.mktemp("phone") / "scripts.json"
    scripts.write_text(json.dumps(_pages()), encoding="utf-8")
    proc = subprocess.run(
        ["node", "-e", HARNESS, str(scripts)],
        cwd=ROOT, text=True, capture_output=True, check=False, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_character_names_are_rendered_as_text_never_executed(results):
    out = results["xss"]
    hostile = "x&#39;);globalThis.pwned=true;//\\\"><img src=x onerror=pwned=1>"
    assert out["pwned"] == "undefined"
    assert out["dropdownMarkup"] == ""
    assert out["optionText"] == hostile
    assert out["selected"] == hostile


def test_vocab_delete_attribute_round_trips_exact_term(results):
    markup = results["xss"]["vocabHtml"]
    match = re.search(r'data-correct="([^"]*)"', markup)
    assert match, markup
    assert html.unescape(match.group(1)) == "Say \"hi\" & <b>'yo'</b>"
    assert "<b>" not in markup


def test_following_st_sends_use_state_and_no_auto_chat_source(results):
    out = results["followParams"]
    params = dict(p.split("=", 1) for p in out["params"].split("&"))
    assert params["use_state"] == "1"
    assert "chat_source" not in params
    assert out["regenSource"] == "solo:chat-1"
    assert "/active-chat?use_state=1" in out["previewUrls"]
    # Cast comes from the same server-side state snapshot, not the phone's cache.
    assert "character" not in params
    assert "persona" not in params


def _params(query: str) -> dict:
    return dict(p.split("=", 1) for p in query.split("&"))


def test_explicit_chat_or_manual_context_never_sends_use_state(results):
    out = results["explicitChatNotMixedWithState"]
    picked = _params(out["picked"])
    assert picked["chat_source"] == "individual%3ABea"
    assert "use_state" not in picked
    assert picked["character"] == "Alice"
    manual = _params(out["manual"])
    assert "use_state" not in manual
    assert "chat_source" not in manual
    followed = _params(out["followed"])
    assert followed["use_state"] == "1"
    assert "chat_source" not in followed


def test_st_mode_adopted_after_late_modes_and_user_chip_wins(results):
    out = results["modeAdoptedWhenStateBeatsModes"]
    assert out["beforeModes"] == ""
    assert out["afterModes"] == "plain"
    follow = _params(out["followParams"])
    assert follow["use_state"] == "1"
    assert "mode" not in follow           # server resolves the ST mode from state
    assert out["afterChip"] == "rp_enhance"
    assert _params(out["chipParams"])["mode"] == "rp_enhance"


def test_take_warms_server_uses_recorder_mime_and_regen_sends_entry_id(results):
    out = results["followParams"]
    assert out["warm"] == ["POST"]
    assert out["contentType"] == "audio/webm;codecs=opus"
    assert out["recorderOptions"] == {"audioBitsPerSecond": 32000, "mimeType": "audio/webm;codecs=opus"}
    assert out["reformatBody"]["entry_id"] == "e42"
    assert out["reformatBody"]["chat_source"] == "solo:chat-1"
    assert out["finalState"] == "idle"
    # Only the background refresh at capture start; no blocking re-fetch before upload.
    assert out["stateFetchesBeforeUpload"] <= 2


def test_manual_override_survives_record_and_submit(results):
    out = results["overrideSurvivesRecord"]
    assert out["duringRecording"] == "Bob"
    assert out["afterSubmit"] == "Bob"
    assert "character=Bob" in out["params"]
    assert "use_state" not in out["params"]
    assert out["afterResync"] == "Alice"
    assert out["follow"] is True


def test_stale_state_is_not_adopted_on_record(results):
    out = results["staleStateNotAdopted"]
    assert out["selected"] == "Bob"
    assert "use_state" not in out["params"]


def test_explicit_empty_character_and_persona_clear_selection(results):
    out = results["explicitEmptyClears"]
    assert out["before"] == ["Alice", "p1"]
    assert out["after"] == ["none", "none"]


def test_touchcancel_during_permission_discards_take(results):
    out = results["touchCancelDuringPermission"]
    assert out["during"] == "starting"
    assert out["afterCancel"] == "idle"
    assert out["final"] == "idle"
    assert out["trackStopped"] is True
    assert out["recorders"] == 0


def test_second_start_is_rejected(results):
    out = results["doubleStartRejected"]
    assert out["gumCalls"] == 1
    assert out["second"] is False
    assert out["firstResult"] is True
    assert out["state"] == "recording"
    assert out["recorders"] == 1


def test_recorder_construction_failure_releases_microphone(results):
    out = results["recorderThrowsReleasesMic"]
    assert out["started"] is False
    assert out["trackStopped"] is True
    assert out["state"] == "idle"
    assert out["label"] == "This browser cannot record audio here"


def test_unsupported_codec_is_reported_without_opening_mic(results):
    out = results["unsupportedCodec"]
    assert out["started"] is False
    assert out["gumCalls"] == 0
    assert out["label"] == "This browser cannot record audio here"


def test_mp4_fallback_is_sent_with_its_real_mime(results):
    assert results["mimeFallback"]["contentType"] == "audio/mp4"


def test_browser_initiated_stop_uploads_once_and_resets_ui(results):
    out = results["browserStopResetsUi"]
    assert out["state"] == "idle"
    assert out["uploads"] == 1
    assert out["pressed"] == "false"


def test_vad_toggle_keeps_level_polling(results):
    out = results["vadToggleKeepsLevelPolling"]
    assert out["before"] is not None
    assert out["afterOff"] == out["before"]
    assert out["peakFinite"] is True


def test_transport_failure_keeps_pairing_and_reports_unreachable(results):
    out = results["transportFailureKeepsToken"]
    assert out["down"]["token"] == "synthetic-token"
    assert "reach" in out["down"]["banner"]
    assert out["down"]["status"] == "Offline"
    assert out["tokenAfter401"] is None
    assert out["bannerAfter401"].startswith("Not paired")


def test_bootstrap_only_forgets_token_on_real_401(results):
    out = results["bootstrap"]
    for mode in ("down", "503"):
        assert out[mode]["token"] == "synthetic-token", mode
        assert out[mode]["retryHidden"] is False, mode
        assert out[mode]["wrote"] is None, mode
    assert out["401"]["token"] is None
    assert out["401"]["unpairedHidden"] is False
    assert out["ok"]["wrote"] == '<button id="recordBtn"></button>'
    assert out["ok"]["auth"] == "Bearer synthetic-token"


def test_embed_messages_require_trusted_parent_and_reply_to_its_origin(results):
    out = results["embedOrigins"]
    assert out["afterEvil"] == "rp_enhance"
    assert out["afterWrongSource"] == "rp_enhance"
    assert out["afterTrusted"] == "plain"
    assert out["posted"][-1] == {"data": {"type": "dictation-edit", "text": "x"},
                                 "origin": "https://st.example:9999"}
    assert all(p["origin"] != "*" for p in out["posted"])


def test_export_is_authenticated_and_clear_honours_chat_filter(results):
    out = results["exportClearCert"]
    assert out["exportAuth"] == "Bearer synthetic-token"
    assert out["anchorDownload"] == "session.md"
    assert out["anchorClicked"] is True
    assert out["deletes"] == ["/transcript?chat=chat-1", "/transcript?all=1"]


def test_cert_banner_derives_from_expiry_days(results):
    out = results["exportClearCert"]
    assert out["certShown"] is True
    assert out["certUrgent"] is True
    assert "3 days" in out["certText"]
    assert out["farCert"] is None
