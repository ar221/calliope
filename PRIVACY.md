# Privacy

**Last updated:** 2026-05-07

Calliope is built on a simple promise: **your audio never leaves your
device, and your transcripts never persist longer than you want them to.**

This document is a verifiable claim. Every statement here can be
cross-checked against the source at `scripts/dictation-server` in this
repository. If you find a divergence between this doc and the code, the
code wins — please open an issue.

---

## What stays on your machine

### Audio

Captured on your phone (browser `MediaRecorder` over `navigator.mediaDevices.getUserMedia`)
or your desktop (`pw-record` via PipeWire). Sent over your local network to
the Calliope server running on your PC. Transcribed by `whisper-server`
(whisper.cpp) on your local GPU.

The audio file is deleted immediately after transcription:
`tempfile.NamedTemporaryFile` for the upload, explicit `Path(...).unlink(missing_ok=True)`
in the `finally` block of `/transcribe`. Calliope never uploads audio to
any cloud service; there is no code path that does so.

If `/tmp` is `tmpfs` (the systemd default on most modern Arch / CachyOS /
Fedora installs), the audio file never touches a physical disk. Verify
with `findmnt /tmp`.

### Transcripts

Held in memory only — `session_transcript: list[dict]` lives in the server
process and is never written to disk. Restart the server, the log is gone.

The phone PWA and the `dictation-bridge` ST extension expose:

- `DELETE /transcript` — clear the entire log.
- `DELETE /transcript/<id>` — clear a single entry.

Restart-on-failure is configured in the systemd unit, but the in-memory
transcript is not preserved across restarts.

### Vocabulary, modes, character → mode memory

Stored in `~/.local/share/dictation-server/*.yaml` on your machine, written
only when you change them via the phone UI or `POST /vocab`,
`POST /state/mode-memory`. These files are local-only; no part of Calliope
syncs them anywhere.

### Cert + token

Stored in `~/.local/share/dictation-server/`:

- `cert.pem` (mode 0644), `key.pem` (mode 0600) — self-signed TLS cert,
  90-day validity, auto-renewed at startup.
- `token` (mode 0600) — 32-byte bearer token. Required on every endpoint
  except `/health`.
- `cert.fingerprint` — SHA-256, also printed at server startup.

Treat this directory like `~/.ssh/`. Exclude it from cloud backups (Google
Drive, Dropbox, Syncthing public folders, Backblaze without per-folder
exclusion). The token is a credential.

---

## What may leave your machine (only if you opt in)

### Transcribed text — not audio — to a configured LLM proxy

When you select a `grammar_clean`, `rp_format`, `rp_enhance`, or
`persona_pov` mode, the transcribed *text* (not audio) is sent to whichever
LLM proxy you have configured. The default Calliope ships with two proxies
defined:

- `http://localhost:42069` — `claude-code-proxy` (the pyrite preset path).
  This is a local proxy. It in turn relays to **`claude.ai`** —
  Anthropic's privacy policy applies for that hop. Configure with
  `CLAUDE_RP_PROVIDER=claude` and `CLAUDE_RP_MODEL=...`.
- `http://127.0.0.1:10531/v1` — an OpenAI-compatible proxy. Whether this
  endpoint is local or relays to a cloud is determined by the user-installed
  proxy at that port, not by Calliope. If you point it at a fully local
  model (`llama.cpp`, `ollama`, `text-generation-webui`), nothing leaves the
  box. If you point it at a cloud-relay proxy, the text travels there.

The `plain` mode bypasses all LLM providers entirely. Pick `plain` if you
want hard certainty that no text leaves your machine.

### Active provider visibility

The server's outbound calls are observable:

- **At startup:** the log line declares which providers are reachable and
  where they sit (`Calliope: rp_enhance via claude-code-proxy at
  localhost:42069 → claude.ai`).
- **At runtime:** `GET /audit/network` returns a ring buffer of the last 50
  outbound non-loopback connection events with timestamp, host, port, and
  byte count (MVP-22). The phone PWA exposes this behind the privacy badge
  in the header (MVP-23). External destinations (anything outside
  `127.0.0.0/8` and your configured LAN CIDR) are flagged.
- The startup banner pins the configured proxy URLs so a misconfigured
  upstream can't silently re-route.

### No model auto-download

The Whisper model is downloaded once by you during setup
(see Quickstart in the README). After that, no calls go to Hugging Face.
Calliope has no `huggingface_hub`, no `urllib` to `hf.co`, no version-check
endpoint. Verified by grep: zero matches in 5,900 lines for
`hf.co|huggingface|model.download`.

### No phone-home / no auto-update

Calliope does not check for updates, does not phone home, does not have a
remote version endpoint. You upgrade by pulling from the repo. Verified by
grep: zero matches for `analytics|telemetry|sentry|posthog|mixpanel|google-analytics`.

---

## What never happens

