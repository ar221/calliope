# Calliope Roadmap

Status date: 2026-05-18.

## Shipped / working MVP+

- Phone/PWA dictation UI with HTTPS bearer-token auth.
- SillyTavern `dictation-bridge` extension with mic button and direct textarea insertion.
- Review-before-send flow; auto-send exists but defaults off.
- ST state broadcast to Calliope: active chat, character/group, persona, last AI message, scene continuity, and mode.
- Phone follow mode for fresh ST state.
- Persistent whisper.cpp HTTP daemon on `127.0.0.1:9001` with fallback paths.
- Formatter modes: `plain`, `grammar_clean`, `rp_format`, `rp_enhance`, `persona_pov`, `narrator_past`, `command`.
- SSE events for state, raw transcript preview, formatter tokens, final result, edits, and voice commands.
- Privacy badge and `/audit/network` external-destination audit.
- Token-bearing URL log redaction.
- Group-chat state payloads: group id, group members, and last speaker.
- TTS controls: Kokoro voices, voice profiles, test button, auto-read toggles, audiobook/export endpoints.
- Hardened user units and CORS override support.
- POL-17 first slice: in-RAM Raw → Cleaned → Final repair trace in phone UI and result payloads; explicit “Accept as vocab” is the only persistence path.
- MVP-26 first slice: request-scoped in-memory scene contract injected into RP+ and Persona POV prompts; no persistent scene DB.
- MVP-27 first slice: deterministic local RP eval harness with synthetic fixtures for intent/addressee/privacy/expansion checks.

## Release-hardening queue

1. **Pairing clarity**
   - Done: distinguish server health, token validity, SSE status, and ST-follow freshness.
   - Done: ST settings panel can open a fresh paired phone page or copy a tokenized pairing URL.
   - Done: local settings-panel QR renderer uses vendored Nayuki MIT browser JS and Canvas; no server round-trip or third-party QR service.

2. **Group-chat QA**
   - Live-test addressee picker in actual ST group chats.
   - Confirm last-speaker default and remembered addressee/mode behavior.
   - Add a small regression fixture if ST state payloads can be mocked.

3. **TTS completion**
   - Decide whether streaming partial TTS is core or deferred.
   - If core: implement server streaming and enable `ttsReadStreamingPartials`.
   - If deferred: keep the toggle disabled and document it as non-shipped.

4. **Token rotation UX**
   - Done: `dictation-server --rotate-token` rotates the bearer token with mode 0600 and prints only the token path plus live-service next steps.
   - Live sequence: stop service, rotate, update ST bridge token setting, restart, hard-refresh ST, re-pair phone.

5. **Packaging**
   - Prepare AUR `calliope-git` once repo paths/docs are public-safe.
   - Prepare pipx/install script if the single-file server remains the distribution model.

6. **Docs/publication hygiene**
   - Keep public docs free of user-specific absolute paths.
   - Keep `docs/config.md`, `docs/architecture.md`, and systemd unit examples synchronized with live defaults.
   - Add issue templates that explicitly forbid attaching audio or tokenized URLs.

## Known intentional defaults

- Auto-send is off: Ayaz’s preferred workflow is dictate → format → review in ST textarea → click send.
- Phone URLs may include a token briefly; the page stores it in session storage and scrubs the visible URL afterward.
- `whisper-server` may idle-shutdown to reclaim VRAM unless `WHISPER_IDLE_SHUTDOWN_DISABLED` is set.
