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

Labels are stored in:

```text
~/.codex-switch/local-web-config.json
```

This only changes the web-dashboard label; it does not rewrite OAuth tokens.

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

Scheduler state is visible in the dashboard and in:

```text
http://localhost:8787/api/config
```

## Security notes

- The web UI never displays access tokens, refresh tokens, or raw `auth.json`.
- It shells out to the reviewed local binary: `~/.local/bin/codex-switch-secure`.
- Login subprocesses use device-code flow and write profiles through codex-switch itself.
- Do not expose this service beyond localhost.
