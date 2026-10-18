# Mason MyFitnessPal MCP integration

This project keeps its weekly-plan engine, reconciliation rules, local ledger, and
safe preview/apply workflow. For current MyFitnessPal web authentication, use
[Mason-Levyy/myfitnesspal-mcp](https://github.com/Mason-Levyy/myfitnesspal-mcp)
as a separate MCP server instead of the repository's legacy `cookie-session`
backend.

Mason uses the current `__Secure-next-auth.session-token` cookie directly and
uses `curl_cffi` to match a browser TLS fingerprint. This avoids the obsolete
`/user/auth_token?refresh=true` exchange used by the legacy backend.

## Install on Windows

Install `uv`, then install Mason as a persistent user tool:

```powershell
python -m pip install uv
uv tool install mfp-mcp
```

Connect the account once. Do this in a visible PowerShell terminal. Paste the
cookie only into the hidden prompt; never put it in Git, `.env`, or chat:

```powershell
mfp-mcp auth
mfp-mcp auth --check
```

To get the cookie value: log in at myfitnesspal.com, open DevTools (F12) →
**Application** (Chrome/Edge) or **Storage** (Firefox) → **Cookies** →
`https://www.myfitnesspal.com`, and copy the value of
`__Secure-next-auth.session-token`. Paste it into the `mfp-mcp auth` prompt.
Mason validates and stores it under the local user profile. The password is not
stored. `mfp-mcp auth --check` reports whether the saved session still works
without changing anything.

### Sessions expire (~30 days) — enable auto-refresh to avoid re-pasting

Mason sessions last around 30 days. Rather than re-running `mfp-mcp auth` each
time one expires, enable **auto-refresh**: Mason seeds a headless browser
profile that rotates the token when MyFitnessPal rejects it mid-call, then
retries automatically. Strongly recommended for an unattended tunnel setup:

```powershell
# reinstall the persistent tool WITH the autorefresh extra
uv tool install --force "mfp-mcp[autorefresh]"
# one-time: fetch the headless browser used for token rotation
uvx --from "mfp-mcp[autorefresh]" playwright install chromium
# re-auth once so the refresh profile is seeded
mfp-mcp auth
```

Then run Mason via the same `mfp-mcp[autorefresh]` form. Without this, expiry
shows up as a `SESSION_EXPIRED`-style failure and you must re-run `mfp-mcp auth`.

## OpenAI Secure MCP Tunnel

> ⚠️ **Mason has no built-in authentication.** Its own docs say: *"never expose
> it to the internet"* — bind it to localhost and let an authenticating layer
> (a tunnel/gateway or VPN/tailnet) front it. The OpenAI Secure MCP Tunnel below
> provides that layer. Do **not** run `mfp-mcp --http` on a public interface.

The setup below is the one that worked here: OpenAI Secure MCP Tunnel fronting
Mason over stdio. (It's one option, not the only one — Mason also works behind a
tailnet or an authenticating reverse proxy.) Create or select a tunnel in OpenAI
Platform and create a runtime key with Tunnels Read + Use. Store those values as
Windows user environment variables; do not commit them:

```powershell
[Environment]::SetEnvironmentVariable('CONTROL_PLANE_TUNNEL_ID', 'tunnel_...', 'User')
[Environment]::SetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'sk-...', 'User')
```

Install the official `tunnel-client` binary from the OpenAI tunnel settings or
its release page. Configure Mason as a stdio child:

```powershell
$env:CONTROL_PLANE_TUNNEL_ID = [Environment]::GetEnvironmentVariable('CONTROL_PLANE_TUNNEL_ID', 'User')
$env:CONTROL_PLANE_API_KEY = [Environment]::GetEnvironmentVariable('CONTROL_PLANE_API_KEY', 'User')

tunnel-client init `
  --sample sample_mcp_stdio_local `
  --profile mfp-local `
  --tunnel-id $env:CONTROL_PLANE_TUNNEL_ID `
  --mcp-command 'C:/Users/<user>/.local/bin/mfp-mcp.exe'

tunnel-client doctor --profile mfp-local --explain
tunnel-client run --profile mfp-local
```

Use the absolute path to `mfp-mcp.exe`. Running the persistent executable avoids
bootstrap output from `uvx` on the MCP stdio channel.

In ChatGPT select **Tunnel**, enter only the `tunnel_...` ID, and select **No
authentication**. The runtime key is for `tunnel-client`, not ChatGPT. Keep
`tunnel-client` running for discovery and tool calls.

Verify the local tunnel before testing ChatGPT (`:8080` here is
`tunnel-client`'s own health server, not Mason — Mason runs as its stdio child):

```powershell
Invoke-WebRequest http://127.0.0.1:8080/healthz
Invoke-WebRequest http://127.0.0.1:8080/readyz
```

Both endpoints should return HTTP 200. Do not run the old Cloudflare quick
tunnel at the same time.

## Available Mason operations

Mason exposes current web-backed tools. The ones you'll use most:

| Tool | What it does |
|---|---|
| `fitness_get_day` | Read a day's totals + diary entries (read-only) |
| `fitness_search_food` | Candidate matches with brand, calories, macros, ids |
| `fitness_draft_food` | Numbered options with every serving size, rankable by calorie/macro targets |
| `fitness_log_food` | Log a specific draft option + serving (or a remembered/exact food) |
| `fitness_modify_food` | Replace an entry or change its quantity |
| `fitness_delete_food` | Remove an entry by name match |
| `fitness_bulk_export` | Whole date range in one call, for analysis |

(Mason also covers weight, water, exercise, and the daily note — see its README.)

### Use the draft → confirm flow (don't bare-log)

Mason's logging is **draft-then-confirm** so what lands in your diary never
depends on MyFitnessPal's search order — the same safeguard this project learned
the hard way (verify the serving, never trust the first result). Prefer:

1. `fitness_draft_food("greek yogurt", min_protein=15, max_calories=150)` →
   numbered options, each showing every serving size with per-entry macros.
2. `fitness_log_food(draft_id=..., option=2, serving=1)` → logs exactly that
   food + serving (and remembers the choice for that query).

Always do a read-only `fitness_get_day` first, draft/preview the write, and
confirm the exact food and serving before logging. `fitness_delete_food` and
`fitness_modify_food` are destructive — use only with explicit user confirmation.

## Troubleshooting

- **403 / Cloudflare blocked:** set `MFP_IMPERSONATE=chrome124` (or another
  `curl_cffi` target) in Mason's environment. Datacenter IPs get challenged far
  more than residential ones.
- **"Session expired":** run `mfp-mcp auth --check`, then `mfp-mcp auth`, or set
  up auto-refresh (above).
- **"couldn't read your MyFitnessPal profile":** set `MFP_USERNAME` to your
  username (not email) — MFP's profile endpoint 500s for some accounts.

## Important boundary

Mason is a separate MCP server. It does not use this repository's
`mfp-sync-mcp` tools or its weekly-plan SQLite ledger, and it keeps its own
cache + browser profile in its own data dir (`MFP_MCP_DATA_DIR`). Use this
repository for plan generation, resolution, reconciliation, and idempotent batch
workflows; use Mason when direct current web-session CRUD is required.

Mason's cookie session expires (~30 days) and must be refreshed with
`mfp-mcp auth`, or kept fresh automatically via auto-refresh (above). Never
commit the cookie file, API key, tunnel profile, or diary database.
