# Troubleshooting

Start with:

```bash
mfp-sync doctor                       # mock backend
mfp-sync --backend myfitnesspal doctor
```

`doctor` checks Python version, state-dir writability, backend reachability,
authentication, read access, inferred write capability, and whether the backend
exposes stable entry IDs (required for safe idempotency). It never writes.

## Error codes

Every failure carries a stable `error_code` (visible with `--json`) and a
matching process exit code:

| error_code | exit | meaning / fix |
|---|---|---|
| `OK` | 0 | success |
| `INVALID_PLAN` | 2 | plan failed validation — read the listed field errors |
| `PLAN_NOT_FOUND` | 3 | the plan file path is wrong |
| `UNRESOLVED_FOODS` | 4 | run `mfp-sync resolve <plan>` first |
| `AUTH_REQUIRED` | 5 | set `MFP_USERNAME` / `MFP_PASSWORD` |
| `AUTH_FAILED` | 6 | wrong credentials, or MFA/CAPTCHA required (see authentication.md) |
| `SESSION_EXPIRED` | 7 | re-run; a fresh login occurs automatically |
| `BACKEND_UNAVAILABLE` | 8 | network issue, or `mfp-api` not installed |
| `WRITE_UNCERTAIN` | 9 | a write's outcome couldn't be confirmed — see below |
| `CONFLICT` | 10 | a managed entry was changed; choose an `--on-conflict` policy |
| `RATE_LIMITED` | 11 | slow down; retry later |
| `CONFIG_ERROR` | 12 | bad `settings.toml` or invalid flag value |
| `USER_ABORTED` | 13 | you declined the confirmation prompt |

## Common situations

### "N entries are unresolved; run 'mfp-sync resolve' first"
A food name didn't map confidently to a database item. Run
`mfp-sync resolve <plan>` interactively to pick the right food; your choice is
saved to the local catalog and reused next time. In `--non-interactive` mode,
`resolve --json` lists candidates for an agent to choose from.

### "cannot convert X <unit> ... Refusing to invent a conversion"
The requested unit doesn't match any serving the food defines, and there's no
honest way to convert it (e.g. "1 cup" of a food that only has a gram serving).
Fixes: use a unit the food supports (often `g`), or add the food via `resolve`
with a serving that matches your unit.

### A nutrition discrepancy is flagged
The matched food's actual calories/macros differ from your plan's *expected*
values beyond the configured tolerance. This is a warning, not a failure — it
means the database food may not be the one you intended. Review it, or adjust
tolerances in `config/settings.toml`.

### WRITE_UNCERTAIN after an apply
A write was sent but its result couldn't be confirmed (lost response, or
read-back didn't see it). The tool deliberately does **not** retry it, to avoid
a possible duplicate. Just run `apply` again: the next run reads the diary and
reconciles — it will adopt the entry if it actually landed, or add it if it
didn't. No duplicate either way.

### Re-running added duplicates (it shouldn't)
This should never happen with a stable-id backend. If you see it:
- Confirm `doctor` reports `stable entry IDs: OK`.
- Confirm you're using the same `--home` state directory between runs (the sync
  ledger lives there). A different `--home` means a fresh ledger.

### Recovering from an interrupted weekly upload
Just run `apply` again with the same plan and same `--home`. Entries already
written are SKIPped; only the missing ones are added. See
`test_partial_failure_then_recovery` for the guarantee.
