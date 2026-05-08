# Third-Party Notices

The Calliope dictation server (`dictation-server`) is licensed under the MIT
License — see [LICENSE](LICENSE). Copyright (c) 2024-2026 Ayaz Rashid.

This document lists third-party software the dictation server depends on,
either at runtime (shelled out, statically linked, or fetched), or through
optional integrations. Each entry names the component, license, role, and
upstream URL. The dictation server bundles none of these — they are user-
installed system tools or downloaded artifacts. Attribution is provided per
the redistribution requirements of the relevant licenses.

---

## STT engine and model weights

### whisper.cpp

- **License:** MIT
- **URL:** https://github.com/ggerganov/whisper.cpp
- **Role:** Local Whisper speech-to-text inference. Invoked as a subprocess
  (`whisper-cli`) per request in v1; migrated to a persistent `whisper-server`
  HTTP backend in Phase 2. Built with HIP/ROCm for AMD GPU acceleration.

### ggml

- **License:** MIT
- **URL:** https://github.com/ggerganov/whisper.cpp (vendored / statically
  linked into whisper.cpp)
- **Role:** Tensor library providing the inference primitives whisper.cpp uses.

### Whisper model weights (`large-v3-turbo`)

- **License:** MIT (OpenAI)
- **URL:** https://huggingface.co/ggml-org/whisper.cpp
- **Role:** Pretrained Whisper acoustic model used for transcription. Downloaded
  by the user, not bundled.

---

## LLM post-processing (optional)

### claude-code-proxy

- **License:** MIT (`horselock/claude-code-proxy`)
- **URL:** https://github.com/horselock/claude-code-proxy
- **Role:** Optional local OAuth bridge at `localhost:42069` used by the
  `rp_enhance` mode for Anthropic-shape requests with the upstream "pyrite"
  preset. Not bundled; user installs separately. The pyrite preset itself
  ships with the upstream proxy and remains under that project's MIT license.

---

## Python dependencies (all optional)

### PyYAML

- **License:** MIT
- **URL:** https://github.com/yaml/pyyaml
- **Role:** Hot-reload of `modes.yaml`, `vocab.yaml`, `char-modes.yaml`. The
  server falls back to a tiny stdlib parser if PyYAML is absent.

### wordfreq

- **License:** Apache-2.0
- **URL:** https://github.com/rspeer/wordfreq
- **Role:** Common-English-word frequency gate for the vocab fuzzy-correction
  step (ADR-10). Prevents short common words like `'yaz'` being silently
  rewritten to character names like `'Ayaz'`. Optional — server falls back to
  a hardcoded ~1000-word stoplist if absent.

---

## System tools (shelled out, not bundled, not statically linked)

### PipeWire `pw-record`

- **License:** MIT
- **URL:** https://gitlab.freedesktop.org/pipewire/pipewire
- **Role:** Microphone capture on the desktop client. The dictation-server
  itself receives uploaded audio over HTTP; `pw-record` is invoked by the
  `dictate` push-to-talk client.

### wtype

- **License:** MIT
- **URL:** https://github.com/atx/wtype
- **Role:** Wayland virtual keyboard injection — types the transcribed text
  into the focused window. Used by the `dictate` client.

### wl-clipboard (`wl-copy`)

- **License:** GPL-3.0
- **URL:** https://github.com/bugaevc/wl-clipboard
- **Role:** Clipboard fallback when `wtype` cannot inject (e.g., locked focus).
  Shelled out to as a system tool; not linked — the GPL relink clause does not
  apply.

### ffmpeg

- **License:** LGPL-2.1+ / GPL-2.0+ (depending on build)
- **URL:** https://ffmpeg.org/
- **Role:** Audio normalization and resampling (16 kHz mono PCM) before
  whisper invocation. Shelled out as a system tool; not statically linked.

### canberra-gtk-play (optional)

- **License:** LGPL-2.1+
- **URL:** https://www.freedesktop.org/wiki/Software/libcanberra/
- **Role:** Optional audible cue on dictation start/stop. Shelled out; can be
  disabled via `--no-sound`.

---

## Notes on bundling

- Nothing in this list is bundled into the dictation-server source. All
  dependencies are user-installed system packages or fetched artifacts.
- Static-linking only happens transitively inside `whisper-cli` /
  `whisper-server` (ggml). Both upstream projects are MIT, so the
  redistribution chain stays clean.
- The pyrite preset and its persona content remain the property of the
  `horselock/claude-code-proxy` upstream — Calliope consumes whatever is
  served at the configured proxy URL and does not redistribute the preset.
