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

In the browser, copy the value of `__Secure-next-auth.session-token` from the
MyFitnessPal cookie storage. Mason validates and stores it under the local user
profile. The password is not stored.

## OpenAI Secure MCP Tunnel

The repository's supported tunnel setup is OpenAI Secure MCP Tunnel. Create or
select a tunnel in OpenAI Platform and create a runtime key with Tunnels Read +
Use. Store those values as Windows user environment variables; do not commit
them:

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

Verify the local tunnel before testing ChatGPT:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/healthz
Invoke-WebRequest http://127.0.0.1:8080/readyz
```

Both endpoints should return HTTP 200. Do not run the old Cloudflare quick
tunnel at the same time.

## Available Mason operations

Mason exposes current web-backed tools including `fitness_get_day`,
`fitness_search_food`, `fitness_log_food`, `fitness_modify_food`,
`fitness_delete_food`, and `fitness_bulk_export`. Use a read-only call first,
then draft/preview a write, and confirm the exact food and serving before
logging. Delete and modify operations are destructive; use them only with an
explicit user confirmation.

## Important boundary

Mason is a separate MCP server. It does not use this repository's
`mfp-sync-mcp` tools or its weekly-plan SQLite ledger. Use this repository for
plan generation, resolution, reconciliation, and idempotent batch workflows;
use Mason when direct current web-session CRUD is required.

Mason's cookie session expires and must be refreshed with `mfp-mcp auth`. Never
commit the cookie file, API key, tunnel profile, or diary database.
