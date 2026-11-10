# Dictation Bridge

A SillyTavern extension that wires the self-hosted Calliope dictation server
(HTTPS, port 8384 by default) into ST's chat input, replacing the
copy-paste workflow.

## What it does

The mic button in the chat send bar has three modes (Extensions → Dictation
Bridge → **Microphone action**):

- **Phone popup** (default) — opens the Calliope phone page in a popup window
  (or a modal iframe on desktop) paired to this ST tab with a one-time code.
- **Desktop push-to-talk** — hold the mic (pointer, or Space/Enter while it is
  focused) to record in this browser; release to transcribe.
- **Desktop toggle** — click to start, click again to stop.

Desktop capture records with `MediaRecorder` in the first container the
browser supports (`audio/webm;codecs=opus`, `audio/mp4`, `audio/ogg;codecs=opus`)
and uploads it once to `POST /transcribe`; the server converts it with ffmpeg.
Audio lives only in memory and in that request body, and recording stops
after 60 seconds. Capture start also sends a fire-and-forget `POST /warm` so
whisper can boot while you speak.

Finished text reaches ST in exactly one way: the authenticated `/events`
Server-Sent Events stream (`dictation-result`), plus the `/transcribe`
response for desktop captures. Both go through the same per-request dedupe
and cross-tab ownership, so a dictation is inserted once. Formatter output can
stream into the textarea as it is generated (`dictation-token`); the final
result replaces the streamed text.

## Install

Third-party extensions live under SillyTavern's user extension tree:

```
<ST-root>/data/default-user/extensions/third-party/dictation-bridge/
```

Symlink or copy this `extension/` directory there and hard-refresh
SillyTavern. A symlink means every edit here goes live on the next refresh.

## Configuration

All settings live under ST's Extensions drawer → "Dictation Bridge":

| Setting | Default | Meaning |
|---|---|---|
| Server URL | `https://<current ST host>:8384` | Defaults to the hostname ST is opened on, so LAN/Tailscale sessions follow automatically. Changing it reconnects SSE and reloads the mode, model and voice lists. |
| Server bearer token | blank | Paste the server token (`~/.local/share/dictation-server/token` on the server). Status shows missing / valid / rejected (401) / unreachable separately. Changing it reconnects and reloads the lists. |
| Re-pair this phone / Copy pairing URL / Show local QR | n/a | Asks the server for a one-time pairing code (`POST /pair/bootstrap`) and opens, copies or renders a phone URL carrying `?pair=<code>`. The phone exchanges the code once (`POST /pair/exchange`); it expires after two minutes. The QR is drawn locally with the vendored Nayuki library; Hide clears the canvas. |
| Open style | Popup | Popup window or (desktop only) modal iframe for the phone page. |
| Microphone action | Phone popup | See above. The quick-launch "Dictation" button opens the phone page in phone mode and starts/stops a recording in both desktop modes. |
| Text handling | Replace | `Replace` overwrites the message box, `Append` adds below the existing text. |
| Auto-send after dictation | off | Click Send after inserting the result. |
| Push last AI message as tonal context | on | Desktop capture sends the last AI message (≤4,000 chars) in the `X-Dictation-Context` header; the phone page receives it via `dictation-set-context`. |
| Live mirror edits | off | Mirror `dictation-edit` messages from an embedded phone page into the textarea. |
| Broadcast ST state | on | `POST /state` on chat/character/persona changes, on focus/visibility, and a 30 s heartbeat sent only by the visible tab you used last. The last AI message is capped at 4,000 chars. A rejected update is reported (diagnostics, and a warning when launching the phone page). |
| Receive dictation via SSE | on | Subscribe to `/events`. The server pings every 15 s; after 45 s of silence the status shows *stale* and the extension reconnects. |
| Voice commands | on | Act on `dictation-command` events (send, undo, clear, read last, …). |
| Formatter mode (per character) | server default | Remembered per character through `/state/mode-memory`; desktop capture uses the mode of the character that was active when recording started. Disabled while the new character's mode loads. |
| Global model override / Model assigned to a mode | Auto | Sticky OmniRoute model override, and per-mode pins saved server-side. |

