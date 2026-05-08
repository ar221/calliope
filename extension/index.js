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
//     { type: 'dictation-result', text, raw?, mode?, formatting_skipped?, formatting_reason? }
//     { type: 'dictation-edit', text }        // optional live mirror
//   extension -> server:
//     { type: 'dictation-set-context', context: string }
//     { type: 'dictation-set-mode', mode: string }

import { eventSource, event_types, name1, this_chid, characters, user_avatar, chat } from '../../../../script.js';
import { extension_settings, getContext } from '../../../extensions.js';
import { selected_group, groups } from '../../../group-chats.js';

const MODULE = 'dictation-bridge';
const LOG = (...a) => console.log('[dictation-bridge]', ...a);
const WARN = (...a) => console.warn('[dictation-bridge]', ...a);
const ERR = (...a) => console.error('[dictation-bridge]', ...a);

function defaultServerUrl() {
    const host = window.location.hostname || '127.0.0.1';
    const safeHost = (host === '0.0.0.0' || host === '::') ? '127.0.0.1' : host;
    return `https://${safeHost}:8384`;
}

const LEGACY_DEFAULT_SERVER_URLS = new Set([
    'https://192.168.50.110:8384',
    'https://192.168.50.113:8384',
]);

function isLocalHost(host) {
    return host === '127.0.0.1' || host === 'localhost' || host === '::1';
}

function shouldMigrateServerUrl(serverUrl) {
    if (LEGACY_DEFAULT_SERVER_URLS.has(serverUrl)) return true;

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
        return '*';
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
            if (stopBtn && stopBtn.offsetParent !== null) {
                stopBtn.click();
                toast('success', 'Stopped');
            } else {
                // No-op — nothing in flight.
                toast('info', 'Nothing to stop');
            }
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

function updateSseStatusIndicator() {
    const dot = document.getElementById('dictation_bridge_sse_dot');
    const label = document.getElementById('dictation_bridge_sse_label');
    if (!dot || !label) return;
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
                }
            }
        } else {
            writeToTextarea(text, { autoSend: doAutoSend, appendMode: cfg.appendMode });
        }

        if (data.formatting_skipped && window.toastr) {
            const reason = data.formatting_reason ? `: ${data.formatting_reason}` : '';
            window.toastr.warning(`RP formatting skipped${reason}. Raw transcript used.`, 'Dictation Bridge');
        } else if (window.toastr) {
            window.toastr.success('Received from phone', 'Dictation Bridge', { timeOut: 1500 });
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

function buildEmbedUrl() {
    const cfg = settings();
    const ctx = currentContext();
    const base = cfg.serverUrl.replace(/\/+$/, '');
    const qp = new URLSearchParams({ embed: '1' });
    if (ctx.chatId) qp.set('chat', String(ctx.chatId));
    if (ctx.personaId) qp.set('persona', String(ctx.personaId));
    if (ctx.characterId) qp.set('character', String(ctx.characterId));
    // Pass bearer token via query so the embedded UI can stash it in
    // sessionStorage and attach to its own fetch + EventSource calls.
    // Server-side / is auth-exempt; child API calls remain gated.
    const token = (cfg.serverToken || '').trim();
    if (token) qp.set('token', token);
    return `${base}/?${qp.toString()}`;
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
        return res.ok;
    } catch (e) {
        // Self-signed cert will trip this on first visit. Caller decides how to react.
        WARN('server probe failed', e?.message || e);
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
    modal.innerHTML = `
        <div class="dictation-bridge-backdrop"></div>
        <div class="dictation-bridge-frame-wrap">
            <div class="dictation-bridge-close" title="Close">&times;</div>
            <iframe class="dictation-bridge-iframe" src="${url}" allow="microphone; clipboard-write"></iframe>
        </div>
    `;
    modal.querySelector('.dictation-bridge-backdrop').addEventListener('click', closeActive);
    modal.querySelector('.dictation-bridge-close').addEventListener('click', closeActive);
    document.body.appendChild(modal);
    activeModal = modal;
    activeTarget = modal.querySelector('.dictation-bridge-iframe').contentWindow;
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
    try {
        activeTarget.postMessage(payload, serverOrigin());
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
    if (origin !== '*' && event.origin !== origin) return;
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
// voice commands the dictation pipeline will recognise once Phase 5 lands.
// Even before the grammar ships, printing the affordance signals that voice
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
    { phrase: '"stop" / "cancel"', action: 'Discard the in-flight utterance.' },
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

    urlEl.addEventListener('change', () => {
        s.serverUrl = urlEl.value.trim() || DEFAULTS.serverUrl;
        saveSettings();
        // URL changed — if SSE is on, reconnect to the new host.
        if (s.sseEnabled) connectSSE();
    });
    tokenEl.addEventListener('change', () => {
        s.serverToken = tokenEl.value.trim();
        saveSettings();
        // Token changed — bounce SSE so the new query-string token takes effect.
        if (s.sseEnabled) connectSSE();
    });
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
    // Initial push in case we loaded mid-chat.
    setTimeout(() => postState('init'), 1500);

    // Phase 2: subscribe to server-sent events for direct inject from phone.
    if (settings().sseEnabled) connectSSE();

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
