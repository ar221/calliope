# Calliope

Local-first voice dictation server for SillyTavern. Hold-to-talk audio is transcribed by whisper.cpp on the local GPU, shaped by the active character/persona, and dropped into the ST textarea. Audio never leaves the LAN.

## Stack
- Python single-file server (`server/calliope-server`) — packaging via `packaging/pyproject.toml`.
- whisper.cpp (`large-v3-turbo`) on ROCm/CUDA via `whisper-cli` / `whisper-server`.
- Browser extension (`extension/`) — vanilla JS (`index.js`, `manifest.json`), embedded phone PWA.
- systemd user unit for the server; niri/GNOME/KDE hotkey (`Mod+Shift+M`).

## Layout
- `server/` — `calliope-server` (executable wrapper: HTTP handler, auth, cert/token, main/CLI) + `calliope_server/` package (`config`, `events`, `formatter`, `sillytavern`, `transcribe`, `tts`, `web_ui`, `wizard` — see package `__init__.py` for the module map), `rp_eval.py`, `voice_catalog.json`.
- `extension/` — SillyTavern `dictation-bridge` extension (JS, CSS, manifest, QR lib).
- `systemd/` — user service unit(s) for the dictation server.
- `scripts/` — helper/install scripts.
- `packaging/` — `pyproject.toml`, AUR/pipx packaging (unpublished).
- `docs/` — roadmap, guides; `tests/` — pytest suite.

## Entrypoint
- Install server: `scripts/install-server` (wrapper → `~/.local/bin/dictation-server`, package + voice catalog → `~/.local/share/dictation-server/`)
- First run wizard: `dictation-server --setup` (probes audio, picks model, gen cert+token, installs unit, self-test).
- Tests: `pytest` (see `.pytest_cache`, `.ruff_cache` present).

## Status
- Branch: `feature/calliope-tts-polish`. Active development.
- Pipeline: 5 modes (`plain`, `grammar_clean`, `rp_format`, `rp_enhance`, `persona_pov`), remembered per character.
- TODO (README roadmap): AUR `calliope-git` + pipx `calliope-dictation` packaging not yet published.

## Conventions
Inherits ~/CLAUDE.md (Alfred). See also `AGENTS.md`, `factory_rules.md`, `mission.md`. Repo-specific overrides here.