### Multiple tabs and chat switches

Every open ST tab receives the same events. Tabs arbitrate per request with
Web Locks (localStorage fallback); only opaque request IDs and timestamps
cross tabs. The tab that wins a streamed request holds a lock until its final
result is handled. Other tabs, including tabs opened mid-stream, wait for
that lock and only deliver the result if the owning tab closed without
delivering it.

Switching chats cancels a desktop recording or upload, removes partially
streamed text without restoring the previous chat's draft, and drops any
result, queued result, voice command or undo snapshot that belongs to the
previous chat. A request is tied to the chat generation that was open when
this tab first saw it (or, for desktop captures, when recording started), so
work begun in one chat does not land in the next. A phone result that this
tab first sees after a switch carries no originating chat id and is inserted
into whichever chat is open at that moment.

### Self-signed cert

Visit the server URL once in a normal tab on the same browser profile and
accept the certificate. The extension probes `/health` (8 s timeout) before
opening the phone page and explains whether the server is unreachable or the
token was rejected.

## Security notes

- The bearer token is stored in ST's extension settings and shown in a
  password field. Every other ST extension runs in the same origin and can
  read it; only install extensions you trust.
- `EventSource` cannot send headers, so the SSE connection carries the token
  as a `?token=` query parameter. Keep the server's request logging free of
  query strings and do not share `/events` URLs.
- Pairing URLs carry a short-lived one-time code, not the token; still treat
  copied URLs and QR screenshots as private until they expire.
- The embed `postMessage` handler only accepts messages whose origin is the
  configured server and whose source is the window this extension opened.
  Canonical dictation results arrive only over SSE or the desktop
  `/transcribe` response; the embedded phone page's live edits
  (`dictation-edit`, applied only when Live mirror is on) are the one case
  where dictated text travels over this origin- and source-checked
  `postMessage` channel.

The authentication matrix for every endpoint is documented in
`../PRIVACY.md#authentication`.

## Development notes

### Embed postMessage contract

Phone page → extension (origin and source checked):

```js
{ type: 'dictation-ready' }               // page wired up; extension replies with context
{ type: 'dictation-edit', text: string }  // optional live mirror (liveMirror setting)
```

Extension → phone page (target origin = configured server):

```js
{ type: 'dictation-set-context', context: string }
{ type: 'dictation-set-mode', mode: string }
```

### Server fields the extension reads

- SSE `dictation-result`: `requestId`, `text`, `mode`, `model`, `model_fallback`,
  `formatting_skipped`, `formatting_reason`, `is_ooc`, `mode_override`, `auto_send`.
- `/transcribe` response: the same plus `request_id`, `entry_id`,
  `raw`/`cleaned`/`repair_trace` and `low_confidence_spans`
  (`{word, start_idx, end_idx, confidence, logprob, alternatives, text_only?}`).
  When SSE delivered the text first, the response only adds the confidence
  chips, repair trace and entry id.
- `/word-alternatives`: `{alternatives: [{text, score, source}]}`.
- `/audit/network`: `{totals_60s, recent: [...], warning?}` (`?all=1` for the
  whole ring buffer).
- SSE `ping` (liveness) and `dictation-state` (`error` aborts a live stream).

Missing endpoints or fields on older servers are tolerated (`/warm` 404s are
ignored).

### Debugging

Filter the browser console for `[dictation-bridge]`. The Diagnostics button
on the quick-launch card checks `/health`, the token, SSE freshness, the last
accepted `/state` post, Kokoro and the audit endpoint in parallel, without
rendering tokens, URLs with codes, chat text or audio.

### Files

- `manifest.json` — extension manifest (ST loads this first)
- `index.js` — all logic (ES module, no build step)
- `qrcodegen.min.js` — vendored Nayuki QR Code generator (MIT), used only for local pairing QR rendering
- `style.css` — mic button, quick-launch card and modal styling
- `README.md` — this file

The only bundled dependency is `qrcodegen.min.js`, documented in
`../THIRD_PARTY_NOTICES.md`.
