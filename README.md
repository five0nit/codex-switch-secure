# codex-switch

**The multi-account manager for [OpenAI Codex CLI](https://github.com/openai/codex)** — manage unlimited accounts, monitor live quota across all of them, and auto-switch to the best one before each session.

[**中文文档 →**](README_CN.md)

---

### Local web dashboard

![Local web dashboard showing Codex account cards with plan labels, 5h and weekly usage windows, health status, Auth, Share Auth, and Refresh actions](docs/web-dashboard.jpg)

### TUI

![TUI](docs/tui.png)

### CLI

![CLI](docs/cli.png)

## Features

- **Profile Management** — Save, switch, rename, delete Codex accounts
- **Auto-Detection** — Automatically discovers and tracks the current `auth.json`
- **Usage Dashboard** — Live quota monitoring (5h and 7d windows) with status indicators and per-account refresh timestamps
- **Adaptive Auto-Switch** — `codex-switch use` without arguments ranks accounts with a unified 5-component scoring algorithm, with Team accounts prioritized by default
- **Background Daemon (Beta)** — Optional `daemon` command keeps the next session ready by monitoring usage and switching automatically when the current account crosses a threshold
- **Stale-Only Refresh** — `use`, `list`, and TUI refresh only accounts whose cached usage has expired
- **Progress Display** — Long-running `use`, `list`, and directory `import` operations show a single-line cross-platform progress indicator
- **Interactive TUI** — Full terminal UI with live usage data, color-coded status, and keyboard shortcuts
- **OAuth Login** — Built-in PKCE browser login flow, no manual token copying
- **Token Auto-Refresh** — Automatically refreshes expired tokens using refresh_token
- **Validated Bulk Import** — Import a single `auth.json` or recursively scan a directory, validate files, and auto-assign unique aliases
- **Pace Marker** — Visual indicator on usage bars showing expected consumption based on elapsed window time
- **Warmup** — `warmup` sends a minimal request to activate the quota window countdown, skipping already-active accounts
- **Manual Self-Update** — `self-update --check` checks GitHub Releases on demand; `self-update` installs the latest release (supports stable and dev channels)
- **Launch with Profile** — `launch` starts Codex CLI with a specific (or best) profile's auth, transparently forwarding all arguments. Auth is swapped only during startup, then immediately restored
- **Over-Pace Warning** — Red `!` indicator on 5h/7d columns when usage exceeds expected pace
- **Proxy Support** — HTTP/HTTPS/SOCKS4/SOCKS5/SOCKS5H with authentication
- **Cross-Platform** — macOS, Linux, Windows (full RGB color palette for consistent TUI rendering)
- **JSON Output** — `--json` flag for scripting and automation

## Installation

### One-Liner (Recommended)

**macOS / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/five0nit/codex-switch-secure/master/scripts/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/five0nit/codex-switch-secure/master/scripts/install.ps1 | iex
```

### Homebrew (macOS / Linux)

```bash
brew install xjoker/tap/codex-switch
```

### Dev Build (Latest Development Version)

**macOS / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/five0nit/codex-switch-secure/master/scripts/install.sh | bash -s -- --dev
```

**Windows (PowerShell):**

```powershell
$env:CS_DEV="1"; irm https://raw.githubusercontent.com/five0nit/codex-switch-secure/master/scripts/install.ps1 | iex
```

### Uninstall

**macOS / Linux:**

```bash
curl -fsSL https://raw.githubusercontent.com/five0nit/codex-switch-secure/master/scripts/install.sh | bash -s -- --uninstall
```

**Windows (PowerShell):**

```powershell
$env:CS_UNINSTALL="1"; irm https://raw.githubusercontent.com/five0nit/codex-switch-secure/master/scripts/install.ps1 | iex
```

### From GitHub Releases (Manual)

Download pre-built binaries from [Releases](https://github.com/five0nit/codex-switch-secure/releases):

| Platform | Architecture | File |
|----------|-------------|------|
| macOS | Apple Silicon (M1/M2/M3) | `cs-darwin-arm64.tar.gz` |
| macOS | Intel | `cs-darwin-amd64.tar.gz` |
| Linux | x86_64 | `cs-linux-amd64.tar.gz` |
| Linux | ARM64 | `cs-linux-arm64.tar.gz` |
| Windows | x86_64 | `cs-windows-amd64.zip` |
| Windows | ARM64 | `cs-windows-arm64.zip` |

### From Source

Requires [Rust](https://rustup.rs/) 1.88+:

```bash
git clone https://github.com/five0nit/codex-switch-secure.git
cd codex-switch-secure
cargo build --release
# Binary: target/release/codex-switch (or target\release\codex-switch.exe on Windows)
sudo cp target/release/codex-switch /usr/local/bin/  # macOS/Linux
```

## Quick Start

### Guided setup wizard (recommended)

Run the wizard first so you don't accidentally mix accounts, skip config, or paste auth payloads into the wrong shell:

```bash
codex-switch setup
```

Headless server / SSH / WSL-safe setup:

```bash
codex-switch setup --device --alias work-pro
```

The wizard:

- creates `~/.codex-switch/` and a starter `config.toml` if missing,
- detects an existing `~/.codex/auth.json` and offers to save it as a managed profile,
- starts browser or device-code OAuth login when no auth exists,
- verifies with `codex-switch list`, and
- prints safe next steps without exposing raw tokens.

See [Setup Wizard Guide](docs/setup-wizard.md) for the no-footgun flow and troubleshooting.

### Manual flow

```bash
# 1. Log in to your first Codex account
codex-switch login

# 1b. On headless servers (no browser), use device code flow:
codex-switch login --device

# 2. Log in to another account
codex-switch login

# 3. See all accounts with live usage
codex-switch list

# 4. Switch to a specific account
codex-switch use alice

# 5. Auto-switch to the best available account
codex-switch use

# 6. Launch interactive TUI
codex-switch tui

# 7. Launch Codex with the best available account
codex-switch launch

# 8. Launch Codex with a specific account
codex-switch launch alice -- --model gpt-4o

# 9. Start the background daemon (Beta, optional)
codex-switch daemon start

# 10. Check for a new release manually
codex-switch self-update --check
```

## Tutorial: how it works

`codex-switch` is a small account router for Codex CLI. It keeps your saved accounts in one local profile store, checks each account's current quota window, and can switch or launch Codex with the best available account.

### The mental model

| Piece | Location | What it does |
|---|---|---|
| Live Codex auth | `~/.codex/auth.json` | The auth file Codex CLI reads when it starts. Only one account is active here at a time. |
| Saved profiles | `~/.codex-switch/profiles/<alias>/auth.json` | Private copies of each account's Codex OAuth payload. |
| Active profile marker | `~/.codex-switch/current` | Records which profile `codex-switch use` selected last. |
| Config | `~/.codex-switch/config.toml` | Non-secret behavior settings: cache TTL, proxy, scoring preferences, daemon options. |
| Usage cache | `~/.codex-switch/cache.json` | Short-lived quota results so the tool does not hammer the usage endpoint. |

The tool does **not** invent new OpenAI credentials. It organizes Codex OAuth auth files you already own and refreshes them through OpenAI's normal token refresh path.

### First install, no footguns

1. Install the binary:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/five0nit/codex-switch-secure/master/scripts/install.sh | bash
   ```

2. Run the setup wizard:

   ```bash
   codex-switch setup
   ```

   If you're on SSH, WSL, a server, or a browser might be logged into the wrong OpenAI account, use device-code login:

   ```bash
   codex-switch setup --device --alias main-pro
   ```

3. Confirm the account appears and usage can be fetched:

   ```bash
   codex-switch list
   ```

4. Add another account when you have one:

   ```bash
   codex-switch login --device spare-pro
   codex-switch list --force
   ```

5. Before starting work, let the tool choose the best available account:

   ```bash
   codex-switch use
   codex
   ```

   Or launch Codex through the safer wrapper:

   ```bash
   codex-switch launch -- --model gpt-5
   ```

### What happens when you run `codex-switch use`

1. Reads every saved profile under `~/.codex-switch/profiles/`.
2. Fetches or reuses cached usage for each account.
3. Scores accounts by 5h usage, 7d usage, reset timing, plan type, and recent use.
4. Copies the chosen profile's `auth.json` into `~/.codex/auth.json`.
5. Updates `~/.codex-switch/current`.

After that, a normal `codex` command starts with the selected account.

### What happens when you run `codex-switch launch`

`launch` is for people who do not want their live `~/.codex/auth.json` permanently changed:

1. Selects the requested alias, or the best available account if you omit it.
2. Temporarily stages that profile into `~/.codex/auth.json`.
3. Starts Codex with your forwarded args.
4. Restores the previous live auth file after startup.

Example:

```bash
codex-switch launch spare-pro -- --model gpt-5 --approval-mode suggest
```

### Understanding the usage display

`codex-switch list` shows quota windows from OpenAI's Codex usage endpoint:

- `5h` / primary window: short rolling session quota.
- `7d` / weekly window: longer rolling weekly quota.
- Spark/model-specific windows may appear separately when OpenAI exposes `additional_rate_limits`.
- Usage percentages are allowance/quota windows, not an all-time billing ledger.

Use `--force` when you want fresh data instead of the short-lived cache:

```bash
codex-switch list --force
```

### Recommended daily workflow

```bash
# Start of day / before a big Codex session
codex-switch list --force
codex-switch use
codex

# If you want temporary account selection instead of changing live auth
codex-switch launch -- --model gpt-5

# If a fresh account has no reset timers yet
codex-switch warmup <alias>
```

### Safety checklist before sharing auth between machines

- Treat Share Auth payload installers like passwords.
- Only paste payload installers into machines/users you trust.
- Prefer the generated Bash installer in Linux/macOS/WSL shells and the PowerShell → WSL installer from Windows Terminal into WSL.
- Never paste payloads into GitHub, Telegram, Discord, CI logs, or bug reports.
- If a payload leaks, re-authorize/rotate that OpenAI account.

## Web dashboard option

Run the web dashboard when you want a visual view of all accounts, easier one-click auth switching on the current machine, or copy/paste Share Auth commands for another trusted machine.

```bash
# From a source checkout
python3 local-web/server.py

# If codex-switch is not on PATH, point the dashboard at it explicitly
CODEX_SWITCH_BIN=/absolute/path/to/codex-switch python3 local-web/server.py
```

Then open:

```text
http://localhost:8787/
```

The dashboard binds to `127.0.0.1` only. It shows account cards, plan/usage windows, editable display names, refresh buttons, and **Share Auth** actions. Share Auth can generate:

- a same-machine `codex-switch use <alias>` repoint command,
- a Bash payload installer for Linux/macOS/WSL,
- a PowerShell → WSL installer for Windows Terminal users targeting WSL, and
- a native Windows installer for Codex running outside WSL.

Those payload installers contain OAuth material. Treat them like passwords and only paste them into machines you trust.

See [Local web UI](local-web/README.md) for the full dashboard guide.

## Commands

| Command | Description |
|---------|-------------|
| `codex-switch setup [--device] [--alias <name>] [--yes] [--skip-login]` | Guided first-run wizard: creates starter config, captures existing auth or starts login, verifies usage, and prints safe next steps |
| `codex-switch use [alias] [--force]` | Switch to a profile. Omit alias to auto-select with the adaptive scoring algorithm. `--force` skips the running-process warning |
| `codex-switch list [-f]` | List all profiles with account info, usage, and availability (`-f` force refresh) |
| `codex-switch launch [alias] [-- args...]` | Launch Codex CLI with a profile's auth. Omit alias to auto-select with adaptive scoring. All arguments after `--` are forwarded to codex |
| `codex-switch warmup [alias]` | Send a minimal request to start the 5h/7d quota window countdown. Omit alias to warm up all profiles |
| `codex-switch login [--device] [alias]` | Log in via OAuth (`--device` for headless servers). If alias exists, re-authorizes |
| `codex-switch rename <old> <new>` | Rename a profile |
| `codex-switch delete <alias>` | Delete a profile |
| `codex-switch import <path> [alias]` | Import one auth.json file, or recursively validate and import all JSON files under a directory |
| `codex-switch daemon start [--foreground]` | Start the auto-switch daemon (Beta). Detached by default; use `--foreground` for service managers |
| `codex-switch daemon stop` | Stop a running Beta daemon |
| `codex-switch daemon status` | Show Beta daemon status |
| `codex-switch daemon install` | Install the Beta daemon as a user service (LaunchAgent on macOS, systemd user service on Linux) |
| `codex-switch daemon uninstall` | Remove the Beta daemon user service |
| `codex-switch self-update [--check] [--dev]` | Manually check GitHub Releases or update the current direct-install binary. `--dev` switches to the dev channel |
| `codex-switch tui` | Launch the interactive terminal UI |
| `codex-switch open` | Open the config directory in file manager |

### Global Options

| Option | Description |
|--------|-------------|
| `--json` | Output as compact JSON (for scripting/pipes) |
| `--json-pretty` | Output as pretty-printed JSON |
| `--proxy <URL>` | Set proxy (see [Proxy](#proxy-support) section) |
| `--color <auto\|always\|never>` | Color output mode (default: auto) |
| `--debug` | Enable debug logging (shows HTTP requests, API responses, cache status) |
| `-V, --version` | Print version |

## TUI Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `j` / `k` or `Up` / `Down` | Navigate accounts |
| `Enter` | Switch to selected account |
| `/` | Search / filter accounts |
| `r` | Refresh usage data (marked accounts, or all if none marked) |
| `a` | Toggle auto-refresh for all accounts (usage, active profile detection, warmup checks) |
| `s` | Cycle sort mode (name / quota / status) |
| `Space` | Mark / unmark account for batch operations |
| `w` | Warm up accounts (marked accounts, or all if none marked) |
| `c` | Clear all marks |
| `n` | Rename selected profile |
| `d` | Delete selected profile (with confirmation) |
| `q` / `Esc` | Quit |

## Updating

Update checks are manual except for TUI launch. `codex-switch tui` checks once at startup; `startup`, `list`, and `use` do not check automatically.

```bash
# Check whether a newer release exists
codex-switch self-update --check

# Update a direct install to the latest release
codex-switch self-update

```

- Homebrew installs are not self-overwritten. Use `brew upgrade xjoker/tap/codex-switch`.
- Direct installs verify the release `.sha256` before replacing the current executable.
- Use `--dev` to install the latest dev build. Run `self-update` (without `--dev`) to return to the stable channel.
- Homebrew users must `brew uninstall codex-switch` before using `--dev`.

## Proxy Support

Proxy resolution priority (highest to lowest):

1. `--proxy` CLI flag
2. `CS_PROXY` environment variable
3. Config file `~/.codex-switch/config.toml`
4. Standard environment variables (`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`)

### Supported Protocols

| Protocol | DNS Resolution | Auth |
|----------|---------------|------|
| `http://[user:pass@]host:port` | Local | Yes |
| `https://[user:pass@]host:port` | Local | Yes |
| `socks4://host:port` | Local | No |
| `socks5://[user:pass@]host:port` | Local | Yes |
| `socks5h://[user:pass@]host:port` | Remote (via proxy) | Yes |

### Config File

`~/.codex-switch/config.toml`:

```toml
[proxy]
url = "socks5h://user:pass@127.0.0.1:1080"
no_proxy = "localhost,127.0.0.1"

[cache]
ttl = 300  # Cache TTL in seconds (default: 300)

[network]
max_concurrent = 20  # Max concurrent usage requests (default: 20)

[tui]
auto_refresh_interval_secs = 120  # Auto-refresh interval in seconds (default: 120, minimum: 30)

[use]
safety_margin_7d = 20       # Weekly safety margin used by adaptive scoring (default: 20)
team_priority = true        # Prefer Team accounts with a +500 tier bonus (default: true)

[daemon]
poll_interval_secs = 60         # Usage poll interval (default: 60)
switch_threshold = 80           # Switch when current 5h usage >= this % (default: 80)
token_check_interval_secs = 300 # Background token refresh check interval (default: 300)
notify = false                  # Desktop notification on switch (default: false)
log_level = "error"             # Daemon log level (default: "error")

[launch]
restore_delay_secs = 3          # Seconds to wait before restoring auth.json after codex starts (default: 3)
```

### Examples

```bash
# CLI flag
codex-switch --proxy socks5h://127.0.0.1:1080 list

# Environment variable
export CS_PROXY="http://user:pass@proxy.corp.com:8080"
codex-switch list

# Standard env var (reqwest reads this automatically)
export HTTPS_PROXY="http://proxy.corp.com:8080"
codex-switch list
```

## Common Usage Scenarios

### Auto-switch before each Codex session

```bash
# Add to your shell profile (.zshrc / .bashrc):
codex-switch use && codex
```

### Keep the next session ready with the daemon (Beta)

Use the Beta daemon when you want `codex-switch` to monitor the current account continuously and prepare the next Codex launch in the background:

```bash
# Start a detached daemon
codex-switch daemon start

# Check whether it is running
codex-switch daemon status

# Stop it
codex-switch daemon stop

# Install/remove a user service
codex-switch daemon install
codex-switch daemon uninstall
```

The Beta daemon uses the same adaptive scoring logic as `codex-switch use`. It refreshes the current account on each poll, switches only when `daemon.switch_threshold` is met or exceeded and a better candidate exists, and refreshes expiring tokens on a separate timer. It prepares future Codex launches; an already-running Codex process still needs to be restarted after a switch.

### Scheduled token refresh via cron (optional)

Keep cached usage data and tokens fresh in the background so `codex-switch use` is instant:

```bash
# Edit crontab
crontab -e

# Refresh all account usage every 5 minutes
*/5 * * * * /usr/local/bin/codex-switch list --json > /dev/null 2>&1
```

This runs `codex-switch list` periodically, which refreshes stale tokens and caches usage data. It does **not** switch accounts automatically.

### Use in CI / automation

```bash
# Switch to best account and launch Codex in one line
codex-switch use --json && codex --quiet ...
```

## Troubleshooting

If you encounter errors, run with `--debug` to see detailed HTTP requests, API responses, and cache status:

```bash
codex-switch --debug list
codex-switch --debug use
```

If the issue persists, please [open an issue](https://github.com/xjoker/codex-switch/issues) with the debug output attached (remember to redact any sensitive information like tokens or emails).

## How It Works

### File Locations

| Path | Description |
|------|-------------|
| `~/.codex/auth.json` | Live Codex CLI auth file (or `$CODEX_HOME/auth.json`) |
| `~/.codex-switch/profiles/<alias>/auth.json` | Saved profile data |
| `~/.codex-switch/current` | Currently active profile name |
| `~/.codex-switch/auth.lock` | File lock that serializes live `auth.json` switches |
| `~/.codex-switch/config.toml` | Configuration file |

### Auto-Detection

On every interactive launch, codex-switch compares the live `~/.codex/auth.json` against all saved profiles:

- **New account detected** (e.g., you ran `codex login`) — prompts to save as a new profile
- **Tokens refreshed** for an existing account — prompts to update that profile
- **Non-interactive environments** (pipes, cron, CI) — reports the change but never mutates state silently

When you run `codex-switch list` or `codex-switch tui`, the tool also checks if the live auth.json belongs to an untracked account and automatically saves it as a new profile (using the email username as alias).

### Deduplication

On login or import, the tool matches accounts by `account_id` (primary) or `email` (fallback). If the same account already exists under a different alias, it updates the existing profile instead of creating a duplicate.

### Import Validation

`codex-switch import` validates every candidate file in stages:

1. File format — valid JSON
2. Structure — required `tokens` fields and a decodable `id_token`
3. Usage validation — token refresh and usage API check, unless explicitly skipped in tests
4. Save — deduplicate by identity and assign a unique alias if needed

If the input path is a directory, the command scans recursively for `.json` files and reports imported vs skipped files.

### Smart Auto-Switch (`codex-switch use`)

When called without an alias, `codex-switch use` ranks every account with a single adaptive algorithm. It first reuses fresh cache entries and only refreshes stale accounts.

The algorithm uses a **two-phase** approach:
1. **Eligibility gate** — filters accounts that are exhausted, weekly-critical with a distant reset, or below the Free-plan safety floor. If **all** accounts are ineligible, the highest-scoring one is used as a fallback.
2. **Adaptive score** — ranks the remaining accounts with five components:

```text
score = tier_bonus + headroom + sustain + drain_value + recency
```

- `tier_bonus` (0 or +500) — Team accounts are preferred by default when `team_priority = true`. This is a priority, not a guarantee: exhausted or unsafe Team accounts can still lose or be filtered out.
- `headroom` (0..1100) — Pace-aware 5h capacity based on burn rate and time-to-reset, not just static remaining%.
- `sustain` (-800..0) — 7d budget-per-window safety penalty.
- `drain_value` (0..300) — Bonus for spending quota that will reset within 60 minutes; the weight adapts to pool size and exhaustion ratio.
- `recency` (-60..0) — Small spread penalty to avoid repeatedly hammering the same account.

This replaces `max-remaining`, `drain-first`, and `round-robin`. There is no mode selection in v0.0.13+.

> **Note:** After switching accounts, you must **restart Codex** for it to pick up the new `auth.json`. The Codex CLI reads `auth.json` at startup and does not watch for file changes.

#### Eligibility Gate

Accounts are marked **ineligible** when:
- 5h window is fully exhausted (>=100%)
- 7d window is fully exhausted (>=100%)
- 7d remaining is below the critical threshold (`25%` of `safety_margin_7d`, minimum `1%`) and the 7d reset is more than 48 hours away
- Free plan account has fallen below the built-in 5h safety floor

Ineligible accounts are excluded from selection unless ALL accounts are ineligible, in which case the best-scoring one is used as a last resort.

### Auto-Switch Configuration

`[use]` now has only two knobs:

- `safety_margin_7d` — Weekly safety threshold used by the sustain component and the eligibility gate
- `team_priority` — Default `true`; grants Team accounts a `+500` tier bonus

Legacy `mode` and `min_remaining` are ignored with a warning in v0.0.13+.

### Cache Behavior

- Usage cache is stored per profile alias in `~/.codex-switch/cache.json`
- Each cached entry keeps its own refresh timestamp; JSON output exposes it as `usage.fetched_at`
- `list`, `use`, and the TUI only refresh stale accounts by default
- `list -f` and TUI `r` bypass cache and force a refresh for all accounts
- Directory import always validates every file and shows progress as it advances

### Token Auto-Refresh

When a usage query returns HTTP 401/403, the tool automatically attempts to refresh the token using the stored `refresh_token`. If successful, the new tokens are persisted back to the profile and the live auth.json.

### Safety Notes

- CLI and TUI both refuse to delete the active profile
- JSON mode keeps stdout machine-readable; human progress/messages go to stderr instead

## Platform Notes

### macOS

- Default Codex auth path: `~/.codex/auth.json`
- Browser opens via system `open` command
- File manager opens via `open`

### Linux

- Default Codex auth path: `~/.codex/auth.json`
- Browser opens via `xdg-open` (ensure a desktop browser is configured)
- File manager opens via `xdg-open`
- WSL: browser opening may require `wslu` package (`sudo apt install wslu`)
- **Headless servers (no browser):** Use `codex-switch login --device` for device code flow — displays a URL and code to enter on any device with a browser

### Windows

- Default Codex auth path: `%USERPROFILE%\.codex\auth.json`
- Browser opens via `rundll32.exe url.dll,FileProtocolHandler` (fallback: `webbrowser` crate)
- File manager opens via `explorer.exe`
- Terminal: works with Windows Terminal, PowerShell, and cmd.exe
- TUI rendering uses Windows Console API via `crossterm`
- **Recommended terminal: [Windows Terminal](https://aka.ms/terminal).** Git Bash (mintty) has known compatibility issues with TUI rendering — use Windows Terminal or PowerShell instead

## JSON Output

Most commands support `--json` for machine-readable output (except `tui` and `open`):

```bash
# List profiles as JSON
codex-switch --json list

# Switch and get result
codex-switch --json use alice

# Check updates in JSON mode
codex-switch --json self-update --check
```

## Building

```bash
# Debug build
cargo build

# Release build (optimized, stripped)
cargo build --release

# Cross-compile for Linux (from macOS)
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl

# Cross-compile for Windows (from macOS/Linux)
rustup target add x86_64-pc-windows-gnu
cargo build --release --target x86_64-pc-windows-gnu
```

## Releasing

Maintainer-only. See [docs/RELEASE.md](docs/RELEASE.md) for the full procedure (dev rolling tag, stable tag, refspec gotchas).

## Changelog

See [docs/CHANGELOG.md](docs/CHANGELOG.md) for a detailed list of changes in each release.

## License

[MIT](LICENSE)
