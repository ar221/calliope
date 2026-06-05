# Security Policy

## Reporting

Until the repository is published, report issues directly to the maintainer/operator. Do not include:

- audio recordings,
- bearer tokens,
- URLs containing `?token=`,
- full SillyTavern settings dumps,
- private character/persona/chat content.

Redact sensitive values as `[REDACTED]`.

## Supported surface

The supported local deployment is:

- Calliope server on HTTPS port `8384`, bearer-token protected.
- whisper.cpp HTTP daemon on loopback `127.0.0.1:9001`.
- optional Kokoro TTS daemon on loopback `127.0.0.1:9002`.
- optional formatter proxies on loopback unless intentionally exposed.
- SillyTavern extension storing the bearer token in ST extension settings.

## Baseline expectations

- `/health` may be public; protected APIs require bearer auth except loopback exemptions.
- Token-bearing query strings are redacted from server logs.
- Phone pairing tokens are session-scoped in the browser and scrubbed from the visible URL after bootstrap.
- Token rotation uses `dictation-server --rotate-token`; the safe default prints only the token path and operational next steps, not the token value.
- External model/provider calls are surfaced through `/audit/network`.
