# Local access guide

This project is intended to run **locally**, not as a hosted public web service.

## Installed local binary

A local source build can be installed at any path on your `PATH`, for example:

```bash
~/.local/bin/codex-switch
```

Check it with:

```bash
codex-switch --version
```

Expected repository line:

```text
https://github.com/five0nit/codex-switch-secure
```

## First run

Use the guided wizard:

```bash
codex-switch setup
```

For SSH/headless/WSL use:

```bash
codex-switch setup --device --alias main-pro
```

## Run the TUI or CLI dashboard

```bash
codex-switch list
codex-switch tui
```

## Add/import accounts

Use a throwaway/test Codex account first if you are evaluating the tool.

Interactive OAuth login:

```bash
codex-switch login test-account
```

Import an existing Codex `auth.json` backup:

```bash
codex-switch import /path/to/auth.json test-account
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

Never paste either file into chat, tickets, logs, screenshots, or GitHub issues.

## Update policy

Runtime self-update is disabled in this security-focused fork. To update from source:

```bash
git fetch origin
git status
# review any changes before merging/pulling
cargo test --locked
cargo build --release --locked
cp target/release/codex-switch ~/.local/bin/codex-switch
chmod 700 ~/.local/bin/codex-switch
```

## Local hosting note

This project is primarily a CLI/TUI account manager. The optional `local-web/` dashboard should bind to localhost only and must not be exposed publicly without adding authentication.
