# Calliope Roadmap

Status date: 2026-06-26.

## Shipped / working MVP+

- Phone/PWA dictation UI with HTTPS bearer-token auth.
- SillyTavern `dictation-bridge` extension with mic button and direct textarea insertion.
- Review-before-send flow; auto-send exists but defaults off.
- ST state broadcast to Calliope: active chat, character/group, persona, last AI message, scene continuity, and mode.
- Phone follow mode for fresh ST state.
- Persistent whisper.cpp HTTP daemon on `127.0.0.1:9001` with fallback paths.
- Formatter modes: `plain`, `grammar_clean`, `rp_format`, `rp_enhance`, `persona_pov`, `narrator_past`, `narrator_present`, `command`.
- SSE events for state, raw transcript preview, formatter tokens, final result, edits, and voice commands.
- Privacy badge and `/audit/network` external-destination audit.
- Token-bearing URL log redaction.
- Group-chat state payloads: group id, group members, and last speaker.
- TTS controls: Kokoro voices, voice profiles, test button, auto-read toggles, audiobook/export endpoints.
- Hardened user units and CORS override support.
- POL-17 first slice: in-RAM Raw → Cleaned → Final repair trace in phone UI and result payloads; explicit “Accept as vocab” is the only persistence path.
- MVP-26 first slice: request-scoped in-memory scene contract injected into RP+ and Persona POV prompts; no persistent scene DB.
- MVP-27 first slice: deterministic local RP eval harness with synthetic fixtures for intent/addressee/privacy/expansion checks.
- WOW-2 group cast voicing: one-click `Auto-cast group` assigns a distinct Kokoro voice to every group member via the stateless `POST /tts/voices/autocast` endpoint (greedy distinct-voice pick reusing the `_suggest_voices` heuristic), plus an editable name→voice roster (per-row Sample/Remove) in the ST settings panel. Client owns persistence into `ttsVoiceProfiles`; server stays stateless.

## Release-hardening queue

1. **Pairing clarity**
   - Done: distinguish server health, token validity, SSE status, and ST-follow freshness.
   - Done: ST settings panel can open a fresh paired phone page or copy a tokenized pairing URL.
   - Done: local settings-panel QR renderer uses vendored Nayuki MIT browser JS and Canvas; no server round-trip or third-party QR service.

2. **Group-chat QA**
   - Partial: synthetic server pytest now covers avatar filename → character-name
     resolution, missing-avatar stem fallback, latest non-user/non-system last
     speaker, `*all` addressee round-trip, and group scene-contract identity.
   - Pending: run the live ST group smoke in
     [`docs/troubleshooting.md`](troubleshooting.md#11-manual-smoke-group-chat-addressee-qa)
     without copying private chat text, cards, avatars, tokens, or tokenized URLs.
   - Pending: confirm remembered addressee/mode behavior in the same live smoke.

3. **TTS completion**
   - Done: opt-in streaming read-back speaks complete sentence chunks while the AI message DOM is still streaming.
   - Done: quick-launch TTS stream status shows off/watching/queued/speaking/paused/stopped/error, queue count, bounded chunk preview, stop-all, skip, pause/resume, and reread-last controls.
   - `ttsReadStreamingPartials` defaults false, is user-toggleable, and reuses the existing authenticated `POST /tts` Kokoro endpoint instead of adding a new audio-stream endpoint.
   - Existing non-streaming Kokoro read-back, voice profiles, samples, suggestions, reset, and audiobook export remain the supported path.

4. **Diagnostics and repair visibility**
   - Done: redacted ST-side Diagnostics panel distinguishes server reachability, token validity, SSE status/age, ST-state freshness, Whisper health payload, Kokoro voice endpoint, and formatter/audit summary.
   - Done: ST-side in-memory Repair Trace drawer shows Raw → Cleaned → Final for the current result only, clears on new dictation/chat switch/manual dismiss/textarea edit, and never writes private text to disk.

5. **Token rotation UX**
   - Done: `dictation-server --rotate-token` rotates the bearer token with mode 0600 and prints only the token path plus live-service next steps.
   - Live sequence: stop service, rotate, update ST bridge token setting, restart, hard-refresh ST, re-pair phone.

6. **Packaging**
   - Done: AUR `calliope-git` packaging now follows the repo layout, installs the single-file server through package wrappers, includes the adjacent voice catalog, and keeps runtime state/certs/tokens/models out of the package.
   - Done: pipx packaging remains a thin adapter around the single-file server source; Hatch copies `server/calliope-server` into the wheel as the `calliope-server` entry point without lifting out the embedded PWA.
   - Pending: publish/tag/release steps remain blocked until a release is explicitly cut.

7. **Docs/publication hygiene**
   - Done: public-path scan completed; generic runtime paths stay as examples and Ayaz-local SillyTavern paths remain documented as local operational defaults.
   - Done: CI mirrors local gates: pytest, ruff, Python syntax, extension JS syntax, embedded phone UI JS syntax, packaging shell syntax, and systemd unit verification.
   - Done: `.github/ISSUE_TEMPLATE/` issue forms explicitly forbid audio attachments, tokenized URLs, bearer tokens, certs/keys, cookies, and private chat logs.

## Known intentional defaults

- Auto-send is off: Ayaz’s preferred workflow is dictate → format → review in ST textarea → click send.
- Phone URLs may include a token briefly; the page stores it in session storage and scrubs the visible URL afterward.
- `whisper-server` may idle-shutdown to reclaim VRAM unless `WHISPER_IDLE_SHUTDOWN_DISABLED` is set.
