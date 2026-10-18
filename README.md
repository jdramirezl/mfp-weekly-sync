# mfp-weekly-sync

Prepopulate your **MyFitnessPal** food diary from a weekly meal plan — generated
by ChatGPT or any agent — with a single command. Built for the **free**
MyFitnessPal tier, runs entirely **locally**, and is **idempotent**: re-running
the same plan never duplicates entries and never clobbers the changes you make
by hand in the app.

```text
ChatGPT  →  meal_plan.json  →  mfp-sync preview  →  mfp-sync apply  →  MyFitnessPal
                                        ▲                                   │
                                        └──────── edit freely in the app ◄──┘
                              (re-running is safe: it preserves your edits)
```

- **Safe by default.** Preview shows every operation before anything is written.
  Writes require confirmation. Manual edits/deletions are detected and preserved.
- **Idempotent.** A local SQLite ledger records the exact remote entry id of each
  food it logs, so re-syncing adds only what's genuinely missing.
- **Honest.** It never invents serving conversions, never silently picks a random
  food search result, and never claims a live write succeeded without confirming
  it.
- **Offline-capable.** Ships a deterministic mock backend; the whole tool
  (including idempotency) works with no account and no network.
- **Agent-friendly.** `--json` output, stable error codes, a JSON Schema for the
  plan, and a `--non-interactive` mode.

> ⚠️ **Live sync status.** The `myfitnesspal` (username/password) backend's
> credential-free steps are verified but its credentialed path is untested (no
> password account was available). The legacy `cookie-session` backend is
> retained for compatibility, but current MyFitnessPal NextAuth sessions may
> reject its cookie exchange. For current SSO web-session CRUD, use
> [docs/mason-mcp.md](docs/mason-mcp.md). The underlying mobile/web APIs are
> undocumented and reverse-engineered, so treat them as subject to change.

## Backends

| Backend | For | Auth | Install extra |
|---|---|---|---|
| `mock` (default) | offline use, tests | none | — |
| `cookie-session` | **Google/Apple SSO logins** (no password) | your existing browser session (macOS/Windows: Chromium or Firefox) | `.[cookie]` |
| `myfitnesspal` | username/password logins | native mobile OAuth | `mfp-api` (git) |

