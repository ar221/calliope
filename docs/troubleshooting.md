# Troubleshooting

Common failure modes Calliope users hit, with concrete fixes. If
yours isn't here, report it locally until the repository is published — and read the relevant sub-doc:
[`architecture.md`](architecture.md), [`cert-trust.md`](cert-trust.md),
[`tailscale.md`](tailscale.md), [`desktop-hotkey.md`](desktop-hotkey.md).

For faster diagnosis, always start with:

```bash
journalctl --user -u dictation-server -n 50 --no-pager
curl -k https://localhost:8384/health
```

The first line of journalctl after a request usually tells you which
stage failed.

---

## 1. Browser shows "your connection is not private" / cert untrusted

**Symptom:** opening `https://<host>:8384` in any browser shows the
generic untrusted-cert warning page. On Chrome you might see
`NET::ERR_CERT_AUTHORITY_INVALID`; on Firefox, `SEC_ERROR_UNKNOWN_ISSUER`.

**Cause:** the cert is self-signed by your machine, not by a public CA.
Browsers warn on every untrusted cert until you import the issuer into
the trust store.

**Fix:**

1. Match the SHA-256 fingerprint from `journalctl --user -u dictation-server | grep fingerprint`
   against what the browser shows in "view certificate". They must match.
2. **Quick fix:** click through the advanced → proceed dialog. The
   browser remembers per profile.
3. **Real fix:** install the cert as a user CA on the device. See
   [`cert-trust.md`](cert-trust.md) for platform steps (Android, iOS,
   Firefox, Chromium-based, all covered).
4. **Best fix:** switch to Tailscale for a real Let's Encrypt cert. See
   [`tailscale.md`](tailscale.md). No more warnings on any device.

---

## 2. Hold-to-record on phone does nothing (tap-toggle works)

**Symptom:** on Z Fold 6 / Samsung Internet (and historically on iOS
Safari), the hold-to-record gesture starts but never captures audio.
Tap-to-toggle works fine — first tap arms, second tap submits.

**Cause:** `getUserMedia` from `touchstart` was being blocked by the
iframe permissions-policy when ST loaded the phone UI in an embedded
iframe. The user-gesture context wasn't propagating from `touchstart`
through an awaited Promise correctly.

**Fix (Phase 1 + 2, shipped as MVP-11):** the server's touch handler
now calls `await startRecording()` synchronously inside the
`touchstart` callback, and the `dictation-bridge` extension defaults
to `openStyle: 'popup'` on touch devices (the iframe path is desktop-only).

If hold-to-record still fails on your device:

1. Confirm you are on a build that includes `bb464b2 feat(calliope): MVP-11`.
   (`git log --oneline | grep MVP-11` in the repo.)
2. Open the phone PWA in a *standalone* tab (not embedded in ST). If
   hold works there but not in the ST popup, the issue is the popup's
   permissions delegation — open an issue with browser + ST version.
3. **Workaround:** use tap-toggle. First tap arms, talk, second tap
   submits. Same effective UX.

---

## 3. SSL EOF spam in journalctl

**Symptom:** `journalctl --user -u dictation-server` shows recurring
lines like:

```
ssl.SSLError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in
violation of protocol (_ssl.c:2711)
```

dozens of times per hour.

**Cause:** mobile browsers (especially Samsung Internet, Chrome Android)
close TLS sockets mid-handshake when reconnecting to `/events` SSE
streams under background-tab throttling. The server's stdlib HTTP
handler logs the broken handshake at WARNING.

**Fix (MVP-18, shipped):** these benign EOFs are now demoted to DEBUG.
You should not see them at the default `LogLevel=warning` (now baked
into the systemd unit).

If you still see them on an older build:

1. Confirm you are on `46fc429 feat(calliope): MVP-18` or newer.
2. Add `LogLevel=warning` to your `dictation-server.service.d/override.conf`
   if you're running a fork without the demotion.
