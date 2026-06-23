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

## Security notes

- The web UI never displays access tokens, refresh tokens, or raw `auth.json`.
- It shells out to the reviewed local binary: `~/.local/bin/codex-switch-secure`.
- Login subprocesses use device-code flow and write profiles through codex-switch itself.
- Do not expose this service beyond localhost.
