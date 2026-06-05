"""Vendored QR renderer smoke checks.

The ST settings-panel QR flow depends on the vendored Nayuki browser library.
Keep this Python-owned smoke tiny: no jsdom/vitest scaffold, just Node parsing
and importing the ES-module wrapper.
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
QR_LIB = ROOT / "extension" / "qrcodegen.min.js"


def test_vendored_qrcodegen_imports_and_encodes():
    script = """
        import { qrcodegen } from './extension/qrcodegen.min.js';
        const qr = qrcodegen.QrCode.encodeText('https://example.invalid/pair', qrcodegen.QrCode.Ecc.MEDIUM);
        if (!qr || qr.size < 21 || !qr.getModule(0, 0)) process.exit(1);
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
