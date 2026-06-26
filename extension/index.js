// Dictation Bridge — SillyTavern extension
// Connects ST's chat input to the self-hosted dictation server at
// https://<pc-ip>:8384. A mic button in the send bar opens the server UI
// (popup or modal iframe) with the active chat/persona/character IDs pre-
// filled, then receives the formatted text via window.postMessage and
// drops it into #send_textarea.
//
// Matching server-side embed contract (see humble-munching-island.md
// phase 2e) — both sides must agree:
//   server -> extension:
//     { type: 'dictation-ready' }
//     SSE dictation-transcript { requestId, phase, has_text, source, latency_ms? } // no raw ASR text over SSE
//     SSE dictation-result { text, has_repair_trace?, mode?, formatting_skipped?, formatting_reason? }
//     postMessage dictation-ready only; dictation text flows through authed SSE, not iframe parent messaging
//     { type: 'dictation-edit', text }        // optional live mirror
//   extension -> server:
//     { type: 'dictation-set-context', context: string }
//     { type: 'dictation-set-mode', mode: string }

import { eventSource, event_types, name1, this_chid, characters, user_avatar, chat } from '../../../../script.js';
import { extension_settings, getContext } from '../../../extensions.js';
import { selected_group, groups } from '../../../group-chats.js';
import { qrcodegen } from './qrcodegen.min.js';

const MODULE = 'dictation-bridge';
const LOG = (...a) => console.log('[dictation-bridge]', ...a);
const WARN = (...a) => console.warn('[dictation-bridge]', ...a);
const ERR = (...a) => console.error('[dictation-bridge]', ...a);

function defaultServerUrl() {
    const host = window.location.hostname || '127.0.0.1';
    const safeHost = (host === '0.0.0.0' || host === '::') ? '127.0.0.1' : host;
    return `https://${safeHost}:8384`;
}

function isLocalHost(host) {
    return host === '127.0.0.1' || host === 'localhost' || host === '::1';
}

function shouldMigrateServerUrl(serverUrl) {
    // If the user opens ST through Wi-Fi/Tailscale but the saved dictation URL
    // is pinned to a different LAN interface, the browser probe fails even
    // though both services are alive. Follow the current ST host instead.
    try {
        const savedHost = new URL(serverUrl).hostname;
        const pageHost = window.location.hostname || '';
        const savedIsPrivateLan = /^192\.168\.\d+\.\d+$/.test(savedHost);
        return savedIsPrivateLan && pageHost && !isLocalHost(pageHost) && savedHost !== pageHost;
    } catch {
        return false;
    }
}

const DEFAULTS = {
    serverUrl: defaultServerUrl(),
    serverToken: '',             // MVP-1: bearer token from ~/.local/share/dictation-server/token
    autoSend: false,
    appendMode: 'replace',       // 'replace' | 'append'
    openStyle: 'popup',          // 'popup' | 'iframe'
    liveMirror: false,           // dictation-edit -> textarea while typing on phone
    pushContext: true,           // send last AI message to server on ready
    broadcastState: true,        // POST /state on chat/char/persona change + 30s heartbeat
    sseEnabled: true,            // Phase 2: subscribe to /events for direct-inject from phone
    voiceCommandsEnabled: true,  // POL-1: server-emitted dictation-command SSE dispatcher
    // ─── TTS read-back (Calliope Kokoro backend) ───────────────────────────
    ttsAutoReadAi: false,        // auto-fire TTS on every new AI message
    ttsAutoReadPersonaQuoted: true, // auto-fire TTS on new user messages, quoted dialogue only
    ttsReadStreamingPartials: false, // speak complete sentence chunks while AI text is streaming
    ttsVoice: 'af_heart',        // Kokoro default voice id
    ttsVoiceProfiles: {},        // WOW-2: character/addressee name -> Kokoro voice id
};

/**
 * MVP-1: build Authorization header object when a bearer token is configured.
 * Server (Calliope) requires this on every endpoint except /health, /manifest.json,
 * /icon-*.png, and OPTIONS preflight. Localhost requests bypass auth server-side,
 * so an empty token is a valid local-only configuration.
 */
function authHeaders() {
    const t = (settings().serverToken || '').trim();
    return t ? { 'Authorization': `Bearer ${t}` } : {};
}

// Phase 1: state broadcast.
// Posts current ST context to <serverUrl>/state so the phone UI can auto-
// configure without the user re-picking mode/character each time.
const STATE_HEARTBEAT_MS = 30_000;
let stateHeartbeatTimer = null;
let lastStatePayload = null; // for dedupe — avoid firing on duplicate events
let mobileLifecycleStatePushBound = false;

/** Active connection (popup window or iframe element). */
let activeTarget = null;
let activeIsIframe = false;
let activeModal = null;
let popupWatcher = null;

function settings() {
    if (!extension_settings[MODULE] || typeof extension_settings[MODULE] !== 'object') {
        extension_settings[MODULE] = structuredClone(DEFAULTS);
    } else {
        for (const [k, v] of Object.entries(DEFAULTS)) {
            if (extension_settings[MODULE][k] === undefined) extension_settings[MODULE][k] = v;
        }
        if (shouldMigrateServerUrl(extension_settings[MODULE].serverUrl)) {
            extension_settings[MODULE].serverUrl = defaultServerUrl();
            try { saveSettings(); } catch {}
        }
    }
    return extension_settings[MODULE];
}

function saveSettings() {
    const ctx = getContext();
    ctx.saveSettingsDebounced?.();
}

/** Resolve server origin (scheme://host[:port]) for postMessage targeting. */
function serverOrigin() {
    try {
        return new URL(settings().serverUrl).origin;
    } catch {
        return null;
    }
}

/** Best-effort current-context snapshot for query params. */
function currentContext() {
    const s = selected_group
        ? (groups?.find(g => g.id == selected_group) || null)
        : (characters?.[this_chid] || null);

    const chatId = selected_group
        ? (groups?.find(g => g.id == selected_group)?.chat_id ?? '')
        : (characters?.[this_chid]?.chat ?? '');

    const characterId = selected_group
        ? (selected_group || '')
        : (characters?.[this_chid]?.avatar || characters?.[this_chid]?.name || '');

    // Persona ID in ST is tracked via user_avatar (persona image filename).
    const personaId = user_avatar || name1 || '';

    // Last non-user message text for tonal context.
    let lastAi = '';
    if (Array.isArray(chat)) {
        for (let i = chat.length - 1; i >= 0; i--) {
            const m = chat[i];
            if (m && !m.is_user && !m.is_system && typeof m.mes === 'string' && m.mes.trim()) {
                lastAi = m.mes;
                break;
            }
        }
    }

    return { chatId, personaId, characterId, lastAi, groupName: s?.name || '' };
}

/**
 * POL-6: resolve full member name list for the current group.
 * ST stores group.members as an array of avatar filenames; map each back
 * to a human-readable character name via the global `characters[]` array.
 */
function resolveGroupMembers(group) {
    if (!group || !Array.isArray(group.members)) return [];
    const out = [];
    for (const avatarFile of group.members) {
        const ch = (characters || []).find(c => c?.avatar === avatarFile);
        if (ch?.name) out.push(ch.name);
        else if (typeof avatarFile === 'string') out.push(avatarFile.replace(/\.png$/i, ''));
    }
    return out;
}

/**
 * POL-6: scan the chat tail (most recent first) for the last AI message
 * (not user, not system) and return the speaking character name. Group
 * chats stamp `name` onto each message; fall back to `original_avatar`
 * lookup if needed.
 */
function resolveLastSpeaker() {
    if (!Array.isArray(chat)) return '';
    for (let i = chat.length - 1; i >= 0; i--) {
        const m = chat[i];
        if (!m || m.is_user || m.is_system) continue;
        if (typeof m.name === 'string' && m.name.trim()) return m.name;
        if (typeof m.original_avatar === 'string') {
            const ch = (characters || []).find(c => c?.avatar === m.original_avatar);
            if (ch?.name) return ch.name;
        }
    }
    return '';
}

function stringifySceneContinuityValue(value, depth = 0) {
    if (value == null || depth > 3) return '';
    if (typeof value === 'string') return value.trim();
    if (Array.isArray(value)) {
        return value.map(v => stringifySceneContinuityValue(v, depth + 1)).filter(Boolean).join('; ');
    }
    if (typeof value === 'object') {
        const parts = [];
        for (const [k, v] of Object.entries(value)) {
            if (typeof v === 'function') continue;
            const text = stringifySceneContinuityValue(v, depth + 1);
            if (text) parts.push(`${k}: ${text}`);
        }
        return parts.join('; ');
    }
    return String(value).trim();
}

function extractSceneContinuity() {
    const candidates = [];
    const add = (label, value) => {
        const text = stringifySceneContinuityValue(value);
        if (text && text.length > 8) candidates.push(`${label}: ${text}`);
    };
    const preferred = [];
    const addPreferred = (value) => {
        const text = stringifySceneContinuityValue(value);
        if (text && text.length > 8) preferred.push(text);
    };

    // Known/likely tracker globals if a custom continuity extension exposes one.
    try {
        addPreferred(window.sceneContinuityTracker?.getText?.());
        addPreferred(window.SceneContinuityTracker?.getText?.());
        add('scene', window.sceneContinuity || window.currentSceneContinuity);
    } catch (e) {
        WARN('scene continuity global scan failed', e?.message || e);
    }

    if (preferred.length) {
        return preferred
            .map(s => s.replace(/\s+/g, ' ').trim())
            .filter(Boolean)
            .join('\n')
            .slice(0, 2000);
    }

    try {
        add('scene', getContext()?.chatMetadata?.sceneContinuity);
    } catch (e) {
        WARN('scene continuity chatMetadata scan failed', e?.message || e);
    }

    const seen = new Set();
    return candidates
        .map(s => s.replace(/\s+/g, ' ').trim())
        .filter(s => {
            const key = s.toLowerCase();
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        })
        .join('\n')
        .slice(0, 2000);
}

function normalizeVoiceProfileKey(name) {
    return String(name || '').trim().toLowerCase();
}

function currentTtsProfileName() {
    if (selected_group && activeAddressee?.groupId === String(selected_group)
        && activeAddressee.characterName && activeAddressee.characterName !== '*all') {
        return activeAddressee.characterName;
    }
    if (selected_group) return resolveLastSpeaker() || '';
    return characters?.[this_chid]?.name || '';
}

function currentPersonaTtsProfileName() {
    return String(name1 || user_avatar || currentContext().personaId || 'Persona')
        .replace(/\.png$/i, '')
        .trim();
}

function messageSpeakerName(mesEl) {
    if (!mesEl) return '';
    const mesid = parseInt(mesEl.getAttribute('mesid') || '-1', 10);
    if (Array.isArray(chat) && mesid >= 0 && chat[mesid]) {
        const m = chat[mesid];
        if (m.is_user) return currentPersonaTtsProfileName();
        if (typeof m.name === 'string' && m.name.trim()) return m.name.trim();
        if (typeof m.original_avatar === 'string') {
            const ch = (characters || []).find(c => c?.avatar === m.original_avatar);
            if (ch?.name) return ch.name;
        }
    }
    const nameEl = mesEl.querySelector('.name_text, .ch_name, .mes_name, .avatar img[title]');
    return String(nameEl?.textContent || nameEl?.getAttribute?.('title') || '').trim();
}

function resolveTtsVoiceForMessage(mesEl) {
    const s = settings();
    const profiles = s.ttsVoiceProfiles || {};
    const speaker = messageSpeakerName(mesEl) || currentTtsProfileName();
    const profiled = profiles[normalizeVoiceProfileKey(speaker)];
    return profiled || s.ttsVoice || 'af_heart';
}

function rememberTtsVoiceForCurrentProfile(voice) {
    const name = currentTtsProfileName();
    if (!name || !voice) return '';
    const s = settings();
    if (!s.ttsVoiceProfiles || typeof s.ttsVoiceProfiles !== 'object') s.ttsVoiceProfiles = {};
    s.ttsVoiceProfiles[normalizeVoiceProfileKey(name)] = voice;
    return name;
}

function rememberTtsVoiceForPersona(voice) {
    const name = currentPersonaTtsProfileName();
    if (!name || !voice) return '';
    const s = settings();
    if (!s.ttsVoiceProfiles || typeof s.ttsVoiceProfiles !== 'object') s.ttsVoiceProfiles = {};
    s.ttsVoiceProfiles[normalizeVoiceProfileKey(name)] = voice;
    return name;
}

/** Build the /state payload the server expects. */
function buildStatePayload() {
    const ctx = currentContext();
    const s = selected_group
        ? (groups?.find(g => g.id == selected_group) || null)
        : (characters?.[this_chid] || null);
    const characterName = selected_group
        ? (s?.name || '')
        : (characters?.[this_chid]?.name || '');

    // POL-6: when the active chat is a group, expose the member roster +
    // the last AI speaker so the server (and phone UI) can render the
    // addressee picker. groupId is the ST UUID; groupMembers are full
    // human-readable character names.
    const isGroup = !!selected_group;
    const groupId = isGroup ? String(selected_group) : '';
    const groupMembers = isGroup ? resolveGroupMembers(s) : [];
    const lastSpeaker = isGroup ? resolveLastSpeaker() : '';

    return {
        chatId: ctx.chatId,
        chatType: isGroup ? 'group' : 'solo',
        characterId: ctx.characterId,
        characterName,
        personaId: ctx.personaId,
        lastAiMessage: ctx.lastAi,
        sceneContinuity: extractSceneContinuity(),
        sceneContinuityMeta: JSON.stringify(window.sceneContinuityTracker?.getMeta?.() || {}),
        sourceDevice: 'st-desktop',
        // POL-6 additions — server treats them as optional.
        groupId,
        groupName: s?.name || '',
        groupMembers,
        lastSpeaker,
    };
}

/** Fire-and-forget POST to /state. Silent on failure — server may be off. */
async function postState(reason) {
    if (!settings().broadcastState) return;
    const payload = buildStatePayload();
    if (!payload.chatId && !payload.characterId) return; // no context loaded yet
    // Dedupe identical payloads within 2s window (ST fires overlapping events)
    const key = JSON.stringify(payload);
    if (lastStatePayload && lastStatePayload.key === key
        && (Date.now() - lastStatePayload.t) < 2000) return;
    lastStatePayload = { key, t: Date.now() };

    const url = `${settings().serverUrl.replace(/\/+$/, '')}/state`;
    try {
        await fetch(url, {
            method: 'POST',
            mode: 'cors',
            cache: 'no-store',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify(payload),
        });
    } catch (e) {
        // Expected when server is off; don't spam logs.
        if (reason === 'heartbeat') return;
        WARN(`postState[${reason}] failed`, e?.message || e);
    }
}

function startStateHeartbeat() {
    stopStateHeartbeat();
    if (!settings().broadcastState) return;
    stateHeartbeatTimer = setInterval(() => postState('heartbeat'), STATE_HEARTBEAT_MS);
}

function stopStateHeartbeat() {
    if (stateHeartbeatTimer) { clearInterval(stateHeartbeatTimer); stateHeartbeatTimer = null; }
}

function setupMobileLifecycleStatePush() {
    if (mobileLifecycleStatePushBound) return;
    mobileLifecycleStatePushBound = true;
    const push = (reason) => { try { postState(reason); } catch {} };
    // Mobile browsers aggressively freeze background tabs. Push a final fresh
    // snapshot before ST is hidden and another when it is foregrounded again so
    // the standalone phone tab does not need a manual refresh to catch up.
    document.addEventListener('visibilitychange', () => {
        push(document.hidden ? 'visibility-hidden' : 'visibility-visible');
    });
    window.addEventListener('pagehide', () => push('pagehide'));
    window.addEventListener('pageshow', () => push('pageshow'));
    window.addEventListener('focus', () => push('focus'));
}

// ─── MVP-13: streaming formatter partials ──────────────────────────────────
// Server emits SSE 'dictation-token' events with {requestId, delta, done} as
// the formatter LLM streams its output. We accumulate deltas into the
// textarea via rAF (one paint per frame, regardless of how many deltas
// arrived) for perceived-latency wins. The terminal 'dictation-result' is
// still source-of-truth; when it arrives, we replace the streamed span with
// the canonical text.
let streamingSession = null; // { requestId, base, accumulated, raf }

function flushStreamingFrame() {
    if (!streamingSession) return;
    streamingSession.raf = null;
    const ta = document.getElementById('send_textarea');
    if (!ta) return;
    const sep = streamingSession.base ? '\n\n' : '';
    ta.value = streamingSession.base + sep + streamingSession.accumulated;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
}

function endStreamingSession() {
    if (streamingSession?.raf) {
        try { cancelAnimationFrame(streamingSession.raf); } catch {}
    }
    streamingSession = null;
}

// ─── MVP-16: state-machine bar above #send_textarea ───────────────────────
// Server emits SSE 'dictation-state' events with
// {requestId, state, details?, ts} as the pipeline advances. We paint a
// thin Apollo-themed band above the textarea so the user can tell which
// stage stalled. Hidden by default; appears on first non-terminal event for
// a requestId; auto-hides ~800ms after 'done'/'error'.
//
// States in pipeline order:
//   listening, transcribing, hallucination_check, vocab_correct,
//   cleaning_disfluency, cleaning_grammar, formatting, done, error.
const STATE_BAR_ID = 'dictation_bridge_state_bar';
const STATE_BAR_HIDE_DELAY_MS = 800;
const STATE_LABELS = {
    listening: 'Listening',
    transcribing: 'Transcribing',
    hallucination_check: 'Checking',
    vocab_correct: 'Vocab',
    cleaning_disfluency: 'Cleaning',
    cleaning_grammar: 'Grammar',
    formatting: 'Formatting',
    done: 'Done',
    error: 'Error',
};
const STATE_DOTS = {
    listening: '◉',          // ◉ active
    transcribing: '◐',       // ◐ in-progress
    hallucination_check: '◐',
    vocab_correct: '◐',
    cleaning_disfluency: '◐',
    cleaning_grammar: '◐',
    formatting: '◐',
    done: '✓',               // ✓
    error: '⚠',              // ⚠
};
const STATE_TERMINAL = new Set(['done', 'error']);

