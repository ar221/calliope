"""First-run setup wizard (MVP-19) — `dictation-server --setup`.

Extracted from the calliope-server script. Helpers that still live in the
script (token + cert management) are injected via `WizardDeps` so the
script's module-level monkeypatch points keep working; mutable config is
read through `calliope_server.config` at call time.
"""

import difflib
import hashlib
import json
import logging
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
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
    # (id, vram_min_mb, file_size_mb, label, sha256)
    # vram_min_mb=0 sentinels CPU fallback. sha256=None means no pinned hash
    # is known for that file yet, so only the ±10% size check applies.
    ("large-v3-turbo", 8000, 1624, "fastest GPU path, default",
     "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"),
    ("distil-large-v3", 4000, 756, "good quality, ~700MB", None),
    ("base.en", 2000, 142, "basic, ~150MB", None),
    ("tiny.en",       0, 75,  "CPU fallback, ~75MB", None),
]
WIZARD_MODEL_BASE_URL = "https://huggingface.co/ggml-org/whisper.cpp/resolve/main"
WIZARD_HEALTH_TIMEOUT_S = 30
WIZARD_GROUND_TRUTH = "the quick brown fox jumps over the lazy dog"

WIZARD_UNIT_DIR = Path.home() / ".config/systemd/user"
# Units the wizard installs. Only dictation-server is enabled; whisper-server
# and kokoro-server have no [Install] section because dictation-server starts
# them on demand and stops them when idle.
WIZARD_UNITS = ("dictation-server.service", "whisper-server.service",
                "kokoro-server.service")


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
    for model_id, vram_min, _size, label, _sha in WIZARD_MODELS:
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
    for mid, vmin, size, label, _sha in WIZARD_MODELS:
        marker = " ← recommended" if mid == rec_model else ""
        print(f"    {mid:20s}  {size} MB  ({label}){marker}")
    known = [m[0] for m in WIZARD_MODELS]
    chosen_model = _wizard_prompt("  Which model to use?", default=rec_model)
    while chosen_model not in known:
        _wizard_warn(f"unknown model {chosen_model!r}; pick one of: {', '.join(known)}")
        chosen_model = _wizard_prompt("  Which model to use?", default=rec_model)

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
def _wizard_sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(1024 * 1024), b""):
            h.update(buf)
    return h.hexdigest()