- **Telemetry.** None. No analytics SDK, no error reporting, no usage
  metrics phoned home. The codebase has no equivalent of Sentry / PostHog /
  GA / Mixpanel.
- **Auto-update.** None. Calliope does not check for updates.
- **Background uploads.** None. Calliope is dormant when not actively
  serving a request.
- **Third-party JS in the phone UI.** None. The phone PWA is a single
  HTML+CSS+JS document served by your local server. No CDN, no Google
  Fonts, no analytics beacon, no external assets. Inspect the page source
  and you will see exactly what runs.
- **Default LAN exposure.** The server binds to `127.0.0.1` by default
  (`DICTATION_BIND_HOST` env override required to bind `0.0.0.0`). LAN
  exposure is opt-in.
- **Wide-open CORS.** The server echoes `Origin` only when it matches the
  configured allowlist (default: `https://localhost:*`,
  `https://127.0.0.1:*`, your configured ST host). No `Access-Control-Allow-Origin: *`
  in production.
- **Unauthenticated access.** Every endpoint except `/health` requires
  `Authorization: Bearer <token>`. Token is generated at first run and
  stored at `~/.local/share/dictation-server/token` mode 0600.

---

## Known caveats

### Self-signed cert

Your browser will show "your connection is not private" on first visit.
That's because the cert is signed by your own machine, not by a public CA.
The wizard prints the SHA-256 fingerprint at startup; match it before
trusting. See [`docs/cert-trust.md`](docs/cert-trust.md) for
platform-specific install steps.

If you want a real public-CA cert without owning a domain, see
[`docs/tailscale.md`](docs/tailscale.md) — Tailscale's `tailscale cert`
issues a Let's Encrypt cert for `<host>.<tailnet>.ts.net` over DNS-01.

### Multi-user systems

Calliope is designed for a single-user workstation. The runtime data dir
(`~/.local/share/dictation-server/`) sets mode 0600 on the token and
private key, but if your machine has other untrusted local users, treat
this as a deployment that needs additional hardening. The `dictation-server.service`
unit's `ProtectHome=read-only` + `ReadWritePaths=` directives narrow the
write surface, but a user with read access to your `$HOME` can still read
the cert and (depending on your umask) potentially the YAML files.

### LAN exposure

Calliope binds to localhost by default. If you have set `DICTATION_BIND_HOST=0.0.0.0`
(or the equivalent `--allow-network` flag once shipped) to use the phone
UI from another device, the server is reachable from any device on your
LAN. Token auth defends against unauthorized read; physical network
security is still on you. If your LAN includes IoT devices with known
CVEs, prefer Tailscale + tailnet-only binding (see
[`docs/tailscale.md`](docs/tailscale.md)).

### Stolen device

All on-disk content is readable by anyone who has the laptop. Calliope
does not encrypt its runtime data dir at rest. Full-disk encryption (LUKS,
FileVault, BitLocker) is the user's responsibility.

### Vocabulary and modes leak via backups

`vocab.yaml`, `modes.yaml`, `char-modes.yaml` contain character names and
the terms you have biased into whisper. They are sensitive in the sense
that they reveal what kinds of content you transcribe. Exclude
`~/.local/share/dictation-server/` from any cloud backup that you wouldn't
trust with `~/.ssh/`.

### Recording session screenshot leak on phone

The VU meter and waveform don't reveal text, but the transcript bubble
does after `/transcribe` returns. If your phone gallery cloud-syncs
screenshots, the post-transcribe bubble may end up in the cloud. The PWA's
"transcript visible by default" toggle in settings lets you hide bubble
text behind a tap.

---

## Reporting issues

Open an issue at `<repo>` — _placeholder, repo URL pending Phase 5
extraction._

**Do not include audio in bug reports.** If you need to share a transcript
snippet to reproduce a bug, redact character names and any explicit
content. The issue template defaults the "audio attached?" checkbox to NO
and reminds you on submit.

For privacy-sensitive disclosures (a leak, a misclaim in this document, a
gap between the code and a stated promise here), prefer the security
contact: see `SECURITY.md` once published, or open an issue with the
"privacy" label.

---

## Provenance

This document is the product of a privacy / safety / release-readiness
audit performed on the 5,900-line `dictation-server` source plus the
`dictation-bridge` ST extension. Every PASS / FAIL claim is grounded in a
specific source line or `grep` query, not hand-waved.

Phase 1 + 2 + 3 of the Calliope roadmap are shipped as of the date above —
bearer-token auth, loopback default, CORS allowlist, sandboxed systemd
unit, monkey-patched outbound audit log, in-RAM transcripts, ephemeral
audio temp files, persistent `whisper-server` (MVP-8), streaming SSE
partials (MVP-13), and the privacy badge + audit modal in the phone UI
(MVP-23) are all live. Phase 4 (release-readiness, wizard, packaging) is
in progress.
