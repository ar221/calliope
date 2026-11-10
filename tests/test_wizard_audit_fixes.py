"""Setup wizard: self-test wire format, audio cleanup, model pinning, --force reruns."""
from __future__ import annotations

import http.server
import importlib
import pathlib
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
wizard = importlib.import_module("calliope_server.wizard")

WAV = b"RIFF" + b"\x00" * 2044  # > 1 KiB: passes the "recording empty" gate


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "WIZARD_UNIT_DIR", tmp_path / "systemd-user")
    monkeypatch.setattr(wizard.config, "MODEL_DIR", tmp_path / "whisper")
    monkeypatch.setattr(wizard.config, "DEFAULT_MODEL", "large-v3-turbo")
    monkeypatch.setattr(wizard.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


# ─── SC-01: self-test sends the raw WAV like the real clients ─────────

class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: dict = {}
    reply = (200, b'{"text": "the quick brown fox jumps over the lazy dog"}')

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        type(self).seen = {
            "path": self.path,
            "content_type": self.headers["Content-Type"],
            "auth": self.headers["Authorization"],
            "body": self.rfile.read(length),
        }
        status, body = type(self).reply
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


@pytest.fixture
def recorder():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        _Recorder.reply = (200, b'{"text": "the quick brown fox jumps over the lazy dog"}')


def test_post_wav_sends_raw_audio_body_with_audio_wav_type(recorder, tmp_path):
    wav = tmp_path / "take.wav"
    wav.write_bytes(WAV)

    status, body = wizard._wizard_post_wav(f"{recorder}/transcribe?mode=plain", wav, "tok")

    assert status == 200
    assert body["text"].startswith("the quick")
    assert _Recorder.seen["content_type"] == "audio/wav"
    assert _Recorder.seen["body"] == WAV
    assert _Recorder.seen["auth"] == "Bearer tok"
    assert _Recorder.seen["path"] == "/transcribe?mode=plain"


def test_post_wav_returns_http_error_status_instead_of_raising(recorder, tmp_path):
    _Recorder.reply = (415, b'{"error": "unsupported_media_type"}')
    wav = tmp_path / "take.wav"
    wav.write_bytes(WAV)

    status, body = wizard._wizard_post_wav(f"{recorder}/transcribe", wav, "tok")

    assert status == 415
    assert body == {"error": "unsupported_media_type"}


# ─── SC-03: recording never outlives stage 5 ──────────────────────────

def _stage5_env(monkeypatch, *, record=True, post=None):
    recorded: list[pathlib.Path] = []

    def fake_run(cmd, **_kw):
        if cmd[0] == "pw-record":
            path = pathlib.Path(cmd[-1])
            recorded.append(path)
            if record:
                path.write_bytes(WAV)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)
    monkeypatch.setattr(wizard, "_wizard_confirm", lambda *_a, **_kw: True)
    if post is not None:
        monkeypatch.setattr(wizard, "_wizard_post_wav", post)
    return recorded


def _boom(*_a, **_kw):
    raise OSError("connection refused")


@pytest.mark.parametrize("record, post, exits", [
    (True, _boom, True),
    (True, lambda *_a: (401, {"error": "unauthorized"}), True),
    (True, lambda *_a: (200, {"text": ""}), True),
    (True, lambda *_a: (200, {"text": "the quick brown fox jumps over the lazy dog"}), False),
    (False, None, False),
])
def test_stage_5_removes_recording_on_every_path(monkeypatch, tmp_path, record, post, exits):
    recorded = _stage5_env(monkeypatch, record=record, post=post or _boom)
    deps = SimpleNamespace(get_token=lambda: "tok")

    if exits:
        with pytest.raises(SystemExit):
            wizard._wizard_stage_5(SimpleNamespace(), deps)
    else:
        wizard._wizard_stage_5(SimpleNamespace(), deps)

    assert len(recorded) == 1
    assert not recorded[0].exists()
    assert not recorded[0].parent.exists()
    assert list((tmp_path / "tmp").iterdir()) == []


def test_stage_5_records_into_unique_private_directory(monkeypatch, tmp_path):
    modes = []

    def post(_url, wav, _token):
        modes.append(wav.parent.stat().st_mode & 0o777)
        return 200, {"text": "the quick brown fox jumps over the lazy dog"}

    recorded = _stage5_env(monkeypatch, post=post)
    deps = SimpleNamespace(get_token=lambda: "tok")
    wizard._wizard_stage_5(SimpleNamespace(), deps)
    wizard._wizard_stage_5(SimpleNamespace(), deps)

    assert modes == [0o700, 0o700]
    assert recorded[0].parent != recorded[1].parent