def _wizard_stage_2(args, state: dict) -> None:
    """Download whisper model. No-op if already cached and verified."""
    _wizard_say(2, "pull model")
    model = state["model_id"]
    target = config.MODEL_DIR / f"ggml-{model}.bin"

    expected_mb, expected_sha = next(
        ((size, sha) for mid, _, size, _, sha in WIZARD_MODELS if mid == model),
        (0, None),
    )

    if target.exists() and not args.force:
        actual_mb = target.stat().st_size // (1024 * 1024)
        if expected_sha:
            if _wizard_sha256_file(target) == expected_sha:
                _wizard_skip(2, f"{target} already present (SHA-256 verified)")
                return
            _wizard_warn(f"{target} SHA-256 does not match the pinned hash; re-fetching")
        # No pinned hash: tolerate ±10% — HF mirror sizes drift slightly on re-encodes.
        elif expected_mb == 0 or abs(actual_mb - expected_mb) <= max(20, expected_mb // 10):
            _wizard_skip(2, f"{target} already present ({actual_mb} MB, size check only)")
            return
        else:
            _wizard_warn(f"{target} exists but size {actual_mb} MB differs from "
                         f"expected ~{expected_mb} MB; re-fetching")

    url = f"{WIZARD_MODEL_BASE_URL}/ggml-{model}.bin"
    print(f"  URL:   {url}")
    print(f"  Size:  ~{expected_mb} MB")
    print(f"  SHA-256: {expected_sha or '(not pinned; size check only)'}")
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
        digest = hashlib.sha256()
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
                    digest.update(buf)
                    got += len(buf)
                    if total:
                        pct = (got * 100) // total
                        print(f"  ...{pct:3d}%  ({got // (1024 * 1024)} / {total // (1024 * 1024)} MB)",
                              end="\r", flush=True)
            print()  # newline after progress
        if expected_sha and digest.hexdigest() != expected_sha:
            raise ValueError(f"SHA-256 mismatch: got {digest.hexdigest()}, "
                             f"expected {expected_sha}")
        tmp.rename(target)
        actual_mb = target.stat().st_size // (1024 * 1024)
        if expected_sha:
            _wizard_ok(f"saved {target} ({actual_mb} MB, SHA-256 verified)")
        else:
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
def _wizard_repo_unit(name: str, package_dir: Path | None = None) -> Path | None:
    """Locate the repo's copy of a systemd unit, if reachable.

    Checkout layout: `<repo>/server/calliope_server/` → `<repo>/systemd/<name>`.
    Installed layout: scripts/install-server stages the repo units next to the
    package as `<DATADIR>/units/<name>` (not a systemd load path).
    """
    pkg = (package_dir or Path(__file__).resolve().parent)
    repo_root = pkg.parents[1]
    if (repo_root / "server" / "calliope-server").is_file():
        candidate = repo_root / "systemd" / name
    else:
        candidate = pkg.parent / "units" / name
    return candidate if candidate.is_file() else None


def _wizard_install_unit(src: Path) -> bool:
    """Copy a repo unit into ~/.config/systemd/user/. Return True if a write happened."""
    dst = WIZARD_UNIT_DIR / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    new_content = src.read_text(encoding="utf-8")

    if dst.exists() and dst.read_text(encoding="utf-8") == new_content:
        _wizard_skip(4, f"{dst.name} already up-to-date")
        return False

    dst.write_text(new_content, encoding="utf-8")
    _wizard_ok(f"wrote {dst} (from {src})")
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


WIZARD_MODEL_DROPIN = "calliope-model.conf"
# Units that read DICTATION_WHISPER_MODEL: whisper-server loads that model file,
# dictation-server uses it as config.DEFAULT_MODEL (CLI fallback + mode default).
WIZARD_MODEL_UNITS = ("whisper-server.service", "dictation-server.service")


def _wizard_service_active(unit: str) -> bool:
    """True when the user unit is currently active. Never starts/stops anything."""
    if not shutil.which("systemctl"):
        return False
    try:
        res = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit],
                             check=False, capture_output=True, text=True)
    except Exception:
        return False
    return res.returncode == 0


def _wizard_model_dropin(unit: str) -> Path:
    return WIZARD_UNIT_DIR / f"{unit}.d" / WIZARD_MODEL_DROPIN


def _wizard_write_model_dropins(model: str) -> None:
    """Pin DICTATION_WHISPER_MODEL for both units (rewrite only on change)."""
    content = (
        "# Written by `dictation-server --setup` (stage 1 model choice).\n"
        "[Service]\n"
        f"Environment=DICTATION_WHISPER_MODEL={model}\n"
    )
    for unit in WIZARD_MODEL_UNITS:
        dst = _wizard_model_dropin(unit)
        if dst.is_file() and dst.read_text(encoding="utf-8") == content:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        _wizard_ok(f"wrote {dst} (model {model})")


def _wizard_effective_model() -> str:
    """Model the installed services will load: wizard drop-in, else the default."""
    prefix = "Environment=DICTATION_WHISPER_MODEL="
    try:
        lines = _wizard_model_dropin("whisper-server.service").read_text(
            encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if line.startswith(prefix) and line[len(prefix):].strip():
            return line[len(prefix):].strip()
    return config.DEFAULT_MODEL


def _wizard_unit_started_at(unit: str) -> float | None:
    """Wall-clock time an active user unit entered `active`; None if inactive/unknown."""
    if not shutil.which("systemctl"):
        return None
    try:
        res = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic"],
            check=False, capture_output=True, text=True)
    except Exception:
        return None
    props = dict(line.split("=", 1) for line in (res.stdout or "").splitlines() if "=" in line)
    if props.get("ActiveState") != "active":
        return None
    try:
        mono_us = int(props.get("ActiveEnterTimestampMonotonic", "0"))
    except ValueError:
        return None
    if mono_us <= 0:
        return None
    return time.time() - (time.clock_gettime(time.CLOCK_MONOTONIC) - mono_us / 1e6)


