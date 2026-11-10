"""Session-wide isolation from the operator's runtime state.

Runs at conftest import time — before pytest imports any test module, and so
before `calliope_server.config` (or the hyphenated wrapper) evaluates its
module-level paths. Every runtime location, the HOME used for the fish
secret-file fallback, and the OmniRoute credential are replaced with a
throwaway per-session tree and a dummy key. Operator overrides
(`DICTATION_*`, `WHISPER_*`, `KOKORO_*`, `TTS_*`, `CALLIOPE_*`) are dropped so
an exported shell value cannot redirect tests to live files or servers.
Tests that set their own values via monkeypatch still win (they run later).

Network: an autouse fixture makes any non-loopback socket connect raise.
"""
from __future__ import annotations

import atexit
import importlib.util
import ipaddress
import os
import shutil
import socket
import sys
import tempfile
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "server" / "calliope-server"
DUMMY_OMNIROUTE_KEY = "test-dummy-omniroute-key"

_OVERRIDE_PREFIXES = ("DICTATION_", "WHISPER_", "KOKORO_", "TTS_", "CALLIOPE_")

_RUNTIME_ENV = {
    "HOME": "home",
    "XDG_DATA_HOME": "xdg/data",       # MODEL_DIR and the DATA_DIR fallback
    "XDG_CONFIG_HOME": "xdg/config",
    "XDG_CACHE_HOME": "xdg/cache",
    "XDG_STATE_HOME": "xdg/state",
    "XDG_RUNTIME_DIR": "xdg/runtime",  # no real user bus for stray systemctl --user
    "CALLIOPE_DATA_DIR": "data",
    "DICTATION_ST_DATA_ROOT": "st",
    "DICTATION_PERSONAS_DIR": "personas",
    "DICTATION_RULES_DIR": "rules",
}

TEST_ROOT = Path(tempfile.mkdtemp(prefix="calliope-tests-"))
atexit.register(shutil.rmtree, TEST_ROOT, ignore_errors=True)

for _key in [k for k in os.environ if k.startswith(_OVERRIDE_PREFIXES)]:
    del os.environ[_key]
os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
for _key, _sub in _RUNTIME_ENV.items():
    _path = TEST_ROOT / _sub
    _path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ[_key] = str(_path)
os.environ["OMNIROUTE_API_KEY"] = DUMMY_OMNIROUTE_KEY


def load_wrapper(prefix: str = "calliope_server_test"):
    """Execute `server/calliope-server` as a fresh, uniquely named module.

    The wrapper has no `.py` suffix, so it needs an explicit SourceFileLoader.
    Its bootstrap purges cached `calliope_server` modules, so `config` re-reads
    the environment on every call — set env (monkeypatch) before calling.
    """
    name = f"{prefix}_{uuid.uuid4().hex}"
    loader = SourceFileLoader(name, str(WRAPPER))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_script(path: Path, name: str):
    """Load an extensionless script (e.g. scripts/learn-vocab) as a module."""
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session", autouse=True)
def _isolate_runtime_dirs():
    yield TEST_ROOT


def _is_loopback(address) -> bool:
    if not isinstance(address, tuple):
        return True  # AF_UNIX path / abstract socket
    host = address[0]
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _deny_external_network(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(address):
        if not _is_loopback(address):
            raise OSError(f"tests must not reach the network (connect to {address!r})")

    def connect(self, address):
        guard(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        guard(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
