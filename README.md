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

> ⚠️ **Live sync status:** the live MyFitnessPal backend uses the undocumented,
> reverse-engineered mobile API via [`mfp-api`](https://github.com/Rift-Walker/mfp-api).
> Its read/write/delete have been verified against the mock backend and are
> designed from `mfp-api`'s own live-verified round-trips, but **live writes to a
> real account have not been exercised in this environment.** See
> [docs/architecture.md](docs/architecture.md). Until you verify it against your
> own account (`doctor` + a single confirmed test entry), treat live sync as
> *unverified*.

## Requirements

- **Python 3.10–3.12.** (Not 3.13+: the live backend's transitive `lxml`/scraper
  dependencies lack wheels there.)
- macOS, Linux, or Windows. All examples use a POSIX shell.
- The live backend additionally needs the `mfp-api` package (git install, below).

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

# (Optional) the LIVE MyFitnessPal backend — only if you want real writes
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

## 2. Authentication (live backend only)

The default `mock` backend needs no credentials. For live sync, set your
MyFitnessPal credentials in the environment — they never leave your machine and
are never needed by any agent:

```bash
cp .env.example .env       # then edit MFP_USERNAME / MFP_PASSWORD
set -a; source .env; set +a
mfp-sync --backend myfitnesspal doctor     # read-only check, never writes
```

Full details, session expiry, and MFA/CAPTCHA notes: [docs/authentication.md](docs/authentication.md).

## 3. Generating a weekly plan

A plan is a JSON file (CSV also supported). Get the schema and an agent prompt:

```bash
mfp-sync schema                                    # prints the JSON Schema
cat docs/agent-integration.md                      # includes a ready-to-use prompt
```

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
├── config/settings.example.toml
├── examples/weekly_plan.json
├── src/mfp_sync/
│   ├── cli.py              # Typer CLI (the only module that prints)
│   ├── models.py           # Pydantic plan models + validation + units
│   ├── config.py           # settings precedence
│   ├── planner.py          # load/validate JSON+CSV; JSON Schema
│   ├── resolver.py         # food name -> (food_id, serving), catalog-first
│   ├── nutrition.py        # honest conversions + planned-vs-matched checks
│   ├── reconciliation.py   # three-way diff -> actions (safety core)
│   ├── writer.py           # safe mutations: retries, read-after-write, crash-safe
│   ├── storage.py          # SQLite: food catalog + sync ledger
│   ├── errors.py           # error codes <-> exit codes
│   ├── schema/weekly_plan.schema.json
│   └── backends/
│       ├── base.py         # DiaryBackend contract
│       ├── mock.py         # deterministic backend (tests/offline)
│       └── myfitnesspal.py # live backend (adapts mfp-api)
├── tests/                  # pytest suite (mock backend only)
└── docs/
    ├── architecture.md
    ├── authentication.md
    ├── troubleshooting.md
    └── agent-integration.md
```

## License

MIT. Not affiliated with or endorsed by MyFitnessPal / Under Armour. Uses an
undocumented API at your own risk.
