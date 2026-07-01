# Security Policy

## Auth/token handling

`codex-switch` manages OpenAI Codex OAuth payloads. Treat every `auth.json`, generated Share Auth command, and exported bundle like a password.

Rules for users and contributors:

- Never paste raw `auth.json`, access tokens, refresh tokens, or Share Auth payload installers into GitHub issues, chat, logs, or screenshots.
- The setup wizard and dashboard must show only short safe fingerprints, never full token values.
- OAuth tokens belong in `~/.codex/auth.json` and `~/.codex-switch/profiles/*/auth.json`, with private file permissions.
- Behavioral settings belong in `~/.codex-switch/config.toml`; do not document token storage in `.env`.
- If a token or payload is exposed, re-authorize/rotate that Codex account immediately.

## Local web UI

The local web dashboard is intended to bind to `127.0.0.1` only. Do not expose it to a LAN, public interface, tunnel, or reverse proxy unless you add separate authentication and understand that Share Auth payloads can move account credentials.

## Reporting vulnerabilities

If you find a vulnerability, open a private security advisory or contact the maintainer directly. Do not include secrets in the report; use redacted examples and safe fingerprints.