let stateBarSession = null;       // { requestId, lastState }
let stateBarHideTimer = null;
let stateBarObserver = null;
let lastDoneMode = '';            // remembered from dictation-result for "Done · <mode>"
let prefersReducedMotion = false;
try { prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch {}

function ensureStateBar() {
    let bar = document.getElementById(STATE_BAR_ID);
    if (bar) return bar;
    const ta = document.getElementById('send_textarea');
    if (!ta || !ta.parentElement) return null;

    bar = document.createElement('div');
    bar.id = STATE_BAR_ID;
    bar.className = 'dictation-bridge-state-bar';
    bar.setAttribute('role', 'status');
    bar.setAttribute('aria-live', 'polite');
    // Inline styles — Apollo amber tokens, sharp 2px corners, hidden by default.
    const transition = prefersReducedMotion ? 'none' : 'opacity 160ms ease, border-color 160ms ease';
    bar.style.cssText = [
        'display:none',
        'box-sizing:border-box',
        'width:100%',
        'min-height:30px',
        'padding:6px 12px',
        'margin:0 0 4px 0',
        'background:#1C150C',
        'border:1px solid rgba(255, 182, 72, 0.35)',
        'border-radius:2px',
        'color:#C9B28B',
        'font-size:12px',
        'line-height:1.4',
        'font-family:inherit',
        'align-items:center',
        'gap:10px',
        `transition:${transition}`,
        'opacity:0',
    ].join(';');
    bar.innerHTML = `
        <span class="dbb-state-dot" style="display:inline-block;min-width:1em;color:#FFB648;font-size:14px"></span>
        <span class="dbb-state-label" style="flex:1 1 auto;color:#C9B28B"></span>
        <span class="dbb-state-detail" style="color:#98876F;font-size:11px;opacity:0.85"></span>
    `;
    ta.parentElement.insertBefore(bar, ta);
    return bar;
}

/**
 * MutationObserver fallback: ST may rebuild the chat send form (e.g. on theme
 * reload or layout swap), which would orphan our state bar. Re-inject if the
 * textarea reappears without our bar adjacent.
 */
function ensureStateBarObserver() {
    if (stateBarObserver) return;
    try {
        stateBarObserver = new MutationObserver(() => {
            if (!document.getElementById(STATE_BAR_ID)
                && document.getElementById('send_textarea')) {
                ensureStateBar();
            }
        });
        stateBarObserver.observe(document.body, { childList: true, subtree: true });
    } catch (e) {
        WARN('state-bar observer setup failed', e?.message || e);
    }
}

function showStateBar() {
    const bar = ensureStateBar();
    if (!bar) return;
    if (stateBarHideTimer) {
        clearTimeout(stateBarHideTimer);
        stateBarHideTimer = null;
    }
    bar.style.display = 'flex';
    // Force reflow so the opacity transition lands instead of skipping the
    // initial frame (matters once display flips from none to flex).
    void bar.offsetWidth;
    bar.style.opacity = '1';
}

function scheduleStateBarHide() {
    if (stateBarHideTimer) clearTimeout(stateBarHideTimer);
    stateBarHideTimer = setTimeout(() => {
        stateBarHideTimer = null;
        const bar = document.getElementById(STATE_BAR_ID);
        if (!bar) return;
        bar.style.opacity = '0';
        bar.style.display = 'none';
        stateBarSession = null;
    }, prefersReducedMotion ? 0 : STATE_BAR_HIDE_DELAY_MS);
}

function paintStateBar(state, details) {
    const bar = ensureStateBar();
    if (!bar) return;
    const dot = bar.querySelector('.dbb-state-dot');
    const label = bar.querySelector('.dbb-state-label');
    const detail = bar.querySelector('.dbb-state-detail');
    if (!dot || !label || !detail) return;

    const dotChar = STATE_DOTS[state] || '○';
    const labelText = STATE_LABELS[state] || state;

    let dotColor = '#FFB648';   // amber active
    let borderColor = 'rgba(255, 182, 72, 0.35)';
    if (state === 'done') {
        dotColor = '#A8C97B';   // sage
        borderColor = 'rgba(168, 201, 123, 0.45)';
    } else if (state === 'error') {
        dotColor = '#FF5A4E';   // ember
        borderColor = 'rgba(255, 90, 78, 0.55)';
    }

    dot.textContent = dotChar;
    dot.style.color = dotColor;
    bar.style.borderColor = borderColor;

    let labelOut = labelText;
    if (state === 'done' && lastDoneMode) {
        labelOut = `${labelText} · ${lastDoneMode}`;
    }
    label.textContent = labelOut;
    detail.textContent = details ? String(details).slice(0, 140) : '';
}

function handleDictationStateEvent(data) {
    const requestId = String(data.requestId || '');
    const state = String(data.state || '');
    const details = data.details ? String(data.details) : '';
    if (!requestId || !state) return;

    // Switch session on new requestId; clear any pending hide timer so a fresh
    // utterance fired inside the 800ms hide window doesn't get auto-killed.
    if (!stateBarSession || stateBarSession.requestId !== requestId) {
        stateBarSession = { requestId, lastState: state };
    } else {
        stateBarSession.lastState = state;
    }

    paintStateBar(state, details);

    if (STATE_TERMINAL.has(state)) {
        // Brief moment of "Done"/"Error" before fade-out.
        showStateBar();
        scheduleStateBarHide();
    } else {
        showStateBar();
    }
}

// ─── POL-6: group-chat addressee picker (extension side) ──────────────────
// When chatType === 'group', render a chip row in the settings panel:
// member chips with the last-speaker highlighted, plus an "All members"
// joint-context chip. Click = persist the choice to the server's
// /state/mode-memory, keyed by `<groupId>:<characterName>` so the next
// dictation request can resolve the addressee server-side. The phone UI
// renders an equivalent picker via the /state contract.

let activeAddressee = null; // { groupId, characterName | '*all' }

async function persistAddresseeChoice(groupId, characterName) {
    if (!groupId) return;
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/state/mode-memory`;
    const key = `${groupId}:${characterName || ''}`;
    try {
        await fetch(url, {
            method: 'POST',
            mode: 'cors',
            cache: 'no-store',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({
                key,
                groupId,
                addressee: characterName || '',
                jointContext: characterName === '*all',
                ts: Date.now(),
            }),
        });
    } catch (e) {
        WARN('persistAddresseeChoice failed', e?.message || e);
    }
}

function renderAddresseePicker() {
    const host = document.getElementById('dictation_bridge_addressee');
    if (!host) return;

    if (!selected_group) {
        host.style.display = 'none';
        host.innerHTML = '';
        return;
    }

    const group = groups?.find(g => g.id == selected_group) || null;
    const members = resolveGroupMembers(group);
    const lastSpeaker = resolveLastSpeaker();
    const groupId = String(selected_group);

    // Default selection: persisted choice if it matches, else last speaker,
    // else nothing.
    const cur = activeAddressee && activeAddressee.groupId === groupId
        ? activeAddressee.characterName
        : (lastSpeaker || '');

    if (members.length === 0) {
        host.style.display = 'block';
        host.innerHTML = `<div style="font-size:12px;color:#98876F;padding:6px 0">Group has no resolved members yet.</div>`;
        return;
    }

    const chipBase = 'padding:3px 10px;border-radius:2px;cursor:pointer;font-size:12px;font-family:inherit;background:transparent';
    const chips = members.map(name => {
        const isLast = lastSpeaker && name === lastSpeaker;
        const isSelected = name === cur;
        const border = isSelected
            ? '1px solid #FFB648'
            : (isLast ? '1px solid rgba(255, 182, 72, 0.55)' : '1px solid rgba(201, 178, 139, 0.35)');
        const color = isSelected ? '#FFB648' : (isLast ? '#FFB648' : '#C9B28B');
        const tag = isLast ? `<span style="margin-left:6px;font-size:10px;color:#FFB648;opacity:0.8">last</span>` : '';
        return `<button type="button" class="dbb-addr-chip" data-name="${escapeHtml(name)}" style="${chipBase};border:${border};color:${color}">${escapeHtml(name)}${tag}</button>`;
    }).join('');

    const allSelected = cur === '*all';
    const allBorder = allSelected ? '1px solid #FFB648' : '1px dashed rgba(201, 178, 139, 0.35)';
    const allColor = allSelected ? '#FFB648' : '#98876F';
    const allChip = `<button type="button" class="dbb-addr-chip" data-name="*all" style="${chipBase};border:${allBorder};color:${allColor}">All members (joint)</button>`;

    host.style.display = 'block';
    host.innerHTML = `
        <div style="font-size:12px;color:#98876F;margin:6px 0 4px 0;display:flex;justify-content:space-between;align-items:center">
            <span>Talking to <strong style="color:#C9B28B">${escapeHtml(group?.name || 'group')}</strong></span>
            ${lastSpeaker ? `<span style="font-size:11px;color:#98876F">last spoke: <strong style="color:#C9B28B">${escapeHtml(lastSpeaker)}</strong></span>` : ''}
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${chips}${allChip}</div>
    `;

    host.querySelectorAll('.dbb-addr-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const name = chip.getAttribute('data-name') || '';
            activeAddressee = { groupId, characterName: name };
            persistAddresseeChoice(groupId, name);
            renderAddresseePicker(); // repaint with the new selection.
        });
    });
}

// ─── POL-3: low-confidence "did you mean?" banner ─────────────────────────
// Server adds `low_confidence_spans: [{text, start, end, confidence,
// alternatives?: string[]}]` to the /transcribe response and the
// dictation-result SSE event. We render a slim banner above the textarea:
//
//   Low confidence: [wear] [shore] [lithium]   ✕
//
// Each chip is a button: click → small popover with top-3 alternatives +
// "Keep original". Selecting an alternative replaces the word in the
// textarea (best-effort whole-word replacement, preserving cursor).
//
// This is the v1 banner-style implementation. The full overlay version
// (positioned wavy underline aligned with textarea content) is deferred
// to Phase 5 v2 — see roadmap POL-3 for the rationale.
const LOWCONF_BANNER_ID = 'dictation_bridge_lowconf_banner';
const LOWCONF_POPOVER_ID = 'dictation_bridge_lowconf_popover';
const LOWCONF_BANNER_HIDE_MS = 10_000; // auto-dismiss after 10s

let lowConfBannerTimer = null;
let lowConfBannerInputBound = false;

function clearLowConfBanner() {
    if (lowConfBannerTimer) { clearTimeout(lowConfBannerTimer); lowConfBannerTimer = null; }
    const el = document.getElementById(LOWCONF_BANNER_ID);
    if (el) try { el.remove(); } catch {}
    closeLowConfPopover();
}

function closeLowConfPopover() {
    const pop = document.getElementById(LOWCONF_POPOVER_ID);
    if (pop) try { pop.remove(); } catch {}
}

/** Replace first whole-word occurrence of `word` in #send_textarea with `replacement`. */
function replaceWordInTextarea(word, replacement) {
    const ta = document.getElementById('send_textarea');
    if (!ta || !word) return false;
    const value = ta.value || '';
    // Word-boundary match, case-insensitive, first occurrence only.
    const re = new RegExp(`\\b${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i');
    const m = value.match(re);
    if (!m || m.index == null) return false;
    pushUndoSnapshot('lowconf-replace');
    const start = m.index;
    const end = start + m[0].length;
    // setRangeText is the cleanest path that preserves cursor placement.
    try {
        ta.focus();
        ta.setSelectionRange(start, end);
        if (typeof ta.setRangeText === 'function') {
            ta.setRangeText(replacement, start, end, 'end');
        } else {
            ta.value = value.slice(0, start) + replacement + value.slice(end);
            ta.setSelectionRange(start + replacement.length, start + replacement.length);
        }
    } catch {
        ta.value = value.slice(0, start) + replacement + value.slice(end);
    }
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
}

async function fetchWordAlternatives(word, contextText) {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/word-alternatives`;
    try {
        const res = await fetch(url, {
            method: 'POST',
            mode: 'cors',
            cache: 'no-store',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ word, context: contextText || '' }),
        });
        if (!res.ok) return [];
        const data = await res.json();
        if (Array.isArray(data?.alternatives)) return data.alternatives.slice(0, 3);
        return [];
    } catch (e) {
        WARN('word-alternatives fetch failed', e?.message || e);
        return [];
    }
}

function buildLowConfPopover(word, alternatives, anchorRect) {
    closeLowConfPopover();
    const pop = document.createElement('div');
    pop.id = LOWCONF_POPOVER_ID;
    pop.style.cssText = [
        'position:fixed',
        `left:${Math.max(8, Math.round(anchorRect.left))}px`,
        `top:${Math.max(8, Math.round(anchorRect.bottom + 6))}px`,
        'z-index:10002',
        'background:#1C150C',
        'border:1px solid #FFB648',
        'border-radius:2px',
        'padding:8px',
        'min-width:180px',
        'max-width:280px',
        'box-shadow:0 6px 18px rgba(0,0,0,0.45)',
        'font-size:12px',
        'color:#C9B28B',
    ].join(';');
    const altRows = (alternatives || []).map(a => `
        <div class="dbb-lc-alt" data-alt="${escapeHtml(a)}" style="padding:4px 6px;cursor:pointer;border-radius:2px">${escapeHtml(a)}</div>
    `).join('');
    pop.innerHTML = `
        <div style="font-size:11px;color:#98876F;margin-bottom:4px">Replace &ldquo;${escapeHtml(word)}&rdquo; with:</div>
        <div class="dbb-lc-list">
            ${altRows || '<div style="opacity:0.65;padding:4px 6px">No alternatives available</div>'}
            <div class="dbb-lc-keep" style="padding:4px 6px;cursor:pointer;border-top:1px solid rgba(255, 182, 72, 0.18);margin-top:4px;color:#98876F">Keep original</div>
        </div>
    `;
    document.body.appendChild(pop);

    // Hover affordance.
    pop.querySelectorAll('.dbb-lc-alt, .dbb-lc-keep').forEach(el => {
        el.addEventListener('mouseenter', () => { el.style.background = 'rgba(255, 182, 72, 0.12)'; });
        el.addEventListener('mouseleave', () => { el.style.background = 'transparent'; });
    });

    pop.querySelectorAll('.dbb-lc-alt').forEach(el => {
        el.addEventListener('click', () => {
            const alt = el.getAttribute('data-alt') || '';
            if (alt && replaceWordInTextarea(word, alt)) {
                toast('success', `Replaced &ldquo;${word}&rdquo; → &ldquo;${alt}&rdquo;`);
            }
            closeLowConfPopover();
            // Remove the chip whose word we resolved.
            const chip = document.querySelector(`#${LOWCONF_BANNER_ID} [data-word="${CSS.escape(word.toLowerCase())}"]`);
            if (chip) try { chip.remove(); } catch {}
            const banner = document.getElementById(LOWCONF_BANNER_ID);
            if (banner && !banner.querySelector('.dbb-lc-chip')) clearLowConfBanner();
        });
    });
    pop.querySelector('.dbb-lc-keep')?.addEventListener('click', () => {
        const chip = document.querySelector(`#${LOWCONF_BANNER_ID} [data-word="${CSS.escape(word.toLowerCase())}"]`);
        if (chip) try { chip.remove(); } catch {}
        closeLowConfPopover();
        const banner = document.getElementById(LOWCONF_BANNER_ID);
        if (banner && !banner.querySelector('.dbb-lc-chip')) clearLowConfBanner();
    });

    // Clicks outside dismiss.
    setTimeout(() => {
        document.addEventListener('click', onDocClickClosePopover, { once: true, capture: true });
    }, 0);
}

function onDocClickClosePopover(e) {
    const pop = document.getElementById(LOWCONF_POPOVER_ID);
    if (!pop) return;
    if (pop.contains(e.target)) {
        // Re-arm if the click was inside the popover (keep open).
        document.addEventListener('click', onDocClickClosePopover, { once: true, capture: true });
        return;
    }
    closeLowConfPopover();
}

/**
 * POL-3 entry point: render the low-confidence banner from a list of
 * spans (shape: {text, alternatives?, confidence?}). De-dups by word,
 * skips empty alternatives, no-ops if list is empty.
 */
function renderLowConfBanner(spans) {
    clearLowConfBanner();
    if (!Array.isArray(spans) || spans.length === 0) return;

    // De-dup by lowercased word; preserve first occurrence's alternatives.
    const seen = new Map();
    for (const s of spans) {
        const word = String(s?.text || '').trim();
        if (!word) continue;
        const key = word.toLowerCase();
        if (seen.has(key)) continue;
        const alts = Array.isArray(s.alternatives) ? s.alternatives.filter(Boolean).slice(0, 3) : [];
        seen.set(key, { word, alts });
    }
    if (seen.size === 0) return;

    const ta = document.getElementById('send_textarea');
    if (!ta || !ta.parentElement) return;

    const banner = document.createElement('div');
    banner.id = LOWCONF_BANNER_ID;
    banner.setAttribute('role', 'status');
    banner.style.cssText = [
        'display:flex',
        'flex-wrap:wrap',
        'align-items:center',
        'gap:6px',
        'box-sizing:border-box',
        'width:100%',
        'padding:6px 10px',
        'margin:0 0 4px 0',
        'background:#1C150C',
        'border:1px solid rgba(255, 182, 72, 0.35)',
        'border-radius:2px',
        'color:#C9B28B',
        'font-size:12px',
    ].join(';');

    const labelHtml = `<span style="color:#FFB648;font-weight:600">Low confidence:</span>`;
    const chips = [...seen.values()].map(({ word, alts }) => `
        <button type="button"
                class="dbb-lc-chip"
                data-word="${escapeHtml(word.toLowerCase())}"
                data-alts="${escapeHtml(JSON.stringify(alts))}"
                title="Click to choose alternative"
                style="padding:2px 8px;border:1px solid rgba(255, 182, 72, 0.55);background:transparent;color:#FFB648;border-radius:2px;cursor:pointer;font-size:12px;font-family:inherit;text-decoration:underline wavy var(--ap-amber, #FFB648);text-decoration-thickness:1px">${escapeHtml(word)}</button>
    `).join('');
    const dismissHtml = `<button type="button" id="dbb_lc_dismiss" title="Dismiss (Esc)" style="margin-left:auto;background:transparent;border:0;color:#98876F;cursor:pointer;font-size:14px;padding:0 4px">&times;</button>`;

    banner.innerHTML = `${labelHtml}${chips}${dismissHtml}`;
    ta.parentElement.insertBefore(banner, ta);

    // Wire chip clicks.
    banner.querySelectorAll('.dbb-lc-chip').forEach(chip => {
        chip.addEventListener('click', async (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            const word = chip.textContent.trim();
            let alts = [];
            try { alts = JSON.parse(chip.getAttribute('data-alts') || '[]'); } catch {}
            // If server didn't ship alternatives, fetch fresh — uses bearer auth.
            if (!alts || alts.length === 0) {
                const ctxText = (document.getElementById('send_textarea')?.value || '').slice(0, 400);
                alts = await fetchWordAlternatives(word, ctxText);
            }
            const rect = chip.getBoundingClientRect();
            buildLowConfPopover(word, alts, rect);
        });
    });

    banner.querySelector('#dbb_lc_dismiss')?.addEventListener('click', clearLowConfBanner);

    // Auto-dismiss after 10s, on textarea input, or Esc.
    lowConfBannerTimer = setTimeout(clearLowConfBanner, LOWCONF_BANNER_HIDE_MS);
    if (!lowConfBannerInputBound) {
        ta.addEventListener('input', onTextareaInputDismissBanner, { passive: true });
        document.addEventListener('keydown', onEscDismissBanner);
        lowConfBannerInputBound = true;
    }
}

function onTextareaInputDismissBanner() {
    if (document.getElementById(LOWCONF_BANNER_ID)) clearLowConfBanner();
}

function onEscDismissBanner(e) {
    if (e.key === 'Escape' && document.getElementById(LOWCONF_BANNER_ID)) {
        clearLowConfBanner();
    }
}

// ─── POL-1: undo stack + voice command dispatcher ─────────────────────────
// Per Agent 4 §5.4: undo stack lives in the extension, not the server.
// Each writeToTextarea() snapshot pushes {prevValue, ts} (capped at 8).
// Voice command "scratch that" / "undo" pops and restores. Voice command
// "clear" wipes textarea AFTER pushing prior state to the stack so a
// follow-up "undo" recovers it.
const UNDO_STACK_CAP = 8;
const undoStack = [];

function pushUndoSnapshot(reason) {
    const ta = document.getElementById('send_textarea');
    if (!ta) return;
    const prev = ta.value || '';
    // Skip duplicate snapshots — repeated identical state pollutes the stack.
    const top = undoStack[undoStack.length - 1];
    if (top && top.prevValue === prev) return;
    undoStack.push({ prevValue: prev, ts: Date.now(), reason: reason || '' });
    while (undoStack.length > UNDO_STACK_CAP) undoStack.shift();
}

function popUndoSnapshot() {
    return undoStack.pop() || null;
}

/** Toast helper. Reuses ST's globally jQuery-loaded toastr. 1.2s default. */
function toast(level, msg, opts = {}) {
    if (!window.toastr) return;
    const fn = window.toastr[level] || window.toastr.success;
    try { fn(msg, 'Dictation Bridge', { timeOut: 1200, ...opts }); }
    catch {}
}

/**
 * POL-1: voice-command dispatcher. Server emits SSE 'dictation-command'
 * with shape {requestId, intent, args, source_text, residual}. We map
 * intents to ST DOM ops. All commands fire a 1.2s toastr success with the
 * action label so the user sees voice → action feedback.
 */
function appendToTextarea(extra) {
    const ta = document.getElementById('send_textarea');
    if (!ta) return;
    pushUndoSnapshot('append');
    const sep = ta.value && !/\s$/.test(ta.value) ? '' : '';
    ta.value = (ta.value || '') + sep + extra;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    try { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } catch {}
}

function replaceTextarea(value) {
    const ta = document.getElementById('send_textarea');
    if (!ta) return;
    pushUndoSnapshot('replace');
    ta.value = value || '';
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    try { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } catch {}
}

function clickIfVisible(selector, scope) {
    const root = scope || document;
    const el = root.querySelector(selector);
    if (!el) return false;
    // crude visibility check (offsetParent is null when display:none)
    if (el.offsetParent === null && el !== document.body) return false;
    el.click();
    return true;
}

function lastMessageEl() {
    const list = document.querySelectorAll('#chat .mes');
    return list.length ? list[list.length - 1] : null;
}

function handleDictationCommand(data) {
    if (!settings().voiceCommandsEnabled) return;
    const intent = String(data.intent || '').toLowerCase().trim();
    const args = (data.args && typeof data.args === 'object') ? data.args : {};
    const residual = typeof data.residual === 'string' ? data.residual : '';
    if (!intent) return;

    switch (intent) {
        case 'send': {
            const ok = clickIfVisible('#send_but');
            if (ok) toast('success', 'Sent');
            else toast('warning', 'Send button not found');
            break;
        }
        case 'swipe': {
            const direction = String(args.direction || 'right').toLowerCase();
            const last = lastMessageEl();
            if (!last) { toast('warning', 'No message to swipe'); break; }
            const sel = direction === 'left' ? '.mes_swipe_left' : '.mes_swipe_right';
            const ok = clickIfVisible(sel, last);
            if (ok) toast('success', `Swiped ${direction}`);
            else toast('warning', `Swipe ${direction} unavailable`);
            break;
        }
        case 'regenerate': {
            const ok = clickIfVisible('#option_regenerate');
            if (ok) toast('success', 'Regenerated');
            else toast('warning', 'Regenerate option not found');
            break;
        }
        case 'clear': {
            const ta = document.getElementById('send_textarea');
            if (!ta) { toast('warning', 'Textarea not found'); break; }
            pushUndoSnapshot('clear');
            ta.value = '';
            ta.dispatchEvent(new Event('input', { bubbles: true }));
            // 3s undo affordance per spec — toastr "info" with longer hold + clickable.
            if (window.toastr) {
                try {
                    window.toastr.info(
                        'Cleared. Tap to undo.',
                        'Dictation Bridge',
                        {
                            timeOut: 3000,
                            extendedTimeOut: 1000,
                            closeButton: true,
                            tapToDismiss: true,
                            onclick: () => {
                                const snap = popUndoSnapshot();
                                if (snap && document.getElementById('send_textarea')) {
                                    const t = document.getElementById('send_textarea');
                                    t.value = snap.prevValue;
                                    t.dispatchEvent(new Event('input', { bubbles: true }));
                                    toast('success', 'Restored');
                                }
                            },
                        },
                    );
                } catch { toast('success', 'Cleared'); }
            }
            break;
        }
        case 'delete that':
        case 'delete_that':
        case 'delete last':
        case 'delete_last': {
            // Prefer the last AI message delete button; fall back to popping
            // the most recent dictation snapshot when there's no AI message
            // visible (per task spec).
            const last = lastMessageEl();
            const btn = last?.querySelector('.mes_button.mes_delete') || last?.querySelector('[data-i18n="Delete this message"]') || last?.querySelector('.mes_button_delete');
            if (btn) {
                btn.click();
                toast('success', 'Deleted last');
            } else {
                const snap = popUndoSnapshot();
                if (snap) {
                    const ta = document.getElementById('send_textarea');
                    if (ta) {
                        ta.value = snap.prevValue;
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    toast('success', 'Deleted last dictation');
                } else {
                    toast('warning', 'Nothing to delete');
                }
            }
            break;
        }
        case 'scratch that':
        case 'scratch_that':
        case 'undo': {
            const snap = popUndoSnapshot();
            if (!snap) { toast('warning', 'Nothing to undo'); break; }
            const ta = document.getElementById('send_textarea');
            if (ta) {
                ta.value = snap.prevValue;
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                try { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } catch {}
            }
            toast('success', 'Reverted last dictation');
            break;
        }
        case 'new paragraph':
        case 'new_paragraph': {
            appendToTextarea('\n\n');
            toast('success', 'New paragraph');
            break;
        }
        case 'scene break':
        case 'scene_break': {
            appendToTextarea('\n\n***\n\n');
            toast('success', 'Scene break');
            break;
        }
        case 'stop':
        case 'cancel': {
            const stopBtn = document.getElementById('mes_stop');
            const ttsActive = !!(currentTtsAudio && currentTtsBtn);
            let stopped = false;
            if (ttsActive) {
                stopTts();
                stopped = true;
            }
            if (stopBtn && stopBtn.offsetParent !== null) {
                stopBtn.click();
                stopped = true;
            }
            if (stopped) toast('success', 'Stopped');
            else toast('info', 'Nothing to stop');
            break;
        }
        case 'append': {
            if (residual) {
                appendToTextarea((residual.startsWith(' ') ? '' : ' ') + residual);
                toast('success', 'Appended');
            } else {
                toast('warning', 'Append: no text');
            }
            break;
        }
        case 'replace': {
            if (residual) {
                replaceTextarea(residual);
                toast('success', 'Replaced');
            } else {
                toast('warning', 'Replace: no text');
            }
            break;
        }
        case 'read last':
        case 'read_last':
        case 'read': {
            const ok = readLastAiMessage();
            if (ok) toast('success', 'Reading last AI message');
            // readLastAiMessage already toasts on no-message-found.
            break;
        }
        case 'read all':
        case 'read_all':
        case 'toggle read':
        case 'toggle_read':
        case 'auto read':
        case 'auto_read': {
            toggleAutoReadAi();
            // toggleAutoReadAi already toasts current state.
            break;
        }
        default:
            WARN(`unknown voice command intent: ${intent}`);
            break;
    }
}

// ─── Phase 2: SSE direct-inject from phone ─────────────────────────────────
// The phone POSTs to /send-to-st, which fans out to all ST tabs subscribed
// to /events. Reconnect is best-effort with exponential backoff.
let sseSource = null;
let sseReconnectDelay = 1000;
const SSE_RECONNECT_CAP = 30_000;
let sseReconnectTimer = null;
let sseStatus = { state: 'disconnected', lastEventAt: 0, lastError: '' };
let serverAuthStatus = { health: 'unknown', token: 'unknown', lastCheckedAt: 0, lastError: '' };

function currentContextLabel() {
    const ctx = currentContext();
    const parts = [];
    if (selected_group) {
        const group = groups?.find(g => g.id == selected_group) || null;
        if (group?.name) parts.push(`Group: ${group.name}`);
        else if (ctx.chatId) parts.push(`Group: ${ctx.chatId}`);
        const lastSpeaker = resolveLastSpeaker();
        if (lastSpeaker) parts.push(`Last speaker: ${lastSpeaker}`);
    } else {
        const characterName = characters?.[this_chid]?.name || '';
        if (characterName) parts.push(`Character: ${characterName}`);
        else if (ctx.characterId) parts.push(`Character: ${ctx.characterId}`);
    }
    if (ctx.personaId) parts.push(`Persona: ${String(ctx.personaId).split(/[\\/]/).pop()}`);
    return parts.join(' • ');
}

function tokenStatusLabel() {
    if (!(settings().serverToken || '').trim()) return 'missing';
    return serverAuthStatus.token || 'unknown';
}

function updateSseStatusIndicator() {
    const dot = document.getElementById('dictation_bridge_sse_dot');
    const label = document.getElementById('dictation_bridge_sse_label');
    if (dot && label) {
        const colors = {
            connected: '#4caf50',
            connecting: '#e6a756',
            disconnected: '#7a7a9a',
            error: '#cc5555',
        };
        dot.style.background = colors[sseStatus.state] || colors.disconnected;
        const ago = sseStatus.lastEventAt
            ? ` (last ${Math.max(0, Math.round((Date.now() - sseStatus.lastEventAt) / 1000))}s ago)`
            : '';
        label.textContent = `SSE: ${sseStatus.state}${ago}`;
    }
    // Mirror into the quick-launch card if it's mounted.
    try { paintQuickLaunchStatus(); } catch {}
}

function connectSSE() {
    if (!settings().sseEnabled) return;
    disconnectSSE(); // ensure single connection

    // MVP-1: EventSource API does not support custom headers, so the bearer token
    // is passed as a query param. Server's /events handler accepts ?token=<value>
    // as an Authorization-equivalent for SSE specifically.
    const token = (settings().serverToken || '').trim();
    const base = `${settings().serverUrl.replace(/\/+$/, '')}/events`;
    const url = token ? `${base}?token=${encodeURIComponent(token)}` : base;
    try {
        sseSource = new EventSource(url);
        sseStatus.state = 'connecting';
        updateSseStatusIndicator();
    } catch (e) {
        ERR('SSE construct failed', e?.message || e);
        sseStatus.state = 'error';
        sseStatus.lastError = e?.message || String(e);
        updateSseStatusIndicator();
        scheduleSseReconnect();
        return;
    }

    sseSource.addEventListener('open', () => {
        LOG('SSE open');
        sseStatus.state = 'connected';
        sseReconnectDelay = 1000; // reset backoff
        updateSseStatusIndicator();
    });

    sseSource.addEventListener('ready', (e) => {
        // Server's opening handshake.
        sseStatus.state = 'connected';
        sseStatus.lastEventAt = Date.now();
        updateSseStatusIndicator();
    });

    // MVP-13: streaming formatter deltas (perceived-latency only; the canonical
    // dictation-result event below replaces the streamed text with the polished
    // assembled version once formatting finishes).
    sseSource.addEventListener('dictation-token', (e) => {
        sseStatus.lastEventAt = Date.now();
        try {
            let data;
            try { data = JSON.parse(e.data); }
            catch { WARN('SSE dictation-token: bad JSON'); return; }
            const requestId = String(data.requestId || '');
            const delta = String(data.delta || '');
            const done = !!data.done;
            if (!requestId) return;

            const ta = document.getElementById('send_textarea');
            if (!ta) return;
            const cfg = settings();

            // First token for a new request: snapshot base and init session.
            if (!streamingSession || streamingSession.requestId !== requestId) {
                clearRepairTrace();
                endStreamingSession();
                const base = (cfg.appendMode === 'append' && ta.value)
                    ? ta.value.replace(/\s+$/, '')
                    : '';
                streamingSession = {
                    requestId,
                    base,
                    accumulated: '',
                    raf: null,
                    appendMode: cfg.appendMode,
                };
                if (cfg.appendMode !== 'append') {
                    // Clear textarea so the streamed text shows from a clean slate.
                    ta.value = '';
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }

            if (delta) {
                streamingSession.accumulated += delta;
                if (!streamingSession.raf) {
                    streamingSession.raf = requestAnimationFrame(flushStreamingFrame);
                }
            }

            if (done) {
                // Force a final flush so the user sees the fully streamed text
                // before the canonical dictation-result lands.
                flushStreamingFrame();
                // Keep the session alive (don't null) so the upcoming
                // dictation-result handler can detect + replace the streamed
                // span with the canonical polished text.
                if (streamingSession) streamingSession.streamComplete = true;
            }
        } catch (err) {
            WARN('dictation-token handler failed; falling back to batch result', err?.message || err);
            endStreamingSession();
        }
    });

    // POL-1: server-emitted voice command dispatch. Server runs the
    // 'computer:' / 'OOC:' regex pre-pass; on a hit it emits a
    // dictation-command SSE event. When voiceCommandsEnabled is false,
    // ignore the event entirely so a single ST instance can opt out
    // (useful when two ST tabs would otherwise both fire the action).
    sseSource.addEventListener('dictation-command', (e) => {
        sseStatus.lastEventAt = Date.now();
        if (!settings().voiceCommandsEnabled) return;
        try {
            const data = JSON.parse(e.data);
            handleDictationCommand(data);
        } catch (err) {
            WARN('SSE dictation-command: bad JSON', err?.message || err);
        }
    });

    // MVP-16: pipeline state-machine bar above #send_textarea.
    sseSource.addEventListener('dictation-state', (e) => {
        sseStatus.lastEventAt = Date.now();
        try {
            const data = JSON.parse(e.data);
            handleDictationStateEvent(data);
        } catch (err) {
            WARN('SSE dictation-state: bad JSON', err?.message || err);
        }
    });

    // Whisper-Flow steal: raw ASR final preview before formatter/pipeline work.
    // It is intentionally separate from dictation-token/dictation-result so raw
    // transcripts never write into #send_textarea or duplicate canonical output.
    sseSource.addEventListener('dictation-transcript', (e) => {
        sseStatus.lastEventAt = Date.now();
        updateSseStatusIndicator();
        let data;
        try { data = JSON.parse(e.data); }
        catch { WARN('SSE dictation-transcript: bad JSON'); return; }
        const requestId = String(data.requestId || '');
        const phase = String(data.phase || '');
        if (!requestId) return;
        const label = phase === 'final' ? 'Heard' : 'Hearing';
        const preview = 'speech';
        const latency = Number.isFinite(Number(data.latency_ms)) ? ` · ${Math.round(Number(data.latency_ms))}ms` : '';
        paintStateBar('transcribing', `${label}: ${preview}${latency}`);
        showStateBar();
    });

    sseSource.addEventListener('dictation-result', (e) => {
        sseStatus.lastEventAt = Date.now();
        updateSseStatusIndicator();
        let data;
        try { data = JSON.parse(e.data); }
        catch { WARN('SSE dictation-result: bad JSON'); return; }
        let text = String(data.text || '').trim();
        if (!text) { WARN('SSE dictation-result: empty text'); endStreamingSession(); return; }
        // MVP-16: remember the mode for "Done · <mode>" labelling on the
        // state bar's terminal frame.
        if (data.mode && typeof data.mode === 'string') lastDoneMode = data.mode;
        const cfg = settings();
        // Per-event auto_send overrides setting when explicitly true; otherwise setting applies.
        const doAutoSend = data.auto_send === true ? true : !!cfg.autoSend;

        // POL-1: server strips the 'OOC:' prefix and signals via mode_override.
        // Prepend the OOC tag back so the chat displays the convention ST
        // readers expect (OOC chunks are routed differently downstream).
        const modeOverride = String(data.mode_override || data.modeOverride || '').toLowerCase();
        if (modeOverride === 'ooc' || modeOverride === 'grammar_clean'
                && data.is_ooc === true) {
            if (!/^\s*ooc\b/i.test(text)) text = `OOC: ${text}`;
        }

        // MVP-13: if we streamed deltas for this request, replace the streamed
        // span with the canonical text. The session captured the original
        // textarea base, so we can rebuild deterministically regardless of
        // appendMode.
        if (streamingSession && (!data.requestId || streamingSession.requestId === String(data.requestId))) {
            const ta = document.getElementById('send_textarea');
            const sessAppendMode = streamingSession.appendMode;
            const base = streamingSession.base;
            endStreamingSession();
            if (ta) {
                pushUndoSnapshot('dictation-result-stream');
                const sep = (sessAppendMode === 'append' && base) ? '\n\n' : '';
                ta.value = (sessAppendMode === 'append' ? base + sep : '') + text;
                ta.dispatchEvent(new Event('input', { bubbles: true }));
                try { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } catch {}
                if (doAutoSend) {
                    const btn = document.getElementById('send_but');
                    if (btn) btn.click(); else WARN('send_but not found, cannot auto-send');
                    setTimeout(() => maybeReadDictatedPersonaText(text), 200);
                }
            }
        } else {
            writeToTextarea(text, { autoSend: doAutoSend, appendMode: cfg.appendMode });
            if (doAutoSend) setTimeout(() => maybeReadDictatedPersonaText(text), 200);
        }

        renderRepairTraceFromPayload(data, text);

        if (data.formatting_skipped && window.toastr) {
            const reason = data.formatting_reason ? `: ${data.formatting_reason}` : '';
            window.toastr.warning(`RP formatting skipped${reason}. Raw transcript used.`, 'Dictation Bridge');
        } else if (window.toastr) {
            const repair = data.has_repair_trace === true
                ? ' · repair trace available on phone'
                : '';
            window.toastr.success(`Received from phone${repair}`, 'Dictation Bridge', { timeOut: 1500 });
        }

        // POL-3: render low-confidence "did you mean?" banner if the server
        // tagged any spans below the confidence threshold. Banner auto-hides
        // on textarea input, Esc, or after 10s.
        if (Array.isArray(data.low_confidence_spans) && data.low_confidence_spans.length) {
            try { renderLowConfBanner(data.low_confidence_spans); }
            catch (err) { WARN('lowconf banner render failed', err?.message || err); }
        } else if (Array.isArray(data.lowConfidenceSpans) && data.lowConfidenceSpans.length) {
            // Tolerate camelCase server payload as well.
            try { renderLowConfBanner(data.lowConfidenceSpans); }
            catch (err) { WARN('lowconf banner render failed', err?.message || err); }
        }
    });

    sseSource.addEventListener('error', (e) => {
        // EventSource auto-reconnects, but on a closed state we force our own
        // backoff path so the UI reflects it and self-signed cert failures
        // don't silently loop.
        if (sseSource && sseSource.readyState === EventSource.CLOSED) {
            sseStatus.state = 'error';
            sseStatus.lastError = 'Connection closed';
            updateSseStatusIndicator();
            scheduleSseReconnect();
        } else {
            sseStatus.state = 'connecting';
            updateSseStatusIndicator();
        }
    });
}

function scheduleSseReconnect() {
    if (sseReconnectTimer) return;
    if (!settings().sseEnabled) return;
    const delay = sseReconnectDelay;
    sseReconnectDelay = Math.min(SSE_RECONNECT_CAP, sseReconnectDelay * 2);
    LOG(`SSE reconnect in ${delay}ms`);
    sseReconnectTimer = setTimeout(() => {
        sseReconnectTimer = null;
        connectSSE();
    }, delay);
}

function disconnectSSE() {
    if (sseReconnectTimer) { clearTimeout(sseReconnectTimer); sseReconnectTimer = null; }
    if (sseSource) {
        try { sseSource.close(); } catch {}
        sseSource = null;
    }
    sseStatus.state = 'disconnected';
    updateSseStatusIndicator();
    // MVP-13: drop any in-flight stream — caller will reconnect and the next
    // dictation-result will land via the batch path.
    endStreamingSession();
}

function buildPairedPhoneUrl({ embed = true } = {}) {
    const cfg = settings();
    const ctx = currentContext();
    const base = cfg.serverUrl.replace(/\/+$/, '');
    const qp = new URLSearchParams();
    if (embed) qp.set('embed', '1');

    if (ctx.chatId) qp.set('chat', String(ctx.chatId));
    if (ctx.personaId) qp.set('persona', String(ctx.personaId));
    if (ctx.characterId) qp.set('character', String(ctx.characterId));
    // Pass bearer token via query so the phone UI can stash it in
    // sessionStorage and attach to its own fetch + EventSource calls. The
    // server scrubs ?token= from browser history on load and redacts request
    // logs, but users should still avoid screenshots of the copied URL.
    const token = (cfg.serverToken || '').trim();
    if (token) qp.set('token', token);
    return `${base}/?${qp.toString()}`;
}

function buildEmbedUrl() {
    return buildPairedPhoneUrl({ embed: true });
}

async function copyPairedPhoneUrl() {
    const url = buildPairedPhoneUrl({ embed: false });
    try {
        await navigator.clipboard.writeText(url);
        toast('success', 'Paired phone URL copied. Treat it like a password.');
    } catch (e) {
        window.prompt('Copy paired phone URL (contains bearer token):', url);
    }
}

function openPairedPhoneUrl() {
    const url = buildPairedPhoneUrl({ embed: false });
    window.open(url, 'calliope_pair_phone', 'width=500,height=900,menubar=no,toolbar=no,location=yes,status=no,scrollbars=yes,resizable=yes');
}

function drawQrToCanvas(text, canvas) {
    const QrCode = qrcodegen?.QrCode;
    if (!QrCode) throw new Error('QR library unavailable');
    const qr = QrCode.encodeText(text, QrCode.Ecc.MEDIUM);
    const border = 4;
    const targetPx = 220;
    const scale = Math.max(2, Math.floor(targetPx / (qr.size + border * 2)));
    const width = (qr.size + border * 2) * scale;
    canvas.width = width;
    canvas.height = width;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${width}px`;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas 2D unavailable');
    ctx.fillStyle = '#fffaf0';
    ctx.fillRect(0, 0, width, width);
    ctx.fillStyle = '#1C150C';
    for (let y = 0; y < qr.size; y++) {
        for (let x = 0; x < qr.size; x++) {
            if (qr.getModule(x, y)) {
                ctx.fillRect((x + border) * scale, (y + border) * scale, scale, scale);
            }
        }
    }
}

function clearPairQrPanel(host = document) {
    const canvas = host.querySelector?.('#dictation_bridge_pair_qr_canvas');
    if (canvas) {
        try {
            const ctx = canvas.getContext('2d');
            if (ctx) ctx.clearRect(0, 0, canvas.width || 0, canvas.height || 0);
        } catch {}
        canvas.width = 0;
        canvas.height = 0;
    }
    const urlNode = host.querySelector?.('#dictation_bridge_pair_qr_url');
    if (urlNode) urlNode.remove();
}

function hidePairQrPanel(host = document) {
    clearPairQrPanel(host);
    const panel = host.querySelector?.('#dictation_bridge_pair_qr_panel');
    if (panel) panel.style.display = 'none';
}

async function showPairQrPanel(host = document) {
    const panel = host.querySelector?.('#dictation_bridge_pair_qr_panel');
    const canvas = host.querySelector?.('#dictation_bridge_pair_qr_canvas');
    if (!panel || !canvas) return;
    hidePairQrPanel(host);
    const url = buildPairedPhoneUrl({ embed: false });
    const urlNode = document.createElement('span');
    urlNode.id = 'dictation_bridge_pair_qr_url';
    urlNode.hidden = true;
    urlNode.textContent = url;
    try {
        drawQrToCanvas(url, canvas);
        panel.appendChild(urlNode);
        panel.style.display = 'block';
    } catch (e) {
        urlNode.remove();
        clearPairQrPanel(host);
        WARN('local QR render failed; falling back to copy URL', e?.message || e);
        toast('warning', 'QR render failed; copying pairing URL instead.');
        await copyPairedPhoneUrl();
    }
}

/** Ping the server's /health to give an early failure before opening UI. */
async function probeServer() {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/health`;
    try {
        // /health does not require auth server-side, but sending the header is
        // harmless and lets the server count authed clients in audit logs.
        const res = await fetch(url, {
            method: 'GET',
            mode: 'cors',
            cache: 'no-store',
            headers: { ...authHeaders() },
        });
        serverAuthStatus.health = res.ok ? 'reachable' : 'error';
        serverAuthStatus.lastCheckedAt = Date.now();
        serverAuthStatus.lastError = res.ok ? '' : `health_http_${res.status}`;
        paintQuickLaunchStatus();
        return res.ok;
    } catch (e) {
        // Self-signed cert will trip this on first visit. Caller decides how to react.
        WARN('server probe failed', e?.message || e);
        serverAuthStatus.health = 'unreachable';
        serverAuthStatus.lastCheckedAt = Date.now();
        serverAuthStatus.lastError = e?.message || 'network';
        paintQuickLaunchStatus();
        return false;
    }
}

/** Check a protected endpoint so health does not get misread as pairing/auth. */
async function probeServerAuth() {
    const cfg = settings();
    const token = (cfg.serverToken || '').trim();
    if (!token) {
        serverAuthStatus.token = 'missing';
        serverAuthStatus.lastCheckedAt = Date.now();
        paintQuickLaunchStatus();
        return false;
    }
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/state`;
    try {
        const res = await fetch(url, {
            method: 'GET',
            mode: 'cors',
            cache: 'no-store',
            headers: { ...authHeaders() },
        });
        serverAuthStatus.lastCheckedAt = Date.now();
        if (res.status === 401) {
            serverAuthStatus.token = 'invalid';
            serverAuthStatus.lastError = 'unauthorized';
            paintQuickLaunchStatus();
            return false;
        }
        serverAuthStatus.token = res.ok ? 'valid' : 'error';
        serverAuthStatus.lastError = res.ok ? '' : `state_http_${res.status}`;
        paintQuickLaunchStatus();
        return res.ok;
    } catch (e) {
        serverAuthStatus.token = 'unknown';
        serverAuthStatus.lastCheckedAt = Date.now();
        serverAuthStatus.lastError = e?.message || 'network';
        paintQuickLaunchStatus();
        return false;
    }
}

/** Inject the mic button into the chat send bar. Idempotent. */
function injectMicButton() {
    if (document.getElementById('dictation_bridge_mic')) return;
    const host = document.getElementById('rightSendForm');
    if (!host) {
        WARN('rightSendForm not found — delaying mic button inject');
        setTimeout(injectMicButton, 500);
        return;
    }

    const btn = document.createElement('div');
    btn.id = 'dictation_bridge_mic';
    btn.className = 'fa-solid fa-microphone interactable dictation-bridge-mic';
    btn.setAttribute('title', 'Dictation bridge (open dictation UI)');
    btn.setAttribute('tabindex', '0');
    btn.addEventListener('click', onMicClick);
    btn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onMicClick(); }
    });

    // Sit to the left of the paper-plane send button if present, otherwise at the end.
    const sendBut = document.getElementById('send_but');
    if (sendBut && sendBut.parentElement === host) {
        host.insertBefore(btn, sendBut);
    } else {
        host.appendChild(btn);
    }
}

function setMicActive(active) {
    const btn = document.getElementById('dictation_bridge_mic');
    if (btn) btn.classList.toggle('dictation-bridge-mic--active', !!active);
}

async function onMicClick() {
    if (activeTarget) {
        // Toggle: already open -> focus or close.
        if (activeIsIframe) {
            closeActive();
        } else {
            try { activeTarget.focus(); } catch {}
        }
        return;
    }

    const ok = await probeServer();
    if (!ok) {
        const toast = window.toastr;
        const msg = 'Dictation server unreachable. Check serverUrl, accept the self-signed cert in a new tab, then retry.';
        if (toast?.error) toast.error(msg, 'Dictation Bridge');
        else alert(msg);
        return;
    }

    const authed = await probeServerAuth();
    if (!authed) {
        const toast = window.toastr;
        const tokenState = tokenStatusLabel();
        const msg = tokenState === 'missing'
            ? 'Dictation server reachable, but no bearer token is configured. Paste the server token in Dictation Bridge settings.'
            : 'Dictation server reachable, but the bearer token is invalid/stale. Re-pair or sync the token before opening the phone page.';
        if (toast?.error) toast.error(msg, 'Dictation Bridge');
        else alert(msg);
        return;
    }

    // Make the launch itself a context handoff. This matters on Android/Chrome:
    // once the dictation tab is foregrounded, the ST tab can be frozen before
    // the normal 30s heartbeat gets another chance to run.
    await postState('mic-open');

    const url = buildEmbedUrl();
    if (settings().openStyle === 'iframe') {
        openIframe(url);
    } else {
        openPopup(url);
    }
    setMicActive(true);
}

function openPopup(url) {
    const w = 500, h = 900;
    const features = `width=${w},height=${h},menubar=no,toolbar=no,location=no,status=no,scrollbars=yes,resizable=yes`;
    const win = window.open(url, 'dictation_bridge', features);
    if (!win) {
        const toast = window.toastr;
        const msg = 'Popup blocked. Allow popups for SillyTavern or switch openStyle to "iframe".';
        if (toast?.error) toast.error(msg, 'Dictation Bridge');
        else alert(msg);
        setMicActive(false);
        return;
    }
    activeTarget = win;
    activeIsIframe = false;
    popupWatcher = setInterval(() => {
        if (!activeTarget || activeTarget.closed) closeActive();
    }, 500);
}

function openIframe(url) {
    const modal = document.createElement('div');
    modal.className = 'dictation-bridge-modal';

    const backdrop = document.createElement('div');
    backdrop.className = 'dictation-bridge-backdrop';

    const wrap = document.createElement('div');
    wrap.className = 'dictation-bridge-frame-wrap';

    const close = document.createElement('div');
    close.className = 'dictation-bridge-close';
    close.title = 'Close';
    close.textContent = '×';

    const iframe = document.createElement('iframe');
    iframe.className = 'dictation-bridge-iframe';
    iframe.src = url;
    iframe.allow = 'microphone; clipboard-write';

    wrap.append(close, iframe);
    modal.append(backdrop, wrap);
    backdrop.addEventListener('click', closeActive);
    close.addEventListener('click', closeActive);
    document.body.appendChild(modal);
    activeModal = modal;
    activeTarget = iframe.contentWindow;
    activeIsIframe = true;
}

function closeActive() {
    if (popupWatcher) { clearInterval(popupWatcher); popupWatcher = null; }
    if (activeIsIframe && activeModal) {
        try { activeModal.remove(); } catch {}
    } else if (activeTarget && !activeTarget.closed) {
        try { activeTarget.close(); } catch {}
    }
    activeTarget = null;
    activeIsIframe = false;
    activeModal = null;
    setMicActive(false);
}

/** Ship tonal context to the server UI once it's ready. */
function pushContextIfEnabled() {
    if (!settings().pushContext) return;
    const { lastAi } = currentContext();
    if (!lastAi) return;
    postToServer({ type: 'dictation-set-context', context: lastAi });
}

function postToServer(payload) {
    if (!activeTarget) return;
    const origin = serverOrigin();
    if (!origin) {
        WARN('postToServer skipped: invalid serverUrl');
        return;
    }
    try {
        activeTarget.postMessage(payload, origin);
    } catch (e) {
        WARN('postToServer failed', e?.message || e);
    }
}

/** Write text into ST's chat input, firing the input event so ST picks it up. */
function writeToTextarea(text, { autoSend = false, appendMode = 'replace' } = {}) {
    const ta = document.getElementById('send_textarea');
    if (!ta) { WARN('send_textarea not found'); return; }
    // POL-1: snapshot pre-write state so 'scratch that' / 'undo' can restore.
    pushUndoSnapshot('writeToTextarea');
    const next = appendMode === 'append' && ta.value
        ? (ta.value.replace(/\s+$/, '') + '\n\n' + text)
        : text;
    ta.value = next;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    // ST auto-grows the textarea on input; focus helps visual feedback.
    try { ta.focus(); ta.setSelectionRange(next.length, next.length); } catch {}

    if (autoSend) {
        // Canonical ST path: click #send_but. This routes through sendTextareaMessage()
        // -> Generate(), preserving all the usual pre-send hooks.
        const btn = document.getElementById('send_but');
        if (btn) btn.click();
        else WARN('send_but not found, cannot auto-send');
    }
}

/** Global postMessage handler. */
function onWindowMessage(event) {
    if (!activeTarget) return;
    const origin = serverOrigin();
    if (!origin || event.origin !== origin) return;
    const data = event?.data;
    if (!data || typeof data !== 'object') return;
    const type = typeof data.type === 'string' ? data.type : '';
    if (!type.startsWith('dictation-')) return;

    switch (type) {
        case 'dictation-ready':
            LOG('server ready');
            pushContextIfEnabled();
            break;
        case 'dictation-result': {
            const text = String(data.text ?? '');
            if (!text) { WARN('dictation-result had empty text'); return; }
            const cfg = settings();
            writeToTextarea(text, { autoSend: cfg.autoSend, appendMode: cfg.appendMode });
            if (cfg.autoSend) setTimeout(() => maybeReadDictatedPersonaText(text), 200);
            renderRepairTraceFromPayload(data, text);
            if (data.formatting_skipped && window.toastr) {
                const reason = data.formatting_reason ? `: ${data.formatting_reason}` : '';
                window.toastr.warning(`RP formatting skipped${reason}. Raw transcript used.`, 'Dictation Bridge');
            }
            // POL-3: low-confidence banner if the server tagged spans.
            const spans = data.low_confidence_spans || data.lowConfidenceSpans;
            if (Array.isArray(spans) && spans.length) {
                try { renderLowConfBanner(spans); }
                catch (err) { WARN('lowconf banner render failed', err?.message || err); }
            }
            // For popups, close so the user is back in ST. For iframes, leave open
            // so they can see the result — they close via the X or backdrop.
            if (!activeIsIframe) closeActive();
            break;
        }
        case 'dictation-edit':
            if (settings().liveMirror) {
                writeToTextarea(String(data.text ?? ''), { autoSend: false, appendMode: 'replace' });
            }
            break;
        default:
            // Unknown — ignore.
            break;
    }
}

// ─── Repair trace drawer (in-memory only) ──────────────────────────────────
const REPAIR_TRACE_ID = 'dictation_bridge_repair_trace';
let latestRepairTrace = null;

function extractRepairTracePayload(data, finalText) {
    const trace = data?.repair_trace || data?.repairTrace || null;
    const raw = data?.raw || data?.raw_text || data?.rawTranscript || trace?.raw || trace?.raw_text || '';
    const cleaned = data?.cleaned || data?.cleaned_text || data?.cleanedText || trace?.cleaned || trace?.cleaned_text || '';
    const final = finalText || data?.final || data?.final_text || trace?.final || trace?.final_text || data?.text || '';
    if (!raw && !cleaned && !final && data?.has_repair_trace !== true) return null;
    return { raw: String(raw || ''), cleaned: String(cleaned || ''), final: String(final || '') };
}

function clearRepairTrace() {
    latestRepairTrace = null;
    const el = document.getElementById(REPAIR_TRACE_ID);
    if (el) try { el.remove(); } catch {}
}

function ensureRepairTraceDrawer() {
    let el = document.getElementById(REPAIR_TRACE_ID);
    if (el) return el;
    const ta = document.getElementById('send_textarea');
    const host = ta?.parentElement || document.getElementById('send_form') || document.getElementById('chat');
    if (!host) return null;
    el = document.createElement('div');
    el.id = REPAIR_TRACE_ID;
    el.style.cssText = 'margin:6px 0;padding:8px;border:1px solid rgba(255,182,72,0.35);background:rgba(0,0,0,0.22);border-radius:3px;color:#C9B28B;font-size:12px';
    if (ta && ta.parentElement) ta.parentElement.insertBefore(el, ta);
    else host.prepend(el);
    return el;
}

function renderRepairTraceFromPayload(data, finalText) {
    const trace = extractRepairTracePayload(data, finalText);
    if (!trace) { clearRepairTrace(); return; }
    latestRepairTrace = trace;
    const el = ensureRepairTraceDrawer();
    if (!el) return;
    const row = (label, value) => `<div style="display:grid;grid-template-columns:72px 1fr;gap:8px;margin-top:4px"><strong style="color:#98876F">${label}</strong><span style="white-space:pre-wrap">${escapeHtml(value || '—')}</span></div>`;
    el.innerHTML = `<div style="display:flex;justify-content:space-between;gap:8px;align-items:center">
        <strong style="color:#FFB648">Repair Trace</strong>
        <button id="dictation_bridge_repair_trace_close" type="button" class="menu_button" title="Clear this in-memory repair trace" style="padding:2px 7px;font-size:11px">Dismiss</button>
    </div>
    ${row('Raw', trace.raw)}${row('Cleaned', trace.cleaned)}${row('Final', trace.final)}`;
    el.querySelector('#dictation_bridge_repair_trace_close')?.addEventListener('click', clearRepairTrace);
}

// ─── MVP-23: privacy badge + audit log peek ───────────────────────────────
// Click the "🔒 LOCAL" chip in the settings panel to open a peek panel
// listing what the dictation server talks to. Calls GET /audit/network on
// the configured server and renders the last 5-10 entries (timestamp,
// method/path, host:port, latency). Loopback destinations colour green;
// non-loopback red. If the response includes a `warning` field (sibling
// server agent emits this when a non-loopback host is seen in the last
// 60s), the modal header gets a red banner.
//
// Endpoint shape (best-effort; tolerant to changes):
//   { entries: [ { ts, method, path, host, port, latency_ms,
//                  bytes?, status? } ], warning?: string,
//     summary?: { audio?, transcript?, llm?, phone? } }
const PRIVACY_PEEK_ID = 'dictation_bridge_privacy_peek';

function isLoopbackHost(host) {
    if (!host) return true;
    if (host === 'localhost' || host === '::1') return true;
    if (host.startsWith('127.')) return true;
    return false;
}

function escapeHtml(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function fmtTime(ts) {
    if (!ts) return '--:--:--';
    try {
        const d = (typeof ts === 'number' && ts < 1e12) ? new Date(ts * 1000) : new Date(ts);
        if (isNaN(d.getTime())) return '--:--:--';
        return d.toTimeString().slice(0, 8);
    } catch { return '--:--:--'; }
}

async function fetchAuditNetwork() {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/audit/network`;
    try {
        const res = await fetch(url, {
            method: 'GET',
            mode: 'cors',
            cache: 'no-store',
            headers: { ...authHeaders() },
        });
        if (!res.ok) {
            return { error: `HTTP ${res.status}`, entries: [] };
        }
        const data = await res.json();
        return data && typeof data === 'object' ? data : { entries: [] };
    } catch (e) {
        return { error: e?.message || String(e), entries: [] };
    }
}

function renderAuditEntries(entries, limit) {
    if (!Array.isArray(entries) || entries.length === 0) {
        return '<div style="opacity:0.6">No outbound calls recorded.</div>';
    }
    const slice = entries.slice(-Math.max(1, limit | 0)).reverse();
    const rows = slice.map((e) => {
        const host = String(e.host ?? e.destination ?? '');
        const port = e.port != null ? `:${e.port}` : '';
        const loopback = isLoopbackHost(host);
        const color = loopback ? '#A8C97B' : '#FF5A4E';
        const label = loopback ? 'local' : 'EXTERNAL';
        const lat = e.latency_ms != null ? `${Math.round(e.latency_ms)}ms` : '';
        const path = String(e.path ?? e.url ?? '');
        const method = String(e.method ?? '').toUpperCase() || 'GET';
        return `
            <div style="display:grid;grid-template-columns:auto 1fr auto auto;gap:8px;padding:3px 0;font-family:var(--monoFontFamily, monospace);font-size:11px;border-bottom:1px solid rgba(255, 182, 72, 0.08)">
                <span style="color:#98876F">${escapeHtml(fmtTime(e.ts))}</span>
                <span style="color:#C9B28B">${escapeHtml(method)} ${escapeHtml(path)}</span>
                <span style="color:${color}">${escapeHtml(host + port)} (${label})</span>
                <span style="color:#98876F">${escapeHtml(lat)}</span>
            </div>
        `;
    }).join('');
    return rows;
}

function buildPrivacyPeekHtml(audit, opts = {}) {
    const cfg = settings();
    const limit = opts.full ? 200 : 10;
    const summary = audit?.summary || {};
    const phoneHost = (() => {
        try { return new URL(cfg.serverUrl).host; }
        catch { return cfg.serverUrl; }
    })();
    const llm = String(summary.llm || 'configured proxy (see server /health)');
    const audio = String(summary.audio || 'whisper.cpp (HIP/ROCm) · localhost');
    const transcript = String(summary.transcript || 'in-RAM only, wiped on restart');

    const warningBanner = audit?.warning
        ? `<div style="background:rgba(255, 90, 78, 0.18);border:1px solid #FF5A4E;color:#FF8274;padding:6px 10px;margin:0 0 10px 0;border-radius:2px;font-size:12px">⚠ ${escapeHtml(audit.warning)}</div>`
        : '';
    const errorBanner = audit?.error
        ? `<div style="background:rgba(255, 182, 72, 0.10);border:1px dashed rgba(255, 182, 72, 0.45);color:#C9B28B;padding:6px 10px;margin:0 0 10px 0;border-radius:2px;font-size:12px">Audit endpoint unavailable (${escapeHtml(audit.error)}). The server may not yet expose /audit/network.</div>`
        : '';

    return `
        <div class="dictation-bridge-modal" id="${PRIVACY_PEEK_ID}" style="z-index:10001">
            <div class="dictation-bridge-backdrop"></div>
            <div class="dictation-bridge-frame-wrap" style="background:#1C150C;border:1px solid #FFB648;border-radius:2px;width:min(90vw, 560px);max-width:560px;max-height:80vh;overflow:auto;padding:16px;color:#C9B28B;font-family:inherit">
                <div class="dictation-bridge-close" style="color:#FFB648">&times;</div>
                <h3 style="margin:0 0 10px 0;color:#A8C97B;font-size:16px;letter-spacing:0.04em">🔒 LOCAL</h3>
                ${warningBanner}
                ${errorBanner}
                <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 12px;font-size:12px;margin-bottom:12px">
                    <span style="color:#98876F">Audio:</span><span>${escapeHtml(audio)}</span>
                    <span style="color:#98876F">Transcript:</span><span>${escapeHtml(transcript)}</span>
                    <span style="color:#98876F">LLM cleanup:</span><span>${escapeHtml(llm)}</span>
                    <span style="color:#98876F">Phone &harr; PC:</span><span>${escapeHtml(phoneHost)}</span>
                </div>
                <div style="margin:0 0 6px 0;font-size:12px;color:#98876F">Network calls in last 5 min:</div>
                <div id="dbb_privacy_audit_list" style="max-height:240px;overflow:auto;border:1px solid rgba(255, 182, 72, 0.18);border-radius:2px;padding:4px 8px;background:rgba(0,0,0,0.25)">
                    ${renderAuditEntries(audit?.entries, limit)}
                </div>
                <div style="display:flex;gap:8px;margin-top:12px;font-size:12px">
                    <button id="dbb_privacy_full_btn" class="menu_button" style="flex:0 0 auto">${opts.full ? '[Show last 10]' : '[View full audit log]'}</button>
                    <button id="dbb_privacy_help_btn" class="menu_button" title="Calliope only contacts hosts you configured. Loopback (127.0.0.1) means the request never left this machine. Anything else is colour-coded so you can spot it." style="flex:0 0 auto">[What does this mean?]</button>
                </div>
            </div>
        </div>
    `;
}

async function openPrivacyPeek(opts = {}) {
    closePrivacyPeek();
    const audit = await fetchAuditNetwork();
    const wrapper = document.createElement('div');
    wrapper.innerHTML = buildPrivacyPeekHtml(audit, opts);
    const modal = wrapper.firstElementChild;
    if (!modal) return;
    document.body.appendChild(modal);
    modal.querySelector('.dictation-bridge-backdrop')?.addEventListener('click', closePrivacyPeek);
    modal.querySelector('.dictation-bridge-close')?.addEventListener('click', closePrivacyPeek);
    modal.querySelector('#dbb_privacy_full_btn')?.addEventListener('click', () => {
        openPrivacyPeek({ full: !opts.full });
    });
    modal.querySelector('#dbb_privacy_help_btn')?.addEventListener('click', () => {
        if (window.toastr) {
            window.toastr.info(
                'Loopback (127.0.0.1) destinations never leave this machine. External hosts are flagged so you can audit what your dictation pipeline reaches.',
                'Dictation Bridge — Privacy',
                { timeOut: 6000 }
            );
        }
    });
}

function closePrivacyPeek() {
    const existing = document.getElementById(PRIVACY_PEEK_ID);
    if (existing) try { existing.remove(); } catch {}
}

// ─── POL-15: voice-edit cheatsheet overlay ────────────────────────────────
// A '?' chip in the settings panel opens a static help modal listing the
// voice commands the dictation pipeline recognises. The server strips
// command prefixes and dispatches actions through SSE when enabled.
// is the primary surface — Wispr Flow / Superwhisper take the same approach.
const CHEATSHEET_ID = 'dictation_bridge_cheatsheet';

const CHEATSHEET_ROWS = [
    { phrase: '"scratch that"', action: 'Undo the last utterance — restores prior textarea state.' },
    { phrase: '"append: <words>"', action: 'Force append-mode for this utterance regardless of setting.' },
    { phrase: '"replace: <words>"', action: 'Force replace-mode regardless of setting.' },
    { phrase: '"send" / "send it"', action: 'After commit, fire the send button.' },
    { phrase: '"clear"', action: 'Empty the textarea (3-second undo toast).' },
    { phrase: '"computer: …"', action: 'Sentinel prefix that flags the rest of the utterance as a voice command rather than RP content.' },
    { phrase: '"new paragraph" / "scene break"', action: 'Insert formatting markers without typing them.' },
    { phrase: '"stop" / "cancel"', action: 'Discard the in-flight utterance — also halts any active TTS playback.' },
    { phrase: '"computer: read last"', action: 'TTS-read the most recent AI message via Calliope (Kokoro).' },
    { phrase: '"computer: read all"', action: 'Toggle auto-read mode — every new AI message gets voiced.' },
];

function openCheatsheet() {
    closeCheatsheet();
    const rows = CHEATSHEET_ROWS.map(r => `
        <div style="display:grid;grid-template-columns:160px 1fr;gap:10px;padding:6px 0;border-bottom:1px solid rgba(255, 182, 72, 0.10);font-size:13px">
            <span style="color:#FFB648;font-family:var(--monoFontFamily, monospace)">${escapeHtml(r.phrase)}</span>
            <span style="color:#C9B28B">${escapeHtml(r.action)}</span>
        </div>
    `).join('');
    const wrapper = document.createElement('div');
    wrapper.innerHTML = `
        <div class="dictation-bridge-modal" id="${CHEATSHEET_ID}" style="z-index:10001">
            <div class="dictation-bridge-backdrop" style="background:rgba(14, 11, 8, 0.7)"></div>
            <div class="dictation-bridge-frame-wrap" style="background:#1C150C;border:2px solid #FFB648;border-radius:2px;width:min(90vw, 600px);max-height:80vh;overflow:auto;padding:18px;color:#C9B28B;font-family:inherit">
                <div class="dictation-bridge-close" style="color:#FFB648">&times;</div>
                <h3 style="margin:0 0 6px 0;color:#FFB648;font-size:16px;letter-spacing:0.04em">Voice commands</h3>
                <div style="margin:0 0 12px 0;font-size:11px;color:#98876F;font-style:italic">Voice grammar is live (Phase 5 / POL-1). The dictation pipeline strips these prefixes and dispatches the action; toggle off in settings to ignore command events on this ST instance.</div>
                <div style="margin:8px 0">${rows}</div>
                <div style="margin-top:14px;font-size:12px;color:#98876F;border-top:1px solid rgba(255, 182, 72, 0.18);padding-top:10px">
                    <div style="margin-bottom:4px"><strong style="color:#C9B28B">Held mic</strong> — dictate while held, release to send.</div>
                    <div><strong style="color:#C9B28B">Tap mic</strong> — toggle (tap again to send).</div>
                </div>
            </div>
        </div>
    `;
    const modal = wrapper.firstElementChild;
    if (!modal) return;
    document.body.appendChild(modal);
    modal.querySelector('.dictation-bridge-backdrop')?.addEventListener('click', closeCheatsheet);
    modal.querySelector('.dictation-bridge-close')?.addEventListener('click', closeCheatsheet);
}

function closeCheatsheet() {
    const existing = document.getElementById(CHEATSHEET_ID);
    if (existing) try { existing.remove(); } catch {}
}

// ─── TTS read-back (Kokoro backend) ────────────────────────────────────────
// Per-message 🔊 button + auto-read mode. Server contract:
//   POST /tts          body {text, voice?}  -> audio/wav blob
//   GET  /tts/voices                        -> {voices: [{id, label, sample_url?}]}
// Both subject to require_auth. Sibling backend agent loads kokoro-server on
// demand. If endpoints aren't there yet, click handlers fall back to a one-
// shot toast and don't error noisily.

const TTS_BTN_CLASS = 'dictation-bridge-tts-btn';
const TTS_BTN_FLAG = 'data-dbb-tts';     // marker: this .mes already got the button
const TTS_AUTO_READ_LAST_KEY = 'dbb_auto_read_last_mesid';

let currentTtsAudio = null;     // single global Audio so a new click stops the prior one
let currentTtsBlobUrl = null;
let currentTtsBtn = null;
let currentTtsAudioContext = null;
let currentTtsAudioSource = null;
let ttsBackendAvailable = null; // null = unknown; true/false after first call
let ttsBackendNotifiedMissing = false;
let ttsAutoReadInitDone = false; // gate: don't auto-read on chat-load first messages
let ttsLastReadMesid = -1;
let ttsLastReadPersonaMesid = -1;
let ttsLastDictatedPersonaQuoted = '';
let ttsLastDictatedPersonaAt = 0;
let ttsObserver = null;
let ttsStreamingSession = null; // { mesid, mesEl, voice, btn, spokenUntil, queue, playing, paused, stopped }
const TTS_STREAM_MIN_CHARS = 48;
const TTS_STREAM_FORCE_CHARS = 260;
const TTS_STREAM_PREVIEW_CHARS = 72;
let ttsStreamUiState = { state: 'off', queueCount: 0, preview: '', paused: false };

function ttsAudioErrorDetails(audio, blob) {
    const err = audio?.error;
    return `code=${err?.code || 'none'} ready=${audio?.readyState ?? 'n/a'} network=${audio?.networkState ?? 'n/a'} blob=${blob?.size || 0} type=${blob?.type || 'none'}`;
}

function normalizeTtsBlob(blob) {
    if (blob?.type && blob.type.startsWith('audio/')) return blob;
    return new Blob([blob], { type: 'audio/wav' });
}

function waitForTtsAudioReady(audio, blob) {
    return new Promise((resolve, reject) => {
        let done = false;
        const cleanup = () => {
            audio.removeEventListener('loadedmetadata', onReady);
            audio.removeEventListener('canplay', onReady);
            audio.removeEventListener('error', onError);
        };
        const finish = (fn, value) => {
            if (done) return;
            done = true;
            cleanup();
            fn(value);
        };
        const onReady = () => finish(resolve);
        const onError = () => finish(reject, new Error(`audio_load_failed ${ttsAudioErrorDetails(audio, blob)}`));
        audio.addEventListener('loadedmetadata', onReady, { once: true });
        audio.addEventListener('canplay', onReady, { once: true });
        audio.addEventListener('error', onError, { once: true });
        audio.load();
        if (audio.readyState >= HTMLMediaElement.HAVE_METADATA) onReady();
    });
}

async function createAndPlayTtsAudio(blob, onEnded) {
    const audioBlob = normalizeTtsBlob(blob);
    currentTtsBlobUrl = URL.createObjectURL(audioBlob);
    const audio = new Audio();
    audio.preload = 'auto';
    audio.src = currentTtsBlobUrl;
    currentTtsAudio = audio;
    audio.addEventListener('ended', onEnded);
    audio.addEventListener('error', () => {
        WARN('tts audio element error', ttsAudioErrorDetails(audio, audioBlob));
    });
    try {
        await waitForTtsAudioReady(audio, audioBlob);
        await audio.play();
    } catch (e) {
        WARN('html audio playback failed; trying WebAudio fallback', e?.message || e, ttsAudioErrorDetails(audio, audioBlob));
        try { audio.pause(); } catch {}
        try { URL.revokeObjectURL(currentTtsBlobUrl); } catch {}
        currentTtsBlobUrl = null;
        return await createAndPlayTtsWebAudio(audioBlob, onEnded, e);
    }
    return audio;
}

async function createAndPlayTtsWebAudio(blob, onEnded, originalError) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) {
        const err = new Error(`${originalError?.message || originalError} (${blob?.size || 0} bytes; WebAudio unavailable)`);
        err.name = originalError?.name || 'AudioPlaybackError';
        throw err;
    }
    const ctx = new AudioCtx();
    currentTtsAudioContext = ctx;
    const buffer = await ctx.decodeAudioData(await blob.arrayBuffer());
    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);
    currentTtsAudioSource = source;
    const playback = {
        paused: false,
        pause() {
            this.paused = true;
            try { source.stop(); } catch {}
            try { ctx.close(); } catch {}
        },
    };
    currentTtsAudio = playback;
    source.onended = () => {
        playback.paused = true;
        try { ctx.close(); } catch {}
        if (currentTtsAudio === playback) onEnded?.();
    };
    if (ctx.state === 'suspended') await ctx.resume();
    source.start(0);
    return playback;
}

function ttsSetButtonState(btn, state) {
    if (!btn) return;
    // Clean prior FA/marker classes.
    btn.classList.remove(
        'fa-volume-high', 'fa-volume-xmark', 'fa-spinner', 'fa-spin', 'fa-stop',
        'dbb-tts-loading', 'dbb-tts-playing',
    );
    btn.removeAttribute('disabled');
    let title = 'Read aloud (Calliope TTS)';
    if (state === 'loading') {
        btn.classList.add('fa-spinner', 'fa-spin', 'dbb-tts-loading');
        title = 'Calliope TTS — loading…';
    } else if (state === 'playing') {
        btn.classList.add('fa-stop', 'dbb-tts-playing');
        title = 'Stop TTS playback';
    } else {
        btn.classList.add('fa-volume-high');
    }
    btn.setAttribute('title', title);
    btn.dataset.dbbTtsState = state;
}

function streamStatusForSession(session, fallback = 'watching') {
    if (!settings().ttsReadStreamingPartials) return 'off';
    if (!session) return settings().ttsAutoReadAi ? 'watching' : 'off';
    if (session.stopped) return 'stopped';
    if (session.error) return 'error';
    if (session.paused) return 'paused';
    if (session.playing) return 'speaking';
    if (session.queue?.length) return 'queued';
    return fallback;
}

function setTtsStreamUiState(state, session = ttsStreamingSession, preview = '') {
    const currentPreview = preview || session?.currentChunk || session?.queue?.[0] || '';
    ttsStreamUiState = {
        state,
        queueCount: session?.queue?.length || 0,
        preview: String(currentPreview || '').replace(/\s+/g, ' ').trim().slice(0, TTS_STREAM_PREVIEW_CHARS),
        paused: !!session?.paused,
    };
    paintTtsStreamStatus();
}

function paintTtsStreamStatus() {
    const chip = document.getElementById('dbb_tts_stream_status_chip');
    const preview = document.getElementById('dbb_tts_stream_preview');
    if (!chip) return;
    const st = ttsStreamUiState.state || 'off';
    const count = ttsStreamUiState.queueCount || 0;
    const labels = {
        off: 'TTS stream: off',
        watching: 'TTS stream: watching',
        queued: `TTS stream: queued (${count})`,
        speaking: `TTS stream: speaking (${count})`,
        paused: `TTS stream: paused (${count})`,
        stopped: 'TTS stream: stopped',
        error: 'TTS stream: error',
    };
    chip.textContent = labels[st] || `TTS stream: ${st}`;
    chip.dataset.state = st;
    chip.setAttribute('title', ttsStreamUiState.preview ? `Current chunk: ${ttsStreamUiState.preview}` : labels[st] || st);
    if (preview) {
        preview.textContent = ttsStreamUiState.preview ? `“${ttsStreamUiState.preview}”` : '';
        preview.style.display = ttsStreamUiState.preview ? 'inline' : 'none';
    }
    const pauseBtn = document.getElementById('dbb_tts_pause_resume');
    if (pauseBtn) pauseBtn.textContent = st === 'paused' ? 'Resume' : 'Pause';
}

function cleanupCurrentTtsAudioOnly() {
    try { currentTtsAudioSource?.stop(); } catch {}
    try { currentTtsAudioContext?.close(); } catch {}
    currentTtsAudioSource = null;
    currentTtsAudioContext = null;
    try { currentTtsAudio?.pause?.(); } catch {}
    if (currentTtsBlobUrl) {
        try { URL.revokeObjectURL(currentTtsBlobUrl); } catch {}
        currentTtsBlobUrl = null;
    }
    currentTtsAudio = null;
}

function skipCurrentTtsChunk() {
    const session = ttsStreamingSession;
    if (!session) { stopTts(); return; }
    cleanupCurrentTtsAudioOnly();
    session.playing = false;
    session.currentChunk = '';
    setTtsStreamUiState(session.queue.length ? 'queued' : 'watching', session);
    if (session.queue.length) playNextTtsStreamChunk(session);
}

function toggleTtsStreamPause() {
    const session = ttsStreamingSession;
    if (!session) return;
    if (session.paused) {
        session.paused = false;
        if (currentTtsAudio?.play && currentTtsAudio.paused) {
            currentTtsAudio.play().catch(e => WARN('tts resume failed', e?.message || e));
        } else if (!session.playing && session.queue.length) {
            playNextTtsStreamChunk(session);
        }
        setTtsStreamUiState(streamStatusForSession(session), session);
        return;
    }
    session.paused = true;
    if (currentTtsAudio?.pause) {
        try { currentTtsAudio.pause(); } catch {}
    } else {
        cleanupCurrentTtsAudioOnly();
        session.playing = false;
        session.currentChunk = '';
        toast('info', 'WebAudio playback cannot pause; skipped current chunk instead.');
    }
    setTtsStreamUiState('paused', session);
}

function cancelStreamingTts() {
    if (!ttsStreamingSession) {
        setTtsStreamUiState(settings().ttsReadStreamingPartials && settings().ttsAutoReadAi ? 'watching' : 'off', null);
        return;
    }
    ttsStreamingSession.stopped = true;
    ttsStreamingSession.queue = [];
    ttsStreamingSession.currentChunk = '';
    ttsStreamingSession = null;
    setTtsStreamUiState(settings().ttsReadStreamingPartials && settings().ttsAutoReadAi ? 'stopped' : 'off', null);
}

function stopTts() {
    cancelStreamingTts();
    cleanupCurrentTtsAudioOnly();
    if (currentTtsBtn) {
        ttsSetButtonState(currentTtsBtn, 'idle');
        currentTtsBtn = null;
    }
    currentTtsAudio = null;
}

/**
 * Pull the canonical text for a message from the chat array (DOM dragnet
 * picks up reasoning blocks + extras). Falls back to .mes_text textContent
 * if the chat array slot isn't reachable.
 */

function messageIdFromEl(mesEl) {
    const id = parseInt(mesEl?.getAttribute?.('mesid') || '-1', 10);
    return Number.isFinite(id) ? id : -1;
}

function isAiMessageId(mesid) {
    const m = chat?.[mesid];
    return !!m && !m.is_user && !m.is_system;
}

function extractMessageText(mesEl) {
    if (!mesEl) return '';
    const mesid = parseInt(mesEl.getAttribute('mesid') || '-1', 10);
    if (Array.isArray(chat) && mesid >= 0 && chat[mesid]) {
        const raw = String(chat[mesid].mes || '').trim();
        if (raw) return chat[mesid].is_user ? extractQuotedDialogueForTts(raw) : stripMarkdownForTts(raw);
    }
    const t = mesEl.querySelector('.mes_text');
    const raw = String(t?.textContent || '').trim();
    return isUserMessageEl(mesEl) ? extractQuotedDialogueForTts(raw) : stripMarkdownForTts(raw);
}

/** Light markdown stripping — Kokoro doesn't render markup; raw symbols become noise. */
function stripMarkdownForTts(s) {
    if (!s) return '';
    return s
        .replace(/```[\s\S]*?```/g, ' ')             // fenced code
        .replace(/`([^`]+)`/g, '$1')                  // inline code
        .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')        // images
        .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')      // links -> text
        .replace(/[*_]{1,3}([^*_]+)[*_]{1,3}/g, '$1') // bold/italics/underline
        .replace(/^>\s?/gm, '')                       // blockquote arrows
        .replace(/^#{1,6}\s+/gm, '')                  // headers
        .replace(/<[^>]+>/g, ' ')                     // HTML tags (ST may inject)
        .replace(/\s+\n/g, '\n')
        .replace(/[ \t]{2,}/g, ' ')
        .trim();
}

function isUserMessageEl(mesEl) {
    if (!mesEl) return false;
    if (mesEl.getAttribute('is_user') === 'true') return true;
    const mesid = parseInt(mesEl.getAttribute('mesid') || '-1', 10);
    return Array.isArray(chat) && mesid >= 0 && chat[mesid]?.is_user === true;
}

function extractQuotedDialogueForTts(s) {
    if (!s) return '';
    const text = String(s);
    const parts = [];
    const re = /["“]([^"”]+)["”]/g;
    let match;
    while ((match = re.exec(text)) !== null) {
        const piece = stripMarkdownForTts(match[1]).trim();
        if (piece) parts.push(piece);
    }
    return parts.join('\n').slice(0, 4000).trim();
}

function isTtsMessageEl(mesEl) {
    if (!mesEl) return false;
    const isSystem = mesEl.getAttribute('is_system');
    if (isSystem === 'true') return false;
    return true;
}

async function fetchTts(text, voice) {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/tts`;
    const cleanText = String(text || '').trim();
    if (!cleanText) {
        const err = new Error('tts_empty_text');
        err.status = 400;
        throw err;
    }
    const body = { text: cleanText };
    if (voice) body.voice = voice;
    const res = await fetch(url, {
        method: 'POST',
        mode: 'cors',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
    });
    if (!res.ok) {
        const err = new Error(`tts_http_${res.status}`);
        err.status = res.status;
        throw err;
    }
    return await res.blob();
}

function currentChatTitle() {
    try {
        const ctx = currentContext();
        return ctx.chatId || characters?.[this_chid]?.name || 'calliope-chat';
    } catch {
        return 'calliope-chat';
    }
}

function buildAudiobookPayload() {
    const s = settings();
    const voiceMap = { ...(s.ttsVoiceProfiles || {}) };
    const narratorVoice = s.ttsVoice || 'af_heart';
    const messages = [];
    if (Array.isArray(chat)) {
        for (const m of chat) {
            if (!m || m.is_system) continue;
            const raw = String(m.mes || '');
            const text = m.is_user ? extractQuotedDialogueForTts(raw) : stripMarkdownForTts(raw);
            if (!text) continue;
            const name = m.is_user ? currentPersonaTtsProfileName() : String(m.name || '').trim();
            const voice = voiceMap[normalizeVoiceProfileKey(name)] || '';
            messages.push({ name, text, is_user: !!m.is_user, voice });
        }
    }
    return {
        title: currentChatTitle(),
        narratorVoice,
        voiceMap,
        messages,
    };
}

async function fetchVoiceSuggest(payload) {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/tts/voices/suggest`;
    const res = await fetch(url, {
        method: 'POST',
        mode: 'cors',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(payload),
    });
    if (!res.ok) {
        let detail = '';
        try { detail = (await res.json())?.error || ''; } catch {}
        const err = new Error(detail || `voice_suggest_http_${res.status}`);
        err.status = res.status;
        throw err;
    }
    return await res.json();
}

function buildVoiceSuggestPayload() {
    const s = settings();
    const char = characters?.[this_chid];
    const recent = (Array.isArray(chat) ? chat : [])
        .filter(m => m && !m.is_system && typeof m.mes === 'string')
        .slice(-10)
        .map(m => m.mes);
    return {
        name: currentTtsProfileName(),
        description: char?.description || '',
        personality: char?.personality || '',
        recent_messages: recent,
        existing_voices: s.ttsVoiceProfiles || {},
        narrator: s.ttsVoice || 'af_heart',
        n: 3,
    };
}

async function fetchAudiobookExport(payload) {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/tts/audiobook`;
    const res = await fetch(url, {
        method: 'POST',
        mode: 'cors',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(payload),
    });
    if (!res.ok) {
        let detail = '';
        try { detail = (await res.json())?.error || ''; } catch {}
        const err = new Error(detail || `audiobook_http_${res.status}`);
        err.status = res.status;
        throw err;
    }
    return await res.blob();
}

function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportCurrentChatAudiobook(btn) {
    const payload = buildAudiobookPayload();
    if (!payload.messages.length) {
        toast('warning', 'No chat messages to export');
        return;
    }
    const prev = btn?.textContent || '';
    if (btn) {
        btn.textContent = 'Exporting...';
        btn.setAttribute('disabled', 'disabled');
    }
    try {
        const blob = await fetchAudiobookExport(payload);
        ttsBackendAvailable = true;
        const safeTitle = String(payload.title || 'calliope-audiobook')
            .replace(/[^A-Za-z0-9._-]+/g, '-')
            .replace(/^-+|-+$/g, '') || 'calliope-audiobook';
        downloadBlob(blob, `${safeTitle}.wav`);
        toast('success', `Exported ${payload.messages.length} messages to WAV`);
    } catch (e) {
        const status = e?.status || 0;
        if (status === 404 || status === 501 || status === 503 || status === 0) {
            notifyTtsMissing(e?.message || `status_${status}`);
        } else {
            WARN('audiobook export failed', e?.message || e);
            toast('error', `Audiobook export failed: ${e?.message || 'unknown'}`);
        }
    } finally {
        if (btn) {
            btn.textContent = prev;
            btn.removeAttribute('disabled');
        }
    }
}

function notifyTtsMissing(reason) {
    ttsBackendAvailable = false;
    if (ttsBackendNotifiedMissing) return;
    ttsBackendNotifiedMissing = true;
    if (window.toastr) {
        window.toastr.warning(
            'TTS backend not yet available — install Kokoro on the dictation server.',
            'Dictation Bridge',
            { timeOut: 4000 },
        );
    }
    LOG('tts unavailable:', reason);
}

async function readMessageAloud(mesEl, btn) {
    // Toggle: clicking the same playing button stops it.
    if (currentTtsBtn === btn && currentTtsAudio && !currentTtsAudio.paused) {
        stopTts();
        return;
    }
    // Switching to a new message: stop the prior playback first.
    if (currentTtsBtn && currentTtsBtn !== btn) stopTts();

    const text = extractMessageText(mesEl);
    if (!text) {
        toast('warning', isUserMessageEl(mesEl) ? 'No quoted dialogue to read' : 'No text to read');
        return;
    }

    ttsSetButtonState(btn, 'loading');
    currentTtsBtn = btn;

    let blob;
    try {
        blob = await fetchTts(text, resolveTtsVoiceForMessage(mesEl));
        ttsBackendAvailable = true;
    } catch (e) {
        const status = e?.status || 0;
        // 404/501/503 = endpoint or backend not loaded; treat as missing-backend.
        if (status === 404 || status === 501 || status === 503 || status === 0) {
            notifyTtsMissing(e?.message || `status_${status}`);
        } else {
            WARN('tts fetch failed', e?.message || e);
            toast('error', `TTS failed: ${e?.message || 'unknown'}`);
        }
        ttsSetButtonState(btn, 'idle');
        if (currentTtsBtn === btn) currentTtsBtn = null;
        return;
    }

    try {
        try {
            const audio = await createAndPlayTtsAudio(blob, () => {
                if (currentTtsAudio === audio) stopTts();
            });
            ttsSetButtonState(btn, 'playing');
        } catch (playErr) {
            // Autoplay-policy or NotAllowedError: gesture chain broken
            // by the awaited fetch. Surface the specific reason so users
            // can grant permission instead of guessing.
            const name = playErr?.name || 'Error';
            const msg = playErr?.message || String(playErr);
            WARN('tts play() rejected', name, msg);
            if (name === 'NotAllowedError') {
                toast('error', 'Browser blocked autoplay. Click the 🔊 button again to play (gesture chain expires during fetch).');
            } else {
                toast('error', `TTS play failed: ${name} — ${msg}`);
            }
            stopTts();
        }
    } catch (e) {
        WARN('tts playback setup failed', e?.message || e);
        toast('error', `TTS playback setup: ${e?.message || 'unknown'}`);
        stopTts();
    }
}

/** Inject 🔊 button on a single message bubble. Idempotent. */
function injectTtsButtonOn(mesEl) {
    if (!mesEl || !isTtsMessageEl(mesEl)) return;
    if (mesEl.hasAttribute(TTS_BTN_FLAG)) return;
    const buttons = mesEl.querySelector('.mes_buttons');
    if (!buttons) return;
    mesEl.setAttribute(TTS_BTN_FLAG, '1');

    const btn = document.createElement('div');
    btn.className = `mes_button ${TTS_BTN_CLASS} fa-solid fa-volume-high interactable`;
    btn.setAttribute('title', 'Read aloud (Calliope TTS)');
    btn.setAttribute('tabindex', '0');
    btn.dataset.dbbTtsState = 'idle';
    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        readMessageAloud(mesEl, btn).catch(err => WARN('readMessageAloud', err?.message || err));
    });
    btn.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            readMessageAloud(mesEl, btn).catch(err => WARN('readMessageAloud', err?.message || err));
        }
    });

    // Insert as a top-level button on the .mes_buttons row, before the
    // ellipsis "extras" hint so it sits with the always-visible icons.
    const hint = buttons.querySelector('.extraMesButtonsHint');
    if (hint) buttons.insertBefore(btn, hint);
    else buttons.appendChild(btn);
}

