"""First-run setup wizard (MVP-19) — `dictation-server --setup`.

Extracted from the calliope-server script. Helpers that still live in the
script (token + cert management) are injected via `WizardDeps` so the
script's module-level monkeypatch points keep working; mutable config is
read through `calliope_server.config` at call time.
"""

import difflib
import json
import logging
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, NamedTuple

from . import config

log = logging.getLogger("dictation-server")


class WizardDeps(NamedTuple):
    """Callables the wizard borrows from the composition root."""

    ensure_token: Callable[..., str]
    regenerate_token: Callable[..., str]
    get_token: Callable[[], str]
    discover_local_ips: Callable[[], list]
    generate_self_signed_cert: Callable[..., None]
    cert_days_remaining: Callable[[], "int | None"]
    cert_fingerprint_sha256: Callable[[], "str | None"]


# ─── MVP-19: First-run wizard ─────────────────────────────
# Per Agent 7 §6 + roadmap MVP-19. Idempotent: skip stages whose artifacts
# already exist; honour `--force` to redo. Each stage is gated behind a
# `--skip-stage-N` opt-out for re-runs. Network egress (Stage 2) flows
# through the audited urlopen → /audit/network ring buffer records it.

WIZARD_MODELS = [
    # (id, vram_min_mb, file_size_mb, label)
    # vram_min_mb=0 sentinels CPU fallback.
    ("large-v3-turbo", 8000, 1624, "fastest GPU path, default"),
    ("distil-large-v3", 4000, 756, "good quality, ~700MB"),
    ("base.en", 2000, 142, "basic, ~150MB"),
    ("tiny.en",       0, 75,  "CPU fallback, ~75MB"),
]
WIZARD_MODEL_BASE_URL = "https://huggingface.co/ggml-org/whisper.cpp/resolve/main"
WIZARD_HEALTH_TIMEOUT_S = 30
WIZARD_PAIR_TIMEOUT_S = 30
WIZARD_GROUND_TRUTH = "the quick brown fox jumps over the lazy dog"

WIZARD_DICTATION_UNIT_PATH = Path.home() / ".config/systemd/user/dictation-server.service"
WIZARD_WHISPER_UNIT_PATH = Path.home() / ".config/systemd/user/whisper-server.service"

# Inline templates — used when the dotfiles repo unit isn't reachable.
# Mirror systemd/user/{dictation-server,whisper-server}.service in this repo.
WIZARD_DICTATION_UNIT_TEMPLATE = """\
[Unit]
Description=Remote dictation server (whisper.cpp over HTTPS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/dictation-server
Restart=on-failure
RestartSec=5
Environment=WHISPER_BIN=whisper-cli

# Sandboxing per ADR-14 (Calliope roadmap, Phase 1).
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/dictation-server %h/.local/share/whisper /tmp
PrivateTmp=yes
PrivateDevices=no
DeviceAllow=/dev/kfd rw
DeviceAllow=/dev/dri rw
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
MemoryDenyWriteExecute=no
RestrictNamespaces=yes
RestrictRealtime=yes
SystemCallArchitectures=native
LogLevelMax=warning

[Install]
WantedBy=default.target
"""

WIZARD_WHISPER_UNIT_TEMPLATE = """\
[Unit]
Description=Whisper.cpp HTTP server for Calliope dictation
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=%h/.local/bin/whisper-server \\
    --host 127.0.0.1 \\
    --port 9001 \\
    --model %h/.local/share/whisper/ggml-large-v3-turbo.bin \\
    --threads 8 \\
    --processors 1 \\
    --no-timestamps \\
    --suppress-nst \\
    --no-speech-thold 0.80 \\
    --logprob-thold -0.7 \\
    --entropy-thold 2.4 \\
    --temperature 0.0 \\
    --beam-size 5 \\
    --best-of 5 \\
    --flash-attn
Restart=on-failure
RestartSec=5

NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=%h/.local/share/whisper /tmp
PrivateTmp=yes
PrivateDevices=no
DeviceAllow=/dev/kfd rw
DeviceAllow=/dev/dri rw
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
MemoryDenyWriteExecute=no
RestrictNamespaces=yes
RestrictRealtime=yes
SystemCallArchitectures=native
LogLevelMax=warning

[Install]
WantedBy=default.target
"""


