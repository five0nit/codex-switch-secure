# Local access guide for Mike's codex-switch-secure fork

This fork is intended to run **locally**, not as a hosted web service.

## Installed local binary

A reviewed local build is installed at:

```bash
~/.local/bin/codex-switch-secure
```

Check it with:

```bash
~/.local/bin/codex-switch-secure --version
```

Expected repository line:

```text
https://github.com/five0nit/codex-switch-secure
```

## Run the dashboard/TUI

```bash
~/.local/bin/codex-switch-secure
```

or explicitly:

```bash
~/.local/bin/codex-switch-secure list
```

## Add/import accounts

Use a throwaway/test Codex account first.

Interactive OAuth login:

```bash
~/.local/bin/codex-switch-secure login test-account
```

Import an existing Codex `auth.json` backup:

```bash
~/.local/bin/codex-switch-secure import /path/to/auth.json test-account
```

## Where data is stored

The tool stores its own profiles under:

```text
~/.codex-switch/
```

Codex's live auth file remains:

```text
~/.codex/auth.json
```

Never paste either file into chat.

## Update policy

Runtime self-update is disabled. To update:

```bash
cd /home/fiv30nit/.openclaw/workspace/workspaces/codex-switch
git fetch origin
git status
# review any changes before merging/pulling
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo audit
cargo build --release --locked
cp target/release/codex-switch ~/.local/bin/codex-switch-secure
chmod 700 ~/.local/bin/codex-switch-secure
```

## Local hosting note

This specific project is a CLI/TUI account manager, not a browser-hosted dashboard. If Mike wants a browser page at `localhost:<port>`, build that as a separate local web dashboard that reads this tool's JSON output or uses Codex app-server directly.
