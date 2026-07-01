# Setup Wizard Guide

`codex-switch setup` is the safest first command for new users. It turns a fresh install into a usable account-usage dashboard without asking users to manually copy tokens or guess which auth file matters.

## Recommended flows

### Desktop / browser available

```bash
codex-switch setup
```

### SSH, WSL, containers, or no browser

```bash
codex-switch setup --device --alias work-pro
```

### CI/docs smoke without starting OAuth

```bash
codex-switch setup --yes --skip-login
```

## What the wizard does

1. Creates `~/.codex-switch/` and `~/.codex-switch/profiles/`.
2. Writes a commented starter `~/.codex-switch/config.toml` if one does not already exist.
3. Checks for existing `~/.codex/auth.json`.
4. If existing Codex auth is present, offers to save it as a managed profile.
5. If no auth exists, starts OpenAI Codex OAuth login unless `--skip-login` is set.
6. Runs a usage check with `codex-switch list` after at least one profile exists.
7. Prints safe next steps for adding accounts, choosing the best profile, launching Codex, and opening the TUI.

The wizard never prints raw access tokens, refresh tokens, or full `auth.json` contents.

## Flags

| Flag | Purpose |
|---|---|
| `--device` | Use OpenAI device-code login instead of trying to open a browser. Best for SSH/WSL/headless machines. |
| `--alias <name>` | Suggested alias for the first profile. Aliases are validated before login starts. |
| `--yes`, `-y` | Accept safe non-destructive defaults. Useful for scripted setup. |
| `--skip-login` | Only prepare local directories/config and show next steps. Does not start OAuth. |

## No-footgun rules

- Keep OAuth tokens in `auth.json` files, not `.env`, shell history, tickets, Telegram, or GitHub issues.
- Use `--device` when the browser may be signed into the wrong OpenAI account.
- Use separate browser profiles/incognito windows when adding multiple OpenAI accounts.
- Run `codex-switch list` after setup to confirm the plan/usage windows for each saved account.
- Use `codex-switch launch -- <args>` when you want a Codex session to start with the selected account and then restore previous auth after startup.

## Troubleshooting

### The wrong account was saved

Rename or delete the bad profile, then login again in a clean/incognito browser session:

```bash
codex-switch rename old-name correct-name
codex-switch delete wrong-name
codex-switch setup --device --alias correct-name
```

### Usage check fails with HTTP 401/403

Re-authorize the profile:

```bash
codex-switch login --device <alias>
codex-switch list --force
```

### Existing `~/.codex/auth.json` is not a saved profile

Run setup and accept the save prompt, or save through `codex-switch list` interactively:

```bash
codex-switch setup
```

### You only want to package/test installation docs

Use the no-login smoke path:

```bash
HOME=$(mktemp -d) codex-switch setup --yes --skip-login
```
