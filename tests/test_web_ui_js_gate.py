"""Embedded phone PWA JavaScript syntax gate.

`server/calliope-server` keeps the PWA as a Python raw string for packaging.
These tests make sure the extractor checks the real WEB_UI block and fails
loudly if the markers drift or the embedded JS stops parsing.
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "check-web-ui-js"


def run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GATE), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_embedded_web_ui_javascript_parses():
    result = run_gate()
    assert result.returncode == 0, result.stderr or result.stdout
    assert "extracted 1 script block" in result.stdout


def test_gate_fails_loud_when_web_ui_marker_moves(tmp_path):
    source = tmp_path / "server"
    source.write_text(
        'NO_WEB_UI = r"""<script>const still_not_the_pwa = true;</script>"""\n',
        encoding="utf-8",
    )
    result = run_gate("--source", str(source))
    assert result.returncode != 0
    assert "GATE FAIL: WEB_UI raw-string block not found" in result.stderr


def test_gate_fails_loud_when_embedded_javascript_is_invalid(tmp_path):
    source = tmp_path / "server"
    source.write_text(
        'WEB_UI = r"""<html><script>function broken( {</script></html>"""\n',
        encoding="utf-8",
    )
    result = run_gate("--source", str(source))
    assert result.returncode != 0
    assert "GATE FAIL: node --check failed" in result.stderr