def _wizard_pending_restarts() -> tuple[list[str], list[str]]:
    """Units whose running process predates their unit/drop-ins or credentials.

    Derived from live state (start time vs file mtimes), so a re-run before
    the operator restarts still reports the restart as pending.
    """
    watched: dict[str, list[tuple[Path, str]]] = {}
    for unit in ("dictation-server", "whisper-server"):
        unit_file = WIZARD_UNIT_DIR / f"{unit}.service"
        paths = [(unit_file, "unit file")]
        paths += [(p, f"drop-in {p.name}")
                  for p in sorted((WIZARD_UNIT_DIR / f"{unit}.service.d").glob("*.conf"))]
        watched[unit] = paths
    watched["dictation-server"].append((config.TOKEN_FILE, "bearer token / TLS cert"))

    units: list[str] = []
    reasons: list[str] = []
    for unit, paths in watched.items():
        started = _wizard_unit_started_at(unit)
        if started is None:
            continue
        changed = []
        for path, label in paths:
            try:
                if path.stat().st_mtime > started:
                    changed.append(label)
            except OSError:
                continue
        if changed:
            units.append(unit)
            reasons.append(f"{unit}: {', '.join(changed)} changed since it started")
    return units, reasons


def _wizard_mode_model_pins(model: str) -> list[tuple[str, str]]:
    """Saved modes whose explicit `whisper_model` differs from `model`."""
    if not config.MODES_FILE.is_file():
        return []
    from . import formatter  # local: the wizard otherwise has no formatter dependency
    try:
        parsed = formatter._parse_config(config.MODES_FILE.read_bytes())
    except Exception:
        return []
    pins = []
    for mode in parsed if isinstance(parsed, list) else []:
        if not isinstance(mode, dict):
            continue
        mode_id = str(mode.get("id", "?"))
        pinned = str(mode.get("whisper_model") or "").strip()
        # Same rule as formatter._normalize_mode: "" and the legacy seeded
        # value on a built-in mode inherit DICTATION_WHISPER_MODEL.
        if not pinned or (mode_id in formatter._BUILTIN_MODE_IDS
                          and pinned == formatter._LEGACY_SEEDED_WHISPER_MODEL):
            continue
        if pinned != model:
            pins.append((mode_id, pinned))
    return pins


def _wizard_stage_4(args, state: dict) -> None:
    """Install units + model drop-ins, enable dictation-server. Never restarts."""
    _wizard_say(4, "systemd unit")
    model = state.get("model_id", "")
    if not args.install_systemd:
        _wizard_warn("--no-install-systemd — printing intent only")
        for name in WIZARD_UNITS:
            print(f"  WOULD WRITE: {WIZARD_UNIT_DIR / name}")
        for name in WIZARD_MODEL_UNITS:
            print(f"  WOULD WRITE: {_wizard_model_dropin(name)}  "
                  f"(DICTATION_WHISPER_MODEL={model})")
        print("  WOULD RUN:   systemctl --user daemon-reload && enable --now "
              "dictation-server")
        print("  ASSUMING:    server is already reachable on https://127.0.0.1:8384/health")
        return

    if not shutil.which("systemctl"):
        _wizard_warn("systemctl not on PATH — skipping unit install")
        return

    sources = {name: _wizard_repo_unit(name) for name in WIZARD_UNITS}
    missing = [name for name, src in sources.items() if src is None]
    if missing:
        _wizard_warn(f"repo unit files not found: {', '.join(missing)}")
        print("  The calliope checkout is not reachable from this install. Re-run")
        print("  scripts/install-server from the checkout (it stages the units),")
        print("  then run `dictation-server --setup` again.")
        raise SystemExit(2)

    for src in sources.values():
        _wizard_install_unit(src)

    if model in {m[0] for m in WIZARD_MODELS} and (
            config.MODEL_DIR / f"ggml-{model}.bin").is_file():
        _wizard_write_model_dropins(model)
        state["model_applied"] = True
    else:
        _wizard_warn(f"model {model!r} is not downloaded — services keep "
                     f"{_wizard_effective_model()}")

    was_active = _wizard_service_active("dictation-server")
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
    except subprocess.CalledProcessError as e:
        _wizard_warn(f"enable --now failed: {e.stderr.strip()}")
        raise SystemExit(2)
    if was_active:
        _wizard_ok("dictation-server enabled (already running — not restarted)")
    else:
        _wizard_ok("dictation-server enabled + started")

    health_url = f"https://127.0.0.1:{config.DEFAULT_PORT}/health"
    print(f"  waiting for {health_url} (timeout {WIZARD_HEALTH_TIMEOUT_S}s)...")
    if _wizard_wait_health(health_url, WIZARD_HEALTH_TIMEOUT_S):
        _wizard_ok("server healthy")
    else:
        _wizard_warn("server did not become healthy in time — "
                     "check `systemctl --user status dictation-server`")
        raise SystemExit(2)