/** Sweep all readable messages currently in the DOM. Called on chat-load + as backstop. */
function sweepInjectTtsButtons() {
    const list = document.querySelectorAll('#chat .mes');
    list.forEach(injectTtsButtonOn);
}

function ensureTtsObserver() {
    if (ttsObserver) return;
    const chatRoot = document.getElementById('chat');
    if (!chatRoot) {
        setTimeout(ensureTtsObserver, 500);
        return;
    }
    try {
        ttsObserver = new MutationObserver((records) => {
            for (const rec of records) {
                const targetMes = rec.target?.closest?.('.mes') || rec.target?.parentElement?.closest?.('.mes');
                if (targetMes) handleTtsStreamingMutation(targetMes);
                rec.addedNodes && rec.addedNodes.forEach(node => {
                    if (!(node instanceof HTMLElement)) return;
                    if (node.classList?.contains('mes')) {
                        injectTtsButtonOn(node);
                        handleTtsStreamingMutation(node);
                    }
                    // If a swipe re-renders, .mes_buttons may be replaced inside an existing .mes.
                    const inner = node.querySelector?.('.mes');
                    if (inner) {
                        injectTtsButtonOn(inner);
                        handleTtsStreamingMutation(inner);
                    }
                });
            }
        });
        ttsObserver.observe(chatRoot, { childList: true, subtree: true, characterData: true });
    } catch (e) {
        WARN('tts observer setup failed', e?.message || e);
    }
}

