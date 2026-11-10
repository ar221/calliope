# Privacy

**Last updated:** 2026-11-10

Calliope is built on a simple promise: **your audio never leaves the
machines you own — it goes from the capturing device to your own PC and
no further — and transcripts live in server RAM only.**

This document is a verifiable claim. Every statement here can be
cross-checked against the source at `server/calliope-server` in this
repository. If you find a divergence between this doc and the code, the
code wins — please open an issue.

---

## What stays on your machine

### Audio

Captured on your phone (browser `MediaRecorder` over
`navigator.mediaDevices.getUserMedia`), in the SillyTavern extension, or on
the desktop by an external hotkey client (`pw-record` via PipeWire). Phone
and extension audio **does leave the capturing device**: it is uploaded over
HTTPS — loopback, your LAN, or your tailnet — to the Calliope server on your
PC, and nowhere else. The upload is `POST /transcribe` with the recording as
the raw body (`audio/webm`, `audio/mp4`, `audio/ogg` or `audio/wav`; capped
at `DICTATION_MAX_AUDIO_BODY_BYTES`, default 25 MB). The server converts it
with `ffmpeg` to a 16 kHz mono WAV (a take longer than
`DICTATION_MAX_AUDIO_SECONDS`, default 600 s, is rejected) and transcribes it with
whisper.cpp (`whisper-server`, or the `whisper-cli` fallback) on your
machine.

Both temp files (`/tmp/dictation-*`) are deleted in the `finally` block of
`/transcribe`, on success and on every error path. Calliope never uploads
audio to any cloud service; there is no code path that does so. The setup
wizard's self-test recording lives in a private per-run temp directory that
is removed on every exit path.

The packaged `dictation-server.service` sets `PrivateTmp=yes`; if `/tmp` is
`tmpfs` (the default on most modern Arch / CachyOS / Fedora installs), the
audio never touches a physical disk. Verify with `findmnt /tmp`.

### Transcripts

Held in memory only — `session_transcript` lives in the server process and
is never written to disk. Restart the server and the log is gone
(restart-on-failure in the unit does not preserve it). Each entry is tagged
with the SillyTavern chat it came from.

The phone PWA and the `dictation-bridge` ST extension expose:

- `DELETE /transcript` — clear the log **except starred entries**.
- `DELETE /transcript?all=1` — clear everything, starred included.
- `DELETE /transcript?chat=<id>` — clear one chat's unstarred entries
  (`&all=1` includes its starred ones).
- `DELETE /transcript/<id>` — clear a single entry.

Starring an entry is how you keep it past an ordinary clear — until the
server restarts.

### Vocabulary, modes, character → mode memory

Stored in `~/.local/share/dictation-server/*.yaml` on your machine, written
only when you change them via the phone UI or `POST /vocab`,
`POST /state/mode-memory`. These files are local-only; no part of Calliope
syncs them anywhere.

### Cert + token

Stored in `~/.local/share/dictation-server/`:

- `cert.pem` (mode 0644), `key.pem` (mode 0600) — self-signed TLS cert,
  90-day validity, regenerated when fewer than 7 days remain (checked at
  startup and every 12 hours while running). Externally issued certs
  (Tailscale, your own CA) are never overwritten.
