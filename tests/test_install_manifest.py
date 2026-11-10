"""scripts/install-server + the wrapper's INSTALL_MANIFEST guard."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "scripts" / "install-server"


@pytest.fixture
def installed(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "BINDIR": str(tmp_path / "bin"),
        "DATADIR": str(tmp_path / "data"),
        "CALLIOPE_DATA_DIR": str(tmp_path / "data"),
        "OMNIROUTE_API_KEY": "test",
    }
    (tmp_path / "home").mkdir()
    subprocess.run([str(INSTALL)], env=env, check=True, capture_output=True)
    return tmp_path, env


def _start(tmp_path, env):
    return subprocess.run(
        [sys.executable, str(tmp_path / "bin" / "dictation-server"), "--help"],
        env=env, capture_output=True, text=True,
    )


def test_fresh_install_starts_and_check_reports_no_drift(installed):
    tmp_path, env = installed
    assert _start(tmp_path, env).returncode == 0
    check = subprocess.run([str(INSTALL), "--check"], env=env, capture_output=True, text=True)
    assert check.returncode == 0
    assert "DIFF" not in check.stdout


@pytest.mark.parametrize("tamper", ["edit_module", "stray_module", "drop_manifest", "swap_wrapper"])
def test_mixed_install_refuses_to_start(installed, tamper):
    tmp_path, env = installed
    data = tmp_path / "data"
    if tamper == "edit_module":
        with (data / "calliope_server" / "events.py").open("a") as fh:
            fh.write("# local hotfix\n")
    elif tamper == "stray_module":
        (data / "calliope_server" / "stray.py").write_text("")
    elif tamper == "drop_manifest":
        (data / "INSTALL_MANIFEST").unlink()
    else:
        with (tmp_path / "bin" / "dictation-server").open("a") as fh:
            fh.write("# hand-copied wrapper\n")
    result = _start(tmp_path, env)
    assert result.returncode == 1
    assert "install-server" in result.stderr
    # --check must not call an install healthy that the wrapper refuses to run.
    check = subprocess.run([str(INSTALL), "--check"], env=env, capture_output=True, text=True)
    assert check.returncode == 1
    assert "DIFF" in check.stdout or "EXTRA" in check.stdout


def test_install_through_symlink_updates_the_mirror_and_keeps_the_link(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (tmp_path / "bin").mkdir()
    (mirror / "dictation-server").write_text("stale")
    (tmp_path / "bin" / "dictation-server").symlink_to(mirror / "dictation-server")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "BINDIR": str(tmp_path / "bin"),
        "DATADIR": str(tmp_path / "data"),
    }
    subprocess.run([str(INSTALL)], env=env, check=True, capture_output=True)
    assert (tmp_path / "bin" / "dictation-server").is_symlink()
    assert (mirror / "dictation-server").read_bytes() == (REPO / "server" / "calliope-server").read_bytes()


def test_kokoro_script_is_deployed_and_checked_when_the_tts_runtime_exists(tmp_path):
    tts = tmp_path / "calliope-tts"
    tts.mkdir()
    (tts / "kokoro-server.py").write_text("# stale copy\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "BINDIR": str(tmp_path / "bin"),
        "DATADIR": str(tmp_path / "data"),
        "TTSDIR": str(tts),
    }
    subprocess.run([str(INSTALL)], env=env, check=True, capture_output=True)
    assert (tts / "kokoro-server.py").read_bytes() == (REPO / "scripts" / "kokoro-server.py").read_bytes()

    (tts / "kokoro-server.py").write_text("# drifted\n")
    check = subprocess.run([str(INSTALL), "--check"], env=env, capture_output=True, text=True)
    assert check.returncode == 1
    assert "DIFF  calliope-tts/kokoro-server.py" in check.stdout