# ─── SC-02: the chosen model is the one the services load ─────────────

def _stub_systemctl(monkeypatch, active: dict[str, float] | None = None):
    """Fake systemctl. `active` maps unit → start time in seconds relative to now."""
    active = active or {}
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return subprocess.CompletedProcess(cmd, 0 if cmd[-1] in active else 3, "", "")
        if cmd[:3] == ["systemctl", "--user", "show"]:
            unit = cmd[3]
            if unit not in active:
                return subprocess.CompletedProcess(cmd, 0, "ActiveState=inactive\n", "")
            # Clamp: on a freshly booted CI runner "an hour ago" predates boot.
            mono_us = max(1, int((time.clock_gettime(time.CLOCK_MONOTONIC) + active[unit]) * 1e6))
            out = f"ActiveState=active\nActiveEnterTimestampMonotonic={mono_us}\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(wizard.shutil, "which",
                        lambda name: "/usr/bin/systemctl" if name == "systemctl" else None)
    monkeypatch.setattr(wizard.subprocess, "run", fake_run)
    monkeypatch.setattr(wizard, "_wizard_wait_health", lambda *_a: True)
    return calls


def _download(model: str):
    wizard.config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (wizard.config.MODEL_DIR / f"ggml-{model}.bin").write_bytes(b"weights")


def test_stage_4_pins_downloaded_model_for_both_units(monkeypatch):
    _stub_systemctl(monkeypatch)
    _download("tiny.en")
    state = {"model_id": "tiny.en"}

    wizard._wizard_stage_4(SimpleNamespace(install_systemd=True), state)

    for unit in ("whisper-server.service", "dictation-server.service"):
        dropin = wizard.WIZARD_UNIT_DIR / f"{unit}.d" / "calliope-model.conf"
        lines = dropin.read_text().splitlines()
        assert "[Service]" in lines
        assert "Environment=DICTATION_WHISPER_MODEL=tiny.en" in lines
    assert state["model_applied"] is True
    assert wizard._wizard_effective_model() == "tiny.en"


def test_summary_flags_saved_modes_that_pin_another_model(monkeypatch, tmp_path, capsys):
    modes = tmp_path / "modes.json"
    modes.write_text(
        '[{"id": "plain", "whisper_model": "large-v3-turbo"},'   # legacy seed → inherits
        ' {"id": "rp_enhance", "whisper_model": ""},'            # inherits
        ' {"id": "grammar_clean", "whisper_model": "small"},'    # built-in explicit pin
        ' {"id": "my_mode", "whisper_model": "large-v3-turbo"}]' # custom explicit pin
    )
    monkeypatch.setattr(wizard.config, "MODES_FILE", modes)
    _stub_systemctl(monkeypatch)
    _download("tiny.en")
    wizard._wizard_stage_4(SimpleNamespace(install_systemd=True), {"model_id": "tiny.en"})

    wizard._wizard_stage_7({"model_id": "tiny.en"}, "")

    out = capsys.readouterr().out
    assert "Model:        tiny.en" in out
    assert "mode 'grammar_clean' pins whisper_model=small" in out
    assert "mode 'my_mode' pins whisper_model=large-v3-turbo" in out
    assert "'plain'" not in out
    assert "rp_enhance" not in out


def test_stage_4_does_not_pin_a_model_that_was_not_downloaded(monkeypatch):
    _stub_systemctl(monkeypatch)
    state = {"model_id": "tiny.en"}

    wizard._wizard_stage_4(SimpleNamespace(install_systemd=True), state)

    assert not (wizard.WIZARD_UNIT_DIR / "whisper-server.service.d").exists()
    assert wizard._wizard_effective_model() == "large-v3-turbo"


def test_summary_reports_effective_model_not_unapplied_choice(capsys):
    wizard._wizard_stage_7({"model_id": "tiny.en"}, "")
    out = capsys.readouterr().out
    assert "Model:        large-v3-turbo" in out
    assert "chosen tiny.en was not applied" in out


