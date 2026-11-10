# Calliope Configuration Reference

Calliope is configured through three surfaces:

1. CLI flags on `server/calliope-server` / `~/.local/bin/dictation-server`.
2. Environment variables, usually via `~/.config/systemd/user/dictation-server.service.d/*.conf`.
3. Runtime data under `~/.local/share/dictation-server/`.

Secrets are never safe to paste into issues or screenshots. Redact bearer tokens and pairing URLs (`?pair=` codes are one-use, but an unused one still hands out the bearer).

## CLI flags

Current server flags:

- `--port` — HTTPS listen port; default `8384`.
- `--host` — listen host; usually paired with `DICTATION_BIND_HOST`.
- `--no-ssl` — development-only cleartext HTTP.
- `--setup` — run the setup/wizard path.
- `--tailscale-cert` — issue a Tailscale (Let's Encrypt) cert for this node, install it as `cert.pem`/`key.pem` in the data dir, and offer to restart the service. See [`tailscale.md`](tailscale.md).
- `--rotate-token` — rotate the bearer token and exit; prints the token path and live-rotation next steps, not the token value.
- `--show-token` — print the current bearer token from the token file to stdout and exit.
- `--force` — wizard: rotate token + cert and re-download the model. The wizard never restarts a running service. Whenever a running `dictation-server` or `whisper-server` started before its unit file, drop-ins or (for dictation-server) the token last changed — on this run or an earlier one — it reports "restart pending" with the exact `systemctl --user restart …` command, skips the self-test and pairing, and exits 1.
- `--install-systemd` / `--no-install-systemd` — opt into/out of user-unit installation. The wizard copies the repo's `systemd/dictation-server.service`, `whisper-server.service` and `kokoro-server.service` into `~/.config/systemd/user/` (from the checkout, from the copies `scripts/install-server` stages in `~/.local/share/dictation-server/units/`, or from the packaged `units/` directory) and enables only `dictation-server`. Whisper and Kokoro have no `[Install]` section: dictation-server starts them on demand and stops them when idle, so they do not hold VRAM/RAM from login. If an older install enabled them, run `systemctl --user disable whisper-server kokoro-server` once. When the model chosen in stage 1 is downloaded, it also writes `whisper-server.service.d/calliope-model.conf` and `dictation-server.service.d/calliope-model.conf` setting `DICTATION_WHISPER_MODEL`.
- `--skip-stage-N` (N = 1–7) — skip one wizard stage. After a pending restart, `--setup --skip-stage-1 --skip-stage-2 --skip-stage-3 --skip-stage-4` runs just the self-test and pairing.

## Server environment

### Bind/CORS

- `DICTATION_BIND_HOST` — bind address. Use `0.0.0.0` only when phone/LAN access is required and CORS/token auth are configured.
- `DICTATION_CORS_ORIGINS` — comma-separated allowed browser origins. Keep HTTP limited to localhost; use HTTPS for LAN/Tailscale. The allowlist also gates the loopback auth bypass: a request from `127.0.0.1` that carries a browser `Origin` outside this list (any other site open in your desktop browser) must present the bearer like a remote client. Local tools without an `Origin` (curl, the wizard) are still trusted on loopback. JSON endpoints require `Content-Type: application/json`.
- `DICTATION_CONNECTION_TIMEOUT` — per-connection socket timeout in seconds (default `60`). Bounds the TLS handshake, request parsing and each body/response chunk so a stalled client cannot pin a worker thread.
- `DICTATION_MAX_CONNECTIONS` — concurrent request-handler threads, SSE streams excluded (default `64`). Over the cap a TLS connection is closed before the handshake (no response); only a plain-HTTP (`--no-ssl`) connection gets a bare `503` with `Retry-After: 1`.
- `DICTATION_MAX_SSE_SUBSCRIBERS` — concurrent `/events` subscribers (default `16`); over the cap `/events` answers JSON 503 `sse_capacity`.

### Formatter routing

OmniRoute is the only formatter route; there is no provider setting.

- `DICTATION_OMNIROUTE_URL` — OmniRoute base URL, default `http://127.0.0.1:20128/v1`.
- `OMNIROUTE_API_KEY` — required gateway credential. Calliope reads it from the
  process environment, or imports only that variable from
  `~/.config/fish/conf.d/api-keys.fish`; it never supplies a default key.
- `DICTATION_OMNIROUTE_RP_CHAIN` — comma-separated model fallback chain for
  RP/enhance modes (creative-quality first).
- `DICTATION_OMNIROUTE_CLEAN_CHAIN` — comma-separated model fallback chain for
  cleanup/grammar modes (cheaper/faster first).
- `DICTATION_OMNIROUTE_SELECTABLE_MODELS` — comma-separated allow-list shown in
  the phone and SillyTavern formatter-model pickers. Every value must be a
  catalog-qualified provider/model ID; empty selection uses the RP chain.
  Defaults add OpenRouter Gemini 3.5/3.6 Flash and Kimi K3 plus xAI Grok 4.6
  (`xao/`); Nous inference is not an OmniRoute provider, so OpenRouter carries them.
- `DICTATION_FORMATTER_TIMEOUT` — per-model formatter request timeout in seconds;
  default `18`. Measured RP round-trips with a full persona/character/scene
  prompt land at 3.5–5.6s, so the previous `10` clipped the slow tail. This is
  a per-attempt budget, not the total: a timeout now advances the model chain.
- `DICTATION_FORMATTER_CHAIN_BUDGET` — retry bound for the fallback chain, in
  seconds; default `40`. Checked before each tier starts, and between SSE events
  while streaming, so a timeout-skippable chain cannot stack a full per-attempt
  timeout per tier. It is **not** a total wall-clock cap: `urlopen` timeouts are
  per socket operation, so a response already in flight can outlive the
  deadline, and a non-streaming body that keeps trickling has no total bound.
- `DICTATION_FORMATTER_OFFLINE_TTL` — seconds a refused/unresolvable OmniRoute
  connection marks the gateway offline (default `3`). There is no preflight
  probe: availability is learned from real completion requests, and the cached
  verdict stops the remaining tiers and the next request from each paying a
  failed connection.
- `DICTATION_DISFLUENCY_CLEAN_BUDGET` — shared deadline in seconds for the whole
  disfluency-cleanup chain in `persona_pov` / narrator modes (default `4.5`);
  cleanup is skipped (raw dictation continues) when it runs out.

`grammar_clean` and disfluency cleanup use the cleanup chain; RP modes use the
RP chain. A mode or request pin (below) replaces the chain for the grammar/RP
formatting step only — the disfluency-cleanup pass always uses the cleanup
chain.

Every formatter response carries model attribution — which model in the
active chain produced the text, and whether it was a fallback tier — surfaced
in both the ST extension and the phone PWA.

Current built-in OmniRoute defaults (catalog verified 2026-09-10):

- RP/enhance: `codex/gpt-5.6-sol-high` → `cc/claude-sonnet-5` →
  `claude/claude-opus-4-8`.
- Cleanup: `codex/gpt-5.6-sol-low` → `claude/claude-sonnet-4-6`.

The RP prompt treats profanity, insults, slurs, punchlines, commands, and other
charged diction as verbatim voice anchors. Enhancement may add rhythm and
physical/sensory detail around those anchors, but must not euphemize, sanitize,
or replace them. Override either chain through the environment variables above
when the OmniRoute catalog changes; no source edit is required.

### Per-mode LLM assignment

A mode can pin the model that formats it, instead of every mode sharing one
global picker value. One optional field per mode in `modes.yaml`:

```yaml
- id: rp_enhance
  formatter_model: codex/gpt-5.6-luna  # empty = use OmniRoute fallback chain
```

Older modes files may still carry `formatter_provider:` or `preset:` keys;
they are ignored on load and dropped on the next save.

Precedence, highest first:

1. A request-level `formatter_model` (the phone/extension model picker). Note
   that both clients **persist** this selection and send it on every request, so
   it behaves as a sticky global override, not a one-shot choice: while it is
   set, every per-mode assignment below is ignored. Set the picker back to
   "Auto fallback chain" to let per-mode assignments apply. The extension labels
   it "Global model override (every mode)" and marks a shadowed mode pin as
   inactive.
2. The mode's `formatter_model`.
3. The OmniRoute fallback chain.

Set it from the SillyTavern extension ("Model assigned to a mode"), or via
`POST /modes/formatter` with `{"mode": "...", "model": "..."}`.
An empty `model` clears the pin. The server rejects an unknown mode, a model
outside the OmniRoute allow-list, and a mode with no formatter step.

A pinned model is a one-element chain, so it deliberately has no fallback: an
explicit choice is honoured or reported, never silently substituted.

### Request limits

- `DICTATION_MAX_JSON_BODY_BYTES` — max JSON request body size, default 1 MB.
- `DICTATION_MAX_AUDIO_BODY_BYTES` — max audio upload size, default 25 MB.
- `DICTATION_MAX_AUDIO_SECONDS` — max decoded audio length, default `600`.
  A take whose decoded length exceeds it is rejected with 413
  `audio_too_long` (`details.max_seconds`); it is never truncated.

Oversized `Content-Length` is rejected with HTTP 413 before the body is read;
malformed/negative values get a 400.

### Audio upload

`POST /transcribe` takes the recording as the raw request body. Accepted
`Content-Type`s (codec parameters allowed): `audio/webm`, `audio/mp4`,
`audio/ogg`, `audio/wav`. Every upload is normalised through `ffmpeg` to
16 kHz mono WAV. Clients send the type their `MediaRecorder` actually
produced (first supported of `audio/webm;codecs=opus`, `audio/mp4`,
`audio/ogg;codecs=opus`).

Audio errors (JSON `{error, code, …}`): 415 `unsupported_media_type`, 413
`audio_too_long`, 503 `ffmpeg_missing`, 400 `ffmpeg_failed`.

Query parameters worth knowing:

- `client_request_id` — optional id matching `^[A-Za-z0-9_-]{16,32}$`; when
  well-formed it is used verbatim as the request id (SSE events, timing log),
  otherwise the server mints one.
- `use_state=1` — resolve persona/character/mode/chat from the fresh ST state;
  the chat is the exact one the state names, not the newest file.

The JSON response includes `entry_id` — the transcript entry written for this
take (`""` if none).

`POST /reformat` (JSON) re-runs a mode on existing text. Besides `text` and
`mode` it accepts `chat_id` (scopes session context to that chat),
`entry_id` (the take to replace — clients send the `/transcribe` `entry_id`
on Regenerate; an unknown or non-user id is 404 `entry_not_found`), and
`persist_transcript` (default `true`; `false` returns the result without
touching the transcript). A legacy request without `entry_id` replaces the
newest user entry as of the moment it arrives, so a take that lands while
the formatter runs is never the one replaced.

`GET /active-chat?use_state=1` returns the chat the fresh ST state names
(falling back to the newest-chat lookup only when it cannot be resolved).

`POST /warm` (same auth as other POSTs, no body) boots `whisper-server` in the
background and refreshes the idle timer; it returns 202 `{"status":"warming"}`
or 200 `{"status":"ready"}` and never blocks. Clients call it fire-and-forget
when capture starts.

### SillyTavern data sources

- `DICTATION_ST_DATA_ROOT` — SillyTavern `data/default-user` directory;
  relocates the whole ST data tree in one shot. Default is a local path from
  the operator's own deployment — override this for any other install.
- `DICTATION_ST_SETTINGS_FILE` — optional direct path to SillyTavern's
  `settings.json`; Calliope reads only `power_user.personas` and
  `power_user.persona_descriptions` to populate and shape the full persona list.
- `DICTATION_PERSONAS_DIR` — optional local markdown persona-card source merged
  with SillyTavern's persona list.
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

- `DICTATION_WHISPER_MODEL` — Whisper model name, default `large-v3-turbo`.
  `whisper-server.service` loads
  `%h/.local/share/whisper/ggml-${DICTATION_WHISPER_MODEL}.bin` and its
  sandbox only allows reading `%h/.local/share/whisper` — that path is fixed
  in the unit. dictation-server uses the variable as the default mode model
  and `whisper-cli` fallback model, looking in `$XDG_DATA_HOME/whisper/`
  (which is the same directory unless you set a non-default
  `XDG_DATA_HOME`; then add a `whisper-server` drop-in that restates
  `ExecStart=` with your `--model` path and extends `ReadOnlyPaths=`). Set the
  variable for **both** units — `--setup` does this with drop-ins — and
  restart them. Built-in modes inherit it; a mode in `modes.yaml` with an
  explicit `whisper_model` (other than the legacy `large-v3-turbo` seed on a
  built-in mode) keeps its own value, and `--setup` lists such modes.
- `WHISPER_BIN` — fallback CLI binary (`whisper-cli`). The `whisper-server`
  binary is the unit's `ExecStart`; there is no environment override for it.
- `WHISPER_SERVER_URL` — whisper.cpp HTTP daemon; default `http://127.0.0.1:9001`.
- `WHISPER_IDLE_SHUTDOWN_SECONDS` — seconds before Calliope stops idle whisper-server (default `300`).
- `WHISPER_IDLE_SHUTDOWN_DISABLED` — `1`/`true`/`yes`/`on` keeps whisper-server resident; anything else (including `0`, `false`, empty) leaves idle shutdown on.
- `DICTATION_LOW_CONFIDENCE_THRESHOLD` — word log-probability below which a word is flagged as low-confidence ("Did you mean?" chips); default `-0.7`.

### Kokoro/TTS

- `KOKORO_SERVER_URL` — Kokoro HTTP daemon; default `http://127.0.0.1:9002`.
- `KOKORO_DEFAULT_VOICE` — fallback voice id.
- `KOKORO_IDLE_SHUTDOWN_SECONDS` — seconds before idle Kokoro shutdown (default `600`).
- `KOKORO_IDLE_SHUTDOWN_DISABLED` — `1`/`true`/`yes`/`on` keeps Kokoro resident; anything else leaves idle shutdown on.
- `KOKORO_REQUEST_TIMEOUT` — timeout for TTS requests in seconds (default `240`).
- `TTS_AUDIOBOOK_MAX_MESSAGES` — max messages for audiobook export (default `200`).
- `TTS_AUDIOBOOK_MAX_TOTAL_CHARS` — max total text size for audiobook export (default `60000`).
- `TTS_AUDIOBOOK_SILENCE_MS` — silence gap inserted between audiobook clips (default `350`).

## Runtime files

Under `~/.local/share/dictation-server/` by default (`$XDG_DATA_HOME/dictation-server/` when `XDG_DATA_HOME` is set). Set `CALLIOPE_DATA_DIR` for tests or non-standard runtime state; production units should keep this private and local.

- `cert.pem` / `key.pem` — TLS material. `--tailscale-cert` installs here; mkcert flows should write here too.
- `cert.fingerprint` — SHA-256 fingerprint shown during pairing.
- `token` — bearer token, mode `0600`; sync this into ST Dictation Bridge settings after rotation.
- `vocab.yaml` — term/character biasing.
- `modes.yaml` — pipeline mode definitions.
- `char-modes.yaml` — remembered mode choices.
- `voice_macros.yaml` — optional voice-command grammar overrides (built-in defaults when absent).
- TTS voice profiles are **not** server files: the extension keeps them in SillyTavern's `extension_settings` (`ttsVoiceProfiles`).
- `calliope_server/` + `voice_catalog.json` — installed by `scripts/install-server`; the wrapper searches for the package next to itself first, then here. The installer syncs these before it replaces the wrapper.
- `units/` — repo systemd units and the learner timer, staged by `scripts/install-server` for `dictation-server --setup`; not a systemd load path.
- `INSTALL_MANIFEST` — written last by `scripts/install-server`: the checkout revision (`git describe`) plus SHA-256 of the installed wrapper, `voice_catalog.json` and every `calliope_server/*.py`. When the package is loaded from this directory, the wrapper verifies the manifest at startup and refuses to start on a missing manifest, a checksum mismatch, or an unlisted module. A privileged `GET /health` reports the installed `revision` (`checkout` whenever the package sits next to the wrapper — a checkout, wheel or AUR install).

### Deploying (`scripts/install-server`)

`scripts/install-server` is the only deploy path for a checkout install. It
syncs the package, catalog and units into the data dir, installs the wrapper
as `$BINDIR/dictation-server` (default `~/.local/bin`; if that is a symlink,
the link is kept and its target is overwritten), writes `INSTALL_MANIFEST`,
and restarts nothing. When the TTS runtime dir `~/.local/share/calliope-tts/`
exists it also installs `scripts/kokoro-server.py` there (effective at the
next `kokoro-server` start). `scripts/install-server --check` compares the
checkout with the installed copy (`SAME`/`DIFF`/`EXTRA` per file, manifest
validity, installed vs checkout revision) and exits 1 on any drift. Overrides:
`BINDIR`, `DATADIR`, `TTSDIR`.

## Pairing and state freshness

- The ST extension stores the bearer token in `extension_settings.dictation-bridge.serverToken`.
- The phone page is opened with a one-time `?pair=<code>` URL, minted by the ST extension (**Re-pair this phone** / **Show local QR** / **Copy pairing URL**) or by `dictation-server --setup` stage 6 via authenticated `POST /pair/bootstrap`. The code expires after 120 seconds and works once: the page exchanges it at `POST /pair/exchange` for the bearer, keeps that in session storage, and scrubs the code from the visible URL. The bearer never appears in a URL or QR code.
- `/health` only proves the server is reachable. `/state` with bearer auth proves the token is valid.
- ST bridge state is fresh for about 60 seconds, stale by 120 seconds, and dead after that. If the phone says stale/not paired, reopen it from the current ST chat rather than reusing an old bookmark.
