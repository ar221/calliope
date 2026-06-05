# Calliope Configuration Reference

Calliope is configured through three surfaces:

1. CLI flags on `server/calliope-server` / `~/.local/bin/dictation-server`.
2. Environment variables, usually via `~/.config/systemd/user/dictation-server.service.d/*.conf`.
3. Runtime data under `~/.local/share/dictation-server/`.

Secrets are never safe to paste into issues or screenshots. Redact bearer tokens and URLs containing `?token=`.

## CLI flags

Current server flags:

- `--port` — HTTPS listen port; default `8384`.
- `--host` — listen host; usually paired with `DICTATION_BIND_HOST`.
- `--no-ssl` — development-only cleartext HTTP.
- `--setup` — run the setup/wizard path.
- `--tailscale-cert` — use/write Tailscale cert material into the runtime cert path.
- `--rotate-token` — rotate the bearer token and exit; prints the token path and live-rotation next steps, not the token value.
- `--force` — force wizard/setup regeneration paths where supported.
- `--install-systemd` / `--no-install-systemd` — opt into/out of user-unit installation.
- `--skip-stage-N` — setup self-test escape hatch for known-bad local stages.

## Server environment

### Bind/CORS

- `DICTATION_BIND_HOST` — bind address. Use `0.0.0.0` only when phone/LAN access is required and CORS/token auth are configured.
- `DICTATION_CORS_ORIGINS` — comma-separated allowed browser origins. Keep HTTP limited to localhost; use HTTPS for LAN/Tailscale.

### Formatter/proxy routing

- `DICTATION_FORMATTER_PROVIDER` — formatter provider selector, e.g. `claude` or `openai`.
- `DICTATION_CLAUDE_PROXY_URL` / `DICTATION_PROXY_URL` — Claude-shape proxy base URL.
- `DICTATION_CLAUDE_RP_MODEL` / `DICTATION_RP_MODEL` — Claude RP model override.
- `DICTATION_OPENAI_PROXY_URL` — OpenAI-compatible proxy base URL.
- `DICTATION_OPENAI_RP_MODEL` — OpenAI-compatible RP model.
- `DICTATION_OPENAI_CLEAN_MODEL` — OpenAI-compatible cleanup/disfluency model.

### SillyTavern data sources

- `DICTATION_PERSONAS_DIR` — persona-card source directory.
- `DICTATION_RULES_DIR` — rules/formatting source directory.
- `DICTATION_CHARACTERS_DIR` — character-card source directory.

The checked-in service/source defaults point at Ayaz's local SillyTavern data
root for the operator deployment. Public installs should override these with
site-local paths such as `<ST-root>/data/default-user/...`; do not paste private
chat paths or chat contents into issues.

### Whisper

- `WHISPER_BIN` — fallback CLI binary.
- `WHISPER_SERVER_URL` — whisper.cpp HTTP daemon; current live default is `http://127.0.0.1:9001`.
- `WHISPER_IDLE_SHUTDOWN_SECONDS` — seconds before Calliope stops idle whisper-server.
- `WHISPER_IDLE_SHUTDOWN_DISABLED` — set non-empty to keep whisper-server resident.

### Kokoro/TTS

- `KOKORO_SERVER_URL` — Kokoro HTTP daemon; default `http://127.0.0.1:9002`.
- `KOKORO_DEFAULT_VOICE` — fallback voice id.
- `KOKORO_IDLE_SHUTDOWN_SECONDS` — seconds before idle Kokoro shutdown.
- `KOKORO_IDLE_SHUTDOWN_DISABLED` — set non-empty to keep Kokoro resident.
- `KOKORO_REQUEST_TIMEOUT` — timeout for TTS requests.
- `TTS_AUDIOBOOK_MAX_MESSAGES` — max messages for audiobook export.
- `TTS_AUDIOBOOK_MAX_TOTAL_CHARS` — max total text size for audiobook export.
- `TTS_AUDIOBOOK_SILENCE_MS` — silence gap inserted between audiobook clips.

## Runtime files

Under `~/.local/share/dictation-server/` by default. Set `CALLIOPE_DATA_DIR` for tests or non-standard runtime state; production units should keep this private and local.


- `cert.pem` / `key.pem` — TLS material. Tailscale/mkcert flows should write here.
- `cert.fingerprint` — SHA-256 fingerprint shown during pairing.
- `token` — bearer token, mode `0600`; sync this into ST Dictation Bridge settings after rotation.
- `vocab.yaml` — term/character biasing.
- `modes.yaml` — pipeline mode definitions.
- `char-modes.yaml` — remembered mode choices.
- voice macro/profile files as created by the TTS/profile UI.

## Pairing and state freshness

- The ST extension stores the bearer token in `extension_settings.dictation-bridge.serverToken`.
- The phone page receives a one-time `?token=` URL from the ST mic button, stores it in session storage, then scrubs it from the visible URL.
- `/health` only proves the server is reachable. `/state` with bearer auth proves the token is valid.
- ST bridge state is fresh for about 60 seconds, stale by 120 seconds, and dead after that. If the phone says stale/not paired, reopen it from the current ST chat rather than reusing an old bookmark.