def _wizard_restart_pending(state: dict) -> bool:
    return bool(state.get("restart_units"))


def _wizard_print_restart(state: dict) -> None:
    units = state.get("restart_units") or []
    _wizard_warn(f"restart pending — the running {' and '.join(units)} "
                 "still use the previous settings:")
    for reason in state.get("restart_reasons") or []:
        print(f"    - {reason}")
    print("  The wizard never restarts a running service. When convenient run:")
    print(f"    systemctl --user restart {' '.join(units)}")
    print("  then self-test and pair with:")
    print("    dictation-server --setup --skip-stage-1 --skip-stage-2 "
          "--skip-stage-3 --skip-stage-4")


# ----- Stage 5: self-test --------------------------------------------
def _wizard_post_wav(url: str, audio_path: Path, token: str) -> tuple[int, dict]:
    """POST the recorded WAV as the raw request body, exactly like the clients.

    Returns (status, parsed_json_or_raw); HTTP error statuses are returned,
    not raised.
    """
    req = urllib.request.Request(
        url, data=audio_path.read_bytes(), method="POST",
        headers={
            "Content-Type": "audio/wav",
            "Authorization": f"Bearer {token}",
        },
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace")
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

    # Private (0700) per-run directory: the recording is voice data and must
    # not outlive this stage on any path.
    tmp_dir = Path(tempfile.mkdtemp(prefix="calliope-selftest-"))
    try:
        _wizard_self_test(tmp_dir / "selftest.wav", deps)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _wizard_self_test(wav: Path, deps: WizardDeps) -> None:
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
    # Plain mode: Whisper only, no formatter provider sees the test phrase.
    url = f"https://127.0.0.1:{config.DEFAULT_PORT}/transcribe?mode=plain"
    print("  transcribing...")
    t_xc_0 = time.monotonic()
    try:
        status, body = _wizard_post_wav(url, wav, token)
    except Exception as e:
        _wizard_warn(f"POST /transcribe failed: {e}")
        print("  diagnostics:")
        print("    - whisper-server running?  systemctl --user status whisper-server")
        print("    - dictation-server up?      systemctl --user status dictation-server")
        print("    - GPU detected?             rocm-smi / nvidia-smi")
        raise SystemExit(2)
    t_xc = time.monotonic() - t_xc_0
    if status != 200:
        _wizard_warn(f"server returned {status}: {body.get('error') or body.get('raw', '')}")
        raise SystemExit(2)

    text = (body.get("text") or "").strip().lower()
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


def _wizard_mint_pairing_code(token: str) -> tuple[str, int]:
    """Ask the running server for a one-time phone pairing code.

    POST /pair/bootstrap requires the durable bearer in the Authorization
    header (even on loopback). Returns (code, expires_in_seconds).
    """
    url = f"https://127.0.0.1:{config.DEFAULT_PORT}/pair/bootstrap"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        url, data=b"", method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    code = str(body.get("code") or "")
    if not code:
        raise ValueError("server returned no pairing code")
    return code, int(body.get("expires_in") or 0)


def _wizard_stage_6(args, fingerprint: str, deps: WizardDeps) -> None:
    _wizard_say(6, "phone pairing")
    try:
        code, expires_in = _wizard_mint_pairing_code(deps.get_token())
    except Exception as e:
        _wizard_warn(f"could not mint a pairing code: {e}")
        print("  Pair later from the SillyTavern extension (Show local QR /")
        print("  Copy pairing URL) once dictation-server is running.")
        return

    base_url = f"https://{_wizard_lan_ip()}:{config.DEFAULT_PORT}"
    pair_url = f"{base_url}/?{urllib.parse.urlencode({'pair': code})}"

    if shutil.which("qrencode"):
        try:
            res = subprocess.run(
                ["qrencode", "-t", "UTF8", "-o", "-", pair_url],
                check=True, capture_output=True, text=True, timeout=5,
            )
            print(res.stdout)
        except Exception as e:
            _wizard_warn(f"qrencode failed: {e}")
    else:
        _wizard_warn("install `qrencode` to show this URL as a QR code")

    print(f"  Pairing URL:  {pair_url}")
    print(f"  Code:         {code}  (one use, expires in {expires_in}s)")
    print(f"  Fingerprint:  {fingerprint or '(none)'}")
    print("  Scan or open the URL on the phone and check the cert fingerprint")
    print("  matches. Phone needs LAN access (DICTATION_BIND_HOST=0.0.0.0) or")
    print("  Tailscale. Expired? Re-pair from the SillyTavern extension.")


# ----- Stage 7: summary ----------------------------------------------
def _wizard_stage_7(state: dict, fingerprint: str) -> None:
    _wizard_say(7, "summary")
    lan_ip = _wizard_lan_ip()
    effective = _wizard_effective_model()
    if _wizard_restart_pending(state):
        print("  ! Not finished — restart pending (see stage 4). Self-test and")
        print("    pairing were skipped because the running server still holds")
        print("    the previous settings.")
    else:
        print("  ✓ You're set.")
    print("    Hotkey:       Mod+Shift+M (configure in your WM)")
    print(f"    Phone URL:    https://{lan_ip}:{config.DEFAULT_PORT}")
    print(f"    Bearer token: {config.TOKEN_FILE}")
    print(f"    Cert FP:      {fingerprint or '(none)'}")
    pending_units = state.get("restart_units") or []
    after = " (after the pending restart)" if "whisper-server" in pending_units else ""
    print(f"    Model:        {effective}{after}")
    chosen = state.get("model_id", "")
    if chosen and chosen != effective:
        print(f"    (chosen {chosen} was not applied — download it and re-run setup)")
    for mode_id, pinned in _wizard_mode_model_pins(effective):
        print(f"    ! mode {mode_id!r} pins whisper_model={pinned} in {config.MODES_FILE.name};")
        print("      its requests ask for that model (the whisper-cli fallback loads it).")
    print()
    print("  Next steps:")
    print("    docs/cert-trust.md       — trusting the self-signed cert on phone")
    print("    docs/tailscale.md        — see `dictation-server --tailscale-cert` for LE certs")
    print("    journalctl --user -u dictation-server -f | grep timing  — latency profiling")


# ----- Driver --------------------------------------------------------
def run_setup_wizard(args, deps: WizardDeps) -> int:
    """Top-level wizard driver. Returns process exit code (1 = restart pending)."""
    print("=" * 64)
    print("  Calliope dictation-server — first-run setup")
    print("=" * 64)

    state: dict = {
        "model_id": _wizard_effective_model(),
        "model_applied": False,
        "restart_units": [],
        "restart_reasons": [],
    }
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
        _wizard_stage_4(args, state)
    else:
        _wizard_skip(4, "--skip-stage-4")

    state["restart_units"], state["restart_reasons"] = _wizard_pending_restarts()
    pending = _wizard_restart_pending(state)
    if pending:
        _wizard_print_restart(state)

    if pending:
        _wizard_skip(5, "restart pending")
    elif not args.skip_stage_5:
        _wizard_stage_5(args, deps)
    else:
        _wizard_skip(5, "--skip-stage-5")

    if pending:
        _wizard_skip(6, "restart pending — the running server would reject the new token")
    elif not args.skip_stage_6:
        _wizard_stage_6(args, fingerprint, deps)
    else:
        _wizard_skip(6, "--skip-stage-6")

    if not args.skip_stage_7:
        _wizard_stage_7(state, fingerprint)
    return 1 if pending else 0
