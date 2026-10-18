# Mac runbook

This is an ordered checklist for setting up the personal Mac that runs Mason 24/7.
Each step says how to tell it worked. Commands assume you are in the repo
checkout.

## 1. Prerequisites

- macOS with [Homebrew](https://brew.sh) and git (`xcode-select --install`).
- Your MyFitnessPal login in a browser on this Mac. A home connection matters:
  MyFitnessPal challenges datacenter IPs far more often than residential ones.

## 2. Clone

```bash
mkdir -p ~/code && git clone https://github.com/jdramirezl/<repo>.git ~/code/mfp-mason
cd ~/code/mfp-mason
```

The services run copies of the scripts installed under
`~/Library/Application Support/mfp-mason/bin`, so you can move or rename the
checkout later. Re-run `bin/mfpctl install` after pulling updates.

## 3. Install

```bash
scripts/install-mac.sh --dry-run        # preview
scripts/install-mac.sh                  # add --with-tunnel if you want ChatGPT access
```

This installs:

- `uv`
- `mfp-mcp[autorefresh]==0.4.1`
- Playwright Chromium
- with `--with-tunnel`, `tunnel-client`

It also creates the private folders. If `mfp-mcp` isn't found afterwards, run
`uv tool update-shell` and open a new Terminal.

## 4. Connect MyFitnessPal

1. Log in at myfitnesspal.com.
2. Open DevTools (⌥⌘I), then **Application** → **Cookies** →
   `https://www.myfitnesspal.com`, and copy the value of
   `__Secure-next-auth.session-token`.
3. Run the commands below. Paste the cookie only into the hidden prompt, never
   into chat, git or a file:

```bash
mfp-mcp auth
mfp-mcp auth --check
```

Done when the check prints `Status: valid, connected as <you>` and
`Auto-refresh: ready`. Auto-refresh rotates the roughly 30-day session for you.

If you see a 403 or a Cloudflare error, set `MFP_IMPERSONATE=chrome124`. Use
`MFP_USERNAME` if Mason can't read your profile. Run `auth` with the setting
exported in your shell, and also put it in `~/.config/mfp-mason/mason.env` (see
`config/mason.env.example`) so the service uses it.

## 5. Start the service

```bash
bin/mfpctl install --dry-run   # preview the plists
bin/mfpctl install
bin/mfpctl status
```

Done when `status` shows `com.jdrami.mfp-mcp loaded (state=running ...)` and
`MCP http://127.0.0.1:8484/mcp: OK (myfitnesspal ...)`. Then point
assistant-hub at it as described in [hub-integration.md](hub-integration.md).

## 6. Telegram alerts

```bash
cp ~/.config/mfp-mason/alert.env.example ~/.config/mfp-mason/alert.env
chmod 600 ~/.config/mfp-mason/alert.env
open -e ~/.config/mfp-mason/alert.env   # TELEGRAM_BOT_TOKEN (the hub's bot), TELEGRAM_CHAT_ID (your id)
bin/mfpctl health --simulate-fail auth --dry-run   # preview the alert text
bin/mfpctl health
```

Done when `health` prints `health: ok`. To test a real alert end to end, run
`bin/mfpctl stop`, then `bin/mfpctl health` (you get 🔴), then
`bin/mfpctl start`, wait about 10 s, and run `bin/mfpctl health` (you get 🟢).
The check runs daily at 09:00 and at login.

## 7. Optional: ChatGPT through the OpenAI tunnel

1. In OpenAI Platform → **Tunnels**, create a tunnel and note its `tunnel_...` id.
2. Under **Runtime API keys**, create a **Restricted** key with only **Tunnels
   Read + Use**.
3. Fill in the tunnel env file and enable the tunnel:

```bash
scripts/install-mac.sh --with-tunnel     # if you skipped it in step 3
cp ~/.config/mfp-mason/tunnel.env.example ~/.config/mfp-mason/tunnel.env
chmod 600 ~/.config/mfp-mason/tunnel.env
open -e ~/.config/mfp-mason/tunnel.env   # CONTROL_PLANE_TUNNEL_ID, CONTROL_PLANE_API_KEY
bin/mfpctl tunnel enable
bin/mfpctl tunnel status                 # wait for "/readyz: ready"
```

4. In ChatGPT, add a connector with **Connection: Tunnel**, enter only the
   `tunnel_...` id, and choose **No authentication**.
5. Make a read-only call such as "what did I eat yesterday?"

Anyone who can use that connector can use your diary. To turn it off, run
`bin/mfpctl tunnel disable`.

## 8. Retire the Windows setup

Only one machine should hold and rotate the MyFitnessPal session.

1. On Windows, stop `tunnel-client` and any `mfp-mcp` / `mfp-sync-mcp`
   processes, and remove any scheduled task or startup entry for them.
2. Delete Mason's saved session and data there (`%LOCALAPPDATA%\myfitnesspal-mcp`
   or the path `mfp-mcp auth --check` reports), and clear the
   `CONTROL_PLANE_*` user environment variables.
3. If the Mac's tunnel reuses the same tunnel id, the Windows client must be
   stopped first.

## 9. Keep the Mac awake

Use the `setup-mac.sh` script in the assistant-hub repo, which covers these settings:

- `pmset` to prevent sleep while on power
- the 80% battery charge limit
- an open lid or a dummy HDMI plug, because a closed lid still sleeps

Don't duplicate them here.

## 10. Upgrading Mason

1. Read Mason's changelog.
2. Bump `MFP_MCP_VERSION` in `scripts/lib/common.sh`, then run:

```bash
scripts/install-mac.sh
bin/mfpctl install     # re-renders plists, reloads services
bin/mfpctl status
mfp-mcp auth --check
```

## Troubleshooting

- `bin/mfpctl logs` shows the Mason, health and tunnel logs. `bin/mfpctl logs -f`
  follows them.
- **`MCP ... UNREACHABLE`:** run `bin/mfpctl restart`, then check `mason.err.log`.
  launchd waits 30 s between crash restarts.
- **`Status: rejected (...)`:** auto-refresh retries on the next tool call. If it
  stays rejected, run `mfp-mcp auth` again.
- **Removing everything:** `bin/mfpctl uninstall` removes the LaunchAgents. Your
  session and data stay in `~/Library/Application Support/myfitnesspal-mcp`;
  delete that folder yourself if you want them gone.
