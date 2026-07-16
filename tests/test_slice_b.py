from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTENSION_SRC = ROOT / "extension" / "index.js"
STYLE_SRC = ROOT / "extension" / "style.css"


def _source() -> str:
    return EXTENSION_SRC.read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def test_capture_mode_setting_ui_and_fresh_change_wiring():
    source = _source()
    assert "captureMode: 'phone-popup'" in source
    assert 'id="dictation_bridge_capture_mode"' in source
    assert '<option value="phone-popup">Phone popup (default)</option>' in source
    assert '<option value="desktop-push-to-talk">Desktop push-to-talk (hold)</option>' in source
    assert '<option value="desktop-toggle">Desktop toggle (click to start/stop)</option>' in source

    wiring = _block(
        source,
        "captureModeEl.addEventListener('change'",
        "appendEl.addEventListener('change'",
    )
    expected_order = [
        "const fresh = settings()",
        "cancelDesktopCapture()",
        "fresh.captureMode =",
        "saveSettings()",
        "setMicState('idle')",
    ]
    positions = [wiring.index(fragment) for fragment in expected_order]
    assert positions == sorted(positions)
    assert "DESKTOP_CAPTURE_MODES.has(captureModeEl.value)" in wiring


def test_capture_modes_preserve_phone_popup_and_define_desktop_semantics():
    source = _source()
    click = _block(source, "async function onMicClick()", "function openPopup(url)")
    assert "if (captureMode === 'desktop-push-to-talk') return" in click
    assert "if (captureMode === 'desktop-toggle')" in click
    assert "if (desktopCapture) stopDesktopRecording('toggle')" in click
    assert "else await startDesktopRecording('toggle')" in click
    assert "const ok = await probeServer()" in click
    assert "openIframe(url)" in click
    assert "openPopup(url)" in click

    mic = _block(source, "function injectMicButton()", "function setMicActive(active)")
    for event in ("pointerdown", "pointerup", "pointercancel", "lostpointercapture"):
        assert f"addEventListener('{event}'" in mic
    assert "e.repeat" in mic
    assert "releaseDesktopPushToTalk('keyboard-release')" in mic
    assert "releaseDesktopPushToTalk('keyboard-blur')" in mic


def test_transcribe_request_carries_canonical_context_language_and_auth():
    source = _source()
    submit = _block(source, "async function submitDesktopRecording(capture)", "async function onMicClick()")
    for param in ("mode", "context", "persona", "character", "language"):
        assert f"params.set('{param}'" in submit
    assert "cfg.pushContext ? (ctx.lastAi || '') : ''" in submit
    assert "currentReformatCharacterKey()" in submit
    assert "await fetchCharMode(character)" in submit
    assert "/^(?:[a-z]{2,4}|auto)$/" in submit
    assert "'/transcribe?'" not in submit  # URL is rooted at the configured server, not the ST origin.
    assert "}/transcribe?${params.toString()}" in submit
    assert "...authHeaders()" in submit
    assert "'Content-Type': capture.format.mimeType" in submit
    assert "method: 'POST'" in submit
    assert "body: blob" in submit
    assert "signal: capture.abortController.signal" in submit


def test_media_recorder_races_cleanup_and_exactly_once_submission_are_guarded():
    source = _source()
    start = _block(source, "async function startDesktopRecording(trigger)", "function stopDesktopRecording(")
    assert "desktopCapture !== capture || capture.cancelled || capture.stopRequested" in start
    assert "settings().captureMode === 'desktop-push-to-talk' && !desktopPttHeld" in start
    assert "capture.submissionStarted" in start
    assert "capture.recorderErrorHandled" in start
    assert "if (capture.cancelled || capture.recorderErrorHandled || capture.submissionStarted) return" in start
    assert "capture.submissionStarted = true" in start

    cleanup = _block(source, "function cleanupDesktopCaptureHardware(capture)", "function showDesktopCaptureError(error)")
    assert "clearTimeout(capture.maxTimer)" in cleanup
    assert "capture.stream.getTracks()" in cleanup
    assert "track.stop()" in cleanup
    assert "capture.abortController?.abort()" in cleanup
    assert "capture.chunks.length = 0" in cleanup

    stop = _block(source, "function stopDesktopRecording(", "async function submitDesktopRecording(capture)")
    assert "capture.phase === 'acquiring' || capture.phase === 'transcribing'" in stop
    assert "cancelDesktopCapture(capture)" in stop


def test_desktop_capture_has_sixty_second_guard_and_visible_states():
    source = _source()
    style = STYLE_SRC.read_text(encoding="utf-8")
    assert "const DESKTOP_CAPTURE_MAX_MS = 60_000" in source
    assert "setTimeout(() =>" in _block(
        source, "async function startDesktopRecording(trigger)", "function stopDesktopRecording("
    )
    assert "stopDesktopRecording('max-duration', capture)" in source
    assert "60-second privacy limit" in source
    assert ".dictation-bridge-mic--recording" in style
    assert ".dictation-bridge-mic--transcribing" in style
    assert "prefers-reduced-motion: reduce" in style


def test_desktop_audio_is_not_logged_or_persisted_by_extension():
    source = _source()
    capture = _block(
        source,
        "function desktopAudioFormat()",
        "async function onMicClick()",
    )
    for persistence_api in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "FileSystem",
        "showSaveFilePicker",
        "URL.createObjectURL",
    ):
        assert persistence_api not in capture
    for forbidden_log in (
        "LOG(capture",
        "WARN(capture",
        "ERR(capture",
        "console.log(blob",
        "console.log(capture.chunks",
    ):
        assert forbidden_log not in capture
    assert "body: blob" in capture
    assert "capture.chunks.length = 0" in capture


def test_http_and_sse_results_share_request_id_dedupe_path():
    source = _source()
    result = _block(source, "function rememberAppliedDictationRequest(requestId)", "function buildPairedPhoneUrl(")
    assert "appliedDictationRequestIds.includes(requestId)" in result
    assert "if (dictationRequestWasApplied(requestId)) return false" in result
    assert "rememberAppliedDictationRequest(requestId)" in result
    assert "if (appliedDictationRequestIds.length > 32)" in result

    sse = _block(
        source,
        "sseSource.addEventListener('dictation-result'",
        "sseSource.addEventListener('error'",
    )
    submit = _block(source, "async function submitDesktopRecording(capture)", "async function onMicClick()")
    assert "applyDictationResult(data" in sse
    assert "applyDictationResult(data, { source: 'desktop' })" in submit
    assert "dictationRequestWasApplied(String(data?.request_id || data?.requestId || ''))" in submit


def test_server_errors_are_escaped_before_html_capable_toast_sink():
    source = _source()
    error = _block(source, "function showDesktopCaptureError(error)", "function activateMic()")
    assert "escapeHtml(error?.message" in error
    assert "Desktop dictation failed: ${message}" in error
