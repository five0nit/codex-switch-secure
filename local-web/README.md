# Local web UI

Local browser dashboard for Mike's private `codex-switch-secure` fork.

## Run

```bash
cd /home/fiv30nit/.openclaw/workspace/workspaces/codex-switch
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

2. **Payload installer** — copy/paste command for another trusted machine/user. This command contains the OAuth auth payload and writes it to `~/.codex/auth.json` on the target.

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

## Adding more accounts

Use the **Add account** card in the dashboard:

1. Enter a display name.
2. Optionally enter an internal alias, e.g. `spare-pro-1`.
3. Click **Add account**.
4. The dashboard opens that account's auth page and starts the same OpenAI device-code flow.

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
agent,machine,os,generated_at,current_account_alias,auth_fingerprint,tokens_24h,tokens_7d,thread_count_24h,thread_count_7d,last_used_at,rate_limit_errors_24h
```

Recommended sheet structure:

- Google Drive folder: `Codex Usage Telemetry`
- Google Sheet: `Codex Machine Usage Telemetry`
- Tab: `machine_usage`
- One row per machine/agent.
- Each machine updates only its own row every 15 minutes.
- Dashboard scrapes the Sheet CSV every 30 minutes and caches the result locally.

Set the source from the dashboard's **Machine usage — Google Sheet** card, or programmatically:

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