3. Confirm the service is actually healthy: `curl -k https://localhost:8384/health`
   should return `{"status": "ok", ...}`. The EOF spam is **cosmetic
   only** — the service is fine.

---

## 4. `whisper-cli not found` / `whisper-server not found`

**Symptom:** at first transcribe (or at server startup if `whisper-server`
is the daemon), the request errors with:

```
FileNotFoundError: [Errno 2] No such file or directory: 'whisper-cli'
```

or the systemd unit fails immediately with a similar error for
`whisper-server`.

**Cause:** the binary isn't on `$PATH`, or the wizard expected it at a
location other than where you installed it.

**Fix:**

```bash
which whisper-cli whisper-server
# Both should resolve to ~/.local/bin/

# If empty, follow Quickstart §2 in the README:
cd ~/projects/whisper.cpp
cmake --build build -j --config Release
ln -sf "$PWD/build/bin/whisper-cli"    ~/.local/bin/whisper-cli
ln -sf "$PWD/build/bin/whisper-server" ~/.local/bin/whisper-server
```

If the binaries are at a non-default path:

```ini
# ~/.config/systemd/user/dictation-server.service.d/override.conf
[Service]
Environment=WHISPER_BIN=/full/path/to/whisper-cli
Environment=WHISPER_SERVER_BIN=/full/path/to/whisper-server
```

Then `systemctl --user daemon-reload && systemctl --user restart dictation-server`.

---

## 5. `pw-record: permission denied` on desktop

**Symptom:** running `dictate toggle` immediately fails with
`pw-record: failed to open device` or `permission denied`.

**Cause:** your user is not in the `audio` group, or PipeWire is not
running for this user session.

**Fix:**

```bash
# Confirm you're in the audio group
groups | tr ' ' '\n' | grep audio

# If absent:
sudo usermod -aG audio $USER
# Logout/login (group membership re-evaluates at session start).

# Confirm PipeWire is running
systemctl --user status pipewire pipewire-pulse wireplumber

# Direct pw-record sanity check
pw-record /tmp/test.wav
# Talk for 2 seconds, Ctrl-C
file /tmp/test.wav
# Should report: WAV audio data, ...

# If pw-record itself is missing:
sudo pacman -S pipewire   # or distro equivalent
```

If `pipewire.service` is masked or stopped, re-enable:

```bash
systemctl --user unmask pipewire pipewire-pulse wireplumber
systemctl --user enable --now pipewire pipewire-pulse wireplumber
```

---

## 6. Phone uses an old SillyTavern context after switching tabs

**Symptom:** the phone page is paired, but a recording uses the previous
chat/persona/group after you foreground the standalone phone tab or reopen
from a stale bookmark.

**Cause:** mobile browsers freeze background tabs aggressively. The ST tab
may not get a normal heartbeat after the phone tab takes focus, so the
phone can briefly hold an older `/state` snapshot.

**Fix / check:** open the phone page from the current ST mic button when
possible. Current builds push ST state on mic-open and browser lifecycle
events, and the phone PWA force-refreshes `/state` before recording and
before sending audio. If the follow banner still looks stale, hard-refresh
SillyTavern and reopen the phone page; no server restart is required for
this symptom unless you are deploying new server code.

---

## 7. Phone says stale token / 401 after rotation

**Symptom:** the phone page says the token is stale, `/state` returns 401,
or the ST bridge shows server reachable but auth invalid after you rotated the
runtime token.

**Cause:** the running server keeps the old token in memory until restart, and
SillyTavern/phone sessions keep using the old token until the bridge setting
and browser session are refreshed.

**Fix:** use the live rotation sequence in this order:

1. Stop `dictation-server`.
2. Run `dictation-server --rotate-token`. It prints the token file path and
   next steps, not the token value.
3. Update the ST Dictation Bridge token setting from the token file. Do not
   paste the token or a tokenized URL into logs, issues, or chat.