/**
 * Auto-read hook for new AI messages. Wired into MESSAGE_RECEIVED /
 * CHARACTER_MESSAGE_RENDERED. Gated so chat-load doesn't blast audio.
 */

function nextTtsStreamChunk(session, { final = false } = {}) {
    if (!session || session.stopped) return '';
    const text = extractMessageText(session.mesEl);
    if (!text || text.length <= session.spokenUntil) return '';
    const unseen = text.slice(session.spokenUntil);

    let cut = -1;
    const sentenceEnd = /[.!?…]["'”’)]?(?:\s+|$)/g;
    let match;
    while ((match = sentenceEnd.exec(unseen)) !== null) {
        if (match.index + match[0].length >= TTS_STREAM_MIN_CHARS) {
            cut = match.index + match[0].length;
        }
    }
    if (cut < 0 && unseen.length >= TTS_STREAM_FORCE_CHARS) {
        const slice = unseen.slice(0, TTS_STREAM_FORCE_CHARS);
        cut = Math.max(slice.lastIndexOf(' '), slice.lastIndexOf('\n'));
        if (cut < TTS_STREAM_MIN_CHARS) cut = TTS_STREAM_FORCE_CHARS;
    }
    if (cut < 0 && final) cut = unseen.length;
    if (cut <= 0) return '';

    const chunk = unseen.slice(0, cut).trim();
    session.spokenUntil += cut;
    return chunk;
}

function queueTtsStreamChunks(session, { final = false } = {}) {
    if (!session || session.stopped) return;
    let chunk;
    while ((chunk = nextTtsStreamChunk(session, { final })) !== '') {
        session.queue.push(chunk);
        if (!final) break; // Interim pass: at most one chunk per DOM burst.
    }
    setTtsStreamUiState(streamStatusForSession(session, session.queue.length ? 'queued' : 'watching'), session);
    playNextTtsStreamChunk(session);
}

async function playNextTtsStreamChunk(session) {
    if (!session || session.stopped || session.paused || session.playing || !session.queue.length) return;
    const chunk = session.queue.shift();
    session.currentChunk = chunk;
    session.playing = true;
    setTtsStreamUiState('speaking', session, chunk);
    currentTtsBtn = session.btn || currentTtsBtn;
    ttsSetButtonState(currentTtsBtn, 'loading');
    try {
        const blob = await fetchTts(chunk, session.voice);
        if (session.stopped || ttsStreamingSession !== session) return;
        ttsBackendAvailable = true;
        const audio = await createAndPlayTtsAudio(blob, () => {
            if (ttsStreamingSession !== session) return;
            if (currentTtsAudio === audio) {
                try { currentTtsAudio.pause?.(); } catch {}
                if (currentTtsBlobUrl) {
                    try { URL.revokeObjectURL(currentTtsBlobUrl); } catch {}
                    currentTtsBlobUrl = null;
                }
                currentTtsAudio = null;
                currentTtsAudioSource = null;
                currentTtsAudioContext = null;
            }
            session.playing = false;
            session.currentChunk = '';
            if (session.queue.length && !session.paused) playNextTtsStreamChunk(session);
            else {
                ttsSetButtonState(session.btn, 'idle');
                setTtsStreamUiState(streamStatusForSession(session), session);
            }
        });
        ttsSetButtonState(session.btn, 'playing');
    } catch (e) {
        session.stopped = true;
        session.error = true;
        ttsStreamingSession = null;
        const status = e?.status || 0;
        if (status === 404 || status === 501 || status === 503 || status === 0) {
            notifyTtsMissing(e?.message || `status_${status}`);
        } else {
            WARN('streaming TTS failed', e?.message || e);
            toast('error', `Streaming TTS failed: ${e?.message || 'unknown'}`);
        }
        ttsSetButtonState(session.btn, 'idle');
        session.playing = false;
        setTtsStreamUiState('error', session);
    }
}

function maybeStartStreamingAutoRead(mesid, mesEl) {
    const cfg = settings();
    if (!cfg.ttsAutoReadAi || !cfg.ttsReadStreamingPartials) return false;
    if (!ttsAutoReadInitDone) return false;
    const id = parseInt(mesid, 10);
    if (!Number.isFinite(id) || id < 0 || !isAiMessageId(id)) return false;
    if (id <= ttsLastReadMesid) return true;
    ttsLastReadMesid = id;
    const el = mesEl || document.querySelector(`#chat .mes[mesid="${id}"]`);
    if (!el) return false;
    injectTtsButtonOn(el);
    const btn = el.querySelector(`.${TTS_BTN_CLASS}`);
    if (!btn) return false;
    if (ttsStreamingSession && ttsStreamingSession.mesid !== id) cancelStreamingTts();
    if (!ttsStreamingSession || ttsStreamingSession.mesid !== id) {
        stopTts();
        ttsStreamingSession = {
            mesid: id,
            mesEl: el,
            voice: resolveTtsVoiceForMessage(el),
            btn,
            spokenUntil: 0,
            queue: [],
            playing: false,
            paused: false,
            currentChunk: '',
            stopped: false,
        };
    }
    queueTtsStreamChunks(ttsStreamingSession, { final: false });
    return true;
}

function handleTtsStreamingMutation(mesEl) {
    if (!mesEl || !settings().ttsReadStreamingPartials) return;
    const id = messageIdFromEl(mesEl);
    if (id < 0) return;
    if (ttsStreamingSession?.mesid === id) {
        queueTtsStreamChunks(ttsStreamingSession, { final: false });
        return;
    }
    maybeStartStreamingAutoRead(id, mesEl);
}

function finalizeStreamingAutoRead(mesid) {
    const id = parseInt(mesid, 10);
    if (!Number.isFinite(id) || !ttsStreamingSession || ttsStreamingSession.mesid !== id) return false;
    queueTtsStreamChunks(ttsStreamingSession, { final: true });
    return true;
}

function maybeAutoReadAi(mesid, attempt = 0) {
    const cfg = settings();
    if (!cfg.ttsAutoReadAi) return;
    if (!ttsAutoReadInitDone) return;             // chat just loaded — skip
    if (cfg.ttsReadStreamingPartials && finalizeStreamingAutoRead(mesid)) return;
    const id = parseInt(mesid, 10);
    if (!Number.isFinite(id) || id < 0) return;
    if (id <= ttsLastReadMesid) return;            // already handled / replay
    const m = chat?.[id];
    if (!m || m.is_user || m.is_system) return;
    const mesEl = document.querySelector(`#chat .mes[mesid="${id}"]`);
    if (!mesEl) {
        if (attempt < 8) setTimeout(() => maybeAutoReadAi(id, attempt + 1), 250);
        return;
    }
    const btn = mesEl.querySelector(`.${TTS_BTN_CLASS}`);
    if (!btn) {
        // Inject first, then read.
        injectTtsButtonOn(mesEl);
    }
    const fresh = mesEl.querySelector(`.${TTS_BTN_CLASS}`);
    const text = extractMessageText(mesEl);
    if (!text) {
        // ST can fire message events before the final .mes text/chat slot is
        // populated. Do not mark the message as read yet; retry briefly so
        // auto-TTS doesn't silently burn the mesid or POST an empty /tts body.
        if (attempt < 8) setTimeout(() => maybeAutoReadAi(id, attempt + 1), 250);
        return;
    }
    ttsLastReadMesid = id;
    if (fresh) readMessageAloud(mesEl, fresh).catch(err => WARN('autoRead', err?.message || err));
}

function maybeAutoReadPersonaQuoted(mesid) {
    const cfg = settings();
    if (!cfg.ttsAutoReadPersonaQuoted) return;
    if (!ttsAutoReadInitDone) return;
    const id = parseInt(mesid, 10);
    if (!Number.isFinite(id) || id < 0) return;
    if (id <= ttsLastReadPersonaMesid) return;
    ttsLastReadPersonaMesid = id;
    const m = chat?.[id];
    if (!m || !m.is_user || m.is_system) return;
    const quoted = extractQuotedDialogueForTts(String(m.mes || ''));
    if (!quoted) return;
    if (quoted === ttsLastDictatedPersonaQuoted && Date.now() - ttsLastDictatedPersonaAt < 5000) return;
    const mesEl = document.querySelector(`#chat .mes[mesid="${id}"]`);
    if (!mesEl) return;
    let btn = mesEl.querySelector(`.${TTS_BTN_CLASS}`);
    if (!btn) {
        injectTtsButtonOn(mesEl);
        btn = mesEl.querySelector(`.${TTS_BTN_CLASS}`);
    }
    if (btn) readMessageAloud(mesEl, btn).catch(err => WARN('autoReadPersona', err?.message || err));
}

function maybeReadDictatedPersonaText(text) {
    if (!settings().ttsAutoReadPersonaQuoted) return;
    const quoted = extractQuotedDialogueForTts(text);
    if (!quoted) return;
    ttsLastDictatedPersonaQuoted = quoted;
    ttsLastDictatedPersonaAt = Date.now();
    fetchTts(quoted, (settings().ttsVoiceProfiles || {})[normalizeVoiceProfileKey(currentPersonaTtsProfileName())] || settings().ttsVoice || 'af_heart')
        .then(blob => {
            ttsBackendAvailable = true;
            stopTts();
            return createAndPlayTtsAudio(blob, () => stopTts());
        })
        .catch(e => {
            const status = e?.status || 0;
            if (status === 404 || status === 501 || status === 503 || status === 0) {
                notifyTtsMissing(e?.message || `status_${status}`);
            } else {
                WARN('dictated persona TTS failed', e?.message || e);
                toast('error', `Persona TTS failed: ${e?.message || 'unknown'}`);
            }
        });
}

/** Read the most recent AI message ("computer: read last"). */
function readLastAiMessage() {
    const list = document.querySelectorAll('#chat .mes');
    for (let i = list.length - 1; i >= 0; i--) {
        const el = list[i];
        if (!isTtsMessageEl(el)) continue;
        if (el.getAttribute('is_user') === 'true') continue;
        let btn = el.querySelector(`.${TTS_BTN_CLASS}`);
        if (!btn) {
            injectTtsButtonOn(el);
            btn = el.querySelector(`.${TTS_BTN_CLASS}`);
        }
        if (btn) {
            readMessageAloud(el, btn).catch(err => WARN('readLast', err?.message || err));
            return true;
        }
    }
    toast('warning', 'No AI message to read');
    return false;
}

/** Toggle auto-read mode + persist + flash a confirmation toast. */
function toggleAutoReadAi() {
    const s = settings();
    s.ttsAutoReadAi = !s.ttsAutoReadAi;
    saveSettings();
    // Reflect in settings panel checkbox + quick-launch button.
    const chk = document.getElementById('dictation_bridge_tts_auto_read');
    if (chk) chk.checked = s.ttsAutoReadAi;
    try { paintQuickLaunchAutoReadBtn(); } catch {}
    toast(s.ttsAutoReadAi ? 'success' : 'info',
        s.ttsAutoReadAi ? 'Auto-read AI messages: ON' : 'Auto-read AI messages: OFF');
}

async function fetchTtsVoices() {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}/tts/voices`;
    const res = await fetch(url, {
        method: 'GET',
        mode: 'cors',
        cache: 'no-store',
        headers: { ...authHeaders() },
    });
    if (!res.ok) {
        const err = new Error(`voices_http_${res.status}`);
        err.status = res.status;
        throw err;
    }
    const data = await res.json();
    return Array.isArray(data?.voices) ? data.voices : [];
}

// ─── Diagnostics panel ─────────────────────────────────────────────────────
const DIAGNOSTICS_ID = 'dictation_bridge_diagnostics_panel';

async function fetchJsonStatus(path, opts = {}) {
    const cfg = settings();
    const url = `${cfg.serverUrl.replace(/\/+$/, '')}${path}`;
    const started = Date.now();
    try {
        const res = await fetch(url, {
            method: opts.method || 'GET',
            mode: 'cors',
            cache: 'no-store',
            headers: { ...(opts.auth ? authHeaders() : {}) },
        });
        let data = null;
        try { data = await res.json(); } catch {}
        return { ok: res.ok, status: res.status, ms: Date.now() - started, data };
    } catch (e) {
        return { ok: false, status: 0, ms: Date.now() - started, error: e?.message || String(e) };
    }
}

function diagnosticsRow(label, state, detail, tone = 'neutral') {
    const colors = { ok: '#A8C97B', warn: '#FFB648', bad: '#FF8274', neutral: '#C9B28B' };
    return `<div style="display:grid;grid-template-columns:145px 92px 1fr;gap:8px;padding:4px 0;border-bottom:1px solid rgba(255,182,72,0.08);font-size:12px">
        <strong style="color:#98876F">${escapeHtml(label)}</strong>
        <span style="color:${colors[tone] || colors.neutral}">${escapeHtml(state)}</span>
        <span>${escapeHtml(detail || '')}</span>
    </div>`;
}

async function buildDiagnosticsHtml() {
    const health = await fetchJsonStatus('/health', { auth: true });
    const state = await fetchJsonStatus('/state', { auth: true });
    const voices = await fetchJsonStatus('/tts/voices', { auth: true });
    const audit = await fetchAuditNetwork();
    const now = Date.now();
    const sseAge = sseStatus.lastEventAt ? Math.round((now - sseStatus.lastEventAt) / 1000) : null;
    const stateAge = lastStatePayload?.t ? Math.round((now - lastStatePayload.t) / 1000) : null;
    const tokenState = (() => {
        if (!(settings().serverToken || '').trim()) return 'missing';
        if (state.status === 401 || state.status === 403) return 'invalid';
        if (state.ok) return 'valid';
        return tokenStatusLabel();
    })();
    const formatter = audit?.summary?.llm || health.data?.formatter || health.data?.provider || 'see /audit/network';
    const whisper = health.data?.whisper || health.data?.whisper_server || health.data?.model || health.data?.asr || 'not reported by /health';
    const rows = [
        diagnosticsRow('Calliope server', health.ok ? 'reachable' : 'unreachable', health.ok ? `/health HTTP ${health.status} · ${health.ms}ms` : (health.error || `HTTP ${health.status}`), health.ok ? 'ok' : 'bad'),
        diagnosticsRow('Bearer token', tokenState, serverAuthStatus.lastError || (state.ok ? `/state HTTP ${state.status}` : `state probe HTTP ${state.status || 'network'}`), tokenState === 'valid' ? 'ok' : (tokenState === 'missing' || tokenState === 'invalid' ? 'bad' : 'warn')),
        diagnosticsRow('SSE', sseStatus.state, sseAge == null ? 'no event received yet' : `last event ${sseAge}s ago${sseStatus.lastError ? ` · ${sseStatus.lastError}` : ''}`, sseStatus.state === 'connected' ? 'ok' : 'warn'),
        diagnosticsRow('ST state freshness', stateAge == null ? 'unknown' : (stateAge <= 60 ? 'fresh' : 'stale'), stateAge == null ? 'no local state post recorded' : `last local /state payload ${stateAge}s ago`, stateAge != null && stateAge <= 60 ? 'ok' : 'warn'),
        diagnosticsRow('Whisper', health.ok ? 'reported' : 'unknown', typeof whisper === 'string' ? whisper : JSON.stringify(whisper).slice(0, 160), health.ok ? 'ok' : 'warn'),
        diagnosticsRow('Kokoro', voices.ok ? 'available' : 'unavailable', voices.ok ? `/tts/voices HTTP ${voices.status}` : (voices.error || `HTTP ${voices.status}`), voices.ok ? 'ok' : 'bad'),
        diagnosticsRow('Formatter/audit', audit?.error ? 'limited' : 'available', audit?.error || String(formatter).slice(0, 180), audit?.warning ? 'bad' : (audit?.error ? 'warn' : 'ok')),
    ].join('');
    return `<div class="dictation-bridge-modal" id="${DIAGNOSTICS_ID}" style="z-index:10002">
        <div class="dictation-bridge-backdrop"></div>
        <div class="dictation-bridge-frame-wrap" style="background:#1C150C;border:1px solid #FFB648;border-radius:2px;width:min(92vw, 720px);max-height:80vh;overflow:auto;padding:16px;color:#C9B28B;font-family:inherit">
            <div class="dictation-bridge-close" style="color:#FFB648">&times;</div>
            <h3 style="margin:0 0 10px 0;color:#FFB648;font-size:16px">Calliope Diagnostics</h3>
            <p style="margin:0 0 10px 0;color:#98876F;font-size:12px">Redacted live-state checks only. No bearer token, pairing URL, chat text, or audio path is rendered.</p>
            ${rows}
            <div style="margin-top:10px;font-size:11px;color:#98876F">Server URL host: ${escapeHtml((() => { try { return new URL(settings().serverUrl).host; } catch { return 'invalid'; } })())}</div>
        </div>
    </div>`;
}

function closeDiagnosticsPanel() {
    const existing = document.getElementById(DIAGNOSTICS_ID);
    if (existing) try { existing.remove(); } catch {}
}

async function openDiagnosticsPanel() {
    closeDiagnosticsPanel();
    const wrapper = document.createElement('div');
    wrapper.innerHTML = await buildDiagnosticsHtml();
    const modal = wrapper.firstElementChild;
    if (!modal) return;
    document.body.appendChild(modal);
    modal.querySelector('.dictation-bridge-backdrop')?.addEventListener('click', closeDiagnosticsPanel);
    modal.querySelector('.dictation-bridge-close')?.addEventListener('click', closeDiagnosticsPanel);
}

// ─── Quick-launch panel (above settings drawer) ────────────────────────────
// Compact card: [🎤 Open Dictation] [🔊 Read All AI Msgs] · status dot · mode
// label · "Last: <ago>s". Mounted at #extensions_settings2 INSERT-BEFORE the
// settings drawer container.
const QUICK_LAUNCH_ID = 'dictation_bridge_quick_launch';
const QUICK_LAUNCH_AGO_FRESH_MS = 60_000;       // green dot if state event in last 60s
const QUICK_LAUNCH_AGO_STALE_MS = 5 * 60_000;   // amber if within 5min, red beyond
let quickLaunchTickTimer = null;

function buildQuickLaunchPanel() {
    if (document.getElementById(QUICK_LAUNCH_ID)) return;
    const anchor = document.getElementById('extensions_settings2') || document.getElementById('extensions_settings');
    if (!anchor) { setTimeout(buildQuickLaunchPanel, 500); return; }

    const wrap = document.createElement('div');
    wrap.id = QUICK_LAUNCH_ID;
    wrap.className = 'extension_container dictation-bridge-quick-launch';
    wrap.innerHTML = `
        <div class="dbb-ql-card">
            <div class="dbb-ql-title">
                <span class="dbb-ql-brand">Calliope</span>
            </div>
            <div class="dbb-ql-actions">
                <button id="dbb_ql_open_dictation" type="button" class="menu_button dbb-ql-btn" title="Open the dictation UI (popup or modal — same as the mic button in the send bar)">
                    <span class="fa-solid fa-microphone"></span>
                    <span>Open Dictation</span>
                </button>
                <button id="dbb_ql_toggle_auto_read" type="button" class="menu_button dbb-ql-btn dbb-ql-toggle" title="Toggle auto-read of new AI messages via Calliope TTS">
                    <span class="fa-solid fa-volume-high"></span>
                    <span class="dbb-ql-toggle-label">Read All AI Msgs</span>
                </button>
                <button id="dbb_ql_diagnostics" type="button" class="menu_button dbb-ql-btn" title="Open Calliope diagnostics panel">Diagnostics</button>
            </div>
            <div class="dbb-tts-stream-row" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0;font-size:12px">
                <span id="dbb_tts_stream_status_chip" class="dbb-tts-stream-chip" data-state="off" style="border:1px solid rgba(255,182,72,0.45);border-radius:2px;padding:2px 8px;color:#FFB648">TTS stream: off</span>
                <span id="dbb_tts_stream_preview" style="display:none;color:#98876F;max-width:32em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
                <button id="dbb_tts_stop_all" type="button" class="menu_button" title="Stop current TTS and clear queued chunks" style="padding:2px 7px;font-size:11px">Stop all</button>
                <button id="dbb_tts_skip_chunk" type="button" class="menu_button" title="Skip only the current spoken chunk and continue the queue" style="padding:2px 7px;font-size:11px">Skip</button>
                <button id="dbb_tts_pause_resume" type="button" class="menu_button" title="Pause/resume HTMLAudio playback; WebAudio fallback stops the current chunk" style="padding:2px 7px;font-size:11px">Pause</button>
                <button id="dbb_tts_reread_last" type="button" class="menu_button" title="Reread the last AI message" style="padding:2px 7px;font-size:11px">Reread last</button>
            </div>
            <div class="dbb-ql-status">
                <span class="dbb-ql-dot" id="dbb_ql_status_dot"></span>
                <span class="dbb-ql-status-text" id="dbb_ql_status_text">Status: connecting…</span>
                <span class="dbb-ql-sep">·</span>
                <span class="dbb-ql-mode" id="dbb_ql_mode">Mode: —</span>
                <span class="dbb-ql-sep">·</span>
                <span class="dbb-ql-last" id="dbb_ql_last">Last: never</span>
            </div>
        </div>
    `;

    // Insert ABOVE the settings drawer if it's already mounted; otherwise
    // append (settings panel mount appends after, so order is preserved).
    const settingsContainer = document.getElementById('dictation_bridge_container');
    if (settingsContainer && settingsContainer.parentElement === anchor) {
        anchor.insertBefore(wrap, settingsContainer);
    } else {
        anchor.appendChild(wrap);
    }

    wrap.querySelector('#dbb_ql_open_dictation').addEventListener('click', () => {
        onMicClick().catch(e => WARN('quick-launch open dictation', e?.message || e));
    });
    wrap.querySelector('#dbb_ql_toggle_auto_read').addEventListener('click', () => {
        toggleAutoReadAi();
    });
    wrap.querySelector('#dbb_ql_diagnostics')?.addEventListener('click', () => {
        openDiagnosticsPanel().catch(e => WARN('diagnostics panel failed', e?.message || e));
    });
    wrap.querySelector('#dbb_tts_stop_all')?.addEventListener('click', stopTts);
    wrap.querySelector('#dbb_tts_skip_chunk')?.addEventListener('click', skipCurrentTtsChunk);
    wrap.querySelector('#dbb_tts_pause_resume')?.addEventListener('click', toggleTtsStreamPause);
    wrap.querySelector('#dbb_tts_reread_last')?.addEventListener('click', readLastAiMessage);

    paintQuickLaunchAutoReadBtn();
    paintTtsStreamStatus();
    paintQuickLaunchStatus();

    // Keep "Last: <ago>s" fresh on a 1Hz tick. Cheap; teardown on container
    // remove via a parent observer would be over-engineering.
    if (!quickLaunchTickTimer) {
        quickLaunchTickTimer = setInterval(paintQuickLaunchStatus, 1000);
    }
}

function paintQuickLaunchAutoReadBtn() {
    const btn = document.getElementById('dbb_ql_toggle_auto_read');
    if (!btn) return;
    const on = !!settings().ttsAutoReadAi;
    btn.classList.toggle('dbb-ql-toggle-on', on);
    const label = btn.querySelector('.dbb-ql-toggle-label');
    if (label) label.textContent = on ? 'Auto-read ON' : 'Read All AI Msgs';
    btn.setAttribute('title', on
        ? 'Auto-read is ON — every new AI message will be voiced. Click to disable.'
        : 'Toggle auto-read of new AI messages via Calliope TTS');
}

function paintQuickLaunchStatus() {
    const dot = document.getElementById('dbb_ql_status_dot');
    const txt = document.getElementById('dbb_ql_status_text');
    const modeEl = document.getElementById('dbb_ql_mode');
    const lastEl = document.getElementById('dbb_ql_last');
    if (!dot || !txt) return;

    const now = Date.now();
    const lastEvt = sseStatus?.lastEventAt || 0;
    const ageMs = lastEvt ? now - lastEvt : Infinity;
    const sseState = sseStatus?.state || 'disconnected';

    let color = '#7a7a9a';   // grey — disconnected
    let label = 'SSE disconnected';
    if (sseState === 'connected') {
        if (ageMs <= QUICK_LAUNCH_AGO_FRESH_MS) {
            color = '#A8C97B';   // sage — fresh
            label = 'SSE connected';
        } else if (ageMs <= QUICK_LAUNCH_AGO_STALE_MS) {
            color = '#FFB648';   // amber — stale
            label = 'SSE quiet';
        } else {
            color = '#FFB648';
            label = 'SSE idle';
        }
    } else if (sseState === 'connecting') {
        color = '#FFB648';
        label = 'SSE connecting';
    } else if (sseState === 'error') {
        color = '#FF5A4E';   // ember
        label = 'SSE error';
    }
    const tokenState = tokenStatusLabel();
    if (tokenState === 'invalid' || tokenState === 'missing') color = '#FF5A4E';
    dot.style.background = color;
    const bits = [`Server: ${serverAuthStatus.health}`, `Token: ${tokenState}`, label];
    const context = currentContextLabel();
    if (context) bits.push(context);
    txt.textContent = bits.join(' · ');

    // Mode label — last successful dictation mode is the closest signal.
    if (modeEl) {
        const mode = lastDoneMode || '—';
        modeEl.textContent = `Mode: ${mode}`;
    }

    if (lastEl) {
        if (!lastEvt) {
            lastEl.textContent = 'Last: never';
        } else {
            const seconds = Math.max(0, Math.round(ageMs / 1000));
            let ago;
            if (seconds < 60) ago = `${seconds}s ago`;
            else if (seconds < 3600) ago = `${Math.round(seconds / 60)}m ago`;
            else ago = `${Math.round(seconds / 3600)}h ago`;
            lastEl.textContent = `Last: ${ago}`;
        }
    }
}

// ─── Settings UI ───────────────────────────────────────────────────────────

function buildSettingsPanel() {
    const s = settings();
    const host = document.createElement('div');
    host.id = 'dictation_bridge_container';
    host.className = 'extension_container';
    host.innerHTML = `
        <div class="dictation_bridge_settings">
            <div class="inline-drawer">
                <div class="inline-drawer-toggle inline-drawer-header">
                    <b>Dictation Bridge</b>
                    <div class="inline-drawer-icon fa-solid fa-circle-chevron-down down"></div>
                </div>
                <div class="inline-drawer-content">
                    <div class="dictation-bridge-chip-row" style="display:flex;gap:6px;align-items:center;margin:2px 0 8px 0;flex-wrap:wrap">
                        <button id="dictation_bridge_privacy_badge" type="button" class="menu_button" title="Click to see what the dictation server is talking to" style="display:inline-flex;align-items:center;gap:6px;padding:2px 10px;font-size:12px;border:1px solid #A8C97B;background:#1C150C;color:#A8C97B;border-radius:2px;cursor:pointer">
                            <span aria-hidden="true">🔒</span><span>LOCAL</span>
                        </button>
                        <button id="dictation_bridge_cheatsheet_chip" type="button" class="menu_button" title="Voice-command cheatsheet" style="display:inline-flex;align-items:center;gap:6px;padding:2px 10px;font-size:12px;border:1px solid #FFB648;background:#1C150C;color:#FFB648;border-radius:2px;cursor:pointer;font-weight:600">
                            <span aria-hidden="true">?</span>
                        </button>
                    </div>

                    <label for="dictation_bridge_url">Server URL</label>
                    <input id="dictation_bridge_url" type="text" class="text_pole" placeholder="https://<sillytavern-host>:8384" />

                    <label for="dictation_bridge_token">Server bearer token</label>
                    <input id="dictation_bridge_token" type="password" class="text_pole" placeholder="paste from server startup log" autocomplete="off" />
                    <small class="notes" style="margin-top:0">Found in <code>~/.local/share/dictation-server/token</code> on the dictation server.</small>
                    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0 8px 0">
                        <button id="dictation_bridge_pair_open" type="button" class="menu_button" title="Open a fresh paired phone page for this active ST chat" style="padding:3px 10px;font-size:12px;border:1px solid rgba(255,182,72,0.55);background:rgba(255,182,72,0.08);color:#FFB648;border-radius:2px;cursor:pointer">Re-pair this phone</button>
                        <button id="dictation_bridge_pair_copy" type="button" class="menu_button" title="Copy a tokenized pairing URL; use it as a fallback if QR pairing fails" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201,178,139,0.45);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Copy pairing URL</button>
                        <button id="dictation_bridge_pair_qr_show" type="button" class="menu_button" title="Render a local QR code in this browser; no server round-trip or third-party service" style="padding:3px 10px;font-size:12px;border:1px solid rgba(168,201,123,0.55);background:rgba(168,201,123,0.08);color:#A8C97B;border-radius:2px;cursor:pointer">Show local QR</button>
                        <small class="notes" style="margin:0 0 0 4px">URL/QR contains bearer token; avoid screenshots/logs.</small>
                    </div>
                    <div id="dictation_bridge_pair_qr_panel" style="display:none;margin:0 0 8px 0;padding:8px;border:1px solid rgba(168,201,123,0.35);background:rgba(0,0,0,0.20);border-radius:3px;max-width:max-content">
                        <canvas id="dictation_bridge_pair_qr_canvas" aria-label="Tokenized Calliope pairing QR code" style="display:block;background:#fffaf0;border:6px solid #fffaf0;border-radius:2px"></canvas>
                        <div style="margin-top:6px;display:flex;gap:6px;align-items:center;flex-wrap:wrap">
                            <button id="dictation_bridge_pair_qr_open" type="button" class="menu_button" style="padding:3px 10px;font-size:12px;border:1px solid rgba(255,182,72,0.55);background:rgba(255,182,72,0.08);color:#FFB648;border-radius:2px;cursor:pointer">Open</button>
                            <button id="dictation_bridge_pair_qr_copy" type="button" class="menu_button" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201,178,139,0.45);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Copy URL</button>
                            <button id="dictation_bridge_pair_qr_hide" type="button" class="menu_button" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201,178,139,0.30);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Hide + clear</button>
                        </div>
                        <small class="notes" style="display:block;margin-top:6px;color:#FFB648">This QR encodes the bearer-token pairing URL. Treat it like a password; hide clears the canvas and removes the URL node.</small>
                    </div>

                    <label for="dictation_bridge_open_style">Open style</label>
                    <select id="dictation_bridge_open_style" class="text_pole" title="Iframe mode is desktop-only — phones use popup automatically (parent ST page does not delegate microphone permissions).">
                        <option value="popup">Popup window (recommended for phone workflow)</option>
                        <option value="iframe" data-desktop-only="1">Modal iframe (desktop only — phone needs popup)</option>
                    </select>

                    <label for="dictation_bridge_append_mode">Text handling</label>
                    <select id="dictation_bridge_append_mode" class="text_pole">
                        <option value="replace">Replace textarea</option>
                        <option value="append">Append to textarea</option>
                    </select>

                    <label class="checkbox_label">
                        <input id="dictation_bridge_autosend" type="checkbox" />
                        <span>Auto-send after dictation</span>
                    </label>

                    <label class="checkbox_label">
                        <input id="dictation_bridge_push_context" type="checkbox" />
                        <span>Push last AI message as tonal context</span>
                    </label>

                    <label class="checkbox_label">
                        <input id="dictation_bridge_live_mirror" type="checkbox" />
                        <span>Live mirror edits from server (experimental)</span>
                    </label>

                    <label class="checkbox_label">
                        <input id="dictation_bridge_broadcast_state" type="checkbox" />
                        <span>Broadcast ST state to dictation server (phone follows ST)</span>
                    </label>

                    <label class="checkbox_label">
                        <input id="dictation_bridge_sse_enabled" type="checkbox" />
                        <span>Receive dictation from phone via SSE (direct inject)</span>
                    </label>

                    <label class="checkbox_label">
                        <input id="dictation_bridge_voice_commands" type="checkbox" />
                        <span>Voice commands (computer:, OOC:, "scratch that", "send" …)</span>
                    </label>
                    <small class="notes" style="margin-top:0">Server emits voice-command events; toggle off to ignore them in this extension instance.</small>

                    <div class="dictation-bridge-sse-status" style="display:flex;align-items:center;gap:8px;margin:4px 0 6px;font-size:12px;color:var(--SmartThemeBodyColor, #aaa)">
                        <span id="dictation_bridge_sse_dot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#7a7a9a"></span>
                        <span id="dictation_bridge_sse_label">SSE: disconnected</span>
                    </div>

                    <!-- POL-6: addressee picker (group chats only). Hidden on solo. -->
                    <div id="dictation_bridge_addressee" style="display:none;margin:6px 0 4px 0"></div>

                    <!-- TTS read-back (Kokoro backend) ─────────────────── -->
                    <div class="dbb-tts-settings" style="margin-top:10px;padding-top:8px;border-top:1px solid rgba(201, 178, 139, 0.18)">
                        <div style="font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:#FFB648;margin-bottom:4px">TTS (read-back)</div>

                        <label class="checkbox_label">
                            <input id="dictation_bridge_tts_auto_read" type="checkbox" />
                            <span>Auto-read every new AI message</span>
                        </label>

                        <label class="checkbox_label" title="For your persona messages, only text inside double quotes is read aloud; narration/exposition is skipped.">
                            <input id="dictation_bridge_tts_auto_read_persona" type="checkbox" />
                            <span>Auto-read my quoted dialogue</span>
                        </label>

                        <label class="checkbox_label" title="Speak complete sentence chunks while the AI message is still streaming. Requires Auto-read AI messages.">
                            <input id="dictation_bridge_tts_stream_partials" type="checkbox" />
                            <span>Stream TTS while AI text is streaming</span>
                        </label>

                        <div id="dictation_bridge_voice_profile_card" style="border:1px solid rgba(255,182,72,0.25);border-radius:3px;padding:8px;margin:6px 0;background:rgba(0,0,0,0.18)">
                            <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;margin-bottom:4px">
                                <strong style="color:#FFB648">Voice profile</strong>
                                <span id="dictation_bridge_tts_profile_target" style="font-size:12px;color:#C9B28B">Target: —</span>
                            </div>
                            <label for="dictation_bridge_tts_voice">Active voice</label>
                            <select id="dictation_bridge_tts_voice" class="text_pole">
                                <option value="af_heart">af_heart (Kokoro default)</option>
                            </select>
                            <small id="dictation_bridge_tts_profile_hint" class="notes" style="margin-top:0">Voices populate from <code>/tts/voices</code>. Changing the picker saves a profile for the active character/addressee.</small>
                            <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:4px 0 0 0">
                                <button id="dictation_bridge_tts_test" type="button" class="menu_button" title="Play a short sample with the selected voice" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201, 178, 139, 0.45);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Sample</button>
                                <button id="dictation_bridge_tts_save_character" type="button" class="menu_button" title="Save this voice for the active character or group addressee" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201, 178, 139, 0.45);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Save target</button>
                                <button id="dictation_bridge_tts_save_persona" type="button" class="menu_button" title="Save this voice for your persona" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201, 178, 139, 0.45);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Save persona</button>
                                <button id="dictation_bridge_tts_reset_profile" type="button" class="menu_button" title="Reset only the active target voice profile" style="padding:3px 10px;font-size:12px;border:1px solid rgba(255, 90, 78, 0.45);background:transparent;color:#FF8274;border-radius:2px;cursor:pointer">Reset target</button>
                            </div>
                        </div>

                        <div style="display:flex;gap:6px;align-items:center;margin:6px 0 0 0">
                            <button id="dictation_bridge_audiobook_export" type="button" class="menu_button" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201, 178, 139, 0.45);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Export chat WAV</button>
                            <small class="notes" style="margin:0 0 0 4px">Uses narrator fallback plus saved character voice profiles.</small>
                        </div>

                        <div style="display:flex;gap:6px;align-items:center;margin:4px 0 0 0">
                            <button id="dictation_bridge_tts_suggest" type="button" class="menu_button" style="padding:3px 10px;font-size:12px;border:1px solid rgba(201, 178, 139, 0.45);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer">Suggest voice</button>
                            <small class="notes" style="margin:0 0 0 4px">Top 3 voices based on character card + recent dialogue.</small>
                        </div>
                        <div id="dictation_bridge_tts_suggestion_panel" style="display:none;margin-top:6px;border:1px solid rgba(201,178,139,0.2);border-radius:3px;padding:6px 8px;background:rgba(0,0,0,0.2)"></div>
                    </div>

                    <small class="notes">
                        The dictation server must be running and reachable.
                        Self-signed cert: visit the URL once in a browser tab and accept the warning before using the mic button.
                    </small>
                </div>
            </div>
        </div>
    `;

    const anchor = document.getElementById('extensions_settings2') || document.getElementById('extensions_settings');
    if (!anchor) { WARN('extensions_settings(2) not found — retry'); setTimeout(buildSettingsPanel, 500); return; }
    anchor.appendChild(host);

    const urlEl = host.querySelector('#dictation_bridge_url');
    const tokenEl = host.querySelector('#dictation_bridge_token');
    const pairOpenEl = host.querySelector('#dictation_bridge_pair_open');
    const pairCopyEl = host.querySelector('#dictation_bridge_pair_copy');
    const pairQrShowEl = host.querySelector('#dictation_bridge_pair_qr_show');
    const pairQrOpenEl = host.querySelector('#dictation_bridge_pair_qr_open');
    const pairQrCopyEl = host.querySelector('#dictation_bridge_pair_qr_copy');
    const pairQrHideEl = host.querySelector('#dictation_bridge_pair_qr_hide');
    const openEl = host.querySelector('#dictation_bridge_open_style');
    const appendEl = host.querySelector('#dictation_bridge_append_mode');
    const autoEl = host.querySelector('#dictation_bridge_autosend');
    const pushEl = host.querySelector('#dictation_bridge_push_context');
    const mirrorEl = host.querySelector('#dictation_bridge_live_mirror');
    const broadcastEl = host.querySelector('#dictation_bridge_broadcast_state');
    const sseEl = host.querySelector('#dictation_bridge_sse_enabled');
    const voiceCmdEl = host.querySelector('#dictation_bridge_voice_commands');

    // MVP-11: on touch devices the iframe path fails because ST's parent page
    // does not delegate microphone via Permissions-Policy. Drop the option from
    // the DOM, force the setting to 'popup', and persist so a desktop-then-
    // phone session lands cleanly. Tooltip on the <select> explains why.
    const isTouchDevice = (() => {
        try { return window.matchMedia('(pointer: coarse)').matches; }
        catch { return false; }
    })();
    if (isTouchDevice) {
        const iframeOpt = openEl.querySelector('option[value="iframe"]');
        if (iframeOpt) iframeOpt.remove();
        if (s.openStyle !== 'popup') {
            s.openStyle = 'popup';
            saveSettings();
        }
    }

    urlEl.value = s.serverUrl;
    tokenEl.value = s.serverToken || '';
    openEl.value = s.openStyle;
    appendEl.value = s.appendMode;
    autoEl.checked = !!s.autoSend;
    pushEl.checked = !!s.pushContext;
    mirrorEl.checked = !!s.liveMirror;
    broadcastEl.checked = !!s.broadcastState;
    sseEl.checked = !!s.sseEnabled;
    if (voiceCmdEl) voiceCmdEl.checked = !!s.voiceCommandsEnabled;
    updateSseStatusIndicator(); // paint initial dot color
    probeServer().then((ok) => { if (ok) return probeServerAuth(); }).catch(() => {});

    urlEl.addEventListener('change', () => {
        s.serverUrl = urlEl.value.trim() || DEFAULTS.serverUrl;
        serverAuthStatus = { health: 'unknown', token: 'unknown', lastCheckedAt: 0, lastError: '' };
        saveSettings();
        hidePairQrPanel(host);
        probeServer().then((ok) => { if (ok) return probeServerAuth(); }).catch(() => {});
        // URL changed — if SSE is on, reconnect to the new host.
        if (s.sseEnabled) connectSSE();
    });
    tokenEl.addEventListener('change', () => {
        s.serverToken = tokenEl.value.trim();
        serverAuthStatus.token = 'unknown';
        saveSettings();
        hidePairQrPanel(host);
        probeServerAuth().catch(() => {});
        // Token changed — bounce SSE so the new query-string token takes effect.
        if (s.sseEnabled) connectSSE();
    });
    if (pairOpenEl) pairOpenEl.addEventListener('click', openPairedPhoneUrl);
    if (pairCopyEl) pairCopyEl.addEventListener('click', () => copyPairedPhoneUrl().catch(() => {}));
    if (pairQrShowEl) pairQrShowEl.addEventListener('click', () => showPairQrPanel(host).catch(() => {}));
    if (pairQrOpenEl) pairQrOpenEl.addEventListener('click', openPairedPhoneUrl);
    if (pairQrCopyEl) pairQrCopyEl.addEventListener('click', () => copyPairedPhoneUrl().catch(() => {}));
    if (pairQrHideEl) pairQrHideEl.addEventListener('click', () => hidePairQrPanel(host));
    openEl.addEventListener('change', () => { s.openStyle = openEl.value; saveSettings(); });
    appendEl.addEventListener('change', () => { s.appendMode = appendEl.value; saveSettings(); });
    autoEl.addEventListener('change', () => { s.autoSend = !!autoEl.checked; saveSettings(); });
    pushEl.addEventListener('change', () => { s.pushContext = !!pushEl.checked; saveSettings(); });
    mirrorEl.addEventListener('change', () => { s.liveMirror = !!mirrorEl.checked; saveSettings(); });
    broadcastEl.addEventListener('change', () => {
        s.broadcastState = !!broadcastEl.checked;
        saveSettings();
        if (s.broadcastState) {
            startStateHeartbeat();
            postState('toggle-on');
        } else {
            stopStateHeartbeat();
        }
    });
    sseEl.addEventListener('change', () => {
        s.sseEnabled = !!sseEl.checked;
        saveSettings();
        if (s.sseEnabled) connectSSE();
        else disconnectSSE();
    });
    if (voiceCmdEl) {
        voiceCmdEl.addEventListener('change', () => {
            s.voiceCommandsEnabled = !!voiceCmdEl.checked;
            saveSettings();
        });
    }

    // ─── TTS settings wiring ───────────────────────────────────────────────
    const ttsAutoEl = host.querySelector('#dictation_bridge_tts_auto_read');
    const ttsAutoPersonaEl = host.querySelector('#dictation_bridge_tts_auto_read_persona');
    const ttsStreamEl = host.querySelector('#dictation_bridge_tts_stream_partials');
    const ttsVoiceEl = host.querySelector('#dictation_bridge_tts_voice');
    const ttsTestEl = host.querySelector('#dictation_bridge_tts_test');
    const ttsSaveCharacterEl = host.querySelector('#dictation_bridge_tts_save_character');
    const ttsSavePersonaEl = host.querySelector('#dictation_bridge_tts_save_persona');
    const ttsResetProfileEl = host.querySelector('#dictation_bridge_tts_reset_profile');
    const ttsProfileTargetEl = host.querySelector('#dictation_bridge_tts_profile_target');
    const audiobookExportEl = host.querySelector('#dictation_bridge_audiobook_export');
    const ttsSuggestEl = host.querySelector('#dictation_bridge_tts_suggest');
    const ttsSuggestionPanelEl = host.querySelector('#dictation_bridge_tts_suggestion_panel');
    const ttsProfileHintEl = host.querySelector('#dictation_bridge_tts_profile_hint');

    const paintTtsProfileHint = () => {
        if (!ttsProfileHintEl) return;
        const name = currentTtsProfileName();
        const key = normalizeVoiceProfileKey(name);
        const voice = (settings().ttsVoiceProfiles || {})[key] || settings().ttsVoice || 'af_heart';
        const persona = currentPersonaTtsProfileName();
        if (ttsProfileTargetEl) ttsProfileTargetEl.textContent = name ? `Target: ${name}` : `Target: global fallback · Persona: ${persona}`;
        ttsProfileHintEl.innerHTML = name
            ? `Voice profile for <strong>${escapeHtml(name)}</strong>: <code>${escapeHtml(voice)}</code>. Change picker or Save target to update only this character/addressee.`
            : 'Voices populate from <code>/tts/voices</code>. Change picker to set the global fallback voice.';
    };

    if (ttsAutoEl) {
        ttsAutoEl.checked = !!s.ttsAutoReadAi;
        ttsAutoEl.addEventListener('change', () => {
            s.ttsAutoReadAi = !!ttsAutoEl.checked;
            saveSettings();
            try { paintQuickLaunchAutoReadBtn(); } catch {}
        });
    }
    if (ttsAutoPersonaEl) {
        ttsAutoPersonaEl.checked = !!s.ttsAutoReadPersonaQuoted;
        ttsAutoPersonaEl.addEventListener('change', () => {
            s.ttsAutoReadPersonaQuoted = !!ttsAutoPersonaEl.checked;
            saveSettings();
        });
    }
    if (ttsStreamEl) {
        ttsStreamEl.checked = !!s.ttsReadStreamingPartials;
        ttsStreamEl.addEventListener('change', () => {
            s.ttsReadStreamingPartials = !!ttsStreamEl.checked;
            saveSettings();
            if (!s.ttsReadStreamingPartials) cancelStreamingTts();
        });
    }

    // Populate voice picker — try server, fall back to single hard-coded
    // entry if /tts/voices isn't there yet (sibling backend may still be in
    // flight). Don't disable the picker on failure.
    if (ttsVoiceEl) {
        // Pre-select persisted voice (the markup already has af_heart).
        if (s.ttsVoice && s.ttsVoice !== 'af_heart') {
            const opt = document.createElement('option');
            opt.value = s.ttsVoice;
            opt.textContent = s.ttsVoice;
            ttsVoiceEl.appendChild(opt);
            ttsVoiceEl.value = s.ttsVoice;
        }
        const profileVoice = (s.ttsVoiceProfiles || {})[normalizeVoiceProfileKey(currentTtsProfileName())];
        if (profileVoice && profileVoice !== ttsVoiceEl.value) {
            const opt = document.createElement('option');
            opt.value = profileVoice;
            opt.textContent = profileVoice;
            ttsVoiceEl.appendChild(opt);
            ttsVoiceEl.value = profileVoice;
        }
        fetchTtsVoices().then(voices => {
            if (!voices || !voices.length) return;
            // Replace options with server-provided list. Preserve current
            // selection if it's in the new list.
            const prev = ttsVoiceEl.value;
            ttsVoiceEl.innerHTML = '';
            for (const v of voices) {
                const opt = document.createElement('option');
                opt.value = v.id || v.name || '';
                opt.textContent = v.label || v.id || v.name || '';
                ttsVoiceEl.appendChild(opt);
            }
            ttsVoiceEl.value = voices.some(v => (v.id || v.name) === prev) ? prev : (voices[0].id || voices[0].name || 'af_heart');
            // Persist global fallback only when no character profile is active.
            if (!profileVoice && s.ttsVoice !== ttsVoiceEl.value) {
                s.ttsVoice = ttsVoiceEl.value;
                saveSettings();
            }
            paintTtsProfileHint();
        }).catch(e => {
            // Backend missing — keep the single af_heart fallback. One toast
            // (deduped via notifyTtsMissing) only on user-initiated calls.
            WARN('tts/voices fetch failed (using fallback)', e?.message || e);
        });
        ttsVoiceEl.addEventListener('change', () => {
            s.ttsVoice = ttsVoiceEl.value || 'af_heart';
            const profileName = rememberTtsVoiceForCurrentProfile(s.ttsVoice);
            saveSettings();
            paintTtsProfileHint();
            if (profileName) toast('success', `Saved TTS voice for ${profileName}`);
        });
        paintTtsProfileHint();
    }

    if (ttsTestEl) {
        ttsTestEl.addEventListener('click', async () => {
            const sample = 'Hello, my name is Calliope. I read for you.';
            const prev = ttsTestEl.textContent;
            ttsTestEl.textContent = 'Testing…';
            ttsTestEl.setAttribute('disabled', 'disabled');
            try {
                const blob = await fetchTts(sample, (settings().ttsVoiceProfiles || {})[normalizeVoiceProfileKey(currentTtsProfileName())] || settings().ttsVoice || 'af_heart');
                ttsBackendAvailable = true;
                stopTts();
                const audio = await createAndPlayTtsAudio(blob, () => {
                    if (currentTtsAudio === audio) stopTts();
                });
            } catch (e) {
                const status = e?.status || 0;
                if (status === 404 || status === 501 || status === 503 || status === 0) {
                    notifyTtsMissing(e?.message || `status_${status}`);
                } else {
                    toast('error', `Test voice failed: ${e?.message || 'unknown'}`);
                }
            } finally {
                ttsTestEl.textContent = prev;
                ttsTestEl.removeAttribute('disabled');
            }
        });
    }

    if (ttsSaveCharacterEl && ttsVoiceEl) {
        ttsSaveCharacterEl.addEventListener('click', () => {
            const profileName = rememberTtsVoiceForCurrentProfile(ttsVoiceEl.value || settings().ttsVoice || 'af_heart');
            saveSettings();
            paintTtsProfileHint();
            if (profileName) toast('success', `Saved TTS voice for ${profileName}`);
            else toast('success', 'Saved global fallback TTS voice');
        });
    }

    if (ttsResetProfileEl && ttsVoiceEl) {
        ttsResetProfileEl.addEventListener('click', () => {
            const name = currentTtsProfileName();
            const key = normalizeVoiceProfileKey(name);
            if (key && settings().ttsVoiceProfiles?.[key]) {
                delete settings().ttsVoiceProfiles[key];
                saveSettings();
                ttsVoiceEl.value = settings().ttsVoice || 'af_heart';
                paintTtsProfileHint();
                toast('info', `Reset TTS voice profile for ${name}`);
            } else {
                toast('info', 'No active target voice profile to reset');
            }
        });
    }

    if (ttsSavePersonaEl && ttsVoiceEl) {
        ttsSavePersonaEl.addEventListener('click', () => {
            const profileName = rememberTtsVoiceForPersona(ttsVoiceEl.value || settings().ttsVoice || 'af_heart');
            saveSettings();
            paintTtsProfileHint();
            if (profileName) toast('success', `Saved TTS voice for persona ${profileName}`);
        });
    }

    if (audiobookExportEl) {
        audiobookExportEl.addEventListener('click', () => {
            exportCurrentChatAudiobook(audiobookExportEl).catch(e => WARN('export audiobook', e?.message || e));
        });
    }

    if (ttsSuggestEl && ttsSuggestionPanelEl) {
        ttsSuggestEl.addEventListener('click', async () => {
            const prev = ttsSuggestEl.textContent;
            ttsSuggestEl.textContent = 'Thinking…';
            ttsSuggestEl.setAttribute('disabled', 'disabled');
            ttsSuggestionPanelEl.style.display = 'none';
            try {
                const { suggestions } = await fetchVoiceSuggest(buildVoiceSuggestPayload());
                if (!suggestions || !suggestions.length) {
                    toast('info', 'No suggestions — character card may be empty');
                    return;
                }
                ttsSuggestionPanelEl.innerHTML = suggestions.map(s => `
                    <div style="display:flex;align-items:center;gap:6px;padding:3px 0;border-bottom:1px solid rgba(201,178,139,0.1)">
                        <code style="color:#C9B28B;font-size:12px;min-width:110px">${escapeHtml(s.voice)}</code>
                        <span style="color:#888;font-size:11px;flex:1">${escapeHtml(s.reason)}</span>
                        <button data-voice="${escapeHtml(s.voice)}" class="calliope-suggest-sample menu_button"
                            style="padding:2px 7px;font-size:11px;border:1px solid rgba(201,178,139,0.35);background:transparent;color:#C9B28B;border-radius:2px;cursor:pointer;white-space:nowrap">Sample</button>
                        <button data-voice="${escapeHtml(s.voice)}" class="calliope-suggest-use menu_button"
                            style="padding:2px 7px;font-size:11px;border:1px solid rgba(201,178,139,0.45);background:rgba(201,178,139,0.08);color:#C9B28B;border-radius:2px;cursor:pointer;white-space:nowrap">Use</button>
                    </div>
                `).join('');
                ttsSuggestionPanelEl.style.display = 'block';

                ttsSuggestionPanelEl.querySelectorAll('.calliope-suggest-sample').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const voice = btn.getAttribute('data-voice');
                        const prevLabel = btn.textContent;
                        btn.textContent = '…';
                        btn.setAttribute('disabled', 'disabled');
                        try {
                            const blob = await fetchTts('Hello. My voice is ready for your story.', voice);
                            stopTts();
                            await createAndPlayTtsAudio(blob, () => {});
                        } catch (e) {
                            toast('error', `Sample failed: ${e?.message || 'unknown'}`);
                        } finally {
                            btn.textContent = prevLabel;
                            btn.removeAttribute('disabled');
                        }
                    });
                });

                ttsSuggestionPanelEl.querySelectorAll('.calliope-suggest-use').forEach(btn => {
                    btn.addEventListener('click', () => {
                        const voice = btn.getAttribute('data-voice');
                        if (ttsVoiceEl) {
                            ttsVoiceEl.value = voice;
                            ttsVoiceEl.dispatchEvent(new Event('change'));
                        } else {
                            const profileName = rememberTtsVoiceForCurrentProfile(voice);
                            saveSettings();
                            paintTtsProfileHint();
                            if (profileName) toast('success', `Voice set for ${profileName}`);
                        }
                        ttsSuggestionPanelEl.style.display = 'none';
                    });
                });
            } catch (e) {
                toast('error', `Voice suggestion failed: ${e?.message || 'unknown'}`);
            } finally {
                ttsSuggestEl.textContent = prev;
                ttsSuggestEl.removeAttribute('disabled');
            }
        });
    }

    // POL-6: paint addressee picker now (in case ST is already in a group
    // chat when the panel mounts) and on every state-payload event.
    renderAddresseePicker();

    // MVP-23: privacy badge → audit-log peek modal.
    const privacyBadge = host.querySelector('#dictation_bridge_privacy_badge');
    if (privacyBadge) {
        privacyBadge.addEventListener('click', () => {
            openPrivacyPeek({ full: false }).catch(e => WARN('privacy peek failed', e?.message || e));
        });
    }

    // POL-15: '?' chip → voice-command cheatsheet overlay.
    const cheatsheetChip = host.querySelector('#dictation_bridge_cheatsheet_chip');
    if (cheatsheetChip) {
        cheatsheetChip.addEventListener('click', () => {
            try { openCheatsheet(); }
            catch (e) { WARN('cheatsheet open failed', e?.message || e); }
        });
    }
}

// ─── Bootstrap ─────────────────────────────────────────────────────────────

let initialized = false;

export async function init() {
    if (initialized) return;
    initialized = true;
    settings();
    window.addEventListener('message', onWindowMessage);
    buildQuickLaunchPanel();
    buildSettingsPanel();
    // Inject now if DOM is ready, otherwise on app_ready.
    if (document.getElementById('rightSendForm')) {
        injectMicButton();
    } else {
        eventSource.on(event_types.APP_READY, injectMicButton);
    }

    // MVP-16: pre-create the state bar (hidden) and watch for ST DOM rebuilds
    // that would orphan it. ensureStateBar() is also called lazily on every
    // dictation-state event, so a missed initial inject is self-healing.
    ensureStateBar();
    ensureStateBarObserver();
    if (event_types.APP_READY) eventSource.on(event_types.APP_READY, ensureStateBar);

    // Phase 1: broadcast ST state to dictation server on context changes.
    // CHAT_CHANGED fires on chat switch; CHAT_LOADED on initial load; CHARACTER_EDITED
    // on card edits; SETTINGS_UPDATED catches persona switches (no dedicated event for those).
    const stateEvents = [
        event_types.CHAT_CHANGED,
        event_types.CHAT_LOADED,
        event_types.CHARACTER_EDITED,
        event_types.SETTINGS_UPDATED,
        event_types.APP_READY,
    ];
    for (const evt of stateEvents) {
        if (evt) eventSource.on(evt, () => {
            postState(evt);
            // POL-6: chat/character switches may flip group status — repaint
            // the addressee picker (no-op when not in a group).
            try { renderAddresseePicker(); } catch (e) { WARN('addressee repaint failed', e?.message || e); }
        });
    }
    startStateHeartbeat();
    setupMobileLifecycleStatePush();
    // Initial push in case we loaded mid-chat.
    setTimeout(() => postState('init'), 1500);

    // Phase 2: subscribe to server-sent events for direct inject from phone.
    if (settings().sseEnabled) connectSSE();

    // ─── TTS read-back hooks ───────────────────────────────────────────────
    // Inject 🔊 button on existing messages now (and on every chat-load),
    // plus on each new message via ST events. MutationObserver is the
    // backstop for ST DOM rebuilds (theme reload, swipe re-render).
    ensureTtsObserver();
    sweepInjectTtsButtons();
    if (event_types.CHAT_CHANGED) eventSource.on(event_types.CHAT_CHANGED, () => {
        // New chat: reset auto-read gate + sweep existing messages.
        ttsAutoReadInitDone = false;
        ttsLastReadMesid = -1;
        ttsLastReadPersonaMesid = -1;
        ttsLastDictatedPersonaQuoted = '';
        ttsLastDictatedPersonaAt = 0;
        stopTts();
        clearRepairTrace();
        setTimeout(() => {
            sweepInjectTtsButtons();
            // Mark init complete a tick after the chat is fully painted so
            // any first_message events fired during load don't auto-blast.
            setTimeout(() => { ttsAutoReadInitDone = true; }, 600);
        }, 100);
    });
    if (event_types.CHAT_LOADED) eventSource.on(event_types.CHAT_LOADED, () => {
        sweepInjectTtsButtons();
        setTimeout(() => { ttsAutoReadInitDone = true; }, 600);
    });
    if (event_types.APP_READY) eventSource.on(event_types.APP_READY, () => {
        sweepInjectTtsButtons();
        setTimeout(() => { ttsAutoReadInitDone = true; }, 1000);
    });
    if (event_types.CHARACTER_MESSAGE_RENDERED) {
        eventSource.on(event_types.CHARACTER_MESSAGE_RENDERED, (mesid) => {
            // Always inject; only auto-read if the gate is open.
            const id = parseInt(mesid, 10);
            const el = document.querySelector(`#chat .mes[mesid="${id}"]`);
            if (el) injectTtsButtonOn(el);
            maybeAutoReadAi(mesid);
        });
    }
    if (event_types.MESSAGE_RECEIVED) {
        eventSource.on(event_types.MESSAGE_RECEIVED, (mesid) => {
            const id = parseInt(mesid, 10);
            const el = document.querySelector(`#chat .mes[mesid="${id}"]`);
            if (el) injectTtsButtonOn(el);
            const m = chat?.[id];
            if (m && !m.is_user && !m.is_system) {
                // Some ST builds fire MESSAGE_RECEIVED for completed AI output
                // without CHARACTER_MESSAGE_RENDERED; use the same guarded
                // auto-read path so Read All keeps working across versions.
                maybeAutoReadAi(mesid);
            } else {
                maybeAutoReadPersonaQuoted(mesid);
            }
        });
    }
    // First-load fallback: if there's no chat yet, the gate stays closed
    // until APP_READY/CHAT_LOADED fires. If those don't show up (race), open
    // it after 3s so manual auto-read works on a hot session.
    document.getElementById('send_textarea')?.addEventListener('input', () => {
        if (latestRepairTrace) clearRepairTrace();
    });
    setTtsStreamUiState(settings().ttsReadStreamingPartials && settings().ttsAutoReadAi ? 'watching' : 'off', null);
    setTimeout(() => { if (!ttsAutoReadInitDone) ttsAutoReadInitDone = true; }, 3000);

    LOG('initialized');
}

// Self-bootstrap for robustness in case hooks.activate isn't invoked for
// third-party extensions. init() is idempotent.
try {
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => init().catch(ERR));
    } else {
        init().catch(ERR);
    }
} catch (e) {
    ERR('bootstrap failed', e);
}
