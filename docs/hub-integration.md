# assistant-hub integration

The hub (Mastra, on the same Mac) connects to the shared Mason instance over
streamable HTTP. No token is needed, because the endpoint is loopback-only.

## `mcp-servers.json` entry

```json
{ "name": "mfp", "url": "http://127.0.0.1:8484/mcp" }
```

Mason's tool names already follow the hub's per-app prefix convention
(`fitness_*`), so no renaming or wrapping is needed.

## Approvals

Gate these tools behind the hub's Approve/Deny card, since they change or remove
diary data:

- `fitness_delete_food`, `fitness_delete_exercise`
- `fitness_modify_food`
- `fitness_clear_food_pin`

Everything else either reads or adds an entry the user asked for: `fitness_log_*`
under the "smart" write policy.

## Errors

Mason reports failures in its own way: MCP tool errors (`isError: true`) whose
text describes the cause. Examples are an expired session, "retrying with a
refreshed session" progress messages, and unknown meal names. Its format is not
ours to change, so the hub's MCP adapter normalizes it into the shared shape:

```json
{ "error": { "code": "MFP_SESSION_EXPIRED", "message": "...", "retryable": false } }
```

Suggested mapping:

| Mason situation | `code` | `retryable` |
|---|---|---|
| Endpoint unreachable / connection refused | `MFP_UNAVAILABLE` | true |
| Session rejected after auto-refresh failed | `MFP_SESSION_EXPIRED` | false (user must run `mfp-mcp auth`) |
| 403 / Cloudflare | `MFP_BLOCKED` | false |
| Bad input (unknown meal, no draft option) | `MFP_BAD_REQUEST` | false |
| Anything else | `MFP_ERROR` | false |

If the endpoint is down, the hub should keep working without the `fitness_*`
tools and say so. The daily health check separately alerts you on Telegram.

## v2 hooks (not built yet)

- **Weekly calorie report:** a step in the hub's `weekly-digest` workflow calls
  `fitness_bulk_export` for the last 7 days (or `fitness_get_trends` per metric).
  It summarizes calories and macros against targets on the `report` model.
- **Weekly meal plan:** the hub takes a plan matching
  [`plans/weekly_plan.schema.json`](../plans/weekly_plan.schema.json), generated
  with [`plans/chatgpt-prompt.md`](../plans/chatgpt-prompt.md) or by the agent.
  It applies the plan through `fitness_draft_food` and then `fitness_log_food`,
  following [reconciliation-rules.md](reconciliation-rules.md) so re-runs don't
  duplicate entries or overwrite manual edits.
