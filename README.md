# Calliope

> Voice for SillyTavern. Local-first. Persona-aware.

![demo](docs/demo.gif)

Calliope is a local-first voice dictation server built specifically for
[SillyTavern](https://sillytavernai.com). Hold a mic button on your phone or
hit a hotkey on your desktop, talk, release — your audio is transcribed by
whisper.cpp on your local GPU, shaped by your active character and persona,
and dropped straight into the chat textarea. Your audio never leaves your
network.

---

## What it does (60 seconds)

- One mic button in the SillyTavern send bar (via the `dictation-bridge`
  extension), one hotkey on the desktop (`Mod+Shift+M`), and a phone PWA
  reachable from any device on your LAN.
- Hold to record on phone or desktop, release to transcribe.
- Whisper `large-v3-turbo` runs on your local GPU (ROCm or CUDA via
  whisper.cpp).
- Output is shaped by your active character and persona — actions wrapped
  in asterisks, dialogue in quotes, written in your persona's voice and
  matched to the addressee's tone.
- Five pipeline modes: `plain`, `grammar_clean`, `rp_format`, `rp_enhance`,
  `persona_pov`. The mode is remembered per character.
- Live state-machine bar above the textarea: `idle → listening →
  transcribing → cleaning → done`. Streaming partials so you see words
  solidify as the pipeline finishes.
- Transcripts live in RAM only. Audio temp files are deleted in the
  `finally` block of every request. There is no telemetry, no auto-update,
  no model auto-download at runtime, no third-party JS in the phone UI.

## Why this and not something else?

> **ST-Extras was deprecated April 2024; the official ST extension is
> cloud-only in 2026. Calliope is the only maintained local-first STT
> path for SillyTavern.**

| Feature | **Calliope** | **SillyTavern-Extras `whisper-stt`** | **ST official Speech Recognition** |
|---|---|---|---|
| Maintained? | Active 2026 | **Deprecated April 24, 2024** | Active |
| Local STT? | whisper.cpp + GPU (ROCm/CUDA) | whisper / faster-whisper, but stale | Cloud only (OpenAI / Mistral / Groq / Z.AI). Browser Web Speech API hits Google. |
| GPU acceleration | HIP/ROCm + CUDA | partial | n/a |
| Persona-aware formatting | Five-mode pipeline (plain → rp_enhance → persona_pov) | plain transcript only | plain transcript only |
| Character vocab biasing | per-character `vocab.yaml` → whisper `--prompt` | none | none |
| Phone push-to-talk | embedded PWA, MediaRecorder + VAD | terminal only | none |
| Desktop hotkey | `dictate` bash + niri/GNOME/KDE keybinds, smart-paste detection | none | none |
| Active-chat context | reads ST disk to inject recent messages | none | none |
| Audio leaves your network? | **No** in `plain` mode; RP modes pass *text* (not audio) to a configured local LLM proxy | self-hosted Extras only | **Yes** by default |
| License | MIT | GPLv3 | GPLv3 |

vs general-purpose dictation tools (Wispr Flow, Superwhisper, MacWhisper,
OpenWhispr, Vocalinux): those are excellent, but they are not shaped for
SillyTavern. Calliope is — persona-aware formatting, character vocab
biasing, in-extension UI, group-chat awareness, ST disk-context injection.

## Quickstart (90 seconds, copy-pasteable)

Tested on Arch / CachyOS. Other Linux distributions follow the same shape.

**1. Install runtime deps** (Arch/Cachy):

```bash
sudo pacman -S python pipewire wtype wl-clipboard ffmpeg qrencode
```

(macOS / Debian equivalents: `brew install pipewire wtype wl-clipboard ffmpeg qrencode` or `apt install`.)

**2. Build whisper.cpp with GPU** (ROCm shown; CUDA: swap `-DGGML_HIP=1`
for `-DGGML_CUDA=1`):

```bash
git clone https://github.com/ggerganov/whisper.cpp ~/projects/whisper.cpp
cd ~/projects/whisper.cpp
cmake -B build -DGGML_HIP=1 -DCMAKE_HIP_PLATFORM=amd
cmake --build build -j --config Release
ln -s "$PWD/build/bin/whisper-cli"    ~/.local/bin/whisper-cli
ln -s "$PWD/build/bin/whisper-server" ~/.local/bin/whisper-server
```

**3. Download the model** from the official whisper.cpp HuggingFace mirror:

```bash
mkdir -p ~/.local/share/whisper
curl -L -o ~/.local/share/whisper/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggml-org/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

**4. Install Calliope.** Until the AUR / pipx packages publish, drop the
single-file server onto your PATH:

```bash
install -m 755 server/calliope-server ~/.local/bin/dictation-server
```

(AUR `calliope-git` and pipx packaging via `calliope-dictation` are tracked in
[the roadmap](docs/roadmap.md); neither channel is published yet.)

**5. Run the wizard.** It probes audio, picks a model size from your VRAM,
generates a self-signed cert + bearer token, installs the systemd unit, and
runs a 10-second self-test:

```bash
dictation-server --setup
```

The wizard prints the cert SHA-256 fingerprint and stores the bearer token.
Save the fingerprint somewhere — you will match it on first phone connect.
Pair phones from the SillyTavern extension after it is installed.

**6. Install the SillyTavern extension.** Drop `dictation-bridge/` under
`<ST install>/data/default-user/extensions/third-party/` (or symlink from
the canonical copy in `~/Github/dotfiles/sillytavern/extensions/dictation-bridge/`).
Hard-refresh ST. A mic button appears in the send bar.

**7. First desktop hotkey** (niri shown, GNOME / KDE in
[`docs/desktop-hotkey.md`](docs/desktop-hotkey.md)):

```kdl
Mod+Shift+M  { spawn "bash" "-c" "$HOME/.local/bin/dictate toggle"; }
```

**8. First phone connection.** In ST, open Extensions → Dictation Bridge and
use **Re-pair this phone**, **Show local QR**, or **Copy pairing URL**. The QR
is rendered locally in the ST browser and contains the bearer-token pairing
URL, so treat it like a password. Visit the URL, trust the cert (one-time per
browser profile, see [`docs/cert-trust.md`](docs/cert-trust.md)), bookmark it,
install as PWA, then test recording.

## Architecture

```
                                    [phone browser / PWA]
                                        ▲ │
                                        │ │ HTTPS (bearer-auth)
                                        │ ▼
[ST tab + dictation-bridge ext] ◀─ /events SSE ─[Calliope server :8384]
                                        │  │
                                        │  ├── shell ──▶ [whisper-server :9001]
                                        │  │                   (whisper.cpp / GPU)
                                        │  │
                                        │  ├── HTTPS ──▶ [claude-code-proxy :42069]
                                        │  │                   (rp_enhance / pyrite)
                                        │  │
                                        │  ├── HTTP  ──▶ [OpenAI proxy :10531]
                                        │  │                   (grammar / disfluency)
                                        │  │
                                        │  └── reads ──▶ [SillyTavern data/]
                                        │                  (characters, chats, groups)
                                        ▼
                              ~/.local/share/dictation-server/
                                 cert.pem · key.pem · token
                                 vocab.yaml · modes.yaml · char-modes.yaml
```

Full breakdown — endpoints, trust boundaries, SSE event taxonomy — in
[`docs/architecture.md`](docs/architecture.md).

## Configuration

Runtime data and config live under `~/.local/share/dictation-server/`:

- `cert.pem` / `key.pem` — self-signed TLS cert (90-day rotation,
  auto-renewed at startup).
- `token` — 32-byte bearer token, mode 0600. Rotate with `dictation-server --rotate-token`; the command prints the token path and live-service next steps, not the token value. Live rotation sequence: stop the user service, rotate, update the ST bridge token setting from the token file, restart the service, hard-refresh SillyTavern, then re-pair the phone.
- `cert.fingerprint` — SHA-256, also printed at startup.
- `vocab.yaml` — character/term biasing for whisper `--prompt`.
- `modes.yaml` — pipeline mode definitions.
- `char-modes.yaml` — per-character last-used mode memory.

Environment-variable overrides (set in `~/.config/systemd/user/dictation-server.service.d/override.conf`):

- `DICTATION_BIND_HOST` — defaults to `127.0.0.1`. Set `0.0.0.0` to expose
  on LAN (token auth still required).
- Port is currently a CLI option: set `ExecStart=.../dictation-server --port 8385` in the user unit override if you need a non-default port.
- TLS cert/key live under `~/.local/share/dictation-server/`; Tailscale/mkcert flows should write `cert.pem` and `key.pem` there. See [`docs/tailscale.md`](docs/tailscale.md).
- `WHISPER_BIN`, `WHISPER_SERVER_BIN`, `WHISPER_MODEL` — override binary
  and model paths.
- `DICTATION_FORMATTER_PROVIDER`, `DICTATION_CLAUDE_RP_MODEL`, `DICTATION_RP_MODEL`, `DICTATION_OPENAI_RP_MODEL`, `DICTATION_OPENAI_CLEAN_MODEL` — formatter routing.

Full reference: [`docs/config.md`](docs/config.md).

## Privacy

See [PRIVACY.md](PRIVACY.md). Short version:

- Audio is captured locally, transcribed locally, deleted immediately.
- Transcripts live in RAM only — server restart wipes them.
- `plain` mode is fully local. RP modes pass *text* (not audio) to whichever
  LLM proxy you've configured; if that proxy talks to a cloud model
  (Anthropic via `claude-code-proxy`), the text travels there. The set of
  active providers is shown at startup and at `GET /audit/network`.
- No telemetry, no auto-update, no model auto-download at runtime, no
  third-party JS in the phone UI.

## License + third-party notices

MIT. See [LICENSE](LICENSE). Third-party components listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) (whisper.cpp, ggml,
claude-code-proxy, Monaspace, Literata, system tools).

## Support

- **Bugs:** file an issue in the repository when it is public. Until then, report locally to the maintainer/operator.
- **Security:** see [`SECURITY.md`](SECURITY.md); never attach audio, bearer tokens, tokenized URLs, certs/keys, or private chat logs.
- **Discussion:** use the project discussion space once published.

When filing an issue, use the templates under `.github/ISSUE_TEMPLATE/`; they
require confirmation that no audio, tokenized URL, cert/key, or private chat
text is attached.

## Status

Phases 1–3 shipped: bearer-auth, loopback default, sandboxed systemd unit,
in-RAM transcripts, monkey-patched outbound audit, persistent `whisper-server`
(MVP-8), streaming SSE partials (MVP-13), Apollo phone-UI re-skin (POL-5),
privacy badge + audit modal (MVP-23), state-machine bar in the bridge
extension (MVP-16), voice-edit cheatsheet overlay (POL-15). Current build is MVP+ / release-hardening: core phone→ST bridge, pairing, privacy audit, ST state following, group addressee support, TTS controls, and service hardening exist. Remaining release work is tracked in [`docs/roadmap.md`](docs/roadmap.md).
