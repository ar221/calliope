# Architecture

Calliope is a single-file Python server that orchestrates four moving
parts: the phone PWA, the SillyTavern `dictation-bridge` extension,
`whisper-server` (whisper.cpp HTTP daemon), and an LLM proxy. The
canonical source is `server/calliope-server` (large lines, Python
stdlib + optional PyYAML).

## Diagram

```
                                ┌────────────────────────────────────┐
                                │  Phone (LAN client / PWA)          │
                                │  MediaRecorder + AnalyserNode VAD  │
                                │  https://<host>:8384/  (Apollo UI) │
                                └────────────┬───────────────────────┘
                                             │ HTTPS · Bearer token
                                             │ POST /transcribe (audio)
                                             │ POST /send-to-st  (commit)
                                             │
┌────────────────────────────────────────────▼────────────────────────────────────┐
│  Calliope server  ·  server/calliope-server  ·  ThreadingHTTPServer + ssl     │
│  bind 127.0.0.1:8384 (default) · self-signed cert (90d) · bearer-token auth     │
│                                                                                  │
│  ┌──────────────┐    ┌─────────────────┐    ┌────────────────────────────────┐  │
│  │ DictationHdlr│───▶│ run_pipeline()  │───▶│  formatter_request(provider)   │  │
│  │ /transcribe  │    │  vocab_correct  │    │   ↳ Claude shape (:42069)      │  │
│  │ /reformat    │    │  disfluency     │    │   ↳ OpenAI shape (:10531)      │  │
│  │ /events SSE  │    │  rp_format      │    └────────────────────────────────┘  │
│  │ /audit/net   │    │  rp_enhance     │                                        │
│  │ /personas    │    │  persona_pov    │    ┌────────────────────────────────┐  │
│  │ /characters  │    └─────────────────┘    │  whisper_server_request()      │  │
│  │ /chat-context│             │             │   POST http://127.0.0.1:9001   │  │
│  │ /state       │             ▼             │     /inference (multipart)     │  │
│  └──────────────┘    ┌─────────────────┐    └────────────────────────────────┘  │
│                      │  ChatReader     │                                        │
│                      │  (read-only,    │    ┌────────────────────────────────┐  │
│                      │   ST disk)      │    │ session_transcript: list[dict] │  │
│                      └─────────────────┘    │   in-RAM only · DELETE wipes   │  │
│                                              └────────────────────────────────┘  │
└──────┬─────────────────────────────────────────────────┬─────────────────────────┘
       │                                                 │
       │ SSE: dictation-result · dictation-state ·       │ subprocess (cold-spare)
       │      dictation-token   · dictation-command       │   $HOME/.local/bin/dictate
       ▼                                                 ▼
┌────────────────────────────────┐         ┌────────────────────────────┐
│  ST tab + dictation-bridge ext │         │  whisper-server (whisper.  │
│  manifest.json · index.js      │         │  cpp HTTP daemon, GPU)     │
│  state-machine bar             │         │  large-v3-turbo · HIP/ROCm │
│  voice-edit cheatsheet         │         │  127.0.0.1:9001 · idle-    │
│  privacy badge · audit peek    │         │  shutdown after N seconds  │
│  EventSource → /events         │         └────────────────────────────┘
└────────────────────────────────┘
                                            ┌────────────────────────────┐
                                            │  LLM proxy (one of):       │
                                            │  · claude-code-proxy :42069│
                                            │    pyrite preset · → claude│
                                            │  · OpenAI shape :10531     │
                                            │    grammar / disfluency    │
                                            └────────────────────────────┘
```

## Components

### Phone PWA (origin: `https://<host>:8384/`)

Single-document HTML+CSS+JS embedded in the server source as `WEB_UI`.
Apollo-themed (POL-5 shipped 2026-04). No third-party JS, no CDN, no
external assets. Keep the single-file server model intact; the embedded
`<script>` is extracted only by `scripts/check-web-ui-js` for `node --check`.

- **Capture:** `navigator.mediaDevices.getUserMedia({audio: true})` →
  `MediaRecorder` with `audio/webm;codecs=opus`.
- **VAD:** browser-side energy VAD (`AudioContext` + `AnalyserNode`,
  fftSize=512, RMS → dB). User-tunable threshold + silence duration.
