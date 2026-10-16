# Windows agent runbook — serve mfp-weekly-sync to ChatGPT over a tunnel

**Audience: an AI agent (or a person) operating on the user's Windows machine.**
Follow these steps in order. Each step has a check; do not proceed until the
check passes. Commands are PowerShell. Stop and report to the user at any
`STOP` marker.

> ⚠️ This exposes a MyFitnessPal-writing endpoint to the internet via a tunnel.
> That is the user's deliberate choice. The server is fail-closed: it will not
> start an HTTP listener without a strong bearer token and rejects any request
> that doesn't present it. Do not weaken these defaults. Do not print the token,
> cookies, or any `*session*` value into the chat.

---

## 0. Prerequisites (verify, install only if missing)

```powershell
python --version        # need 3.10–3.12  (NOT 3.13+)
git --version
```

- If Python is missing or 3.13+, install Python 3.12 from python.org and re-open
  PowerShell. CHECK: `python --version` prints 3.10, 3.11, or 3.12.
- A tunneling tool: ngrok (`winget install ngrok`) or Cloudflare Tunnel
  (`winget install --id Cloudflare.cloudflared`). This runbook uses ngrok.
- The user must have **ChatGPT with Developer Mode / custom connectors** (a paid
  plan feature) **or** the ChatGPT desktop app. If they have neither, STOP and
  tell them Method B below (desktop app, no tunnel) is the only option.

## 1. Clone and install

```powershell
cd $HOME
git clone https://github.com/jdramirezl/mfp-weekly-sync.git
cd mfp-weekly-sync
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[cookie,mcp]"
```

CHECK:
```powershell
mfp-sync version          # prints a version
python -c "import mfp_sync.mcp_server, mfp_sync.browser_cookies; print('ok')"
```

## 2. Pick the browser that has the MyFitnessPal session

The `cookie-session` backend reads the user's existing MyFitnessPal login from a
local browser. On Windows:

- **Firefox is the most reliable** — its cookies are not app-bound encrypted.
- **Chrome / Edge / Brave** work **only** if their cookies are the older
  `v10` DPAPI format. Recent Chrome/Edge use app-bound `v20` encryption that
  **cannot** be read outside the browser; the tool detects this and tells you to
  switch to Firefox.

Ask the user which browser they are logged into myfitnesspal.com with. If they
can choose, prefer **Firefox**: have them open Firefox, go to myfitnesspal.com,
and confirm they are logged in.

