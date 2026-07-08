"""Calliope dictation server package.

Extracted stage by stage from the single-file `calliope-server` script,
which remains the executable entry point and composition root (HTTP
handler, auth/CORS, cert + token lifecycle, outbound network audit,
main/CLI).

Module map:
- config       — env-derived constants, paths, model chains (`config.X` read at call time)
- events       — SSE event bus shared by the HTTP handler and the formatter pipeline
- formatter    — modes/vocab/voice-macros config, prompt construction, provider
                 clients + omniroute chain walker, model attribution,
                 hallucination/disfluency filters, ST state + scene contract,
                 `run_pipeline`
- sillytavern  — SillyTavern data readers (personas, characters, chats, groups)
- transcribe   — whisper-server lifecycle, transcription paths, word confidence
- tts          — Kokoro proxy, audiobook assembly, voice casting
- web_ui       — embedded phone PWA assets (HTML/manifest/pairing bootstrap)
- wizard       — first-run setup wizard (`--setup`)
"""

__version__ = "0.1.0"
