# TTS install — Kokoro-82M

Calliope's read-back UX is powered by [Kokoro-82M](https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX),
an open-weight TTS model (~82M params, MIT-licensed) running on a
loopback HTTP daemon. The dictation-server also exposes:

- `POST /tts` — synthesize a single utterance.
- `GET /tts/voices` — list available voices.
- `POST /tts/voices/suggest` — suggest a voice for a character/addressee.
- `POST /tts/audiobook` — render a bounded multi-message audiobook/readback.

All are proxied through the loopback Kokoro daemon, with on-demand boot
and idle-shutdown — Kokoro stays out of memory between read-backs.

This is an **optional** install. The phone UI degrades gracefully when
the unit isn't present (the `/tts` endpoint returns 503).

## Why Kokoro

- **Small.** ~82M params, ~330 MB on disk (fp16 ONNX + voices). Comfortably fits CPU-only.
- **MIT-licensed.** No tracking, no API key, no telemetry.
- **Fast on CPU.** RTF well below 1.0 on a modern x86 box; ROCm available if you have a wheel that supports your GPU.
- **Multi-voice.** Ships with ~50 voices across English, Japanese, Spanish, French, Hindi, Italian, Portuguese, Mandarin.

## Layout

Calliope keeps Kokoro entirely outside its own venv to preserve the
"single-file Python script + stdlib" contract for `calliope-server`.
The TTS install lives at `~/.local/share/calliope-tts/`:

```
~/.local/share/calliope-tts/
├── venv/                    # dedicated venv — only kokoro-onnx, onnxruntime, numpy
├── kokoro-server.py         # bundled from scripts/kokoro-server.py
└── models/
    ├── onnx/
    │   ├── model.onnx       # full precision (~330 MB)
    │   └── model_fp16.onnx  # fp16 (default; ~165 MB)
    └── voices-v1.0.bin      # voice embeddings (~28 MB)
```

## One-time setup

1. **Create the venv** (Python 3.12+ recommended; the calliope-server
   script targets Python 3.14, but kokoro-onnx wheels are widest at
   3.11–3.12):

   ```sh
   python3.12 -m venv ~/.local/share/calliope-tts/venv
   ~/.local/share/calliope-tts/venv/bin/pip install --upgrade pip
   ~/.local/share/calliope-tts/venv/bin/pip install kokoro-onnx onnxruntime numpy
   ```

   For ROCm hardware, swap `onnxruntime` for the ROCm-enabled wheel
   (or `onnxruntime-gpu` when CUDA). CPU-only is fine for read-back.

2. **Download the model files** (HuggingFace, ~200 MB total with fp16):

   ```sh
   mkdir -p ~/.local/share/calliope-tts/models/onnx
   cd ~/.local/share/calliope-tts/models
   curl -L -o onnx/model_fp16.onnx \
     https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main/onnx/model_fp16.onnx
   curl -L -o voices-v1.0.bin \
     https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/main/voices-v1.0.bin
   ```

   Optional: `curl -L -o onnx/model.onnx ...` for full-precision.

3. **Install the bundled server script** (copied from this repo):

   ```sh
   cp scripts/kokoro-server.py ~/.local/share/calliope-tts/kokoro-server.py
   chmod +x ~/.local/share/calliope-tts/kokoro-server.py
   ```

4. **Install + enable the systemd unit:**

   ```sh
   mkdir -p ~/.config/systemd/user
   cp systemd/kokoro-server.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable kokoro-server.service   # optional — Calliope starts it on demand
   ```

   Note: `enable` is optional. Calliope's dictation-server runs
   `systemctl --user start kokoro-server` on the first `/tts` request
   and `systemctl --user stop kokoro-server` after
   `KOKORO_IDLE_SHUTDOWN_SECONDS` of inactivity (default 600s). If
   you keep many other services warm, leaving `enable` off is fine.

## Verification

