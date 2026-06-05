"""Token rotation CLI/unit coverage using isolated runtime state."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import pathlib
import stat
import subprocess
import sys
import uuid
from importlib.machinery import SourceFileLoader

SRC = pathlib.Path(__file__).resolve().parents[1] / "server" / "calliope-server"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _contains_token(output: str, token: str) -> bool:
    return token in output


def _load_server(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLIOPE_DATA_DIR", str(tmp_path))
    name = f"calliope_server_token_rotation_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(SRC))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_regenerate_token_writes_0600_and_changes_value(tmp_path, monkeypatch):
    mod = _load_server(tmp_path, monkeypatch)

    first = mod.ensure_token(log_new_token=False)
    assert stat.S_IMODE(mod.TOKEN_FILE.stat().st_mode) == 0o600

    second = mod.regenerate_token(log_new_token=False)
    assert _digest(first) != _digest(second)
    assert stat.S_IMODE(mod.TOKEN_FILE.stat().st_mode) == 0o600
    assert _digest(mod.TOKEN_FILE.read_text(encoding="utf-8").strip()) == _digest(second)


def test_rotate_token_cli_does_not_print_token_and_changes_value(tmp_path):
    env = os.environ.copy()
    env["CALLIOPE_DATA_DIR"] = str(tmp_path)
    cmd = [sys.executable, str(SRC), "--rotate-token"]

    first_run = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    token_path = tmp_path / "token"
    first_token = token_path.read_text(encoding="utf-8").strip()
    first_output = first_run.stdout + first_run.stderr

    assert str(token_path) in first_output
    assert "hard-refresh SillyTavern" in first_output
    assert not _contains_token(first_output, first_token)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600

    second_run = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    second_token = token_path.read_text(encoding="utf-8").strip()
    second_output = second_run.stdout + second_run.stderr

    assert _digest(first_token) != _digest(second_token)
    assert not _contains_token(second_output, second_token)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
