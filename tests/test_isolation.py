"""The test session never resolves to the operator's real runtime state."""
from __future__ import annotations

import importlib
import os
import socket
import sys
from pathlib import Path

import pytest

import conftest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config():
    server_dir = str(ROOT / "server")
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)
    for name in [m for m in sys.modules if m.split(".", 1)[0] == "calliope_server"]:
        del sys.modules[name]
    return importlib.import_module("calliope_server.config")


def _under(path: Path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root.resolve())


def test_home_and_xdg_point_into_session_temp_root():
    root = conftest.TEST_ROOT
    assert _under(Path.home(), root)
    for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "CALLIOPE_DATA_DIR"):
        assert _under(Path(os.environ[key]), root), key


def test_config_paths_resolve_under_temp_root(config):
    root = conftest.TEST_ROOT
    for attr in ("DATA_DIR", "MODEL_DIR", "TOKEN_FILE", "CERT_FILE", "KEY_FILE",
                 "VOCAB_FILE", "MODES_FILE", "CHAR_MODES_FILE", "PERSONAS_DIR",
                 "RULES_DIR", "ST_DATA_ROOT", "ST_SETTINGS_FILE", "CHARACTERS_DIR",
                 "ST_CHATS_DIR", "ST_GROUPS_DIR", "ST_GROUP_CHATS_DIR"):
        assert _under(getattr(config, attr), root), attr


def test_omniroute_key_is_the_dummy(config):
    assert config.OMNIROUTE_API_KEY == conftest.DUMMY_OMNIROUTE_KEY


def test_operator_overrides_do_not_leak_into_session():
    leaked = [k for k in os.environ
              if k.startswith(("DICTATION_", "WHISPER_", "KOKORO_", "TTS_"))
              and k not in ("DICTATION_ST_DATA_ROOT", "DICTATION_PERSONAS_DIR",
                            "DICTATION_RULES_DIR")]
    assert leaked == []


def test_wrapper_loader_uses_isolated_paths():
    mod = conftest.load_wrapper("calliope_isolation")
    assert _under(mod.config.DATA_DIR, conftest.TEST_ROOT)
    assert mod.config.OMNIROUTE_API_KEY == conftest.DUMMY_OMNIROUTE_KEY


def test_non_loopback_connect_is_denied():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(OSError, match="must not reach the network"):
            s.connect(("192.0.2.1", 80))


def test_loopback_connect_is_allowed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2):
            pass
