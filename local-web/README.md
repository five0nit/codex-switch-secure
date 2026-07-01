# Local web UI

Local browser dashboard for `codex-switch`: account cards, usage windows, safe auth sharing for trusted machines, and local telemetry views.

## Run

```bash
cd /path/to/codex-switch-secure
python3 local-web/server.py
```

Open:

```text
http://localhost:8787/
```

The server binds to `127.0.0.1` only.

## Initial account links

```text
http://localhost:8787/auth/pro-1
http://localhost:8787/auth/pro-2
http://localhost:8787/auth/business-seat
```

Each link starts an OpenAI device-code login flow for that alias and displays the OpenAI device page plus the short code to enter.

Use separate browser profiles/incognito windows when authorizing different OpenAI accounts, otherwise the browser may silently reuse the currently signed-in account.

## Account display names

Each account card has an editable display-name field. Click **Save name** to persist the label.

Drag cards left/right using the card body or **↔ drag to reorder** handle. The order is saved to `~/.codex-switch/local-web-config.json` and persists across refreshes/restarts.

The account cards are laid out as one horizontal strip so all accounts can be scanned left-to-right. Use horizontal scroll if your window is not wide enough.

Each card also has a **Remove** button. It removes the dashboard slot and deletes the saved local codex-switch profile for that alias when present.

Labels are stored in:

```text
~/.codex-switch/local-web-config.json
```

This only changes the web-dashboard label; it does not rewrite OAuth tokens.

## Sharing / repointing auth

Each connected account card has a **Share Auth** button for rate-limit failover.

It shows two command styles:

1. **Same-machine repoint** — safest/fastest when the impacted Hermes/Codex agent runs as the same Linux user:

```bash
~/.local/bin/codex-switch-secure use <alias>
```

2. **Payload installers** — copy/paste commands for another trusted machine/user. These commands contain the OAuth auth payload and write it to the target Codex `auth.json`.

The Share Auth modal now emits three installer variants:

- **PowerShell → WSL** — use this when copying from Windows Terminal/PowerShell but the target Hermes/Codex agent runs inside WSL. It pipes the base64 payload into `wsl.exe bash -lc ...` and writes WSL `~/.codex/auth.json`.
- **Bash** — use this in Linux/macOS/WSL bash terminals. It uses a bash heredoc and must not be pasted directly into PowerShell.
- **Native Windows** — use only when Codex runs outside WSL and reads `%USERPROFILE%\.codex\auth.json`.

Security rules:

- Treat the payload installer like a password.
- Do not paste it into Telegram, GitHub, logs, or shared docs.
- Only use it on your own trusted agent machines.
- If exposed, re-auth/rotate that account.
- The dashboard also writes a chmod-600 local bundle under `~/.codex-switch/share/` for file-copy workflows.

## Auto refresher

The web server starts a background scheduler automatically.

Schedule:

```text
Every 30 minutes, Monday-Friday, 07:00-18:00 Australia/Melbourne time
```

The scheduled refresher calls:

```bash
~/.local/bin/codex-switch-secure --json list --force
```

Manual refresh is also available from the dashboard button, and programmatically at:

```bash
curl -X POST http://localhost:8787/api/refresh
```

## Adding more auth

Use the **Add auth** card in the dashboard:

1. Choose provider:
   - **OpenAI Codex OAuth** — existing device-code flow, unchanged.
   - **Anthropic API key** — saves a local key entry to `~/.codex-switch/anthropic-accounts.json` with `chmod 600`.
   - **Grok / xAI API key** — saves a local key entry to `~/.codex-switch/grok-accounts.json` with `chmod 600`.
2. Enter a display name.
3. Optionally enter an internal alias, e.g. `spare-pro-1`, `anthropic-admin`, or `xai-main`.
4. For Codex, click **Add auth** and complete the OpenAI device-code auth page.
5. For Anthropic/Grok, paste the full API key and click **Add auth**. The dashboard saves the key locally and refreshes the corresponding validation panel. Raw keys are never returned by the API response.

Anthropic note: normal API key validation uses `/v1/models`; usage/cost numbers require Anthropic Admin Usage & Cost API access, so a valid normal key can still show admin blocked.

Grok/xAI note: validation uses `https://api.x.ai/v1/models`; usage/spend/credit remain unavailable unless xAI exposes a public usage endpoint for the key.

Device codes are valid for roughly 15 minutes. If a code fails or expires, the auth page now shows **Start a fresh auth code**; use that instead of trying to reuse the old code.

You can also add/update an account slot programmatically with:

```bash
curl -X POST http://localhost:8787/api/accounts/name \
  -H 'Content-Type: application/json' \
  -d '{"alias":"spare-pro-1","label":"Spare Pro 1"}'
```

## Machine usage via Google Sheet

Use **Option C** with one shared Google Sheet. The dashboard reads a CSV export/publish URL and ranks machines by local observed token usage.

Template columns are in `local-web/google-sheet-template.csv`:

```csv
agent,machine,os,generated_at,current_account_alias,auth_fingerprint,tokens_30m,tokens_1h,tokens_2h,tokens_3h,tokens_24h,tokens_7d,thread_count_24h,thread_count_7d,last_used_at,rate_limit_errors_24h
```

Recommended sheet structure:

- Google Drive folder: `Codex Usage Telemetry`
- Google Sheet: `Codex Machine Usage Telemetry`
- Tab: `machine_usage`
- One row per machine/agent.
- Each machine updates only its own row every 15 minutes.
- Token windows include `tokens_30m`, `tokens_1h`, `tokens_2h`, `tokens_3h`, `tokens_24h`, and `tokens_7d` for granular short-term routing.
- Dashboard scrapes the Sheet CSV every 30 minutes, caches the result locally, and provides a usage-log filter by agent, machine, account, OS, or status.

Set the source from the dashboard's **Machine usage — Google Sheet** card, or programmatically:

The **Auth setup links → Sharefile setup** box generates a copy/paste agent instruction for remote machines. It includes the Sheet edit/CSV URLs, required columns, local Codex data sources, cron schedule, security rules, and verification checklist for installing a 15-minute background updater job on each machine.

```bash
curl -X POST http://localhost:8787/api/machine-usage/source \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://docs.google.com/spreadsheets/d/<SHEET_ID>/export?format=csv&gid=<GID>"}'
```

Read current rollup:

```text
http://localhost:8787/api/machine-usage
```

Important limitation: this is local observed telemetry pushed by each machine, not an official OpenAI per-machine billing ledger.

## Usage details

Account cards show both allowance windows with exact local reset date/time, e.g. the 5h reset and 7d/weekly reset.

The **Token usage explorer** has start/end date-time filters and reads local Codex state SQLite thread counters from:

```text
~/.codex/state_*.sqlite
```

Important limitation: OpenAI's current Codex usage endpoint exposes allowance percentages and reset times, but not official token totals per workgroup seat. The token explorer is therefore local observed Codex thread usage for this machine, not authoritative OpenAI billing/quota data and not reliably attributable to a specific seat unless Codex records that metadata in future.

Scheduler state is visible in the dashboard and in:

```text
http://localhost:8787/api/config
```

## Security notes

- The web UI never displays access tokens, refresh tokens, or raw `auth.json`.
- It shells out to the reviewed local binary: `~/.local/bin/codex-switch-secure`.
- Login subprocesses use device-code flow and write profiles through codex-switch itself.
- Do not expose this service beyond localhost.
