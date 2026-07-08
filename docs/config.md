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
- `--show-token` — print the current bearer token from the token file to stdout and exit.
- `--force` — force wizard/setup regeneration paths where supported.
- `--install-systemd` / `--no-install-systemd` — opt into/out of user-unit installation.
- `--skip-stage-N` — setup self-test escape hatch for known-bad local stages.

## Server environment

### Bind/CORS

- `DICTATION_BIND_HOST` — bind address. Use `0.0.0.0` only when phone/LAN access is required and CORS/token auth are configured.
- `DICTATION_CORS_ORIGINS` — comma-separated allowed browser origins. Keep HTTP limited to localhost; use HTTPS for LAN/Tailscale.

### Formatter/proxy routing

- `DICTATION_FORMATTER_PROVIDER` — formatter provider selector: `claude`, `openai`,
  or `omniroute` (default). OmniRoute is a local OpenAI-shape aggregator that
  fronts several model subscriptions and walks a fallback chain per request.
- `DICTATION_OMNIROUTE_URL` — OmniRoute proxy base URL, default `http://127.0.0.1:20128/v1`.
- `DICTATION_OMNIROUTE_RP_CHAIN` — comma-separated model fallback chain for
  RP/enhance modes (creative-quality first).
- `DICTATION_OMNIROUTE_CLEAN_CHAIN` — comma-separated model fallback chain for
  cleanup/grammar modes (cheaper/faster first).
- `DICTATION_CLAUDE_PROXY_URL` / `DICTATION_PROXY_URL` — Claude-shape proxy base URL.
- `DICTATION_CLAUDE_RP_MODEL` / `DICTATION_RP_MODEL` — Claude RP model override.
- `DICTATION_OPENAI_PROXY_URL` — OpenAI-compatible proxy base URL.
- `DICTATION_OPENAI_RP_MODEL` — OpenAI-compatible RP model.
- `DICTATION_OPENAI_CLEAN_MODEL` — OpenAI-compatible cleanup/disfluency model.

Every formatter response carries model attribution — which model in the
active chain produced the text, and whether it was a fallback tier — surfaced
in both the ST extension and the phone PWA.

### Request limits

- `DICTATION_MAX_JSON_BODY_BYTES` — max JSON request body size, default 1 MB.
- `DICTATION_MAX_AUDIO_BODY_BYTES` — max audio upload size, default 25 MB.

Oversized `Content-Length` is rejected with HTTP 413 before the body is read;
malformed/negative values get a 400.

### SillyTavern data sources

- `DICTATION_ST_DATA_ROOT` — SillyTavern `data/default-user` directory;
  relocates the whole ST data tree in one shot. Default is a local path from
  the operator's own deployment — override this for any other install.
- `DICTATION_PERSONAS_DIR` — persona-card source directory.
- `DICTATION_RULES_DIR` — rules/formatting source directory.
- `DICTATION_CHARACTERS_DIR` — character-card source directory; overrides
  `DICTATION_ST_DATA_ROOT/characters` when set.
- `DICTATION_ST_CHATS_DIR` — overrides `DICTATION_ST_DATA_ROOT/chats` when set.
- `DICTATION_ST_GROUPS_DIR` — overrides `DICTATION_ST_DATA_ROOT/groups` when set.
- `DICTATION_ST_GROUP_CHATS_DIR` — overrides `DICTATION_ST_DATA_ROOT/group chats` when set.

The checked-in defaults point at the operator's local SillyTavern data root.
Public/other installs should set `DICTATION_ST_DATA_ROOT` (or the per-directory
overrides) to their own `<ST-root>/data/default-user/...` path; do not paste
private chat paths or chat contents into issues.

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
