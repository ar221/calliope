from __future__ import annotations

import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTENSION_SRC = ROOT / "extension" / "index.js"
STYLE_SRC = ROOT / "extension" / "style.css"
SERVER_SRC = ROOT / "server" / "calliope-server"


def _source() -> str:
    return EXTENSION_SRC.read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    return source[start_index : source.index(end, start_index)]


def test_transcript_drawer_loads_only_on_explicit_open_or_refresh():
    source = _source()
    assert "Session transcript" in source
    assert 'id="dictation_bridge_transcript_drawer"' in source
    assert 'id="dictation_bridge_transcript_refresh"' in source

    wiring = _block(source, "// ─── Slice C: transcript drawer wiring", "// ─── TTS settings wiring")
    assert "transcriptDrawerEl.addEventListener('toggle'" in wiring
    assert "transcriptDrawerEl.open && !sessionTranscriptLoaded" in wiring
    assert "loadSessionTranscript()" in wiring
    assert "transcriptRefreshEl.addEventListener('click'" in wiring
    assert "setInterval" not in wiring


def test_transcript_endpoint_and_bearer_auth_schemas_are_exact():
    source = _source()
    server = SERVER_SRC.read_text(encoding="utf-8")
    load = _block(source, "async function loadSessionTranscript()", "async function toggleTranscriptStar(")
    star = _block(source, "async function toggleTranscriptStar(", "async function deleteTranscriptEntry(")
    delete = _block(source, "async function deleteTranscriptEntry(", "function transcriptExportFilename(")

    assert "`${base}/transcript`" in load
    assert "method: 'GET'" in load
    assert "headers: authHeaders()" in load
    assert "data?.transcript" in load

    assert "`${base}/transcript/${encodeURIComponent(entryId)}/star`" in star
    assert "method: 'POST'" in star
    assert "headers: authHeaders()" in star
    assert "body:" not in star

    assert "`${base}/transcript/${encodeURIComponent(entryId)}`" in delete
    assert "method: 'DELETE'" in delete
    assert "headers: authHeaders()" in delete
    assert "body:" not in delete

    assert 'elif path == "/transcript/export.md":' in server
    assert 'elif path.startswith("/transcript/") and path.endswith("/star"):' in server
    assert 'elif path.startswith("/transcript/"):' in server
    assert "if not self.require_auth():" in server


def test_transcript_rendering_is_text_content_only_with_role_time_star_and_expand():
    source = _source()
    render = _block(source, "function renderSessionTranscript()", "async function loadSessionTranscript()")
    assert "textContent" in render
    assert "innerHTML" not in render
    assert "role === 'context' ? 'Context' : 'You'" in render
    assert "formatTranscriptTimestamp(entry?.timestamp)" in render
    assert "aria-pressed" in render
    assert "aria-expanded" in render
    assert "TRANSCRIPT_PREVIEW_CHARS" in render
    assert "fullText.slice(0, TRANSCRIPT_PREVIEW_CHARS)" in render


def test_delete_is_single_entry_only_and_requires_confirmation():
    source = _source()
    delete = _block(source, "async function deleteTranscriptEntry(", "function transcriptExportFilename(")
    assert "window.confirm(" in delete
    assert "encodeURIComponent(entryId)" in delete
    assert "method: 'DELETE'" in delete
    assert '"/transcript"' not in delete
    assert "?all=" not in delete
    assert "clear" not in delete.lower()
    assert "await loadSessionTranscript()" in delete


def test_star_unstar_refreshes_and_reconciles_errors():
    source = _source()
    star = _block(source, "async function toggleTranscriptStar(", "async function deleteTranscriptEntry(")
    assert "method: 'POST'" in star
    assert "if (!res.ok)" in star
    assert "await loadSessionTranscript()" in star
    assert "escapeHtml(error?.message" in star


def test_all_transcript_toasts_escape_server_derived_errors():
    source = _source()
    star = _block(source, "async function toggleTranscriptStar(", "async function deleteTranscriptEntry(")
    delete = _block(source, "async function deleteTranscriptEntry(", "function transcriptExportFilename(")
    export = _block(source, "async function exportSessionTranscript(", "// ─── Quick-launch panel")
    for block in (star, delete, export):
        assert "escapeHtml(error?.message" in block


def test_markdown_export_uses_authenticated_blob_download_and_cleanup():
    source = _source()
    export = _block(source, "async function exportSessionTranscript(", "// ─── Quick-launch panel")
    download = _block(source, "function downloadBlob(", "async function exportCurrentChatAudiobook(")

    assert "`${base}/transcript/export.md`" in export
    assert "method: 'GET'" in export
    assert "headers: authHeaders()" in export
    assert "await res.blob()" in export
    assert "downloadBlob(blob, filename)" in export
    assert "URL.createObjectURL(blob)" in download
    assert "a.click()" in download
    assert "a.remove()" in download
    assert "URL.revokeObjectURL(url)" in download


def test_transcript_requests_never_put_token_in_url():
    source = _source()
    transcript = _block(source, "// ─── Slice C: in-memory session transcript", "// ─── Quick-launch panel")
    assert "serverToken" not in transcript
    assert "?token=" not in transcript
    assert "URLSearchParams" not in transcript
    assert "authHeaders()" in transcript


def test_transcript_is_not_logged_or_persisted():
    source = _source()
    transcript = _block(source, "// ─── Slice C: in-memory session transcript", "// ─── Quick-launch panel")
    for persistence_api in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "saveSettings",
        "extension_settings",
    ):
        assert persistence_api not in transcript
    for logging_api in ("LOG(", "WARN(", "ERR(", "console."):
        assert logging_api not in transcript

    style = STYLE_SRC.read_text(encoding="utf-8")
    assert ".dbb-transcript-list" in style
    assert ".dbb-transcript-entry" in style
