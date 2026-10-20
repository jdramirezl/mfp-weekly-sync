# Using Mason's MyFitnessPal MCP

[Mason-Levyy/myfitnesspal-mcp](https://github.com/Mason-Levyy/myfitnesspal-mcp)
(PyPI `mfp-mcp`) is the MCP server this repo runs. It signs in with your
browser's MyFitnessPal cookies (the NextAuth session `__Secure-next-auth.session-token`
plus the rest of the Cookie header)
and calls MyFitnessPal over a Chrome TLS fingerprint (`curl_cffi`), which passes
Cloudflare. Setup on the Mac is in [mac-runbook.md](mac-runbook.md).

## One instance, shared

Run exactly one Mason process, the `com.jdrami.mfp-mcp` LaunchAgent serving
streamable HTTP at `http://127.0.0.1:8484/mcp`. Every client connects to it:
assistant-hub, the optional tunnel for ChatGPT, and Claude Desktop/Code if you
like. Don't also launch stdio copies with `uvx mfp-mcp`. Mason's SQLite cache,
session-refresh lock and headless browser profile assume a single process, so
two copies can step on each other's cookie rotation.

To point Claude Code at the shared instance:

```bash
claude mcp add --transport http myfitnesspal http://127.0.0.1:8484/mcp
```

## Sessions

- Sessions last about 30 days. With the `autorefresh` extra, which the
  installer always includes, Mason boots a headless Chromium profile when
  MyFitnessPal rejects the session mid-call, lets the site rotate the token,
  saves it and retries the call.
- **Recommended: give the bot its own login** with `bin/mfpctl login`. It
  drives the installed Google Chrome through patchright, because Cloudflare
  Turnstile blocks Mason's bundled Chromium. The manual fallback is to log in
  with email and password in a Chrome Guest window, then
  `pbpaste | bin/mfp-auth` with the full Cookie header. That is a fresh
  session only the bot holds; `__Host-` cookies are dropped. Another option is
  `bin/mfpctl login --manual`. It uses
  `MFP_LOGIN_EMAIL` plus the Keychain item `mfp-login`. A cookie copied from
  your browser shares one session lineage with it, so whichever side refreshes
  first can invalidate the other. With its own login, the bot is independent
  and you can use MFP freely on your phone and in your browser. The health
  check re-logs in automatically, at most once per 6 h, or 1 h after a
  Cloudflare Turnstile timeout. The login waits for MFP's Turnstile widget and
  only treats an explicit credentials error as a rejection.
- Fallback: authenticate with the **full Cookie request header** (DevTools → Network →
  `www.myfitnesspal.com` → Request Headers → `cookie` → Copy value), piped
  with `pbpaste | bin/mfp-auth`. On SSO accounts, a lone session-token cookie
  was rejected within minutes and never rotated. Afterwards, don't log out of
  MFP in that browser; that ends the session.
- Auto-refresh only keeps MFP's own NextAuth session sliding. It never redoes an
  SSO, Google or Apple login. A session that has fully expired or was revoked
  needs a fresh Cookie header: `pbpaste | bin/mfp-auth`.
- Mason refreshes only reactively. This repo adds a weekly proactive refresh
  (`com.jdrami.mfp-refresh`, `mfpctl refresh`). The details are in
  [mac-runbook.md](mac-runbook.md#session-longevity-sso).
- SSO/email accounts need `MFP_USERNAME` (your username, not your email),
  because MFP's profile endpoint returns 500 for them. Set it in
  `~/.config/mfp-mason/mason.env`.
- `mfp-mcp auth --check` is read-only. It exits 0 and prints
  `Status: valid, connected as <user>` when the session works. It exits 1 with
  `Session: none saved` or `Status: rejected (<error>)` otherwise. The 6-hourly
  health check keys off exactly this.
- The cookie is saved as `cookies.json` (mode 600) in
  `~/Library/Application Support/myfitnesspal-mcp/`. The diary cache
  (`data.db`) and browser profile live in the same folder. Never commit or share
  any of it.

## Tools

| Tool | What it does |
|---|---|
| `fitness_get_day` | Nutrition totals, diary entries, the MFP daily note and feel note for a day (read-only) |
| `fitness_search_food` | Candidate matches with brand, calories, macros, serving and ids |
| `fitness_draft_food` | Numbered options with every serving size, ranked by optional calorie/macro targets |
| `fitness_log_food` | Log a draft option and serving, a remembered food, or exact ids |
| `fitness_list_food_pins` / `fitness_clear_food_pin` | Remembered query → food/serving choices (local) |
| `fitness_modify_food` | Replace an entry or change its quantity (destructive) |
| `fitness_delete_food` | Remove a diary entry by name match (destructive) |
| `fitness_log_weight` / `fitness_log_water` | Weight; water (add, or set the day's total with `replace`) |
| `fitness_get_exercise` / `fitness_get_exercise_entries` / `fitness_delete_exercise` | Exercise diary |
| `fitness_get_note` / `fitness_log_note` / `fitness_log_feel` | Daily MFP note; local "how I feel" note |
| `fitness_get_trends` | One metric over a range: weight, calories_in, protein, carbs, fat |
| `fitness_bulk_export` | Whole date range in one call, for analysis and weekly reports |

## Use the draft → confirm flow (don't bare-log)

Food logging is draft-then-confirm, so what lands in the diary never depends on
MyFitnessPal's search order:

1. `fitness_draft_food("greek yogurt", min_protein=15, max_calories=150)` returns
   numbered options, each with every serving size and per-entry macros.
2. `fitness_log_food(draft_id=..., option=2, serving=1)` logs exactly that food
   and serving, and remembers the choice for that query.
3. Next time, `fitness_log_food(query="greek yogurt")` reuses the remembered
   choice.

Read with `fitness_get_day` first, preview the write, and confirm the food and
serving before logging. `fitness_delete_*`, `fitness_modify_food` and
`fitness_clear_food_pin` are destructive and need explicit confirmation. The
hub enforces this with approvals.

## Troubleshooting

- **403 / Cloudflare blocked:** set `MFP_IMPERSONATE=chrome124` (or another
  `curl_cffi` target) in `~/.config/mfp-mason/mason.env`, then run
  `bin/mfpctl install`. Datacenter IPs are challenged far more than home ones.
- **Session expired:** run `mfp-mcp auth --check`, then `bin/mfpctl refresh`.
  If it's still rejected, run `bin/mfpctl login` (or, with the cookie
  fallback, `pbpaste | bin/mfp-auth` with a freshly copied full Cookie header).
- **"couldn't read your MyFitnessPal profile":** set `MFP_USERNAME` to your
  username (not email) in `mason.env`, then run `bin/mfpctl restart` and
  `pbpaste | bin/mfp-auth`. MyFitnessPal's profile endpoint returns 500 for
  SSO/email accounts.
- **Meals:** `breakfast`/`lunch`/`dinner`/`snacks` fall back to your first
  through fourth meal if you renamed them. Any other unknown meal is an error,
  not a silent move.