- **Visualizer:** 24-bar VU meter + waveform sparkline.
- **State machine:** `idle → listening → transcribing → cleaning → done`.
  States surfaced via the bar above the textarea (MVP-16) and via SSE
  events emitted at pipeline transitions.
- **ST-follow freshness:** when opened from ST, the PWA refreshes `/state`
  on foreground lifecycle events, once before recording starts, and again
  before audio is submitted so mobile tab freezing does not leave the phone
  formatting against an old chat/persona snapshot.
- **Privacy badge:** chip in the header expands to an audit peek
  (last-50 outbound calls). Pulls from `GET /audit/network`. (MVP-23.)

### `dictation-bridge` ST extension

Lives at `sillytavern/extensions/dictation-bridge/` in this repo, deployed
under `<ST install>/data/default-user/extensions/third-party/`. Three files:

- `manifest.json` — ST extension manifest, `auto_update: false`.
- `index.js` — mic button injected into `#send_form`, popup or iframe
  to phone UI, SSE subscriber via `EventSource('/events')`, undo stack
  (cap 8), state-machine bar wiring, voice-edit overlay, privacy peek,
  local pairing QR Canvas render, and ST-state broadcasts on
  chat/lifecycle/mic-open events.
- `qrcodegen.min.js` — vendored Nayuki MIT browser QR library used only in
  the settings panel for local pairing QR generation.
- `style.css` — Apollo-matched theming for the bar and overlay.

