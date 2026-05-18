# Dictation Bridge

A SillyTavern extension that wires the self-hosted dictation server
(`~/.local/bin/dictation-server`, HTTPS on port 8384) directly into ST's
chat input, replacing the current copy-paste workflow.

## What it does

Tapping a mic button in the chat send bar opens the dictation server's web
UI (popup window by default, modal iframe optional) with the active chat,
persona, and character IDs passed as query parameters. The server uses
those IDs to pull live ST context and format the dictated line in-persona.
When dictation completes, the server now has two delivery paths:

- primary live path: `dictation-result` over `/events` SSE, which lets the
  extension write directly into `#send_textarea` even when the phone page is
  standalone; and
- embed fallback: `window.postMessage` from the popup/iframe to this extension.

Both paths can optionally fire the Send button automatically.

This closes the loop on Ayaz's primary dictation flow: dictate on the phone
at `https://<pc-ip>:8384`, format/enhance in-persona on the PC, land in ST
with one click — no clipboard round-trip.

## Install

Third-party extensions live under SillyTavern's user extension tree. The
current deployment target is:

```
<ST-root>/data/default-user/extensions/third-party/dictation-bridge/
```

Symlink or copy this `extension/` directory there, then hard-refresh
SillyTavern. Avoid tokenized URLs in screenshots or docs.

After the files are in place, reload SillyTavern (hard refresh the page).
The extension will load automatically; no install flow is needed because
the directory is already inside ST's extensions tree.

## Configuration

All settings live under ST's Extensions drawer → "Dictation Bridge":

| Setting | Default | Meaning |
|---|---|---|
| Server URL | `https://<current ST host>:8384` | Dictation server URL. Defaults to the same hostname used for SillyTavern, with port `8384`, so LAN/mobile ST sessions follow IP changes automatically. |
| Server bearer token | blank | Paste the server token from `~/.local/share/dictation-server/token`; the status chip distinguishes missing, valid, invalid/stale, and unknown tokens. |
| Re-pair this phone / Copy pairing URL | n/a | Opens or copies a fresh tokenized phone URL for the active ST chat/persona/character. Use copy as the source for a QR handoff; treat it like a password. |
| Open style | Popup | Popup spawns a small window next to the chat; iframe overlays a modal inside ST. Popup is recommended for the phone workflow since you're typically holding the phone separately. |
| Text handling | Replace | `Replace` overwrites the current textarea, `Append` adds to whatever's already there. |
| Auto-send after dictation | off | If on, the formatted result both fills the textarea and clicks the Send button. |
| Push last AI message as tonal context | on | On `dictation-ready`, posts the most recent non-user message back to the server so the LLM formatter has tone to lean on. |
| Live mirror edits from server | off | Experimental — if the server sends `dictation-edit` on every keystroke, the textarea updates live. Currently off by default to avoid churn. |

### Phone workflow

From the phone browser, open a fresh paired page from the ST mic button or
from Extensions → Dictation Bridge → **Re-pair this phone**. **Copy pairing
URL** copies the same tokenized URL for QR/phone handoff. The server scrubs
`?token=` from browser history after load, but copied pairing URLs should be
treated like passwords. Standalone phone pages receive results back to ST via
SSE; embed mode additionally uses `postMessage` as a local fallback.

### Self-signed cert

The server ships its own self-signed cert. Before the mic button will
work, visit the server URL once in a regular browser tab on the same
machine as SillyTavern and accept the cert warning. The extension does a
`/health` probe before opening the UI and shows a toast if the probe
fails (cert rejection or server down).

## Limitations / known issues

- **Pairing state** — a page can be token-valid but not following the current SillyTavern chat if it was opened from an old bookmark/tab. Open from the refreshed ST mic button when the phone says stale/not paired.
- **Self-signed cert** — one-time trust prompt on every browser profile.
  Some browsers will silently block cross-origin `fetch` to an untrusted
  cert; the probe will fail cleanly and instruct the user.
- **Popup blockers** — if popups are blocked for SillyTavern, either
  allow them or switch Open style to "iframe".
- **Mixed content** — the server is HTTPS, so ST at `http://` would
  normally be fine. If ST is behind HTTPS with strict CSP, the iframe
  may be blocked; the popup window bypasses page-level CSP.
- **Grouped chats** — current builds send group id, members, and last speaker; use the addressee picker when you need a specific group member voice. Edge cases should be treated as group-QA bugs, not a missing core feature.
- **Third-party loading** — if ST's extension loader doesn't find the
  `third-party/` subdirectory convention, move the folder one level up
  into `extensions/` and reload.

## Development notes

### postMessage contract

Both halves must agree on the message shape. The server emits the
following to the parent window (this extension):

```js
// ready to receive context, UI is wired up
{ type: 'dictation-ready' }

// final formatted result (primary payload)
{
  type: 'dictation-result',
  text: string,              // formatted, in-persona output — this is what lands in ST
  raw: string,               // optional, original transcript before formatting
  mode: string,              // optional, mode ID that was used (e.g. 'rp-enhance')
  formatting_skipped: boolean, // true if RP+ fell back to plain pass-through
  formatting_reason: string    // optional explanation shown in toast
}

// live-edit mirror (optional, only acted on when liveMirror is enabled)
{ type: 'dictation-edit', text: string }
```

The extension emits back:

```js
// tonal context from ST's current chat
{ type: 'dictation-set-context', context: string }

// optional mode switch — not wired to UI yet
{ type: 'dictation-set-mode', mode: string }
```

Origin is verified on inbound messages against the configured `serverUrl`
origin, so pages in other tabs can't inject fake results.

### Debugging

Open the browser devtools on SillyTavern and filter the console for
`[dictation-bridge]`. The extension logs:

- `initialized` — at boot
- `server ready` — when the iframe/popup sends `dictation-ready`
- `server probe failed` — when `/health` fetch errors
- `send_textarea not found` / `send_but not found` — if ST's DOM has
  changed unexpectedly

The server side has its own logging; run the server in a terminal (or
`journalctl --user -u dictation-server -f`) to watch the embed handshake
from the other end.

### Files

- `manifest.json` — extension manifest (ST loads this first)
- `index.js` — main logic (ES module, no build step)
- `style.css` — mic button and modal styling
- `README.md` — this file

No dependencies. jQuery is available globally via ST but the extension
avoids it where vanilla DOM is equivalent.
