# Local AI agent integration

This tool is built to be driven by a local coding/planning agent. The agent's
job is to turn a natural-language meal plan into a valid plan JSON file and then
call the CLI. The agent never needs your MyFitnessPal credentials — those stay
in your environment and are read only by the CLI process.

## Contract for agents

1. **Generate** a plan file conforming to the JSON Schema
   (`mfp-sync schema`, also saved at `src/mfp_sync/schema/weekly_plan.schema.json`).
2. **Validate / preview** with structured output:
   `mfp-sync --json preview plan.json`
3. **Resolve** ambiguous foods non-interactively:
   `mfp-sync --json --non-interactive resolve plan.json`
   → returns candidates for each unresolved food so the agent (or user) can pick.
4. **Apply** non-interactively:
   `mfp-sync --json --non-interactive apply plan.json --yes`

Every command supports `--json`. Every failure returns a stable `error_code`
(see `troubleshooting.md`) and a matching exit code, so an agent can branch
deterministically.

### Safety rails in agent mode

- `--non-interactive` **never** overwrites a manually-changed entry silently
  (conflicts are skipped unless `--on-conflict overwrite` is explicitly set).
- `apply` refuses to run while any food is unresolved (`UNRESOLVED_FOODS`).
- Credentials are never passed on the command line or returned in output.

## Structured output shapes

`preview --json`:

```json
{
  "ok": true,
  "error_code": "OK",
  "plan_id": "weekly_plan_2026-10-19",
  "counts": {"add": 11},
  "actions": [
    {
      "date": "2026-10-19", "meal": "breakfast", "entry_id": "mon-breakfast-oats",
      "food": "Oatmeal, dry", "action": "add",
      "matched_food": "Oatmeal, dry", "matched_food_id": "mock-oatmeal",
      "servings": 40.0, "serving_unit": "g", "nutrition_warnings": []
    }
  ],
  "daily_nutrition": {
    "2026-10-19": {"calories": {"planned": 662, "actual": 661.9, "ok": true}}
  }
}
```

`apply --json`:

```json
{
  "ok": true,
  "error_code": "OK",
  "result": {
    "added": ["2026-10-19/breakfast/mon-breakfast-oats"],
    "skipped": [], "restored": [], "overwritten": [],
    "failed": [], "uncertain": []
  }
}
```

`resolve --json --non-interactive` (unresolved items):

```json
{
  "ok": false,
  "error_code": "UNRESOLVED_FOODS",
  "unresolved": [
    {
      "entry_id": "x", "food": "chicken breast", "status": "ambiguous",
      "message": "best match ... below auto-accept ...",
      "candidates": [
        {"food_id": "mock-chicken-cooked", "description": "Chicken breast, cooked", "brand": null}
      ]
    }
  ]
}
```

## Example agent prompt

Use a prompt like this to turn a natural-language plan into a valid file:

> You produce MyFitnessPal weekly meal plans as JSON that conforms exactly to
> the schema at `src/mfp_sync/schema/weekly_plan.schema.json`.
>
> Rules:
> - `schema_version` is 1.
> - One object per day in `days`, each with an ISO `date` (`YYYY-MM-DD`) and a
>   `meals` object with keys `breakfast`, `lunch`, `dinner`, `snack`.
> - Each food item needs a unique `entry_id` (stable, kebab-case, e.g.
>   `mon-breakfast-oats`), a `food` display name, a numeric `quantity > 0`, and
>   a `unit`. Prefer `g` for solids and a named serving (e.g. `medium`, `cup`)
>   only when that's how the food is naturally measured.
> - Include `expected_calories` (and `expected_macros` if known) so the tool can
>   flag mismatches. These are *planned* estimates, not authoritative values.
> - Do not invent MyFitnessPal food IDs. Leave `mfp_food_id` out unless the user
>   gave you a verified one.
>
> Produce one week (7 days). Output only the JSON.

After the agent writes `plan.json`, the user (or the agent, if trusted) runs:

```bash
mfp-sync --json preview plan.json
mfp-sync --json --non-interactive resolve plan.json   # if anything is unresolved
mfp-sync --json --non-interactive apply plan.json --yes
```

## MCP server

An MCP server is included (optional extra: `pip install -e ".[mcp]"`,
console script `mfp-sync-mcp`). It exposes the sync operations as MCP tools
(`preview_plan`, `apply_plan`, `resolve_plan`, `status_plan`, `export_diary`,
`doctor`, `plan_schema`) and wraps the same engine, so tool output matches this
CLI's `--json` contract exactly.

- Works as a **local stdio subprocess** with ChatGPT Desktop, Claude Desktop,
  Cursor, and Kiro — nothing network-exposed, session stays local.
- For **ChatGPT web** it can run as a token-gated HTTP transport behind a tunnel.
  The HTTP transports are fail-closed: they refuse to start without
  `MFP_MCP_TOKEN` and reject any request lacking the bearer token.

Full setup for every client is in `docs/mcp.md`. The CLI with `--json` remains
the simplest path and needs no MCP client.