4. Restart `dictation-server`.
5. Hard-refresh SillyTavern so the extension reloads its settings.
6. Re-pair the phone from the current ST mic button.

---

## 8. Group chat addressee/context looks wrong

**Symptom:** in an ST group chat, dictation comes through with weak or
wrong character context — `rp_enhance` sounds generic, or it follows the
wrong speaker.

**Cause:** group chat state is richer than one-on-one state. The bridge
now sends the group id, member list, and last speaker, and the server can
filter addressee choices for that group. You still need to pick the
specific addressee when the last-speaker default is not the voice you
want.

**Status:** POL-6 is shipped at the wiring level: addressee picker,
group members, last-speaker default, and mode memory exist. Treat remaining
failures as group-QA bugs.

**Fix / check:**

1. Open the phone page from the current ST group chat, not an old bookmark.
2. Confirm the follow banner says `Paired + following ST` and shows group
   context.
3. Pick the intended addressee in the phone UI before recording if the
   last speaker is not the target voice.
4. If the addressee list is blank, refresh SillyTavern and reopen from the
   ST mic button so `/state` repopulates the group payload.
5. Desktop fallback: `dictate --rp+ --character "Hana Nakamura" toggle`.

---

## 9. `getUserMedia` rejected: "no audio device" on phone

**Symptom:** the phone PWA's mic button fails to start recording. Browser
console shows `NotAllowedError` or `NotFoundError` from `getUserMedia`.

**Cause (most common):** mic permission denied for the page, or no HTTPS
context (`getUserMedia` requires a secure context — `https://` or
`http://localhost`).

**Fix:**

1. Verify you're on `https://`, not `http://`. The lock icon should be
   visible (or a "not secure" indicator with explanation).
2. Tap the lock icon → **Permissions** → **Microphone** → **Allow**.
3. If permission was previously denied: clear site data (browser
   settings → site settings → `<host>:8384` → clear & reset). Reload,
   accept mic permission on the prompt.
4. If on Samsung Internet and the prompt never appears: the Android
   system mic permission for the browser may be revoked. **Settings →
   Apps → Samsung Internet → Permissions → Microphone → Allow only
   while using the app**.
5. Confirm cert is trusted ([cert-trust.md](cert-trust.md)) — some
   browsers refuse `getUserMedia` on cert-untrusted pages even after
   you click through.

---

## 10. Server fails to bind: address in use

**Symptom:** `systemctl --user status dictation-server` shows:

```
OSError: [Errno 98] Address already in use
```

The unit goes into `failed` state.

**Cause:** another process is already bound to port 8384 — usually a
zombie Calliope from a previous `systemctl restart` that didn't clean
up, or you have two units enabled (rare, but possible if you
double-installed).

**Fix:**

```bash
# Find what's on the port
ss -tnlp | grep 8384
# or
lsof -i :8384

# Kill the orphan
pkill -f dictation-server
# Wait 2s, retry
systemctl --user restart dictation-server

# If a different service legitimately wants 8384, pick another port:
# ~/.config/systemd/user/dictation-server.service.d/override.conf
[Service]
Environment=DICTATION_PORT=8385
```

`systemctl --user daemon-reload && systemctl --user restart dictation-server`.
Update phone bookmarks + ST extension settings to the new port.

---

## 11. RP-enhance returns empty / falls through to raw

**Symptom:** dictation produces only the raw whisper transcript — no
asterisks, no enhanced phrasing — even when you've selected
`rp_enhance` mode.

**Cause:** the LLM proxy at `localhost:42069` (`claude-code-proxy`) is
not running, or is returning errors / empty responses, and the pipeline
is falling back to the previous stage's output. The bounded formatter timeout
(default 10 seconds via `DICTATION_FORMATTER_TIMEOUT`) gives up far faster than
the old 60-second one, so this fails loudly now.

**Fix:**

