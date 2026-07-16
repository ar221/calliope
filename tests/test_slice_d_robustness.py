from __future__ import annotations

import pathlib
import re


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension" / "index.js"


def source() -> str:
    return EXTENSION.read_text(encoding="utf-8")


def test_streaming_session_has_refreshable_inactivity_timeout():
    js = source()
    assert "const STREAMING_SESSION_TIMEOUT_MS = 45_000" in js
    assert "function refreshStreamingSessionTimeout()" in js
    assert "refreshStreamingSessionTimeout();" in js
    assert "clearTimeout(streamingSession.inactivityTimer)" in js
    assert "ta.value = streamingSession.base" in js


def test_mismatched_canonical_result_cannot_overwrite_active_stream():
    js = source()
    assert "if (streamingSession && requestId && streamingSession.requestId !== requestId) return false;" in js
    assert "if (streamingSession) {" in js


def test_cross_tab_claims_carry_only_opaque_ids_and_timestamps():
    js = source()
    block = js[js.index("const TAB_INSTANCE_ID"):js.index("function settings()")]
    assert "BroadcastChannel('calliope-dictation-claims')" in block
    assert "navigator.locks.request" in block
    assert "localStorage.setItem(incomingStorageKey(key), String(Date.now()))" in block
    assert "const claimKey = `calliope:claim:${key}`" in block
    assert "transcript" not in block.lower()
    assert "serverToken" not in block
    assert "text:" not in block
    assert "ownIncomingSideEffect('command'" in js
    assert "ownIncomingSideEffect('result'" in js
    assert "data.requestId || data.ts || e.lastEventId" in js
    assert "data.requestId || data.request_id || data.ts || e.lastEventId" in js


def test_server_url_migration_runs_once_outside_settings_hot_path():
    js = source()
    settings_block = js[js.index("function settings() {"):js.index("function initializeSettings()")]
    init_block = js[js.index("function initializeSettings()"):js.index("function saveSettings()")]
    assert "shouldMigrateServerUrl" not in settings_block
    assert "if (settingsInitialized) return settings();" in init_block
    assert "shouldMigrateServerUrl(current.serverUrl)" in init_block
    assert "initializeSettings();" in js[js.index("export async function init()") :]


def test_settings_change_handlers_refresh_settings_object():
    js = source()
    panel = js[js.index("function buildSettingsPanel()") : js.index("// ─── Bootstrap")]
    handlers = re.findall(r"addEventListener\('change',[\s\S]*?\n\s*\}\);", panel)
    assert handlers
    stale_writes = [h for h in handlers if re.search(r"\bs\.[A-Za-z][A-Za-z0-9_]*\s*=", h)]
    assert stale_writes == []


def test_manual_reconnect_resets_backoff_but_automatic_reconnect_does_not():
    js = source()
    helper = js[js.index("function reconnectSSEManually()") : js.index("function currentContextLabel()")]
    scheduler = js[js.index("function scheduleSseReconnect()") : js.index("function disconnectSSE()")]
    assert "sseReconnectDelay = 1000" in helper
    assert "connectSSE();" in helper
    assert "sseReconnectDelay = Math.min" in scheduler
    assert "reconnectSSEManually()" not in scheduler
    assert js.count("if (fresh.sseEnabled) reconnectSSEManually();") >= 3


def test_stale_popup_watcher_cannot_close_newer_target():
    js = source()
    assert "if (activeTarget !== win) return;" in js
    assert "if (win.closed) closeActive(win);" in js
    assert "function closeActive(expectedTarget = null)" in js
    assert "if (expectedTarget && activeTarget !== expectedTarget) return;" in js