Select with `--backend <name>` on any command, or `MFP_SYNC_BACKEND=<name>`.
If you sign in to MyFitnessPal with "Continue with Google", use `cookie-session`
— there is no password for the `myfitnesspal` backend to use. Choose which
browser holds your session with `MFP_COOKIE_BROWSER` (`chrome`, `edge`, `brave`,
`firefox`, …) and optionally `MFP_COOKIE_PROFILE`. On Windows, Firefox is the
most reliable (Chrome/Edge's newest "app-bound" cookies can't be read outside
the browser; the tool detects this and tells you to switch. For current
NextAuth sessions, prefer the separate Mason integration.

## Requirements

- **Python 3.10–3.12.** (Not 3.13+: a transitive dependency lacks wheels there.)
- macOS, Linux, or Windows for `mock`. The `cookie-session` backend works on
  **macOS** (Chromium via Keychain, or Firefox) and **Windows** (Chromium via
  DPAPI, or Firefox); Firefox also works on Linux.
- `cookie-session` needs the `.[cookie]` extra (`cryptography` + `httpx`);
  `myfitnesspal` needs the `mfp-api` package (git install, below).

## 1. Installation from scratch

```bash
git clone <this-repo> mfp-weekly-sync
cd mfp-weekly-sync

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install the tool
pip install -e .

# (Optional) dev tools for tests + linting
pip install -e ".[dev]"

# (Optional) cookie-session backend — for Google/SSO accounts (macOS)
pip install -e ".[cookie]"

# (Optional) MCP server — expose the tool to AI agents
pip install -e ".[mcp]"

# (Optional) the username/password LIVE backend
pip install "git+https://github.com/Rift-Walker/mfp-api"
# NOTE: if you created the venv with `uv venv` (no bundled pip), use:
#   uv pip install "git+https://github.com/Rift-Walker/mfp-api"
# Verify it landed in THIS venv:  python -c "import mfp_api"
```

Verify:

```bash
mfp-sync version
mfp-sync doctor            # uses the safe mock backend by default
```

## 2. Authentication (live backends only)

The default `mock` backend needs no credentials. Credentials/sessions stay on
your machine and are never needed by any agent.

**Google / Apple SSO account (no password) — `cookie-session`:**

```bash
pip install -e ".[cookie]"
# Log into myfitnesspal.com in Chrome first, then:
mfp-sync --backend cookie-session doctor   # read-only; triggers a one-time
                                           # macOS Keychain prompt — click
                                           # "Always Allow"
```
It reuses your existing Chrome session: it reads the MyFitnessPal cookies,
exchanges them for a bearer token, and calls the same v2 API. Expired sessions
are detected with a clear "log in again" message; multiple Chrome profiles are
searched automatically (or pass one explicitly in code). macOS + Chrome only.

**Username/password account — `myfitnesspal`:**

```bash
cp .env.example .env       # then edit MFP_USERNAME / MFP_PASSWORD
set -a; source .env; set +a
mfp-sync --backend myfitnesspal doctor     # read-only check, never writes
```

Full details, session expiry, profiles, and the SSO-vs-password distinction:
[docs/authentication.md](docs/authentication.md).

## Connecting an AI agent (MCP server)

An MCP server (`mfp-sync-mcp`) exposes the operations as agent tools, so
ChatGPT Desktop, Claude Desktop, Cursor, or Kiro can preview and apply plans.

```bash
pip install -e ".[mcp]"
mfp-sync-mcp                      # stdio transport (local subprocess) — safe default
```

Tools: `plan_schema`, `doctor`, `preview_plan` / `preview_plan_json`,
`status_plan`, `resolve_plan`, `export_diary`, `apply_plan` / `apply_plan_json`.
The `*_json` variants take a complete plan **object** (no file needed), and
`apply_plan_json` **requires `confirm=true`** before it writes — otherwise it
returns a preview. Re-running is idempotent and manual diary changes are
preserved.

For ChatGPT web, run a token-gated HTTP transport behind a tunnel. When run over
HTTP the server enters **exposed mode**: it refuses to start without
`MFP_MCP_TOKEN`, rejects any request missing the bearer token, rate-limits
requests, and hides the file-path tools (only the inline-JSON + read-only tools
are reachable). Full setup for every client, including the security model:
[docs/mcp.md](docs/mcp.md).

**On Windows**, a step-by-step runbook for serving this to ChatGPT over a tunnel
(or the ChatGPT desktop app with no tunnel) is in
[docs/windows-agent-runbook.md](docs/windows-agent-runbook.md) — written so an
agent on the Windows machine can execute it directly.


## 3. Generating a weekly plan

A plan is a JSON file (CSV also supported). The easiest way is to have ChatGPT
write it:

```bash
mfp-sync schema                      # prints the JSON Schema
open docs/chatgpt-prompt.md          # copy this into ChatGPT, edit the request block
```

[`docs/chatgpt-prompt.md`](docs/chatgpt-prompt.md) is a ready-to-paste prompt
**tuned so the generated plan resolves cleanly** against MyFitnessPal's food
database — it tells the model to use short generic food names, grams for solids,
required calorie estimates (used to catch wrong matches), and to split composite
dishes into component foods. Paste it, edit the "My plan request" block, save
the reply as `plan.json`, then `preview` and `apply`.

A sample 3-day plan with multiple meals ships at
[`examples/weekly_plan.json`](examples/weekly_plan.json). Minimal shape:

```json
{
  "schema_version": 1,
  "timezone": "America/Mexico_City",
  "days": [
    {
      "date": "2026-10-19",
      "meals": {
        "breakfast": [
          {"entry_id": "mon-breakfast-oats", "food": "Oatmeal, dry",
           "quantity": 40, "unit": "g", "expected_calories": 156}
        ],
        "lunch": [], "dinner": [], "snack": []
      }
    }
  ]
}
```

CSV form (headers required): `date,meal,food,quantity,unit[,entry_id,mfp_food_id,serving_id,brand,expected_calories]`.

## 4–8. The core workflow

```bash
# 4. Resolve any foods that don't map confidently to the database.
#    Your choices are saved to a local catalog and reused next week.
mfp-sync resolve examples/weekly_plan.json

# 5–6. Preview the exact operations, calories, and macros. No writes.
mfp-sync preview examples/weekly_plan.json

# 7. Apply (shows the plan, asks for confirmation, then writes).
mfp-sync apply examples/weekly_plan.json

# 8. Check what's synced vs the current diary.
mfp-sync status examples/weekly_plan.json
```

Add `--backend myfitnesspal` to any command to target your real account (the
default is the mock backend).

## 9–11. Editing, re-running, recovery

```bash
# 9. Edit/delete/replace foods directly in the MyFitnessPal app during the week.

# 10. Re-run the same plan safely. Entries you added are SKIPped; foods you
#     deleted are NOT recreated; your manual additions are left untouched.
mfp-sync apply examples/weekly_plan.json

# want the ones you deleted back? opt in explicitly:
mfp-sync apply examples/weekly_plan.json --restore-deleted

# 11. Interrupted mid-week (crash, timeout)? Just run apply again — it adds only
#     what's missing. Nothing is duplicated.
```

Conflict policy when you edited one of the tool's own entries:

```bash
mfp-sync --on-conflict skip      apply plan.json   # leave your edit (default-safe)
mfp-sync --on-conflict ask       apply plan.json   # prompt per conflict
mfp-sync --on-conflict overwrite apply plan.json   # replace with the plan's value
```

## 12. Updating an expired session

Sessions refresh automatically within a run. If login starts failing, re-source
your credentials and re-check:

```bash
set -a; source .env; set +a
mfp-sync --backend myfitnesspal doctor
```

## Export

```bash
mfp-sync export --from 2026-10-19 --to 2026-10-25 --format json
mfp-sync export --from 2026-10-19 --to 2026-10-25 --format csv --out week.csv
```

## Agent / automation use

```bash
mfp-sync --json preview plan.json
mfp-sync --json --non-interactive resolve plan.json
mfp-sync --json --non-interactive apply plan.json --yes
```

Every command emits JSON; every failure carries a stable `error_code` and exit
code. See [docs/agent-integration.md](docs/agent-integration.md) and
[docs/troubleshooting.md](docs/troubleshooting.md).

## Testing

```bash
pip install -e ".[dev]"
pytest                 # all tests run against the mock backend; never your account
ruff check src tests
```

Tests marked `@pytest.mark.live` (there are none that write to a real account by
default) would be the only ones that touch MyFitnessPal, and they are opt-in.

## How safety is guaranteed

The engine does a three-way comparison between the local sync ledger, the current
remote diary, and the requested plan — keyed on **stable remote entry IDs**. The
full decision table, crash-safety ordering, and the honest limitations are in
[docs/architecture.md](docs/architecture.md).

## Repository layout

```
mfp-weekly-sync/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── .github/workflows/ci.yml   # pytest + ruff on every push / PR
├── config/settings.example.toml
├── examples/weekly_plan.json
├── src/mfp_sync/
│   ├── cli.py              # Typer CLI (core user interface)
│   ├── mcp_server.py       # MCP server (thin wrapper over the engine)
│   ├── serialize.py        # shared JSON contract + EngineSession (CLI & MCP)
│   ├── models.py           # Pydantic plan models + validation + units
│   ├── config.py           # settings precedence
│   ├── planner.py          # load/validate JSON (file + dict) + CSV; JSON Schema
│   ├── resolver.py         # food name -> (food_id, serving), catalog-first
│   ├── nutrition.py        # honest conversions + planned-vs-matched checks
│   ├── reconciliation.py   # three-way diff -> actions (safety core)
│   ├── writer.py           # safe mutations: retries, read-after-write, crash-safe
│   ├── storage.py          # SQLite: food catalog + sync ledger
│   ├── browser_cookies.py  # macOS Chrome cookie extraction (cookie-session)
│   ├── errors.py           # error codes <-> exit codes
│   ├── schema/weekly_plan.schema.json
│   └── backends/
│       ├── base.py            # DiaryBackend contract
│       ├── mock.py            # deterministic backend (tests/offline)
│       ├── myfitnesspal.py    # live backend (username/password, mfp-api)
│       └── cookie_session.py  # live backend (browser session, SSO accounts)
├── tests/                  # pytest suite (mock backend only; never your account)
└── docs/
    ├── architecture.md
    ├── authentication.md
    ├── troubleshooting.md
    ├── agent-integration.md
    ├── chatgpt-prompt.md
    ├── mcp.md
    └── windows-agent-runbook.md
```

## License

MIT. Not affiliated with or endorsed by MyFitnessPal / Under Armour. Uses an
undocumented API at your own risk.
