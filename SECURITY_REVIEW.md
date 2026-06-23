# Security review notes for Mike's fork

Fork/mirror: `five0nit/codex-switch-secure`
Upstream: `xjoker/codex-switch`
Initial reviewed upstream commit: `c6a2567c7d111f2a3cc19b4f8462103b6826b787`
License: MIT

## Threat model

This tool handles Codex/OpenAI OAuth credentials (`auth.json`) and calls ChatGPT/Codex usage endpoints. Treat it as sensitive local infrastructure. The main risks are token exfiltration, unsafe account switching, untrusted self-updates, and accidental leakage in logs.

## Initial findings

### Good signs

- MIT licensed.
- Auth writes use atomic temp-file + rename with restrictive Unix permissions (`0600` for auth files, `0700` for parent dirs).
- Sensitive JSON fields are redacted before debug logging in the existing helper.
- Proxy URL logging masks userinfo.
- The app mostly talks to expected OpenAI/ChatGPT endpoints plus GitHub Releases for update checks.
- Unit/integration tests exist and cover auth/profile/usage behavior.

### Risks found

1. **Endpoint override environment variables**
   - Upstream allowed `CS_TOKEN_URL`, `CS_USAGE_URL`, and `CS_GITHUB_API_URL` directly.
   - On an operator machine, a poisoned environment could redirect OAuth refresh tokens or usage bearer tokens to an attacker-controlled endpoint.

2. **Self-update trust boundary**
   - Upstream self-update targets `xjoker/codex-switch` GitHub releases.
   - Checksums are fetched from the same release source, so this protects against corruption but not a compromised release publisher.

3. **OAuth material is stored locally**
   - Expected for this class of tool, but the repo should be treated like credential-handling software. Do not run random forks/builds against real accounts.

## Hardening applied in this fork

- Added `auth::allow_insecure_endpoint_overrides()`.
- `CS_TOKEN_URL`, `CS_USAGE_URL`, and `CS_GITHUB_API_URL` are ignored unless `CS_ALLOW_INSECURE_ENDPOINT_OVERRIDES=1` is set explicitly.
- Retargeted self-update release lookup from upstream to `five0nit/codex-switch-secure`, so our hardened binary will not auto-replace itself from upstream releases.
- Updated the integration test harness to opt into endpoint overrides only for local mock-server tests.

## Verification run

- Installed Rust toolchain locally via rustup because WSL did not have `rustc`/`cargo`.
- `cargo test --locked` passed after hardening.
- `cargo clippy --locked --all-targets -- -D warnings` passed.
- `cargo audit` initially found `RUSTSEC-2026-0185` in transitive `quinn-proto 0.11.14`; updated `Cargo.lock` to `quinn-proto 0.11.15` and reran `cargo audit` clean.
- Daemon integration initially failed because the new endpoint guard blocked its mock server. The test harness was patched to set `CS_ALLOW_INSECURE_ENDPOINT_OVERRIDES=1` only for local mock-server tests.

## Remaining recommended work before real OAuth accounts

1. Add CI in the private fork for test/clippy/audit.
2. Consider disabling `self-update` entirely or requiring signed releases before distributing binaries.
3. Run the tool first with throwaway/test Codex auth only.

## Operational policy

- Do not paste real `auth.json` into chat.
- Keep the repo private.
- Build from source in this fork; avoid upstream install scripts for production use.
- Never enable `CS_ALLOW_INSECURE_ENDPOINT_OVERRIDES=1` outside local test harnesses.