Reads the bearer token from ST settings; sends on every fetch and as
`Authorization` query param when opening EventSource (since browsers
won't set headers on EventSource constructor).

### Calliope server (`server/calliope-server`)

Single Python file. Stdlib `ThreadingHTTPServer` + `ssl.SSLContext` over
self-signed cert (90-day validity, auto-renew at startup, fingerprint
printed). One `DictationHandler` class dispatches by path.

**Trust boundaries:**

- **Boundary 1: phone / extension → server** — HTTPS, bearer token
  required on every endpoint except `/health`. CORS allowlist
  (configurable, defaults to `https://localhost:*`, `https://127.0.0.1:*`,
  configured ST host). Default bind `127.0.0.1`; LAN bind opt-in.
- **Boundary 2: server → whisper-server** — loopback HTTP, no auth (the
  whisper-server bind is loopback-only; not exposed). Boundary is the
  process boundary, not a network boundary.
- **Boundary 3: server → LLM proxy** — loopback HTTP. Whether the proxy
  itself reaches the cloud depends on which proxy you've installed. The
  startup banner declares which proxies are reachable; `/audit/network`
  records outbound calls.
- **Boundary 4: server → ST data dir** — direct filesystem reads (mode
  0644 from ST's perspective). systemd unit declares
  `ReadOnlyPaths=/mnt/hdd/AI/SillyTavern/data` so a server compromise
  can't write back into ST.

**State surfaces:**

- `session_transcript: list[dict]` — in-RAM, never written to disk.
  Wiped on restart. `DELETE /transcript` clears.
- `~/.local/share/dictation-server/{vocab,modes,char-modes}.yaml` — only
  written when user changes them via API.
- SSE subscriber list — `_subscribers: list[Queue]` with bounded
  per-subscriber queue (size 50), drop-on-full.

### `whisper-server` (whisper.cpp HTTP daemon)

Persistent daemon, replaced subprocess-per-request in MVP-8 (Phase 2).
Binds `127.0.0.1:9001`, idle-shutdown after configurable seconds. Loaded
model: `large-v3-turbo` (1.6 GB). GPU backend: HIP/ROCm (verified via
`ldd`: `libggml-hip.so.0`, `libhipblas.so.3`, `librocblas.so.5`,
`libamdhip64.so.7`).

Calliope sends multipart audio to `/inference`. Hallucination filter
(MVP-12) runs over the result; word-confidences (MVP-9) surface in the
phone UI for low-confidence highlighting.

### LLM proxy (one of two shapes)

- **Claude shape** at `http://localhost:42069` — `claude-code-proxy`
  (upstream `horselock/claude-code-proxy`, MIT). Used for `rp_enhance`
  and `persona_pov` via the pyrite preset (`/v1/pyrite/messages`).
  The proxy itself relays to `claude.ai` over OAuth — that's the cloud
  hop disclosed in PRIVACY.md.
- **OpenAI shape** at `http://127.0.0.1:10531/v1` — used for
  `grammar_clean` and `disfluency_clean`. Whether this is local depends
  on what proxy the user has installed there.

Both routes go through `formatter_request(provider, ...)` which maintains
the outbound-call ring buffer that `/audit/network` exposes.

## Data flows

### `POST /transcribe` (primary path)

```
phone/extension                server                         whisper-server   LLM proxy
      │                          │                                │                │
      │  POST /transcribe        │                                │                │
      │  audio/webm + Bearer     │                                │                │
      ├─────────────────────────▶│                                │                │
      │                          │  validate token + CORS         │                │
      │                          │  emit dictation-state:listening│                │
      │                          │  ffmpeg webm→wav 16k mono      │                │
      │                          │  build_whisper_prompt(char)    │                │
      │                          │  POST /inference (multipart) ─▶│                │
      │                          │                                │  whisper.cpp   │
      │                          │  {text, segments, confs}    ◀──│  HIP/ROCm      │
      │                          │  emit dictation-state:cleaning │                │
      │                          │  run_pipeline(mode):           │                │
      │                          │   vocab_correct   (local)      │                │
      │                          │   disfluency_clean ── HTTP ───────────────────▶│
      │                          │   rp_enhance      ── HTTP ───────────────────▶│
      │                          │  emit dictation-token (SSE) for streaming     │
      │                          │  emit dictation-state:done     │                │
      │  200 {final, partials}   │                                │                │
      │◀─────────────────────────┤                                │                │
      │                          │  append to session_transcript  │                │
      │  SSE: dictation-result   │  (in-RAM)                      │                │
      │◀─────────────────────────┤                                │                │
      │                          │  unlink temp wav (finally)     │                │
```

### SSE pipeline (`GET /events`)

EventSource subscribers receive a stream of named events. Per-subscriber
bounded queue (size 50), drop-on-full to avoid backpressure stalling the
producer.

| Event name | Payload | Producer |
|---|---|---|
| `dictation-state` | `{state: idle\|listening\|transcribing\|cleaning\|done, mode, ts}` | pipeline transitions |
| `dictation-transcript` | `{requestId, phase, text, source, latency_ms?}` | raw Whisper preview before formatter work |
| `dictation-token` | `{requestId, delta, done}` | streaming formatter output (MVP-13) |
| `dictation-result` | `{requestId, text, raw, cleaned, mode, timing, formatting_skipped?}` | completed dictation payload |
| `dictation-edit` | `{text, auto_send?}` | phone "Send to ST" button → fan-out |
| `dictation-command` | `{cmd, args, raw}` | voice-edit grammar (POL-15) |

### `POST /send-to-st` (phone → extension fan-out)

Phone tap of "Send to ST" → server validates token → server emits
`dictation-edit` event over SSE → all subscribed extension instances
write into `#send_textarea`. Server is stateless for this path; it just
fans out.

## Performance characteristics

(See `docs/roadmap.md` ADR-1, ADR-9 for the full latency budget.)

| Stage | Median (rp_enhance mode) | Notes |
|---|---|---|
| ffmpeg webm→wav | 50–150 ms | phone path only; desktop is native WAV |
| whisper-server `/inference` | 300–800 ms (5s clip) | GPU warm; cold first call ~2s extra |
| `vocab_correct` | <10 ms | regex + difflib, in-process |
| `disfluency_clean` | 800–2000 ms | LLM HTTP, 3s timeout, skipped <12 words (MVP-15) |
| `rp_enhance` | 2000–5000 ms | LLM HTTP, 5s timeout (MVP-7), pyrite preset |
| SSE fan-out | <5 ms | `Queue.put_nowait` |
| **Total** | **3–7 s** for `rp_enhance` | LLM dominates |

Per-`/transcribe` `timing` JSON is logged at the end of the request
(MVP-POL-16) — `journalctl --user -u dictation-server | grep timing`
extracts a per-request stage breakdown.

## What's not in this diagram

- Push-to-talk desktop client (`scripts/dictate`, ~490 lines bash) — when
  invoked with `--mode`, it POSTs to `/transcribe` like any other client.
  When invoked without, it shells `whisper-cli` directly and bypasses
  the server. Still useful for offline-only workflows.
- Footswitch daemon (WOW-7, not yet shipped) — would be another
  push-to-talk client targeting `/transcribe`.
- AUR / pipx packaging (MVP-25, in progress) — distribution-only, no
  runtime architecture impact.
