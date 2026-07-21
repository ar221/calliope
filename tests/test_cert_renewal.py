"""In-run TLS cert renewal coverage.

Verifies the periodic renewal check regenerates + hot-reloads only when the
cert is near expiry, and never touches real cert files (regen is mocked).
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import uuid
from importlib.machinery import SourceFileLoader

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    name = f"calliope_server_cert_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_check_renews_when_below_threshold(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls = {"ensure": 0, "reload": 0}
    monkeypatch.setattr(
        mod, "cert_days_remaining", lambda: mod.CERT_RENEW_THRESHOLD_DAYS - 1
    )
    monkeypatch.setattr(
        mod, "ensure_ssl_cert", lambda: calls.__setitem__("ensure", calls["ensure"] + 1)
    )
    monkeypatch.setattr(
        mod,
        "_reload_ssl_context_cert",
        lambda: calls.__setitem__("reload", calls["reload"] + 1) or True,
    )

    assert mod._check_and_renew_cert() is True
    assert calls["ensure"] == 1
    assert calls["reload"] == 1


def test_check_does_not_renew_when_above_threshold(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls = {"ensure": 0}
    monkeypatch.setattr(
        mod, "cert_days_remaining", lambda: mod.CERT_RENEW_THRESHOLD_DAYS + 5
    )
    monkeypatch.setattr(
        mod, "ensure_ssl_cert", lambda: calls.__setitem__("ensure", calls["ensure"] + 1)
    )

    assert mod._check_and_renew_cert() is False
    assert calls["ensure"] == 0


def test_check_skips_when_expiry_unknown(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    calls = {"ensure": 0}
    monkeypatch.setattr(mod, "cert_days_remaining", lambda: None)
    monkeypatch.setattr(
        mod, "ensure_ssl_cert", lambda: calls.__setitem__("ensure", calls["ensure"] + 1)
    )

    assert mod._check_and_renew_cert() is False
    assert calls["ensure"] == 0


def test_reload_is_noop_without_registered_context(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod._ssl_context = None
    assert mod._reload_ssl_context_cert() is False


def test_renew_thread_starts_once(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    mod._start_cert_renew_thread()
    first = mod._cert_renew_thread
    assert first is not None and first.is_alive()
    mod._start_cert_renew_thread()
    assert mod._cert_renew_thread is first  # idempotent, no second thread


def test_extension_diagnostics_shows_cert_expiry_row():
    ext = pathlib.Path(__file__).resolve().parents[1] / "extension" / "index.js"
    src = ext.read_text(encoding="utf-8")
    assert "cert_expires_days" in src
    assert "diagnosticsRow('TLS cert'" in src


# ---------------------------------------------------------------------------
# Cert-ownership discriminator + clobber guard (regression: a Tailscale/LE cert
# was auto-regenerated into a self-signed one, breaking phone pairing).
# ---------------------------------------------------------------------------


def _openssl_available() -> bool:
    try:
        subprocess.run(["openssl", "version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _mint(cert_path, key_path, subj):
    """Mint a self-signed cert with the given subject (subject == issuer)."""
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "90",
            "-subj",
            subj,
        ],
        check=True,
        capture_output=True,
    )


pytestmark = pytest.mark.skipif(not _openssl_available(), reason="openssl not on PATH")


def test_discriminator_true_for_our_self_signed(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    _mint(mod.CERT_FILE, mod.KEY_FILE, "/CN=dictation-server")
    assert mod._cert_is_calliope_self_signed() is True


def test_discriminator_false_for_custom_self_signed(tmp_path, monkeypatch):
    # A user-installed self-signed cert (subject == issuer) with a different CN
    # must NOT be treated as ours — never clobber it.
    mod = _load_server(tmp_path, monkeypatch)
    _mint(mod.CERT_FILE, mod.KEY_FILE, "/CN=my-custom-host.example")
    assert mod._cert_is_calliope_self_signed() is False


def test_discriminator_false_when_cert_missing(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    assert mod._cert_is_calliope_self_signed() is False


def test_ensure_regenerates_our_expiring_self_signed(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    _mint(mod.CERT_FILE, mod.KEY_FILE, "/CN=dictation-server")
    monkeypatch.setattr(
        mod, "cert_days_remaining", lambda: mod.CERT_RENEW_THRESHOLD_DAYS - 1
    )
    called = {"n": 0}
    monkeypatch.setattr(
        mod,
        "_generate_self_signed_cert",
        lambda sans=None: called.__setitem__("n", called["n"] + 1),
    )
    mod.ensure_ssl_cert()
    assert called["n"] == 1  # ours + near expiry -> regenerated


def test_ensure_never_clobbers_external_cert(tmp_path, monkeypatch):
    # Simulate a CA-issued cert by forcing the discriminator False and expiry
    # below threshold: ensure_ssl_cert must NOT regenerate.
    mod = _load_server(tmp_path, monkeypatch)
    _mint(mod.CERT_FILE, mod.KEY_FILE, "/CN=beast.tail351822.ts.net")
    monkeypatch.setattr(mod, "_cert_is_calliope_self_signed", lambda: False)
    monkeypatch.setattr(mod, "cert_days_remaining", lambda: 1)
    called = {"n": 0}
    monkeypatch.setattr(
        mod,
        "_generate_self_signed_cert",
        lambda sans=None: called.__setitem__("n", called["n"] + 1),
    )
    mod.ensure_ssl_cert()
    assert called["n"] == 0  # external cert left intact


def test_ensure_generates_when_missing(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(
        mod,
        "_generate_self_signed_cert",
        lambda sans=None: called.__setitem__("n", called["n"] + 1),
    )
    mod.ensure_ssl_cert()
    assert called["n"] == 1  # first run, no cert on disk
