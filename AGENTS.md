# Calliope

Local-first voice dictation server for SillyTavern. Hold-to-talk audio is transcribed by whisper.cpp on the local GPU, shaped by the active character/persona, and dropped into the ST textarea. Audio goes only from the capturing device to the operator's own PC, never further. Agents working here must preserve privacy, live-service safety, and low-friction phone/desktop UX.

## Stack

- Python stdlib server: executable wrapper `server/calliope-server` (composition root) + the `server/calliope_server/` package — packaging via `packaging/pyproject.toml` (pipx) and `packaging/PKGBUILD` (AUR), both unpublished.
- whisper.cpp (default `large-v3-turbo`, `DICTATION_WHISPER_MODEL`) on ROCm/CUDA via `whisper-server` (on-demand user unit) with `whisper-cli` fallback.
- SillyTavern extension (`extension/`) — vanilla JS (`index.js`, `manifest.json`); the phone PWA is embedded in `calliope_server/web_ui.py`.
- systemd user units for dictation/whisper/kokoro + nightly learner timer. The desktop hotkey client (`dictate`) is external, not in this repo.

## Layout

- `server/` — `calliope-server` (executable wrapper: HTTP handler, auth, cert/token, main/CLI) + `calliope_server/` package (`config`, `events`, `formatter`, `sillytavern`, `transcribe`, `tts`, `web_ui`, `wizard` — see package `__init__.py` for the module map), `rp_eval.py`, `voice_catalog.json`.
- `extension/` — SillyTavern `dictation-bridge` extension (JS, CSS, manifest, QR lib).
- `systemd/` — user units: `dictation-server`, `whisper-server`, `kokoro-server`, `learn-vocab-nightly` (service + timer).
- `scripts/` — `install-server`, `learn-vocab`, `kokoro-server.py`, `check-web-ui-js`.
- `packaging/` — `pyproject.toml`, `PKGBUILD`, `PKGBUILD-stable` (unpublished).
- `docs/` — roadmap, guides; `tests/` — pytest suite (`conftest.py` isolates HOME/XDG/data dirs, sets a dummy `OMNIROUTE_API_KEY`, denies non-loopback sockets, and provides `load_wrapper()` for importing the hyphenated wrapper).

## Entrypoint

- Install server: `scripts/install-server` — the only deploy path (wrapper → `~/.local/bin/dictation-server`, keeping a symlink and overwriting its target; package, voice catalog and units/timer → `~/.local/share/dictation-server/`; writes `INSTALL_MANIFEST` with revision + SHA-256s, which the installed wrapper verifies at startup and refuses to run if mixed). `scripts/install-server --check` reports drift and exits 1. A privileged `/health` reports `revision`.
- First run wizard: `dictation-server --setup` (probes audio, picks model, gen cert+token, installs unit, self-test).
- Tests: `pytest -q` (no real HOME, network or services needed).

## Status

- Branch: `main`. Active development.
- Pipeline: 8 modes (`plain`, `grammar_clean`, `rp_format`, `rp_enhance`, `persona_pov`, `narrator_past`, `narrator_present`, `command`), remembered per character.
- Packaging: AUR `calliope-git` + pipx `calliope-dictation` prepared, not published.

## Conventions

Inherits ~/CLAUDE.md (Alfred). See also `factory_rules.md`, `mission.md`. Repo-specific overrides here.

## Governance Files

Read these root files before implementation work:

- `mission.md` — product scope and non-goals.
- `factory_rules.md` — live-service safety, validation gates, and stop conditions.
- `AGENTS.md` — this file: canonical repo-local capsule + work manual.

`CLAUDE.md` is a relative symlink to `AGENTS.md` for Claude Code compatibility; edit `AGENTS.md` only.

## Source map

- `server/calliope-server` — executable wrapper: HTTP handler/routes, auth, cert/token, CLI.
- `server/calliope_server/` — `config`, `events`, `formatter`, `sillytavern`, `transcribe`, `tts`, `web_ui`, `wizard`.
- `extension/` — SillyTavern `dictation-bridge` extension.
- `scripts/` — helper scripts such as vocab learning or support services.
- `systemd/` — user service unit templates for dictation/whisper/TTS services.
- `docs/` — architecture, config, troubleshooting, install, roadmap.
- `tests/` — pytest coverage (server behaviour, wizard, isolation; extension JS via a Node VM harness).
- `README.md`, `PRIVACY.md`, `SECURITY.md` — public-facing product/security posture.

## Operating rules

- Do not print or commit bearer tokens, tokenized URLs, private keys, cookies, cert keys, SillyTavern session data, or chat logs.
- Do not restart, stop, or redeploy live user services without the operator's approval.
- Repo edits are not live until the appropriate service copy/symlink/restart path is explicitly applied.
- Distinguish server health, SillyTavern bridge health, phone pairing, SSE state, and formatter provider routing. Do not collapse them into one vague “works” claim.
- Treat local audio as sensitive. Do not preserve test audio unless it is an explicit fixture with consent and documentation.
- Preserve loopback/default-private posture unless a task explicitly concerns LAN/Tailscale exposure.

## Validation commands

Use the nearest relevant check:

```bash
node --check extension/index.js
node --check extension/qrcodegen.min.js
scripts/check-web-ui-js
python -m py_compile server/calliope-server server/calliope_server/*.py scripts/learn-vocab scripts/kokoro-server.py
ruff check .          # CI pins ruff 0.17.4
pytest -q
bash -n packaging/PKGBUILD packaging/PKGBUILD-stable scripts/install-server
# pin HOME explicitly if your agent sandbox overrides it, e.g. HOME=/home/<user>
systemd-analyze --user verify systemd/dictation-server.service systemd/whisper-server.service systemd/kokoro-server.service systemd/learn-vocab-nightly.service
```

For narrow changes, run targeted tests instead of the full suite when appropriate, then state what was not tested.

## Live-service verification layers

When troubleshooting live behavior, verify separately:

1. Calliope server `/health` and authenticated state endpoints.
2. Whisper server availability and model path.
3. SillyTavern extension runtime and saved bridge settings.
4. SSE connection state and fresh ST context following.
5. Phone/PWA token bootstrap, certificate trust, and pairing with the current ST tab.
6. Formatter provider routing for RP/cleanup modes.

## Handoff / receipt standard

For non-trivial work, report:

- files changed,
- commands run,
- tests/smokes passed or skipped,
- whether live services were untouched or require restart/sync,
- any privacy/security impact.
