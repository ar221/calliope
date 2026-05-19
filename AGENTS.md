# Calliope — Agent Work Manual

Calliope is the local-first voice dictation stack for SillyTavern. Agents working here must preserve privacy, live-service safety, and low-friction phone/desktop UX.

## Governance Files

Read these root files before implementation work:

- `mission.md` — product scope and non-goals.
- `factory_rules.md` — live-service safety, validation gates, and stop conditions.
- `AGENTS.md` — this repo-local work manual.

Do not create `claud.md`; use `AGENTS.md` for cross-agent context and add `CLAUDE.md` only if Claude Code-specific compatibility becomes necessary.

## Source map

- `server/calliope-server` — main HTTPS dictation server and API surface.
- `extension/` — SillyTavern `dictation-bridge` extension.
- `scripts/` — helper scripts such as vocab learning or support services.
- `systemd/` — user service unit templates for dictation/whisper/TTS services.
- `docs/` — architecture, config, troubleshooting, install, roadmap.
- `tests/` — pytest coverage for pipeline, command dispatch, vocab, TTS, and confidence handling.
- `README.md`, `PRIVACY.md`, `SECURITY.md` — public-facing product/security posture.

## Operating rules

- Do not print or commit bearer tokens, tokenized URLs, private keys, cookies, cert keys, SillyTavern session data, or chat logs.
- Do not restart, stop, or redeploy live user services without Ayaz's approval.
- Repo edits are not live until the appropriate service copy/symlink/restart path is explicitly applied.
- Distinguish server health, SillyTavern bridge health, phone pairing, SSE state, and formatter provider routing. Do not collapse them into one vague “works” claim.
- Treat local audio as sensitive. Do not preserve test audio unless it is an explicit fixture with consent and documentation.
- Preserve loopback/default-private posture unless a task explicitly concerns LAN/Tailscale exposure.

## Validation commands

Use the nearest relevant check:

```bash
python -m py_compile server/calliope-server scripts/learn-vocab scripts/kokoro-server.py
pytest -q
HOME=/home/ayaz systemd-analyze --user verify systemd/dictation-server.service systemd/whisper-server.service systemd/kokoro-server.service systemd/learn-vocab-nightly.service
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
