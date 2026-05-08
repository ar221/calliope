# Test fixtures

The CI integration job (`.github/workflows/ci.yml::test-integration`) needs a
canonical 5-second mono 16 kHz WAV containing a known phrase so it can verify
that `calliope-server` plus a fresh `whisper.cpp` tiny model produces the
expected transcription end-to-end.

## hello.wav (missing — generate locally)

The phrase is **"the quick brown fox jumps over the lazy dog"** — chosen
because (a) it's a pangram, so any model that loses too many phonemes shows
up immediately, (b) every word is in the top-5k frequency list so vocab
fuzzy gates won't perturb it, (c) it's not in the Whisper hallucination
corpus.

The fixture is intentionally **not committed** — binary blobs bloat the
repo. CI is wired (via the `Skip if fixture is missing` gate in the
workflow) to skip the integration smoke when the file is absent and emit a
GitHub `::notice::` line, so the absence isn't a build failure.

### Generate offline (recommended — espeak-ng)

```bash
# Arch
sudo pacman -S espeak-ng

# Generate a 5s mono 16kHz WAV. Speed (-s 130) keeps it under 5s without
# clipping the final word; pitch (-p 50) is neutral.
espeak-ng -s 130 -p 50 -w hello.wav \
  "the quick brown fox jumps over the lazy dog"

# Confirm the format Whisper wants:
ffprobe -hide_banner -i hello.wav 2>&1 | grep -E "Duration|Stream"
# Expect: mono, 22050 Hz (espeak default) — resample to 16k:
ffmpeg -y -i hello.wav -ar 16000 -ac 1 hello.16k.wav
mv hello.16k.wav hello.wav
```

### Generate by recording (alternative — pw-record)

```bash
# 5-second capture; speak the phrase clearly.
pw-record --rate 16000 --channels 1 --format s16 hello.wav &
PID=$!
sleep 5
kill -INT "$PID"
```

### Validate

```bash
ffprobe -hide_banner hello.wav 2>&1 | head -10
# Should report: 16000 Hz, mono, s16, ~5s duration.
```

Drop the resulting `hello.wav` next to this README. The integration smoke
will pick it up on the next CI run.