```bash
# 1. Is the proxy reachable?
curl -s http://localhost:42069/health
# Expected: {"status":"ok"} or similar; 4xx/5xx/connection-refused = down.

# 2. Start it if needed (path varies by your install)
cd ~/STWork/claude-code-proxy-1.2.0
./run.sh
# Or via systemd if you've wrapped it: systemctl --user start claude-code-proxy

# 3. If 401: re-auth
# Visit http://localhost:42069/auth/login in a browser, complete OAuth.

# 4. Confirm Calliope sees it
journalctl --user -u dictation-server -f
# Trigger a transcribe; look for `formatter_request` log lines.
# `formatter_request → 200` = good; `connection refused` / `timeout` = proxy down.

# 5. Fallback: switch mode to `rp_format` (no LLM) or `plain` (no formatter at all)
#    via the phone UI mode selector.
```

Same drill for the OpenAI-shape proxy at `127.0.0.1:10531`, used for
`grammar_clean` / `disfluency_clean`.

---

## 12. Transcribe hangs >2 min (cold model load)

**Symptom:** the first `/transcribe` after server boot takes 30+ seconds,
sometimes hits the 120-second whisper timeout. Subsequent calls are
fast.

**Cause:** whisper.cpp is mmap-loading the 1.6 GB `large-v3-turbo.bin`
into page cache, plus initializing the ROCm / CUDA context. This is
real work that happens once per cold start.

**Fix (MVP-8 shipped):** the persistent `whisper-server` daemon keeps
the model resident, so cold-start cost is paid once at daemon boot
(not per request). After Phase 2 you should rarely see this.

If you still see >30s hangs:

1. Confirm `whisper-server` is the daemon you're using:
   ```bash
   systemctl --user status whisper-server
   ss -tnlp | grep 9001
   curl http://127.0.0.1:9001/  # whisper-server has its own status page
   ```
2. Look at the timing JSON:
   ```bash
   journalctl --user -u dictation-server | grep timing | tail -1 | jq
   ```
   `stages_ms.whisper` >> 1000 = the model isn't warm. Ensure
   `whisper-server` started before `dictation-server`
   (the unit dependency `After=whisper-server.service` handles this; if
   you've overridden, check ordering).
3. Test ROCm / CUDA from the host:
   ```bash
   rocm-smi   # or nvidia-smi
   ```
   If GPU is missing, fall back to `--no-gpu` for diagnosis:
   ```ini
   # override.conf
   [Service]
   Environment=WHISPER_NO_GPU=1
   ```
   CPU-mode is ~3-5× realtime on a modern x86. Slow but functional.
4. Inspect `journalctl --user -u dictation-server -f` during the hang —
   ROCm errors or Python tracebacks usually appear before the timeout
   fires.

---

## 11. Manual smoke: group-chat addressee QA

Use this only after synthetic pytest coverage is green. Do **not** read,
copy, export, or paste private chat text, character cards, avatars, tokens, or
pairing URLs while smoking a live ST group.

Checklist:

1. Hard-refresh SillyTavern only if you are validating newly synced bridge
   files; do not restart `dictation-server`, `whisper-server`, `kokoro-server`,
   or SillyTavern services for this smoke.
2. Open an active SillyTavern group chat and open Extensions → Dictation Bridge.
3. Confirm the addressee/member chips show the expected group members.
4. Confirm the latest non-user/non-system speaker is marked as the last-speaker
   chip/default.
5. Select one member, then select **All members**. The all-members addressee
   should persist as `*all` rather than disappearing or becoming a solo
   character.
6. Verify the server-side `/state`/active-context payload using a safe debug
   surface or logs: check only structural keys such as `chatType=group`,
   `groupId`, `groupMembers`, `lastSpeaker`, and `characterName=*all`.
   Do not print `lastAiMessage`, `mes`, transcript text, bearer tokens, or
   tokenized URLs.
7. Dictate a short synthetic throwaway phrase if needed, then verify the prompt
   context still names the group and member list instead of collapsing to a
   single solo character. Do not paste the actual private prompt or chat text
   into issues or receipts.