- `token` (mode 0600) — 32-byte bearer token; see
  [Authentication](#authentication) for which requests need it.
  `dictation-server --rotate-token` rotates it without printing the new
  value; treat the file contents as a password.
- `cert.fingerprint` — SHA-256, also printed at server startup.

Treat this directory like `~/.ssh/`. Exclude it from cloud backups (Google
Drive, Dropbox, Syncthing public folders, Backblaze without per-folder
exclusion). The token is a credential.

---

## What may leave your machine (only if you opt in)

### Dictated text and RP context — not audio — to OmniRoute

Formatter modes send *text* (never audio) to the local OmniRoute gateway
(`DICTATION_OMNIROUTE_URL`, default `http://127.0.0.1:20128/v1`),
authenticated with `OMNIROUTE_API_KEY`. OmniRoute relays each request to the
provider named by the catalog-qualified model ID, and that provider's
privacy policy applies. Calliope has no direct-provider or unauthenticated
fallback. What each built-in mode puts in the request (a *pass* is one
formatter step; each pass may try several models — and so several upstream
providers — down its fallback chain if earlier ones fail):

| Mode | Passes | Text sent upstream |
|---|---|---|
| `plain`, `command` | 0 | Nothing. Hallucination filter and voice commands run locally. |
| `grammar_clean` | 1 (cleanup chain) | The dictation and the grammar prompt only. |
| `rp_format` | 1 (RP chain) | The dictation; plus the last AI message only if the mode has *use chat context* on (off by default). |
| `rp_enhance` | 1 (RP chain) | The dictation, and — each only when the matching mode switch is on — the persona description (*use persona*), a condensed character card (*use character*), recent ST chat history read from disk, the last AI message, and this chat's earlier dictations (*use chat context*). A scene contract from a fresh SillyTavern state for the same character/chat. |
| `persona_pov` | 1–2 | Optional disfluency cleanup (cleanup chain, 12–500 words): the dictation only. Then the RP-chain pass: system prompt = persona description (*use persona*) + condensed character card (*use character*) + ST chat history and scene contract (*use chat context*); user content = the (cleaned) dictation only. If the persona or card is missing or switched off, it falls back to the `rp_enhance` assembly under the same switches. |
| `narrator_past`, `narrator_present` | 1–2 | Optional disfluency cleanup (dictation only), then an RP-chain pass with the same switch-gated context as `rp_enhance` and a narrator system prompt. |

"Earlier dictations" are limited to entries tagged with the current chat.
User-defined modes choose their own pipeline and switches; check them in
the mode editor. Client-supplied context (the last AI message) travels in
the `X-Dictation-Context` header, never in the URL, so it does not appear in
journaled request lines. The journal logs lengths, ids and timings, never
dictated, chat or TTS text.

`plain` and `command` never contact a provider — pick `plain` for hard
certainty that no text leaves your machine. An `OOC:` prefix in a formatter
mode reroutes that utterance to `grammar_clean` (dictation only, no
persona/chat context); in `plain` the prefix is kept verbatim and nothing
is sent.

### Active provider visibility

The server's outbound calls are observable:

- **At startup:** the log line declares the OmniRoute endpoint, whether a key
  was loaded, and the configured RP model chain without exposing the key.
- **At runtime:** `GET /audit/network` returns a ring buffer of the last 200
  outbound connection events with timestamp, host, port, byte count, and
  whether the destination was loopback (MVP-22). The phone PWA exposes this
  behind the privacy badge in the header (MVP-23). Non-loopback destinations
  are flagged. Note that OmniRoute itself is loopback: the hop from OmniRoute
  to the upstream provider is not visible here, only in OmniRoute's own logs.
- The startup banner pins the configured OmniRoute URL so a misconfigured
  upstream cannot silently re-route.

### Model download only during setup

The Whisper model is downloaded once, by you, in stage 2 of
`dictation-server --setup` (from Hugging Face's `ggml-org/whisper.cpp`
repository, after a confirmation prompt; `large-v3-turbo` is SHA-256
pinned, other sizes are size-checked). The running server never downloads
models and has no version-check endpoint.

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
- **Unauthenticated remote access.** See [Authentication](#authentication).

---

## Authentication

The bearer token is generated at first run and stored at
`~/.local/share/dictation-server/token` (mode 0600). The policy
(`require_auth` in `server/calliope-server`):

| Request | Needs the bearer? |
|---|---|
| From loopback (`127.0.0.0/8`, `::1`) with no `Origin`, or an allowlisted `Origin` | No — local tools and the extension are trusted. A loopback request carrying a non-allowlisted `Origin` (a random web page) must authenticate like a remote one. |
| `GET /`, `/health`, `/manifest.json`, `/icon-*.png` | No, but without the token a remote caller gets only a minimal pairing page / health payload. |
| `POST /pair/exchange` | No — the one-time pairing code is the credential. |
| `POST /pair/bootstrap` (mint a pairing code) | Yes, as an `Authorization` header, even from loopback. |
| `GET /events` (SSE) | `?token=` query parameter (browsers' `EventSource` cannot set headers) or loopback. |
| `OPTIONS` (CORS preflight) | No. |
| Everything else | `Authorization: Bearer <token>`. |

Pairing URLs carry a short-lived one-use code, never the durable token. JSON
API responses (`send_json`, including JSON errors) and the dynamically
rendered phone page send `Cache-Control: no-store`; the static UI page uses
`private, no-cache` with an ETag, 401 responses carry no cache header, and
the SSE stream sends `no-cache, no-transform`. The phone UI pages also send
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and a
`Content-Security-Policy` `frame-ancestors` restriction.

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

Report locally to the maintainer/operator until the repository is published.
Do not attach audio, full settings dumps, bearer tokens, or tokenized URLs.

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

Phase 1–5 of the 2026-06 completion queue are represented in this tree:
bearer-token auth, loopback default, CORS allowlist, sandboxed systemd
unit templates, monkey-patched outbound audit log, in-RAM transcripts,
ephemeral audio temp files, persistent `whisper-server` (MVP-8), formatter
SSE text deltas (MVP-13), privacy badge + audit modal (MVP-23), local
pairing QR rendering, safe token rotation CLI, group-chat synthetic QA,
streaming-partial TTS deferral, packaging hygiene, and privacy-preserving
issue templates. Packaging channels are prepared but not published.
