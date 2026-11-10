"""Phone PWA JavaScript syntax gate.

The PWA lives in Python strings for packaging. These tests make sure the gate
checks every served script (WEB_UI, the pairing bootstrap and inline handlers)
and fails loudly if a page constant goes missing or any script stops parsing.
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


def test_served_javascript_parses():
    result = run_gate()
    assert result.returncode == 0, result.stderr or result.stdout
    assert "ok: WEB_UI <script> #1" in result.stdout
    assert "ok: PAIRING_BOOTSTRAP_HTML <script> #1" in result.stdout
    assert "ok: WEB_UI inline handlers" in result.stdout


def test_gate_fails_loud_when_a_page_constant_is_missing(tmp_path):
    source = tmp_path / "web_ui.py"
    source.write_text(
        'WEB_UI = r"""<script>const fine = true;</script>"""\n',
        encoding="utf-8",
    )
    result = run_gate("--source", str(source))
    assert result.returncode != 0
    assert "GATE FAIL: PAIRING_BOOTSTRAP_HTML not found" in result.stderr


def test_gate_fails_loud_when_bootstrap_javascript_is_invalid(tmp_path):
    source = tmp_path / "web_ui.py"
    source.write_text(
        'WEB_UI = r"""<html><script>const fine = true;</script></html>"""\n'
        'PAIRING_BOOTSTRAP_HTML = """<script>function broken( {</script>"""\n',
        encoding="utf-8",
    )
    result = run_gate("--source", str(source))
    assert result.returncode != 0
    assert "FAIL: PAIRING_BOOTSTRAP_HTML <script> #1" in result.stdout
    assert "GATE FAIL: node --check failed" in result.stderr


def test_gate_checks_inline_handlers(tmp_path):
    source = tmp_path / "web_ui.py"
    source.write_text(
        'WEB_UI = r"""<button onclick="go(&quot;x&quot;"></button><script>1;</script>"""\n'
        'PAIRING_BOOTSTRAP_HTML = """<script>2;</script>"""\n',
        encoding="utf-8",
    )
    result = run_gate("--source", str(source))
    assert result.returncode != 0
    assert "FAIL: WEB_UI inline handlers (1)" in result.stdout