def test_stage_1_rejects_unknown_model_names(monkeypatch):
    answers = iter(["../../etc/x\nEnvironment=EVIL=1", "tiny.en", ""])
    monkeypatch.setattr(wizard, "_wizard_prompt", lambda *_a, **_kw: next(answers))
    monkeypatch.setattr(wizard, "_wizard_detect_gpu_vram_mb", lambda: ("cpu", 0))
    monkeypatch.setattr(wizard, "_wizard_list_audio_sources", lambda: [])
    monkeypatch.setattr(wizard.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert wizard._wizard_stage_1(SimpleNamespace())["model_id"] == "tiny.en"


def test_config_default_model_follows_the_dropin_variable(monkeypatch):
    monkeypatch.setenv("DICTATION_WHISPER_MODEL", "tiny.en")
    for name in [m for m in sys.modules if m.split(".", 1)[0] == "calliope_server"]:
        monkeypatch.delitem(sys.modules, name)
    config = importlib.import_module("calliope_server.config")
    assert config.DEFAULT_MODEL == "tiny.en"


# ─── SC-05: --force against a running service ─────────────────────────

def _force_args(**kw):
    base = dict(force=True, install_systemd=True,
                skip_stage_1=True, skip_stage_2=True, skip_stage_3=False,
                skip_stage_4=False, skip_stage_5=False, skip_stage_6=False,
                skip_stage_7=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _deps(token_file=None):
    def rotate():
        if token_file is not None:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text("new")
        return "new"
    return SimpleNamespace(
        ensure_token=rotate, regenerate_token=rotate,
        get_token=lambda: "new", discover_local_ips=lambda: ["127.0.0.1"],
        generate_self_signed_cert=lambda **_kw: None,
        cert_days_remaining=lambda: 90, cert_fingerprint_sha256=lambda: "",
    )


def _no_stage_5_or_6(monkeypatch):
    def fail(*_a, **_kw):
        raise AssertionError("must not run against a server holding old credentials")
    monkeypatch.setattr(wizard, "_wizard_stage_5", fail)
    monkeypatch.setattr(wizard, "_wizard_mint_pairing_code", fail)


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(wizard.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(wizard.config, "TOKEN_FILE", tmp_path / "data" / "token")
    monkeypatch.setattr(wizard, "_wizard_lan_ip", lambda: "127.0.0.1")
    return tmp_path / "data"


def _assert_restart_pending(code, out, calls):
    assert code == 1
    assert "You're set" not in out
    assert "systemctl --user restart dictation-server" in out
    assert "dictation-server enabled + started" not in out
    verbs = {c[2] for c in calls if c[:2] == ["systemctl", "--user"]}
    assert verbs <= {"is-active", "show", "daemon-reload", "enable"}


def test_force_with_active_service_reports_pending_restart_and_skips_pairing(
        monkeypatch, data_dir, capsys):
    calls = _stub_systemctl(monkeypatch, active={"dictation-server": -3600})
    _no_stage_5_or_6(monkeypatch)

    code = wizard.run_setup_wizard(_force_args(), _deps(wizard.config.TOKEN_FILE))

    out = capsys.readouterr().out
    _assert_restart_pending(code, out, calls)
    assert "bearer token" in out


def test_rerun_before_restart_still_reports_pending(monkeypatch, data_dir, capsys):
    _stub_systemctl(monkeypatch, active={"dictation-server": -3600})
    _no_stage_5_or_6(monkeypatch)
    wizard.run_setup_wizard(_force_args(), _deps(wizard.config.TOKEN_FILE))
    capsys.readouterr()

    calls = _stub_systemctl(monkeypatch, active={"dictation-server": -3600})
    code = wizard.run_setup_wizard(_force_args(force=False), _deps())

    _assert_restart_pending(code, capsys.readouterr().out, calls)


def test_service_restarted_after_changes_is_not_pending(monkeypatch, data_dir, capsys):
    _stub_systemctl(monkeypatch, active={"dictation-server": -3600})
    _no_stage_5_or_6(monkeypatch)
    wizard.run_setup_wizard(_force_args(), _deps(wizard.config.TOKEN_FILE))
    capsys.readouterr()

    _stub_systemctl(monkeypatch, active={"dictation-server": 60})
    code = wizard.run_setup_wizard(
        _force_args(force=False, skip_stage_5=True, skip_stage_6=True), _deps())

    out = capsys.readouterr().out
    assert code == 0
    assert "You're set" in out
    assert "restart pending" not in out


def test_force_with_inactive_service_completes(monkeypatch, data_dir, capsys):
    _stub_systemctl(monkeypatch)

    code = wizard.run_setup_wizard(
        _force_args(skip_stage_5=True, skip_stage_6=True), _deps(wizard.config.TOKEN_FILE))

    out = capsys.readouterr().out
    assert code == 0
    assert "You're set" in out
    assert "restart pending" not in out
