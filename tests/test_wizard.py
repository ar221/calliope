"""Setup wizard: repo unit install, one-time-code pairing, model hash pinning."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import pathlib
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
wizard = importlib.import_module("calliope_server.wizard")

DURABLE_TOKEN = "durable-bearer-must-never-reach-wizard-output"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(wizard, "WIZARD_UNIT_DIR", tmp_path / "systemd-user")
    monkeypatch.setattr(wizard.config, "MODEL_DIR", tmp_path / "whisper")


def _deps(token: str = DURABLE_TOKEN):
    return SimpleNamespace(get_token=lambda: token)


# ─── Stage 4: repo units ───────────────────────────────────

def test_repo_unit_lookup_resolves_checkout_systemd_dir():
    for name in wizard.WIZARD_UNITS:
        assert wizard._wizard_repo_unit(name) == ROOT / "systemd" / name


def test_repo_unit_lookup_uses_units_staged_by_install_server(tmp_path):
    data = tmp_path / "share" / "dictation-server"
    pkg = data / "calliope_server"
    pkg.mkdir(parents=True)
    (data / "units").mkdir()
    (data / "units" / "dictation-server.service").write_text("[Unit]\n")

    assert wizard._wizard_repo_unit("dictation-server.service", pkg) == \
        data / "units" / "dictation-server.service"
    assert wizard._wizard_repo_unit("whisper-server.service", pkg) is None


def _stub_systemctl(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)
    monkeypatch.setattr(wizard, "_wizard_wait_health", lambda *_a: True)
    return calls


def test_stage_4_installs_repo_units_and_enables_only_dictation(monkeypatch):
    calls = _stub_systemctl(monkeypatch)

    wizard._wizard_stage_4(SimpleNamespace(install_systemd=True))

    for name in wizard.WIZARD_UNITS:
        installed = wizard.WIZARD_UNIT_DIR / name
        assert installed.read_text() == (ROOT / "systemd" / name).read_text()
    enables = [c for c in calls if "enable" in c]
    assert enables == [["systemctl", "--user", "enable", "--now", "dictation-server"]]


def test_stage_4_without_repo_units_stops_and_writes_nothing(monkeypatch, capsys):
    calls = _stub_systemctl(monkeypatch)
    monkeypatch.setattr(wizard, "_wizard_repo_unit", lambda _name: None)

    with pytest.raises(SystemExit):
        wizard._wizard_stage_4(SimpleNamespace(install_systemd=True))

    assert not wizard.WIZARD_UNIT_DIR.exists()
    assert calls == []
    assert "scripts/install-server" in capsys.readouterr().out


def _unit_sections(name: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in (ROOT / "systemd" / name).read_text().splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
        elif line and not line.startswith("#"):
            sections[current].append(line)
    return sections


def test_on_demand_units_do_not_start_at_login_and_dictation_logs_info():
    for name in ("whisper-server.service", "kokoro-server.service"):
        assert "Install" not in _unit_sections(name)
    assert "LogLevelMax=info" in _unit_sections("dictation-server.service")["Service"]


# ─── Stage 6: one-time pairing code ────────────────────────

class _FakeResp(io.BytesIO):
    status = 201
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_stage_6_prints_one_time_pair_url_and_never_the_bearer(monkeypatch, capsys):
    requests = []

    def fake_urlopen(req, **_kw):
        requests.append(req)
        return _FakeResp(json.dumps({"code": "one-time-code", "expires_in": 120}).encode())

    qr_inputs = []

    def fake_run(cmd, **_kw):
        qr_inputs.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "<qr>", "")

    monkeypatch.setattr(wizard.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)
    monkeypatch.setattr(wizard, "_wizard_lan_ip", lambda: "192.0.2.10")

    wizard._wizard_stage_6(SimpleNamespace(), "AA:BB", _deps())

    out = capsys.readouterr().out
    pair_url = f"https://192.0.2.10:{wizard.config.DEFAULT_PORT}/?pair=one-time-code"
    assert pair_url in out
    assert qr_inputs == [pair_url]
    assert DURABLE_TOKEN not in out
    assert DURABLE_TOKEN not in qr_inputs[0]
    (req,) = requests
    assert req.full_url.endswith("/pair/bootstrap")
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {DURABLE_TOKEN}"


def test_stage_6_mint_failure_falls_back_without_leaking_bearer(monkeypatch, capsys):
    def fail(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(wizard.urllib.request, "urlopen", fail)

    wizard._wizard_stage_6(SimpleNamespace(), "", _deps())

    out = capsys.readouterr().out
    assert DURABLE_TOKEN not in out
    assert "?pair=" not in out
    assert "SillyTavern extension" in out


# ─── Stage 2: SHA-256 pinning ──────────────────────────────

MODEL_BYTES = b"pretend ggml weights"


def _pin_model(monkeypatch, sha: str):
    monkeypatch.setattr(wizard, "WIZARD_MODELS", [("tiny.en", 0, 0, "test", sha)])
    monkeypatch.setattr(wizard, "_wizard_confirm", lambda *_a, **_kw: True)


def test_stage_2_skips_cached_model_with_matching_hash(monkeypatch):
    _pin_model(monkeypatch, hashlib.sha256(MODEL_BYTES).hexdigest())
    wizard.config.MODEL_DIR.mkdir(parents=True)
    (wizard.config.MODEL_DIR / "ggml-tiny.en.bin").write_bytes(MODEL_BYTES)

    def no_network(*_a, **_kw):
        raise AssertionError("verified cached model must not be re-downloaded")

    monkeypatch.setattr(wizard.urllib.request, "urlopen", no_network)
    wizard._wizard_stage_2(SimpleNamespace(force=False), {"model_id": "tiny.en"})


def test_stage_2_rejects_download_with_wrong_hash(monkeypatch):
    _pin_model(monkeypatch, hashlib.sha256(b"the real weights").hexdigest())
    monkeypatch.setattr(wizard.urllib.request, "urlopen",
                        lambda *_a, **_kw: _FakeResp(MODEL_BYTES))

    with pytest.raises(SystemExit):
        wizard._wizard_stage_2(SimpleNamespace(force=False), {"model_id": "tiny.en"})

    assert list(wizard.config.MODEL_DIR.iterdir()) == []
