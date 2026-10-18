# MCP server — connect an AI agent to mfp-weekly-sync

The MCP server exposes the sync operations as agent **tools** (`preview_plan`,
`apply_plan`, `resolve_plan`, `status_plan`, `export_diary`, `doctor`,
`plan_schema`). It wraps the same engine the CLI uses, so results match the
CLI's `--json` output exactly. Re-running a plan is idempotent.

Install the extra:

```bash
pip install -e ".[mcp]"      # or: uv pip install -e ".[mcp]"
mfp-sync-mcp --help
```

## ⚠️ Read this first — it can write to your real diary

`apply_plan` writes to your MyFitnessPal diary. So:

- **stdio and the HTTP transports bind locally only.** The HTTP transports
  default to `127.0.0.1`. Nothing is exposed to the internet unless *you* put a
  tunnel in front of it.
- **The HTTP transports require a bearer token and are fail-closed:** if
  `MFP_MCP_TOKEN` is not set, the server refuses to start an HTTP listener.
  Every HTTP request must send `Authorization: Bearer <token>`; everything else
  gets `401`. This keeps strangers who find your tunnel URL out of your diary.
- **stdio needs no token** — there's no network listener to protect.

Pick the backend with `--backend` or `MFP_SYNC_BACKEND` (e.g. `cookie-session`
for a Google/SSO account; see `authentication.md`).

For current MyFitnessPal SSO sessions, see [mason-mcp.md](mason-mcp.md). Mason
is a separate MCP server using the current NextAuth cookie flow; it is not
installed into this project's virtual environment.

---

## Method A — ChatGPT Desktop app (recommended, nothing exposed)

The desktop app launches a local MCP server directly, so your session never
leaves your Mac.

1. ChatGPT Desktop → **Settings → MCP servers → Add server**.
2. Choose **STDIO**, and set the command:
   ```
   mfp-sync-mcp
   ```
   (or the absolute path printed by `which mfp-sync-mcp`). Add env vars if you
   want a non-default backend, e.g. `MFP_SYNC_BACKEND=cookie-session`.
3. Save and **Restart**.

The same STDIO config works for **Claude Desktop**, **Cursor**, and **Kiro** —
their MCP config files take a `command`:

```json
{
  "mcpServers": {
    "mfp-weekly-sync": {
      "command": "/absolute/path/to/.venv/bin/mfp-sync-mcp",
      "env": { "MFP_SYNC_BACKEND": "cookie-session" }
    }
  }
}
```

## Method B — ChatGPT web, via a tunnel (works, but higher risk)

ChatGPT on the web can only reach a **public HTTPS** MCP server, so you must run
an HTTP transport locally and tunnel it. **This publishes a diary-writing
endpoint to the internet — the token gate is what keeps it yours.**

When run over an HTTP transport the server enters **exposed mode** automatically:
- Refuses to start without `MFP_MCP_TOKEN` (min 24 chars) and rejects any request
  without `Authorization: Bearer <token>` (401).
- Rate-limits to 60 requests/minute (`--rate-limit N` to change).
- Hides the file-path tools. Only `plan_schema`, `doctor`, `preview_plan_json`,
  `apply_plan_json`, and `export_diary` are reachable — a remote caller can't use
  local file paths anyway, and this stops a leaked URL from reading the
  filesystem. (Force this on any transport with `--expose`.)

On **Windows**, follow [windows-agent-runbook.md](windows-agent-runbook.md) — a
step-by-step runbook (installing, picking the browser, tunnel, ChatGPT connector)
written so an agent on the Windows box can execute it.

1. Pick a strong token and start the HTTP server (loopback):
   ```bash
   export MFP_MCP_TOKEN="$(openssl rand -hex 32)"
   export MFP_SYNC_BACKEND=cookie-session
   export MFP_COOKIE_BROWSER=chrome        # or edge/brave/firefox
   mfp-sync-mcp --transport streamable-http --port 8000
   ```
   (It refuses to start if `MFP_MCP_TOKEN` is unset.)

2. Tunnel the local port to a public HTTPS URL:
   ```bash
   ngrok http 8000           # or: npx localtunnel --port 8000
   ```

3. In ChatGPT: **Settings → Connectors → Advanced → Developer Mode**, then
   **Add custom MCP server**. Paste the tunnel URL with the `/mcp` path, e.g.
   `https://<subdomain>.ngrok.app/mcp`, and configure the
   `Authorization: Bearer <token>` header so ChatGPT sends your token. Save and
   run the tool scan.

Risks to accept consciously:
- Anyone with the URL **and** the token can write to your diary. Keep the token
  secret; rotate it by restarting with a new `MFP_MCP_TOKEN`.
- Without the token, requests are rejected — but the endpoint still exists while
  the tunnel is up. Stop the tunnel/server when you're done.
- Do **not** bind to a non-loopback host. The server refuses `--host 0.0.0.0`
  unless you set `MFP_MCP_ALLOW_NONLOCAL_BIND=1`; prefer the tunnel instead.

If you don't specifically need ChatGPT web, use Method A — it's strictly safer.

## Typical agent flow

1. `plan_schema` → learn the plan JSON shape.
2. (agent writes a `plan.json` on your machine)
3. `preview_plan {plan_path}` → see exact operations (no writes).
4. `resolve_plan {plan_path}` → if `UNRESOLVED_FOODS`, pick foods (a human
   decision; the agent surfaces candidates).
5. `apply_plan {plan_path}` → write. Idempotent; never overwrites your manual
   edits unless `on_conflict="overwrite"` is passed explicitly.

Every tool returns `{"ok": bool, "error_code": "...", ...}` so an agent can
branch deterministically. Error codes are listed in `troubleshooting.md`.

## Note on ChatGPT and file paths

The tools take a `plan_path` on *your* machine. In Method A (local subprocess)
the agent and the files share your filesystem, so this works directly. In
Method B (tunnel), ChatGPT's servers can't see your files — have the agent write
the plan via a path you both agree on, or run the apply from the CLI after
ChatGPT produces the JSON (see `chatgpt-prompt.md`).
