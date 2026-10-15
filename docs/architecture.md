# Architecture & Integration Decision

## Summary

`mfp-weekly-sync` is a local-only CLI that prepopulates a MyFitnessPal food
diary from a weekly meal plan file. It is designed around one hard constraint:
**re-running a plan must never duplicate entries or destroy manual changes.**

## Backend decision

I evaluated the three candidate integrations in the order the brief required:
existing maintained Python integration → adapter around the MCP server →
browser automation.

### What I found (inspected source, not just READMEs)

| Project | What it is | Diary read | Diary write | Stable entry IDs | Auth |
|---|---|---|---|---|---|
| [`coddingtonbear/python-myfitnesspal`](https://github.com/coddingtonbear/python-myfitnesspal) | Website scraper | yes (scrape) | limited / fragile | no stable write IDs exposed | browser cookies |
| [`Rift-Walker/mfp-api`](https://github.com/Rift-Walker/mfp-api) | Native mobile REST v2 client (OAuth) | **yes** | **yes (create/delete)** | **yes — UUID `id` per entry** | username/password → bearer |
| [`AdamWalt/myfitnesspal-mcp-python`](https://github.com/AdamWalt/myfitnesspal-mcp-python) | MCP server wrapping the two above | yes | yes | yes (via mfp-api) | browser session + mfp-api |

Key evidence from reading `mfp-api/mfp_api/client.py` and its
`docs/api-reference.md`:

- `add_food_entry()` POSTs to `/v2/diary` and the `201` response returns
  `items[0].id` — a **stable UUID**. `get_food_diary()` returns the same `id`
  for each line item, and `delete_diary_entry(id)` removes it. Its own live
  integration test (`tests/test_client.py::test_food_diary_add_read_delete`)
  asserts the round-trip: add → find by `id` in the diary → delete → gone.
- Food search (`/v2/search/nutrition`) and food details (`/v2/foods/{id}`)
  return `serving_sizes[]` (with `gram_weight`) and `nutritional_contents`,
  which is everything the resolver and nutrition validator need.
- Auth is Under Armour's UACF Identity flow: username/password are wrapped in a
  signed JWT and exchanged for a bearer token (+ refresh token). No CAPTCHA or
  MFA step appears in the implemented flow, though MyFitnessPal may introduce
  one for a given account (see `docs/authentication.md`).

### Decision

**Use `mfp-api` directly as the live backend.** Rationale:

1. **Stable entry IDs are the linchpin of safe idempotency.** The whole
   reconciliation design (below) depends on being able to record the exact
   remote id of each entry we create and later ask "is *that specific entry*
   still there?". `mfp-api` provides this; a scraper does not.
2. **No unnecessary transport layer.** The MCP server
   (`myfitnesspal-mcp-python`) is itself built on `mfp-api`. Wrapping our CLI
   around an MCP server would add a process boundary and a protocol for no
   functional gain — the brief explicitly says to reuse the library directly
   if it works. So we depend on `mfp-api` and skip MCP as the core path.
   (An optional MCP interface could be layered on later; it is not required.)
3. **Browser automation (Playwright) is not needed** and is reserved as a
   fallback only if the REST path breaks.

The live backend is an **optional dependency**. The application ships a
deterministic **mock backend** that also assigns stable UUID IDs, so the entire
tool — including idempotent re-runs — works offline and in CI without touching a
real account.

### Risk register for the live path

- MyFitnessPal's v2 API is undocumented and reverse-engineered. It has changed
  before (the MCP README notes `/food/diary/{user}/add` started returning 404).
  If writes start failing, that is the most likely cause.
- Auth could begin requiring MFA/CAPTCHA for some accounts. We never bypass such
  controls; `doctor` surfaces an auth failure and `authentication.md` documents
  the manual path.
- Because of the above, **live writes are reported as "verified" only after an
  actual confirmed round-trip against a real account.** Until then the live
  backend's write capability is reported as *inferred, not test-exercised*.

## Module layout

```
cli.py            Typer CLI; the only module that prints. Maps results/errors
                  to exit codes and --json output.
models.py         Pydantic plan models + validation + the unit system.
config.py         Settings precedence: CLI flags > env > settings.toml > defaults.
planner.py        Load/validate plan files (JSON + CSV); JSON Schema generation.
resolver.py       Food name -> concrete (food_id, serving). Catalog-first.
nutrition.py      Honest unit conversion + planned-vs-matched validation.
reconciliation.py Three-way diff -> actions. The safety-critical core.
writer.py         Executes mutations: rate limit, retries, read-after-write,
                  crash-safe record ordering.
storage.py        SQLite: food catalog + sync-history ledger.
errors.py         Machine-readable error codes <-> exit codes.
backends/
  base.py         The DiaryBackend contract + value objects.
  mock.py         Deterministic backend (tests + offline). Stable UUID IDs.
  myfitnesspal.py Live backend adapting mfp-api. Lazily imported.
```

## Reconciliation model (how idempotency is guaranteed)

For each resolved plan entry we hold three states:

- **L** — the local sync record (what we logged last time, incl. the remote id).
- **R** — the current remote diary (read fresh at plan time).
- **P** — the requested plan entry (now).

Decision table (stable-id backend):

| L record | Remote entry (by recorded id) | Plan vs L | Action |
|---|---|---|---|
| none | — | — | **ADD** |
| exists, never got remote id (failed/pending/uncertain) | matching entry found by read | — | SKIP (treat as already synced) |
| exists, never got remote id | none found | — | **ADD** (retry prior write) |
| exists, id present | unchanged | unchanged | SKIP (idempotent) |
| exists, id present | fingerprint differs | — | CONFLICT_EDIT (skip/ask/overwrite) |
| exists, id present | unchanged | changed | CONFLICT_PLAN (skip/ask/overwrite) |
| exists, id gone | — | — | DELETED_REMOTE (leave; `--restore-deleted` to re-add) |
| any entry we never recorded | — | — | PRESERVE (never touched) |

A content **fingerprint** (date|meal|food_id|servings|serving) detects manual
edits to one of our managed entries without relying on nutrition values.

### Crash safety

`writer.py` writes a `pending` sync record **before** the remote call and
updates it to `synced` with the returned remote id **after** a successful
read-after-write verification. If the process dies in between, the record stays
`pending` with no remote id; the next run looks for a matching entry via a diary
read and either adopts it (SKIP) or retries the ADD — never blindly duplicating.

An **uncertain** write (response lost after the server may have committed) is
**never retried**; it is recorded as `uncertain` and left for the next read-based
reconciliation to resolve.

### Degraded mode (no stable IDs)

If a backend reports `supports_stable_ids == False`, the engine refuses
destructive actions and, when a plausible duplicate already exists, reports
`UNCERTAIN` rather than risk a duplicate. This path is tested
(`test_no_stable_ids_backend_is_conservative`).

## What is intentionally *not* done

- No claim of atomic weekly uploads — the backend has no transaction primitive,
  so each entry is its own write and recovery is read-based.
- No invented unit conversions. A mass request converts through grams only when
  a gram-weight is known; volumes/named servings must match a real serving
  definition or the item is reported `UNCONVERTIBLE`.
- No MCP server in the core path (optional future interface only).

## Phase 2: live validation record (verified)

Live validation was run against a real MyFitnessPal account that logs in via
Google SSO. Because an SSO account has no password, the `myfitnesspal`
(password) backend is not usable for it; a third backend, **`cookie-session`**,
was added and is the one verified live.

**Verified live (real account, cookie-session backend):**
- Authentication via the Chrome browser session (NextAuth cookies →
  `www.myfitnesspal.com/user/auth_token?refresh=true` → bearer token).
- Diary read for a specific date.
- Food search and serving/nutrition detail (`/v2/search/nutrition`, `/v2/foods/{id}`).
- Single diary **write**, read-back of the stable remote UUID and nutrition.
- Diary **delete** (used for cleanup).
- **Idempotency**: applying the same plan twice added exactly one entry total
  (second run added 0).

**Two bugs surfaced by live validation (would not appear against the mock) and
fixed, each with a regression test:**

1. **Serving count** — when a plan entry carried an explicit `serving_id`, the
   resolver used the requested `quantity` directly as the number of servings.
   Requesting "118 g" of a serving already defined as "118 g" logged **118
   servings (12 390 kcal)** instead of 1 (105 kcal). Fixed in
   `nutrition.select_serving`: when the request unit matches the serving unit,
   `servings = quantity / serving.value`; incompatible units convert via a
   known gram weight or are refused. Regression:
   `tests/test_nutrition.py::test_explicit_serving_id_same_unit_divides_by_serving_value`.

2. **Account-specific meal names** — the web/v2 API resolves `meal_name` against
   the account's own configured meals and silently coerces an unrecognized name
   to **Dinner**. This account's snack slots are "Snacks Morning/Noon/Night", so
   literal "Snacks" became Dinner. Fixed in the cookie-session backend by
   fetching `diary_preferences.meal_names` and mapping `snack` to the first real
   snack slot. (The web client also requires `type:"food_entry"` + the food
   `version`, now sent.) Regression:
   `tests/test_cookie_session.py::test_meal_name_resolution_against_account`.

**Still environment-specific / not generalized:**
- Cookie auto-extraction is implemented for macOS Chromium browsers and verified
  on Chrome. v20 app-bound cookies are not decryptable (documented).
- The `myfitnesspal` (password) backend's credentialed OAuth steps remain
  unverified (no password account was available); its credential-free identity
  probe is verified.