```sh
systemctl --user start kokoro-server.service
systemctl --user status kokoro-server.service
curl -s http://127.0.0.1:9002/health
# → {"status": "ok", "voices": 54, "model": "model_fp16.onnx"}
curl -s http://127.0.0.1:9002/voices | head -c 200
# → {"voices": [{"id": "af_heart", "label": "af_heart"}, ...]}
```

End-to-end against Calliope (substitute your bearer token):

```sh
TOKEN=$(cat ~/.local/share/dictation-server/token)
curl -k -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello Lord Rashid.","voice":"af_heart"}' \
  https://127.0.0.1:8384/tts -o /tmp/test.wav
mpv /tmp/test.wav
```

## Sample voices

A few good defaults for English read-back:

| Voice ID | Notes |
|---|---|
| `af_heart` | American female; warm, default. |
| `af_bella` | American female; clearer, slightly brighter. |
| `am_michael` | American male; news-anchor cadence. |
| `bf_emma` | British female; measured. |
| `bm_george` | British male; gravitas. |

Full list via `GET /tts/voices` (cached 60s in Calliope).

## Environment knobs

Set on the systemd unit (drop-in `~/.config/systemd/user/kokoro-server.service.d/override.conf`)
or in Calliope's environment for the proxy side:

| Var | Default | Where | Effect |
|---|---|---|---|
| `KOKORO_PORT` | `9002` | unit | Loopback bind port. |
| `KOKORO_MODEL` | `…/models/onnx/model_fp16.onnx` | unit | ONNX model path. |
| `KOKORO_VOICES` | `…/models/voices-v1.0.bin` | unit | Voices file path. |
| `KOKORO_LANG` | `en-us` | unit | Default language tag. |
| `KOKORO_SERVER_URL` | `http://127.0.0.1:9002` | calliope | Proxy upstream URL. |
| `KOKORO_DEFAULT_VOICE` | `af_heart` | calliope | Voice when request omits `voice`. |
| `KOKORO_IDLE_SHUTDOWN_SECONDS` | `600` | calliope | Idle threshold before stopping the unit. |
| `KOKORO_IDLE_SHUTDOWN_DISABLED` | unset | calliope | Set to anything to keep Kokoro warm. |
| `KOKORO_REQUEST_TIMEOUT` | `10` | calliope | Proxy request timeout. |
| `TTS_AUDIOBOOK_MAX_MESSAGES` | `50` | calliope | Maximum messages per audiobook export. |
| `TTS_AUDIOBOOK_MAX_TOTAL_CHARS` | `12000` | calliope | Maximum total text in an audiobook export. |
| `TTS_AUDIOBOOK_SILENCE_MS` | `350` | calliope | Gap between audiobook clips. |

## Sandboxing

The shipped unit mirrors `whisper-server.service` for sandboxing
(`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateTmp`, `RestrictAddressFamilies` to UNIX/INET only). Default
config is CPU-only; if you switch to ROCm, add `DeviceAllow=/dev/kfd
rw` and `DeviceAllow=/dev/dri rw` to the unit and disable
`MemoryDenyWriteExecute` (HIP JIT codegen needs it) — same swap the
whisper unit makes.

## Troubleshooting

- **`kokoro-server unreachable` (503 from `/tts`).** The unit isn't
  starting. `journalctl --user -u kokoro-server -e` — most common cause
  is a missing model file or a venv that doesn't have `kokoro-onnx`.
- **`Synthesis failed: ...` (502).** The unit is up but `Kokoro.create()`
  raised. Common: an unknown voice ID. `curl http://127.0.0.1:9002/voices`
  for the list the loaded model recognises.
- **Slow first request, fast subsequent.** Expected — `_ensure_kokoro_alive`
  waits up to 10s for the unit to boot the first time, then stays warm.
- **Kokoro keeps getting killed.** Check `KOKORO_IDLE_SHUTDOWN_SECONDS`.
  Default 600s. Set `KOKORO_IDLE_SHUTDOWN_DISABLED=1` in Calliope's
  env to leave it warm forever.