Set the backend + browser (replace `firefox` with `chrome`/`edge`/`brave` if
that's where the session is):

```powershell
$env:MFP_SYNC_BACKEND = "cookie-session"
$env:MFP_COOKIE_BROWSER = "firefox"
# optional, if the login is in a non-default profile:
# $env:MFP_COOKIE_PROFILE = "default-release"
```

## 3. Verify READ-ONLY access first (no writes)

```powershell
mfp-sync doctor
```

CHECK: the table shows `authenticated: OK` and `read access: OK`.

- If it says the cookies are **v20 app-bound** → set
  `$env:MFP_COOKIE_BROWSER = "firefox"`, make sure the user is logged into MFP in
  Firefox, and re-run.
- If it says the **session expired** → the user must open myfitnesspal.com in
  that browser and log in again, then re-run.
- If `authenticated: FAIL` for another reason → STOP and report the exact
  `detail` text to the user. Do not continue to writes.

Optional stronger read check (lists today's diary, still no writes):
```powershell
mfp-sync export --from (Get-Date -Format yyyy-MM-dd) --to (Get-Date -Format yyyy-MM-dd) --format json
```

## 4. Create a strong token (never shown to ChatGPT in plaintext by you)

```powershell
$env:MFP_MCP_TOKEN = python -c "import secrets; print(secrets.token_hex(32))"
# Show it ONCE to the user so they can paste it into the ChatGPT connector.
# Do not log it anywhere else.
Write-Host "Your MCP token (give to the ChatGPT connector, keep secret):"
Write-Host $env:MFP_MCP_TOKEN
```

STOP: tell the user to copy this token; they'll paste it into ChatGPT in step 7.

## 5. Start the MCP server (loopback + token-gated + locked surface)

Keep this terminal open — it's the running server.

```powershell
mfp-sync-mcp --transport streamable-http --port 8000
```

What this does automatically:
- Binds to `127.0.0.1` only (the tunnel, not the server, faces the internet).
- Requires `MFP_MCP_TOKEN`; refuses to start without it, and rejects any request
  missing `Authorization: Bearer <token>` with 401.
- Rate-limits to 60 requests/minute (override with `--rate-limit N`).
- Runs in **exposed mode**: only the safe tools are reachable —
  `plan_schema`, `doctor`, `preview_plan_json`, `apply_plan_json`,
  `export_diary`. The file-path tools are hidden (ChatGPT can't send local file
  paths anyway, and this prevents a leaked URL from reading the filesystem).

CHECK: the terminal logs that the server started on 127.0.0.1:8000.

## 6. Open the tunnel (second terminal)

```powershell
ngrok http 8000
```

Copy the `https://XXXX.ngrok-free.app` forwarding URL. The MCP endpoint is that
URL **plus `/mcp`**, e.g. `https://XXXX.ngrok-free.app/mcp`.

CHECK (from a third terminal): an unauthenticated request is refused, an
authenticated one is not.
```powershell
# Expect HTTP 401 (no token):
curl.exe -s -o NUL -w "%{http_code}`n" -X POST https://XXXX.ngrok-free.app/mcp
# Expect NOT 401 (replace with your real URL + token):
curl.exe -s -o NUL -w "%{http_code}`n" -X POST https://XXXX.ngrok-free.app/mcp `
  -H "Authorization: Bearer $env:MFP_MCP_TOKEN" `
  -H "Content-Type: application/json" `
  -H "Accept: application/json, text/event-stream" `
  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"probe\",\"version\":\"1\"}}}'
```
If the first prints `401` and the second prints `200`, the gate works.

## 7. Connect ChatGPT (web, Developer Mode)

Tell the user to:
1. ChatGPT → **Settings → Connectors → Advanced → Developer Mode** (enable).
2. **Add custom MCP server / connector.**
3. URL: the `https://XXXX.ngrok-free.app/mcp` from step 6.
4. Add an HTTP header: `Authorization` = `Bearer <the token from step 4>`.
5. Acknowledge the risk prompt, run the tool scan. They should see the tools:
   `plan_schema`, `doctor`, `preview_plan_json`, `apply_plan_json`,
   `export_diary`.

## 8. How the user drives it from ChatGPT

The agent/user asks ChatGPT in natural language, e.g. *"Plan my dinners for next
week and log them."* ChatGPT should:
1. Call `plan_schema` to get the exact plan shape.
2. Build a plan object following the rules in `chatgpt-prompt.md` (short generic
   food names, grams for solids, `expected_calories` on every item).
3. Call `preview_plan_json` with `{ "plan": <object> }` — review the
   planned-vs-actual calories; fix any item the tool flags.
4. Call `apply_plan_json` with `{ "plan": <object>, "confirm": true }` to write.
   (With `confirm:false` it only previews — use that to show the user first.)

Re-running the same plan is idempotent (no duplicates); manual diary edits are
preserved.

## 9. Shut down

Stop ngrok (Ctrl+C in its terminal) and the server (Ctrl+C), then:
```powershell
Remove-Item Env:MFP_MCP_TOKEN
```
While the tunnel is down the endpoint is unreachable. Rotate the token (repeat
step 4) if you suspect it leaked.

---

## Method B — ChatGPT desktop app (no tunnel, safer)

If the user has the ChatGPT **desktop app**, skip the tunnel entirely:

1. Do steps 1–3 (install, pick browser, verify `doctor`).
2. In the desktop app: **Settings → MCP servers → Add server**, type **STDIO**,
   command:
   ```
   C:\Users\<you>\mfp-weekly-sync\.venv\Scripts\mfp-sync-mcp.exe
   ```
   Add env vars `MFP_SYNC_BACKEND=cookie-session` and
   `MFP_COOKIE_BROWSER=firefox`. Save and restart.

No token or tunnel is needed — the app launches the server locally and nothing
is exposed to the internet. This is the recommended path when available.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `doctor` says v20 app-bound cookies | Use Firefox: `MFP_COOKIE_BROWSER=firefox`, log into MFP in Firefox. |
| `doctor` says session expired | Log into myfitnesspal.com again in that browser. |
| DPAPI could not unprotect the key | Run as the same Windows user that uses the browser. |
| Server won't start: "bearer token required" | `$env:MFP_MCP_TOKEN` is unset — redo step 4. |
| ChatGPT tool scan returns nothing | Check the URL ends in `/mcp` and the `Authorization: Bearer` header is set. |
| 429 responses | Rate limit hit; wait a minute or start with `--rate-limit 120`. |
| Need another browser profile | `$env:MFP_COOKIE_PROFILE = "<profile dir name>"`. |

Error codes returned by the tools are documented in `troubleshooting.md`.