def _wizard_say(stage: int, label: str, msg: str = "") -> None:
    """Stage-tagged stdout. Stays under 80 cols for terminal sanity."""
    head = f"[stage {stage}] {label}"
    if msg:
        print(f"{head}: {msg}")
    else:
        print(head)


def _wizard_warn(msg: str) -> None:
    print(f"  ! {msg}")


def _wizard_ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _wizard_skip(stage: int, reason: str) -> None:
    print(f"[stage {stage}] SKIP — {reason}")


def _wizard_prompt(prompt: str, default: str = "") -> str:
    """input() wrapper that shows the default in brackets and returns it on empty."""
    suffix = f" [{default}]" if default else ""
    try:
        resp = input(f"{prompt}{suffix} ").strip()
    except EOFError:
        return default
    return resp or default


def _wizard_confirm(prompt: str, default_yes: bool = False) -> bool:
    yn = "[Y/n]" if default_yes else "[y/N]"
    try:
        resp = input(f"{prompt} {yn} ").strip().lower()
    except EOFError:
        return default_yes
    if not resp:
        return default_yes
    return resp.startswith("y")


# ----- Stage 1: probe environment ------------------------------------
def _wizard_detect_gpu_vram_mb() -> tuple[str, int]:
    """Return ("rocm"|"nvidia"|"cpu", vram_mb). vram_mb == 0 for CPU."""
    # ROCm
    if shutil.which("rocm-smi"):
        try:
            res = subprocess.run(
                ["rocm-smi", "--showmeminfo", "vram", "--json"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                # Format: {"card0": {"VRAM Total Memory (B)": "8589934592", ...}}
                best_mb = 0
                for card_info in data.values():
                    if not isinstance(card_info, dict):
                        continue
                    for k, v in card_info.items():
                        if "VRAM Total Memory" in k and "(B)" in k:
                            try:
                                best_mb = max(best_mb, int(int(v) / (1024 * 1024)))
                            except (ValueError, TypeError):
                                pass
                if best_mb > 0:
                    return ("rocm", best_mb)
        except Exception as e:
            log.debug(f"rocm-smi probe failed: {e}")
    # NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
                if lines:
                    try:
                        return ("nvidia", int(lines[0]))
                    except ValueError:
                        pass
        except Exception as e:
            log.debug(f"nvidia-smi probe failed: {e}")
    return ("cpu", 0)


def _wizard_recommend_model(vram_mb: int) -> tuple[str, str]:
    """Pick the largest model that fits. Returns (model_id, label)."""
    for model_id, vram_min, _size, label in WIZARD_MODELS:
        if vram_mb >= vram_min:
            return (model_id, label)
    return (WIZARD_MODELS[-1][0], WIZARD_MODELS[-1][3])


def _wizard_list_audio_sources() -> list[str]:
    """Best-effort list of PipeWire/PulseAudio capture sources."""
    sources: list[str] = []
    if shutil.which("pactl"):
        try:
            res = subprocess.run(
                ["pactl", "list", "short", "sources"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0:
                for ln in res.stdout.splitlines():
                    parts = ln.split("\t")
                    if len(parts) >= 2:
                        sources.append(parts[1])
        except Exception as e:
            log.debug(f"pactl probe failed: {e}")
    return sources


def _wizard_stage_1(args) -> dict:
    """Probe env. Return picked {model_id, audio_source, gpu_kind, vram_mb}."""
    _wizard_say(1, "probe environment")

    gpu_kind, vram_mb = _wizard_detect_gpu_vram_mb()
    if gpu_kind == "cpu":
        _wizard_warn("No GPU detected — CPU-only mode. Latency will be poor.")
    else:
        _wizard_ok(f"GPU: {gpu_kind} with ~{vram_mb} MB VRAM")

    rec_model, rec_label = _wizard_recommend_model(vram_mb)
    if vram_mb < 2000 and gpu_kind != "cpu":
        _wizard_warn(f"VRAM <2GB — falling back to {rec_model}; CPU mode is "
                     "your real-world floor.")
    print("  Available models:")
    for mid, vmin, size, label in WIZARD_MODELS:
        marker = " ← recommended" if mid == rec_model else ""
        print(f"    {mid:20s}  {size} MB  ({label}){marker}")
    chosen_model = _wizard_prompt(
        "  Which model to use?", default=rec_model,
    )

    devices = []
    if shutil.which("ffmpeg"):
        _wizard_ok("ffmpeg present")
    else:
        _wizard_warn("ffmpeg MISSING — required for audio conversion. Install before continuing.")
        raise SystemExit(2)
    for tool in ("wtype", "wl-copy", "pw-record", "pactl"):
        if shutil.which(tool):
            _wizard_ok(f"{tool} present")
        else:
            _wizard_warn(f"{tool} not on PATH (warning only — degraded function)")

    devices = _wizard_list_audio_sources()
    if devices:
        print("  Audio capture sources:")
        for d in devices:
            print(f"    {d}")
        audio_source = _wizard_prompt(
            "  Default record source (Enter = system default)",
            default="(system default)",
        )
        if audio_source == "(system default)":
            audio_source = ""
    else:
        _wizard_warn("Could not enumerate audio sources (pactl missing or no PipeWire). "
                     "Will use system default at runtime.")
        audio_source = ""

    return {
        "model_id": chosen_model,
        "audio_source": audio_source,
        "gpu_kind": gpu_kind,
        "vram_mb": vram_mb,
    }


# ----- Stage 2: pull model -------------------------------------------
def _wizard_stage_2(args, state: dict) -> None:
    """Download whisper model. No-op if already cached at expected size."""
    _wizard_say(2, "pull model")
    model = state["model_id"]
    target = config.MODEL_DIR / f"ggml-{model}.bin"

    expected_mb = next(
        (size for mid, _, size, _ in WIZARD_MODELS if mid == model), 0
    )

    if target.exists() and not args.force:
        actual_mb = target.stat().st_size // (1024 * 1024)
        # Tolerate ±10% — HF mirror sizes drift slightly on re-encodes.
        if expected_mb == 0 or abs(actual_mb - expected_mb) <= max(20, expected_mb // 10):
            _wizard_skip(2, f"{target} already present ({actual_mb} MB)")
            return
        _wizard_warn(f"{target} exists but size {actual_mb} MB differs from "
                     f"expected ~{expected_mb} MB; re-fetching")

    url = f"{WIZARD_MODEL_BASE_URL}/ggml-{model}.bin"
    print(f"  URL:   {url}")
    print(f"  Size:  ~{expected_mb} MB")
    print(f"  Dest:  {target}")
    if not _wizard_confirm("  Download now?", default_yes=True):
        _wizard_skip(2, "user declined download")
        return

    config.MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        # Audited urlopen records this in /audit/network. Acceptable: every
        # outbound call should be auditable, model fetch included.
        req = urllib.request.Request(url, headers={"User-Agent": "calliope-setup/1"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec - HTTPS to known host
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            chunk = 1024 * 256
            with tmp.open("wb") as out:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    out.write(buf)
                    got += len(buf)
                    if total:
                        pct = (got * 100) // total
                        print(f"  ...{pct:3d}%  ({got // (1024 * 1024)} / {total // (1024 * 1024)} MB)",
                              end="\r", flush=True)
            print()  # newline after progress
        tmp.rename(target)
        actual_mb = target.stat().st_size // (1024 * 1024)
        _wizard_ok(f"saved {target} ({actual_mb} MB)")
        if expected_mb and abs(actual_mb - expected_mb) > max(20, expected_mb // 10):
            _wizard_warn(f"size {actual_mb} MB differs notably from expected ~{expected_mb} MB; "
                         "verify upstream mirror integrity")
    except Exception as e:
        _wizard_warn(f"download failed: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise SystemExit(2)


# ----- Stage 3: cert + token -----------------------------------------
def _wizard_stage_3(args, deps: WizardDeps) -> str:
    """Mint cert + token. Returns the cert SHA-256 fingerprint."""
    _wizard_say(3, "generate cert + token")
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)

    # --- Token ---
    if args.force or not config.TOKEN_FILE.exists():
        deps.regenerate_token() if args.force else deps.ensure_token()
        if args.force:
            _wizard_ok("rotated bearer token (--force)")
        else:
            _wizard_ok("minted bearer token")
    else:
        deps.ensure_token()
        _wizard_skip(3, "token already exists (use --force to rotate)")
    # Save reference so other stages don't re-prompt.
    print(f"  Token saved at: {config.TOKEN_FILE}  (mode 0600)")

    # --- Cert ---
    sans = deps.discover_local_ips()
    import socket
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""
    if hostname and hostname not in sans:
        sans.append(hostname)

    needs_regen = args.force or not (config.CERT_FILE.exists() and config.KEY_FILE.exists())
    if needs_regen:
        deps.generate_self_signed_cert(sans=sans)
        _wizard_ok(f"cert regenerated with SANs: {', '.join(sans)}")
    else:
        days = deps.cert_days_remaining()
        _wizard_skip(3, f"cert already exists ({days} days remaining; --force to rotate)")

    fp = deps.cert_fingerprint_sha256() or ""
    if fp:
        try:
            config.CERT_FINGERPRINT_FILE.write_text(fp + "\n", encoding="utf-8")
            os.chmod(config.CERT_FINGERPRINT_FILE, 0o644)
        except Exception as e:
            _wizard_warn(f"could not persist fingerprint file: {e}")
        print(f"  Cert fingerprint (SHA-256): {fp}")
    else:
        _wizard_warn("could not compute cert fingerprint")

    # --- Optional mkcert ---
    if shutil.which("mkcert"):
        if _wizard_confirm("  Install local CA via mkcert (replaces self-signed)?", default_yes=False):
            try:
                subprocess.run(["mkcert", "-install"], check=True)
                ip_args = [s for s in sans]
                cmd = [
                    "mkcert",
                    "-cert-file", str(config.CERT_FILE),
                    "-key-file", str(config.KEY_FILE),
                    *ip_args,
                ]
                subprocess.run(cmd, check=True)
                os.chmod(config.CERT_FILE, 0o644)
                os.chmod(config.KEY_FILE, 0o600)
                _wizard_ok("mkcert-issued cert installed; CA trusted in user store")
                fp = deps.cert_fingerprint_sha256() or fp
                if fp:
                    config.CERT_FINGERPRINT_FILE.write_text(fp + "\n", encoding="utf-8")
                    print(f"  New fingerprint: {fp}")
            except subprocess.CalledProcessError as e:
                _wizard_warn(f"mkcert failed: {e}")

    return fp


# ----- Stage 4: systemd units ----------------------------------------
def _wizard_dotfiles_unit(name: str) -> Path | None:
    """Locate the canonical unit file in this repo, if reachable."""
    candidates = [Path.home() / "Github/dotfiles/systemd/user" / name]
    # Repo-relative candidate only when actually running from a checkout —
    # in the installed layout parents[2] is ~/.local/share, and
    # ~/.local/share/systemd/user is a real systemd load path whose stale
    # units must not masquerade as repo-canonical.
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "server" / "calliope-server").is_file():
        candidates.append(repo_root / "systemd/user" / name)
    for c in candidates:
        if c.is_file():
            return c
    return None


def _wizard_install_unit(name: str, fallback_template: str) -> bool:
    """Drop a unit file into ~/.config/systemd/user/. Return True if a write happened."""
    dst = Path.home() / ".config/systemd/user" / name
    dst.parent.mkdir(parents=True, exist_ok=True)

    src = _wizard_dotfiles_unit(name)
    new_content = src.read_text(encoding="utf-8") if src else fallback_template

    if dst.exists():
        if dst.read_text(encoding="utf-8") == new_content:
            _wizard_skip(4, f"{dst.name} already up-to-date")
            return False

    dst.write_text(new_content, encoding="utf-8")
    _wizard_ok(f"wrote {dst} ({'from repo' if src else 'inline template'})")
    return True


def _wizard_wait_health(url: str, timeout_s: int) -> bool:
    """Poll URL until 200 or timeout. Self-signed → custom unverified ctx."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception as e:
            last_err = e
        time.sleep(1.0)
    if last_err:
        _wizard_warn(f"last health probe error: {last_err}")
    return False


def _wizard_stage_4(args) -> None:
    _wizard_say(4, "systemd unit")
    if not args.install_systemd:
        _wizard_warn("--no-install-systemd — printing intent only")
        print(f"  WOULD WRITE: {WIZARD_DICTATION_UNIT_PATH}")
        print(f"  WOULD WRITE: {WIZARD_WHISPER_UNIT_PATH}")
        print("  WOULD RUN:   systemctl --user daemon-reload && enable --now "
              "dictation-server")
        print("  ASSUMING:    server is already reachable on https://127.0.0.1:8384/health")
        return

    if not shutil.which("systemctl"):
        _wizard_warn("systemctl not on PATH — skipping unit install")
        return

    _wizard_install_unit("dictation-server.service", WIZARD_DICTATION_UNIT_TEMPLATE)
    _wizard_install_unit("whisper-server.service", WIZARD_WHISPER_UNIT_TEMPLATE)

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        _wizard_warn(f"daemon-reload failed: {e.stderr.strip()}")
        raise SystemExit(2)

    try:
        subprocess.run(["systemctl", "--user", "enable", "--now",
                        "dictation-server"],
                       check=True, capture_output=True, text=True)
        _wizard_ok("dictation-server enabled + started")
    except subprocess.CalledProcessError as e:
        _wizard_warn(f"enable --now failed: {e.stderr.strip()}")
        raise SystemExit(2)

    health_url = f"https://127.0.0.1:{config.DEFAULT_PORT}/health"
    print(f"  waiting for {health_url} (timeout {WIZARD_HEALTH_TIMEOUT_S}s)...")
    if _wizard_wait_health(health_url, WIZARD_HEALTH_TIMEOUT_S):
        _wizard_ok("server healthy")
    else:
        _wizard_warn("server did not become healthy in time — "
                     "check `systemctl --user status dictation-server`")
        raise SystemExit(2)


# ----- Stage 5: self-test --------------------------------------------
def _wizard_multipart_post(url: str, audio_path: Path, token: str,
                           extra_fields: dict | None = None) -> tuple[int, dict]:
    """Stdlib multipart/form-data POST. Returns (status, parsed_json_or_empty)."""
    boundary = "----calliope-setup-" + secrets.token_hex(8)
    audio_bytes = audio_path.read_bytes()
    parts = []
    for k, v in (extra_fields or {}).items():
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{k}\"\r\n\r\n"
            f"{v}\r\n".encode()
        )
    parts.append(
        (f"--{boundary}\r\n"
         f"Content-Disposition: form-data; name=\"audio\"; "
         f"filename=\"{audio_path.name}\"\r\n"
         f"Content-Type: audio/wav\r\n\r\n").encode()
    )
    parts.append(audio_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
        status = resp.status
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return status, json.loads(raw)
        except Exception:
            return status, {"raw": raw}


def _wizard_stage_5(args, deps: WizardDeps) -> None:
    _wizard_say(5, "self-test (10 seconds)")

    if not shutil.which("pw-record"):
        _wizard_warn("pw-record not on PATH — skipping self-test")
        return

    print("  Read this phrase aloud after the prompt (5s):")
    print(f'    "{WIZARD_GROUND_TRUTH}"')
    if not _wizard_confirm("  Ready?", default_yes=True):
        _wizard_skip(5, "user declined")
        return

    wav = Path(tempfile.gettempdir()) / "calliope-selftest.wav"
    try:
        wav.unlink(missing_ok=True)
    except Exception:
        pass

    print("  recording...")
    t_cap_0 = time.monotonic()
    try:
        subprocess.run(
            ["pw-record", "--channels=1", "--rate=16000", "--format=s16",
             str(wav)],
            timeout=6, check=False,
        )
    except subprocess.TimeoutExpired:
        pass  # we want pw-record to be killed at ~5s
    except Exception as e:
        _wizard_warn(f"pw-record failed: {e}")
        return
    t_cap = time.monotonic() - t_cap_0

    if not wav.exists() or wav.stat().st_size < 1024:
        _wizard_warn("recording empty or missing — check microphone permissions")
        return

    token = deps.get_token()
    url = f"https://127.0.0.1:{config.DEFAULT_PORT}/transcribe"
    print("  transcribing...")
    t_xc_0 = time.monotonic()
    try:
        status, body = _wizard_multipart_post(url, wav, token)
    except Exception as e:
        _wizard_warn(f"POST /transcribe failed: {e}")
        print("  diagnostics:")
        print("    - whisper-server running?  systemctl --user status whisper-server")
        print("    - dictation-server up?      systemctl --user status dictation-server")
        print("    - GPU detected?             rocm-smi / nvidia-smi")
        raise SystemExit(2)
    t_xc = time.monotonic() - t_xc_0
    if status != 200:
        _wizard_warn(f"server returned {status}: {body}")
        raise SystemExit(2)

    text = (body.get("text") or body.get("raw") or "").strip().lower()
    if not text:
        _wizard_warn("server returned empty transcription")
        raise SystemExit(2)

    ratio = difflib.SequenceMatcher(None, text, WIZARD_GROUND_TRUTH).ratio()
    print(f"  heard:  {text!r}")
    print(f"  truth:  {WIZARD_GROUND_TRUTH!r}")
    print(f"  ratio:  {ratio:.3f}  (pass ≥ 0.85)")
    print(f"  timing: capture {t_cap*1000:.0f}ms · transcribe {t_xc*1000:.0f}ms")

    if ratio < 0.85:
        _wizard_warn("transcription quality below threshold")
        print("  diagnostics:")
        print("    - mic level too low? GPU stalling? Re-record + retry.")
        print("    - tail logs: journalctl --user -u dictation-server -f | grep timing")
        # Don't bail — caller may want to push through. Stage 6 still useful.
    else:
        _wizard_ok("self-test passed")

    try:
        wav.unlink(missing_ok=True)
    except Exception:
        pass


# ----- Stage 6: phone pairing ---------------------------------------
def _wizard_lan_ip() -> str:
    """Best-effort outbound-route detection of LAN IP (loopback fallback)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _wizard_wait_pair(timeout_s: int, deps: WizardDeps) -> bool:
    """Subscribe to /events SSE, wait for pair-success, ignore self-signed."""
    token = deps.get_token()
    url = f"https://127.0.0.1:{config.DEFAULT_PORT}/events?token={urllib.parse.quote(token)}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + timeout_s
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, context=ctx, timeout=timeout_s + 5) as resp:
            current_event = ""
            while time.monotonic() < deadline:
                line = resp.readline()
                if not line:
                    break
                s = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if s.startswith("event:"):
                    current_event = s[len("event:"):].strip()
                elif s.startswith("data:") and current_event == "pair-success":
                    return True
                elif s == "":
                    current_event = ""
    except Exception as e:
        log.debug(f"wait_pair: SSE error: {e}")
    return False


def _wizard_stage_6(args, fingerprint: str, deps: WizardDeps) -> None:
    _wizard_say(6, "phone pairing")
    token = deps.get_token()
    lan_ip = _wizard_lan_ip()
    base_url = f"https://{lan_ip}:{config.DEFAULT_PORT}"

    payload = {
        "url": base_url,
        "token": token,
        "fingerprint": fingerprint or "",
    }
    pair_url = (f"{base_url}/pair?token={urllib.parse.quote(token)}"
                f"&fingerprint={urllib.parse.quote(fingerprint or '')}")
    qr_payload = json.dumps(payload, separators=(",", ":"))

    if shutil.which("qrencode"):
        try:
            res = subprocess.run(
                ["qrencode", "-t", "UTF8", "-o", "-", qr_payload],
                check=True, capture_output=True, text=True, timeout=5,
            )
            print(res.stdout)
        except Exception as e:
            _wizard_warn(f"qrencode failed: {e}")
    else:
        _wizard_warn("install `qrencode` for phone QR pairing")

    print(f"  Phone URL:    {base_url}")
    print(f"  Token:        {token}")
    print(f"  Fingerprint:  {fingerprint or '(none)'}")
    print(f"  POST target:  {pair_url}")
    print(f"  Waiting for /pair hit (timeout {WIZARD_PAIR_TIMEOUT_S}s) — Ctrl-C to skip.")
    try:
        ok = _wizard_wait_pair(WIZARD_PAIR_TIMEOUT_S, deps)
    except KeyboardInterrupt:
        _wizard_warn("pairing skipped by user")
        return
    if ok:
        _wizard_ok("phone paired")
    else:
        _wizard_warn(f"no pair within {WIZARD_PAIR_TIMEOUT_S}s — re-run --setup --skip-stage-1 ... when ready")


# ----- Stage 7: summary ----------------------------------------------
def _wizard_stage_7(state: dict, fingerprint: str) -> None:
    _wizard_say(7, "summary")
    lan_ip = _wizard_lan_ip()
    print("  ✓ You're set.")
    print("    Hotkey:       Mod+Shift+M (configure in your WM)")
    print(f"    Phone URL:    https://{lan_ip}:{config.DEFAULT_PORT}")
    print(f"    Bearer token: {config.TOKEN_FILE}")
    print(f"    Cert FP:      {fingerprint or '(none)'}")
    print(f"    Model:        {state.get('model_id', '?')}")
    print()
    print("  Next steps:")
    print("    docs/cert-trust.md       — trusting the self-signed cert on phone")
    print("    docs/tailscale.md        — see `dictation-server --tailscale-cert` for LE certs")
    print("    journalctl --user -u dictation-server -f | grep timing  — latency profiling")


# ----- Driver --------------------------------------------------------
def run_setup_wizard(args, deps: WizardDeps) -> int:
    """Top-level wizard driver. Returns process exit code."""
    print("=" * 64)
    print("  Calliope dictation-server — first-run setup")
    print("=" * 64)

    state: dict = {"model_id": "large-v3-turbo"}
    fingerprint = ""

    if not args.skip_stage_1:
        state.update(_wizard_stage_1(args))
    else:
        _wizard_skip(1, "--skip-stage-1")

    if not args.skip_stage_2:
        _wizard_stage_2(args, state)
    else:
        _wizard_skip(2, "--skip-stage-2")

    if not args.skip_stage_3:
        fingerprint = _wizard_stage_3(args, deps)
    else:
        _wizard_skip(3, "--skip-stage-3")
        fingerprint = deps.cert_fingerprint_sha256() or ""

    if not args.skip_stage_4:
        _wizard_stage_4(args)
    else:
        _wizard_skip(4, "--skip-stage-4")

    if not args.skip_stage_5:
        _wizard_stage_5(args, deps)
    else:
        _wizard_skip(5, "--skip-stage-5")

    if not args.skip_stage_6:
        _wizard_stage_6(args, fingerprint, deps)
    else:
        _wizard_skip(6, "--skip-stage-6")

    if not args.skip_stage_7:
        _wizard_stage_7(state, fingerprint)
    return 0
