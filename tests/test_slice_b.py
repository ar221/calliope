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


def test_server_errors_are_escaped_before_html_capable_toast_sink():
    source = _source()
    error = _block(source, "function showDesktopCaptureError(error)", "function activateMic()")
    assert "escapeHtml(error?.message" in error
    assert "Desktop dictation failed: ${message}" in error
