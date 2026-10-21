"""Session-wide isolation from the operator's runtime state.

Many test modules load `server/calliope-server` in a module-scoped fixture
without pointing `CALLIOPE_DATA_DIR` anywhere. Those modules then read — and
any handler test can *write* — the live vocab/modes/token files under
`~/.local/share/dictation-server`, and read the real SillyTavern tree. This
fixture runs before any of them import the server and redirects every
runtime path to an empty per-session temp tree. Tests that set their own
`CALLIOPE_DATA_DIR` via monkeypatch still win (they run later).
"""
from __future__ import annotations

import os

import pytest

_RUNTIME_ENV = {
    "CALLIOPE_DATA_DIR": "data",
    "XDG_DATA_HOME": "xdg",            # MODEL_DIR and the DATA_DIR fallback
    "DICTATION_ST_DATA_ROOT": "st",
    "DICTATION_PERSONAS_DIR": "personas",
    "DICTATION_RULES_DIR": "rules",
}


@pytest.fixture(scope="session", autouse=True)
def _isolate_runtime_dirs(tmp_path_factory):
    root = tmp_path_factory.mktemp("calliope-runtime")
    saved = {k: os.environ.get(k) for k in _RUNTIME_ENV}
    for key, sub in _RUNTIME_ENV.items():
        path = root / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    try:
        yield root
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
