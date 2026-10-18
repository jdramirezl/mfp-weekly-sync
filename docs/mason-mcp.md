# Using Mason's MyFitnessPal MCP

[Mason-Levyy/myfitnesspal-mcp](https://github.com/Mason-Levyy/myfitnesspal-mcp)
(PyPI `mfp-mcp`) is the MCP server this repo runs. It signs in with your
browser's current NextAuth session cookie (`__Secure-next-auth.session-token`)
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
- `mfp-mcp auth --check` is read-only. It exits 0 and prints
  `Status: valid, connected as <user>` when the session works. It exits 1 with
  `Session: none saved` or `Status: rejected (<error>)` otherwise. The daily
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
- **Session expired:** run `mfp-mcp auth --check`, then `mfp-mcp auth` with a
  fresh cookie if auto-refresh didn't recover it.
- **"couldn't read your MyFitnessPal profile":** set `MFP_USERNAME` to your
  username (not email) in `mason.env`. MyFitnessPal's profile endpoint returns
  500 for some accounts.
- **Meals:** `breakfast`/`lunch`/`dinner`/`snacks` fall back to your first
  through fourth meal if you renamed them. Any other unknown meal is an error,
  not a silent move.
