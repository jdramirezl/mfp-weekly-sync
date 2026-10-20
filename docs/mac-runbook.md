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

**Retire the Windows setup first** ([step 8](#8-retire-the-windows-setup)).
Two machines rotating the same MyFitnessPal session will log each other out.

```bash
scripts/install-mac.sh --dry-run                 # preview
scripts/install-mac.sh --username <mfp username> # add --with-tunnel for ChatGPT access
```

This installs:

- `uv`, from its pinned standalone installer into `~/.local/bin`. Homebrew is
  not used: on Intel Macs it has no bottles and built LLVM, Rust and Python
  from source for hours before stalling.
- Python 3.13, through `uv python install`. Mason 0.4.1's pinned
  `pydantic-core` doesn't build on Python 3.15, so the tool is always pinned
  to 3.13. An install on another Python is rebuilt.
- `mfp-mcp[autorefresh]==0.4.1` and Playwright Chromium
- with `--with-tunnel`, `tunnel-client`. This is the only part that needs
  Homebrew, and it runs with `HOMEBREW_NO_AUTO_UPDATE=1`.

It also creates the private folders. `--username` (or the one-time prompt)
saves `MFP_USERNAME` to `~/.config/mfp-mason/mason.env` (mode 600). Use your
MyFitnessPal **username**, not your email. **SSO/email logins need it:** MFP's
profile lookup returns 500 for them, and Mason can't connect without the
username. If `mfp-mcp` isn't found afterwards, run `uv tool update-shell` and
open a new Terminal.

## 4. Connect MyFitnessPal

### Recommended: give the bot its own password login

Why: a session copied out of your browser shares one NextAuth session lineage
with that browser. When either side refreshes, the other copy can be
invalidated. On setup day every pasted cookie set, token-only or the full
17–22-cookie header, was rejected within minutes to hours, even right after a
refresh reported `rotated: yes`. With a login of its own, the bot holds an
independent session. **You can then keep using MFP freely in your browser and
on your phone.**

1. **Add a password to your MFP account.** If you sign in with Google or
   Apple, log out in a private window and use **Forgot password?** on the
   login page with the account's email. This sets a password on the same
   account and diary.
2. **Tell the bot the login email.** Add it to `mason.env`:

   ```bash
   printf 'MFP_LOGIN_EMAIL=%s\n' 'you@example.com' >> ~/.config/mfp-mason/mason.env
   chmod 600 ~/.config/mfp-mason/mason.env
   ```

3. **Store the password in the login Keychain.** Leave the value after `-w`
   empty so `security` prompts for it, which keeps the password out of argv
   and shell history:

   ```bash
   security add-generic-password -s mfp-login -a 'you@example.com' -w
   ```

4. **Log in, at the Mac's screen:**

   ```bash
   bin/mfpctl login
   ```

   - A Chromium window opens on MFP's login page and types in the email and
     password.
   - MFP's login page uses **Cloudflare Turnstile**: a "Verifying…" widget
     under "Forgot password?" that keeps the Log in button disabled. The
     script waits for it to clear before clicking, up to 60 s. If a
     Cloudflare checkbox appears, click it in the window: the terminal says
     *haz clic en la casilla de Cloudflare*.
   - If a captcha, a challenge or a 2FA prompt appears, finish it in the same
     window. The whole login waits up to 3 minutes.
   - The first time, macOS asks whether `security` may use the `mfp-login`
     item. Click **Always Allow**. Otherwise unattended re-logins block on
     that dialog.
   - The script never clicks "Continue with Google/Facebook".
   - It saves all of MFP's cookies, restarts Mason, and prints
     `✅ Logged in as <you>`.

5. **Check it:** `bin/mfpctl status` and `bin/mfpctl health`.

Never use MFP's "log out of all devices". It ends the bot's session too. The
health check would then re-login automatically within 6 hours (see
[Session longevity](#session-longevity-sso)).

**If MFP won't add a password to your SSO account:**

- A **new** email+password MFP account is a **different diary**. The hub would
  log meals there, not in your main account. It's only an option if you're
  willing to move your tracking to it.
- MFP support may be able to add a password to an SSO account on request.
  This is unverified.
- Otherwise, use the cookie fallback below and expect periodic re-auth.

### Fallback: paste a browser cookie

Use this only if the password login isn't possible. The session stays tied to
your browser, so expect periodic re-auth.

Copy the **full Cookie request header**, not just the session token:

1. Log in at myfitnesspal.com in the browser on this Mac.
2. Open DevTools (⌥⌘I), then the **Network** tab, and reload the page.
3. Select the `www.myfitnesspal.com` document request (the first row).
4. Under **Request Headers**, right-click `cookie` and choose **Copy value**.
   You get all the cookies in one line, about 17 of them.
5. Pipe it from the clipboard. The value never shows on screen, in shell
   history, or in a file:

```bash
pbpaste | bin/mfp-auth --username <mfp username>   # or: pbpaste | bin/mfpctl auth
```

Done when it prints `✅ Connected as <you>; auto-refresh ready`. It reports only
`Input received (N characters, M cookies)`, then runs `mfp-mcp auth --check` for
you. On failure it prints `❌` with hints. If Mason is already running, it
restarts it so the new session is used.

Why the full header: on setup day, with a Google SSO account, a lone
`__Secure-next-auth.session-token` was rejected by MFP within minutes and
`mfpctl refresh` never rotated it. The full header passed `auth --check`, kept
`health: ok`, and refreshes reported `rotated: yes`. `mfp-auth` warns when it
receives only one cookie.

**Don't log out of MyFitnessPal in that browser afterwards.** Logging out ends
the session Mason now uses, and so does "log out everywhere". Closing the tab
or quitting the browser is fine.

Fallback, not recommended: copying only the `__Secure-next-auth.session-token`
value (DevTools Application tab, Cookies) also works, but MFP may reject
it within minutes.

Don't paste into the hidden prompt of a bare `mfp-mcp auth`. On setup day a long
cookie made that prompt hang.

If you see a 403 or a Cloudflare error, add `MFP_IMPERSONATE=chrome124` to
`~/.config/mfp-mason/mason.env` (see `config/mason.env.example`) and retry. The
service reads `mason.env` each time it starts, so after editing it run
`bin/mfpctl restart`.

## 5. Start the service

```bash
bin/mfpctl install --dry-run   # preview the plists
bin/mfpctl install
sleep 20 && bin/mfpctl status
```

Done when `status` shows `com.jdrami.mfp-mcp loaded (state=running ...)` and
`MCP http://127.0.0.1:8484/mcp: OK (myfitnesspal ...)`. `install` loads three
agents: Mason, the 6-hourly health check and the weekly session refresh. It
warns if no `MFP_USERNAME` is configured.

On its first start, Mason runs at about 98% CPU for around 10 s while it loads,
so `status` can show `UNREACHABLE` right after `install`. Wait 15–20 s and run
it again.

Then point assistant-hub at it as described in
[hub-integration.md](hub-integration.md). If Mason was down or still starting
when the hub started, restart the hub, because it only connects to MCP servers
at startup. A fix in the hub is in progress.

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
The check runs every 6 hours (03:00, 09:00, 15:00, 21:00) and at login.

The bot token is stored in two places: assistant-hub's `.env` and
`~/.config/mfp-mason/alert.env`. If you rotate it with @BotFather, update both.
Otherwise the alerts stop arriving, and `bin/mfpctl health` reports
`Telegram send failed`.

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

1. Read Mason's changelog, and check that the new version installs on
   `MASON_PYTHON` (3.13).
2. Bump `MFP_MCP_VERSION` in `scripts/lib/common.sh`, then run:

```bash
scripts/install-mac.sh
bin/mfpctl install     # re-renders plists, reloads services
bin/mfpctl status
mfp-mcp auth --check
```

## Session longevity (SSO)

How Mason's auto-refresh works, from its source (`refresh.py`, `server.py`):

- At auth time, `mfp-mcp auth` seeds a headless Chromium profile with your MFP
  session cookies (`<data dir>/browser-profile`).
- A refresh opens that profile, visits myfitnesspal.com and saves the
  `__Secure-next-auth.session-token` that MFP hands back. This lets
  MyFitnessPal's own NextAuth session slide forward.
- **It never performs an SSO, Google or Apple login.** It only keeps an existing
  MFP session alive.
- Mason refreshes **reactively**: when a tool call is rejected, it refreshes
  once and retries.
- If the session has reached MFP's absolute lifetime, was revoked (log out
  everywhere, password or security reset), or MFP won't extend it, nothing
  valid comes back. Tool calls then fail with "session expired", the health
  check sends 🔴, and the only fix is to copy the full Cookie request header
  again and run `pbpaste | bin/mfp-auth` (step 4).
- Logging out of MFP in the browser you copied the header from, or "log out
  everywhere", revokes the session immediately and requires a re-auth.

What this repo adds so the session doesn't age out while nobody's using it:

- **Weekly proactive refresh:** `com.jdrami.mfp-refresh` runs Sundays at 04:30.
  It does the following:
  - stops Mason
  - calls Mason's public `refresh.refresh_session()`
  - always restarts Mason
  - runs the health check
  - logs `rotated: yes` or `rotated: no (token unchanged)`. Only 8-character
    hashes of the token are logged.
- **To run it now:** `bin/mfpctl refresh`. Check the result with
  `bin/mfpctl logs | grep rotated:`.
- **`rotated: no` after a token-only auth** is the signature of the
  single-cookie problem. Re-auth with the full Cookie header (step 4).
- **Interpreting the log:** otherwise, `rotated: no` isn't an error. It means MFP didn't
  re-issue the token on a plain visit. Whether a visit actually rotates it is
  unverified until the first real run, so check the log after the first Sunday.
  If it never rotates and you still get expiry alerts, re-auth with pbpaste
  roughly monthly.
- **Self-healing:** when the health check finds the session rejected, it tries
  these steps in order:
  1. `refresh`
  2. if a password login is configured, a password **re-login**. A headed
     Chromium window may briefly appear on the Mac. At most one re-login runs
     per 6 h, so MFP doesn't flag the account. After a Turnstile or captcha
     timeout (exit 6), which isn't a credentials rejection, it retries after
     1 h instead.
  3. only then a 🔴 alert. It names what was tried and why the re-login
     failed, e.g. "timed out, Turnstile/captcha needs a click at the Mac's
     screen".

  `bin/mfpctl logs | grep -E 'healed|re-login'` shows what happened. The re-login
  needs the Mac to stay logged in to its desktop session; it already does for
  the hub.
- **Health check every 6 h:** 03:00, 09:00, 15:00 and 21:00, plus at login. A
  dead session alerts within 6 hours.

## Troubleshooting

- `bin/mfpctl logs` shows the Mason, health and tunnel logs. `bin/mfpctl logs -f`
  follows them.
- **`MCP ... UNREACHABLE`:** run `bin/mfpctl restart`, then check `mason.err.log`.
  launchd waits 30 s between crash restarts.
- **`Status: rejected (...)`:** run `bin/mfpctl login` at the screen. With the
  cookie fallback, copy the full Cookie request header again and run
  `pbpaste | bin/mfp-auth`.
- **`bin/mfpctl login` fails:** read `~/Library/Logs/mfp-mason/login.log`. It
  records which selectors matched. If MFP changed its login page, they need
  updating in `scripts/mfp_login.py`. It also records the waiting states:
  `turnstile pending` / `turnstile solved`, `button disabled` /
  `button enabled`, `error banner: …`. Exit codes:
  - 4: no Keychain item
  - 5: MFP showed an explicit credentials error (`error banner: …` in the
    log). Check the password. A page that is merely still on
    `/account/login`, for example while Turnstile shows "Verifying…", is
    never treated as a rejection.
  - 6: timed out, usually `turnstile pending` (Cloudflare didn't clear) or an
    unfinished captcha or 2FA. Run `bin/mfpctl login` again at the screen and
    click the Cloudflare box if it appears. `MFP_IMPERSONATE` doesn't apply
    here, because this is a real browser. If it keeps happening, wait a while
    and retry.

  A failed run saves `login-failure.png` in the same folder. It may show your
  email; the folder is private (0700).
- **The login hangs with no window activity:** a hidden Keychain prompt is
  probably waiting. Look for it on the Mac's screen and click **Always Allow**.
- **Rejected within minutes of auth, or `rotated: no`:** you probably pasted
  only the session token, or logged out of MFP in that browser. Re-auth with the
  full Cookie header and stay logged in.
- **"couldn't read your MyFitnessPal profile" / 500:** set `MFP_USERNAME` in
  `~/.config/mfp-mason/mason.env`, then `bin/mfpctl restart` and re-auth.
- **Hub has no `fitness_*` tools:** Mason was down when the hub started.
  Restart the hub.
- **Removing everything:** `bin/mfpctl uninstall` removes the LaunchAgents. Your
  session and data stay in `~/Library/Application Support/myfitnesspal-mcp`;
  delete that folder yourself if you want them gone.
