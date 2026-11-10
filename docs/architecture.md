# Architecture

Calliope is a single-file Python server that orchestrates four moving
parts: the phone PWA, the SillyTavern `dictation-bridge` extension,
`whisper-server` (whisper.cpp HTTP daemon), and an LLM proxy, plus an
optional Kokoro TTS read-back proxy. The canonical source is
`server/calliope-server` (large lines, Python stdlib + optional PyYAML).

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
│  │ DictationHdlr│───▶│ run_pipeline()  │───▶│ OmniRoute :20128               │  │
│  │ /transcribe  │    │ vocab_correct   │    │ authenticated chat completions │  │
│  │ /reformat    │    │ disfluency/RP   │    │ catalog-qualified model IDs    │  │
│  │ /events SSE  │    │ persona shaping │    └────────────────────────────────┘  │
│  │ /audit/net   │    └─────────────────┘                                        │
│  │ /personas    │             │             ┌────────────────────────────────┐  │
│  │ /characters  │             │             │ whisper_server_request()       │  │
│  │ /chat-context│             │             │  POST http://127.0.0.1:9001    │  │
│  │ /state       │             ▼             │    /inference (multipart)      │  │
│  └──────────────┘    ┌─────────────────┐    └────────────────────────────────┘  │
│                      │ ChatReader      │                                         │
│                      │ (read-only, ST) │    ┌────────────────────────────────┐  │
│                      └─────────────────┘    │ session transcript: RAM only   │  │
│                                             └────────────────────────────────┘  │
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
                                            │ OmniRoute :20128           │
                                            │ model-chain routing        │
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
  required except for trusted loopback requests and the pairing/bootstrap
  surfaces (`/`, `/health`, `/manifest.json`, `/pair/exchange`; `/events`
  takes `?token=`) — full matrix in
  [PRIVACY.md → Authentication](../PRIVACY.md#authentication). CORS allowlist
  (configurable, defaults to `https://localhost:*`, `https://127.0.0.1:*`,
  configured ST host). Default bind `127.0.0.1`; LAN bind opt-in. The phone
  UI is served with `nosniff`, `no-referrer` and a CSP `frame-ancestors`
  policy; API responses are `Cache-Control: no-store`.
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

### OmniRoute formatter gateway

All cloud text formatting uses authenticated OmniRoute at
`http://127.0.0.1:20128/v1`. Calliope sends OpenAI-shape chat completions with
catalog-qualified provider/model IDs, retains explicit user model choices as
single-model requests, and uses separate RP and cleanup fallback chains for
automatic selection. The outbound-call ring buffer exposed by `/audit/network`
continues to record non-loopback destinations. Audio never enters this route.

### Kokoro TTS read-back (optional)

The ST extension can request non-streaming read-back through Calliope's
Kokoro proxy:

- `POST /tts` returns one complete WAV/audio response for the requested text.
- `GET /tts/voices`, `POST /tts/voices/suggest`, and `POST /tts/audiobook`
  support voice profiles, samples, suggestions, and bounded audiobook export.
- `ttsReadStreamingPartials` defaults false but is a real opt-in extension
  toggle. It observes the SillyTavern assistant message DOM while text is still
  appearing, cuts complete sentence chunks, and queues repeated authenticated
  `POST /tts` WAV requests. This is **extension-side queued read-back**, not a
  server `/tts/stream` endpoint.
- The quick-launch card exposes a redacted TTS stream status row with queue
  count, chunk preview, stop/skip/pause/reread controls, and a diagnostics
  panel that separates server, token, SSE, ST-state, Whisper, Kokoro, and
  formatter/audit status without rendering tokens or private chat text.

## Data flows

### `POST /transcribe` (primary path)

```
phone/extension                server                         whisper-server   LLM proxy
      │                          │                                │                │
      │  POST /transcribe        │                                │                │
      │  audio/webm|mp4|ogg|wav  │                                │                │
      │  (POST /warm at capture start boots whisper-server)       │                │
      ├─────────────────────────▶│                                │                │
      │                          │  validate token + CORS         │                │
      │                          │  emit dictation-state          │                │
      │                          │  ffmpeg any→wav 16k mono       │                │
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
      │  200 {text, raw, cleaned, entry_id, …}                    │                │
      │◀─────────────────────────┤                                │                │
      │                          │  append to session_transcript  │                │
      │  SSE: dictation-result   │  (in-RAM)                      │                │
      │◀─────────────────────────┤                                │                │
      │                          │  unlink temp wav (finally)     │                │
```

### SSE pipeline (`GET /events`)

EventSource subscribers receive a stream of named events. Per-subscriber
bounded queue (size 50), drop-on-full to avoid backpressure stalling the
producer. `dictation-transcript` is a phase marker that never carries raw
Whisper text. Other events do carry text: `dictation-token` deltas
(formatter output), `dictation-result.text`, the `raw`/`cleaned` fields of
a `/send-to-st` result, and `dictation-command.source_text` / `residual`.

| Event name | Payload | Producer |
|---|---|---|
| `dictation-state` | `{requestId, state, ts, details?}` | pipeline transitions (`listening`, `transcribing`, `cleaning`, `done`, …) |
| `dictation-transcript` | `{requestId, phase, has_text: true, source: "whisper", ts, …}` | Whisper phase marker — **no text** |
| `dictation-token` | `{requestId, delta, done}` | streaming formatter output (MVP-13), not TTS audio |
| `dictation-result` | `{requestId, text, mode, formatting_skipped, formatting_reason, model, model_fallback, model_tier, is_ooc, mode_override, auto_send, ts, …}` | end of an enhancement-mode `/transcribe` (`has_repair_trace` flag, no trace), and phone **Send to ST** via `POST /send-to-st` (adds `raw`, `cleaned`) |
| `dictation-command` | `{requestId, intent, args, source_text, residual, ts}` | voice-edit grammar (POL-15) |
| `ping` | `{ts}` (epoch seconds) | liveness, every 15 s, alongside a `: heartbeat` comment |

Clients treat any event, including `ping`, as liveness; the extension
reconnects when nothing has arrived for 45 s.

### `POST /send-to-st` (phone → extension fan-out)

Phone tap of "Send to ST" → server validates token → server emits
`dictation-result` (with a fresh `requestId` and `auto_send`) over SSE → the
owning extension instance writes into `#send_textarea`. Server is stateless
for this path; it just fans out.

## Performance characteristics

(See `docs/roadmap.md` ADR-1, ADR-9 for the full latency budget.)

| Stage | Median (rp_enhance mode) | Notes |
|---|---|---|
| ffmpeg webm→wav | 50–150 ms | phone path only; desktop is native WAV |
| whisper-server `/inference` | 300–800 ms (5s clip) | GPU warm; cold first call ~2s extra |
| `vocab_correct` | <10 ms | regex + difflib, in-process |
| `disfluency_clean` | 800–2000 ms | LLM HTTP, 3s timeout, skipped <12 words (MVP-15) |
| `rp_enhance` | 2000–6000 ms | LLM HTTP, 18s per-attempt timeout (env-overridable), OmniRoute model chain; timeouts skip to the next tier, with a 40s retry bound on how many tiers are attempted |
| SSE fan-out | <5 ms | `Queue.put_nowait` |
| **Total** | **3–7 s** for `rp_enhance` | LLM dominates |

Per-`/transcribe` `timing` JSON is logged at the end of the request
(MVP-POL-16): `journalctl --user -u dictation-server -g 'timing \{'`. The
line carries `req`, `mode`, `audio_s`, `whisper_cold` and `stages_ms`,
whose values are milliseconds since the request arrived, insertion-ordered:
`context` (chat context built), `upload` (body read), `ffmpeg`,
`whisper_boot` (only when whisper-server had to start), `whisper`, then
formatter stamps — `ttft` (first streamed token), `step_*`. The
difference between neighbouring keys is that stage's cost. No text is
logged.

## What's not in this diagram

- External push-to-talk desktop client (`dictate`, **not in this
  repository** — see [`desktop-hotkey.md`](desktop-hotkey.md)) — when
  invoked with `--mode`, it POSTs to `/transcribe` like any other client.
  When invoked without, it shells `whisper-cli` directly and bypasses
  the server.
- Footswitch daemon (WOW-7, not yet shipped) — would be another
  push-to-talk client targeting `/transcribe`.
- AUR / pipx packaging (MVP-25) — distribution-only, no runtime architecture
  impact. Both package paths preserve `server/calliope-server` as the
  canonical single-file source; package wrappers/adapters point at that
  artifact instead of splitting the embedded PWA into a separate browser file.
