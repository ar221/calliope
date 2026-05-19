# Mission — Calliope

Calliope gives SillyTavern a local-first, persona-aware voice dictation path.

Its job is to make phone and desktop speech input feel native to roleplay/chat workflows while keeping audio private, low-latency, and under Ayaz's control.

## In scope

- Local microphone capture from phone, browser/PWA, and desktop hotkeys.
- Local whisper.cpp transcription with GPU acceleration where available.
- Persona-aware text formatting for SillyTavern chats and group addressees.
- A SillyTavern bridge extension that follows active chat context and exposes clear state.
- Token-authenticated HTTPS service operation on loopback/LAN/Tailscale as configured.
- Privacy, security, setup, troubleshooting, and packaging documentation.
- Tests and hardening for server endpoints, bridge behavior, formatter routing, and service units.

## Out of scope

- Cloud audio transcription by default.
- Telemetry, auto-update, runtime model auto-downloads, or third-party browser JS in the phone UI.
- General-purpose dictation that ignores SillyTavern context.
- Storing transcripts or audio beyond explicit in-memory/runtime needs.
- Printing, exporting, or preserving private chats, tokens, cert keys, or bearer URLs in artifacts.
- Replacing SillyTavern or becoming a general chat frontend.

## Privacy promise

Audio should stay local. Plain mode should not send audio or transcript text to cloud providers. RP/formatter modes may send text to the configured formatter provider; the active provider and network path must be visible and auditable.

## Operating posture

Calliope is live-service software. Repository changes are not automatically live. Agents may edit and test the repo, but must not restart or redeploy Ayaz's running dictation service without explicit approval.

## Success criteria

A change is successful when:

1. It preserves local-first privacy boundaries.
2. It keeps phone, bridge, server, and formatter states understandable to the user.
3. It is verified with targeted tests or explicit smoke checks.
4. It does not expose secrets, tokenized URLs, private chat logs, or audio.
5. It names whether a service restart/sync is required before the change becomes live.
