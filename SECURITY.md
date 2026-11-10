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

- `/`, `/health`, `/manifest.json` and `/pair/exchange` are reachable without the bearer (remote callers without it get minimal content); `/events` accepts `?token=`; everything else requires the bearer except trusted loopback requests. Full matrix: [PRIVACY.md → Authentication](PRIVACY.md#authentication).
- Token-bearing query strings are redacted from server logs.
- Phone pairing URLs carry a short-lived one-use code; the phone keeps the exchanged token in session-scoped browser storage and scrubs the code from the visible URL.
- Token rotation uses `dictation-server --rotate-token`; the safe default prints only the token path and operational next steps, not the token value. A running server keeps the old token until restarted.
- `/audit/network` records the server's own outbound connections (host, port, bytes, loopback flag). Formatter traffic goes to the loopback OmniRoute gateway, so the gateway's onward hop to the upstream provider is **not** visible there — only in OmniRoute's own logs.
