# Factory Rules — Calliope

These rules govern agent work in the Calliope repository.

## Autonomy level

Default autonomy is **Level 3–4**:

- Agents may inspect, edit, test, document, and produce PR-ready diffs.
- Agents may run local tests and read-only diagnostics.
- Agents may not restart live services, rotate tokens, change exposure, or alter SillyTavern live settings without explicit approval.

This repo is not an unattended production factory. Treat it as live-service code with privacy-sensitive edges.

## Secret and privacy rules

- Never print or commit bearer tokens, tokenized URLs, private keys, cookies, API keys, SillyTavern session files, private chat logs, or raw user audio.
- Redact by construction when inspecting config or logs.
- Do not add telemetry, remote analytics, or third-party phone UI assets.
- Keep audio temp-file handling bounded and cleanup-backed.
- If fixtures are needed, use synthetic/minimal data and document that no private audio/chat was included.

## Live-service rules

- Do not restart `dictation-server`, `whisper-server`, `kokoro-server`, SillyTavern, or related user services unless Ayaz explicitly approves.
- Repo edits are not active until synced/deployed through the service path.
- If a change requires restart or extension hard-refresh, state that as pending instead of implying it is live.
- Preserve loopback/private defaults unless the task explicitly concerns LAN/Tailscale exposure.
- For phone issues, distinguish token validity, cert trust, server reachability, ST pairing, and active-context following.

## Git/work rules

- Check `git status --short --branch` before editing.
- Do not trample unrelated dirty files.
- Keep packaging docs, systemd units, README, and implementation aligned when changing behavior.
- Do not commit generated local state, certs, tokens, model files, cache files, or personal ST data.

## Validation gates

Use the nearest relevant checks:

```bash
python -m py_compile server/calliope-server scripts/learn-vocab scripts/kokoro-server.py
pytest -q
HOME=/home/ayaz systemd-analyze --user verify systemd/dictation-server.service systemd/whisper-server.service systemd/kokoro-server.service systemd/learn-vocab-nightly.service
```

For targeted changes, run the specific pytest file(s) and any relevant smoke. If live services are not restarted, say so.

## Scenario validation

For behavioral fixes, validate the layer that changed:

- server/API: `/health`, auth handling, endpoint response shape, request-log redaction;
- bridge: button/state UI, SSE events, textarea insertion, ST settings compatibility;
- phone/PWA: token bootstrap, cert trust, recording lifecycle, visible pairing state;
- formatter: provider selection, prompt shaping, fallback behavior, no audio cloud leakage;
- systemd: unit verification with `HOME=/home/ayaz`, no sandbox-HOME false failures.

## Stop conditions

Stop and ask before:

- rotating, replacing, or printing token/cert material;
- changing bind host or public exposure;
- restarting live services;
- writing to the live SillyTavern settings file;
- preserving private audio/chat artifacts;
- switching default formatter providers;
- adding new paid/cloud dependencies.

## Receipts

Every non-trivial change should report:

- files changed,
- tests/smokes run,
- live services touched or explicitly not touched,
- deployment/sync/restart required,
- privacy/security impact.
