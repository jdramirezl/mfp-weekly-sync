# Reconciliation rules (weekly plan → diary)

Distilled from the legacy `mfp-sync` engine (full code and its original
`architecture.md` live at the `legacy-mfp-sync` tag). These rules are the
contract for the v2 weekly-plan workflow in assistant-hub, which will apply a
[`plans/`](../plans/weekly_plan.schema.json) document through Mason's tools.
They were learned against a real account; two of them exist because a naive
implementation logged 118 servings (12 390 kcal) and silently moved snacks into
Dinner.

## Invariant

Re-running the same plan must never duplicate entries and never destroy a
change the user made by hand in the app.

## Three-way comparison

For every plan entry (keyed by its `entry_id`), hold three states:

- **L**: the local record of what we logged last time, including the remote
  entry id the diary returned.
- **R**: the current diary, read fresh before any write.
- **P**: the plan entry being applied now.

| L record | Remote (looked up by recorded id) | Plan vs L | Action |
|---|---|---|---|
| none | — | — | ADD |
| exists, no remote id (write pending/failed/uncertain) | a matching entry exists | — | SKIP (adopt it) |
| exists, no remote id | nothing matches | — | ADD (retry the earlier write) |
| exists with id | unchanged | unchanged | SKIP (idempotent) |
| exists with id | content differs | — | CONFLICT_EDIT: the user edited it |
| exists with id | unchanged | changed | CONFLICT_PLAN: the plan changed |
| exists with id | gone | — | DELETED_REMOTE: leave it deleted unless the user opts in to restore |
| diary entry we never recorded | — | — | PRESERVE: never touch it |

Conflicts use a policy: `skip` (default), `ask`, or `overwrite`. A content
fingerprint `date|meal|food_id|servings|serving` detects edits without relying
on nutrition values.

## Write safety

- Record the write as `pending` before calling the diary, and mark it `synced`
  with the returned id only after a read-back confirms it. A crash in between
  is resolved on the next run by reading the diary, never by blindly re-adding.
- An uncertain write (the response was lost after the server may have
  committed it) is never retried automatically.
- Without stable remote ids, refuse destructive actions and report UNCERTAIN
  instead of risking a duplicate.

## Food and serving resolution

- Never take the first search result. Use Mason's `fitness_draft_food` with
  calorie/macro targets, then log an explicit option and serving.
- Use the plan's `expected_calories` to reject a wrong match.
- Servings: when the request unit equals the serving's unit,
  `servings = quantity / serving.value`. Convert mass only through a known gram
  weight. Volumes and named servings must match a real serving definition, or
  the item is reported UNCONVERTIBLE. Never invent a conversion.
- Remember the user's choice per query (Mason's food pins do this) so next
  week resolves the same way.

## Meals

The diary resolves meal names against the account's own configured meals and
coerces unknown names to Dinner. Map plan meals (`breakfast`, `lunch`,
`dinner`, `snack`) onto the account's real meal slots. For example, `snack`
goes to the first snack slot when the account uses "Snacks Morning/Noon/Night".
Mason already falls back to the first through fourth meal for these keywords
and errors on anything unrecognized.

## Not promised

There is no atomic weekly upload: each entry is its own write, and recovery is
based on reading the diary back.
