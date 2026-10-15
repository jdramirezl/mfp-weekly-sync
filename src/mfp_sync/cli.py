"""Typer CLI for mfp-weekly-sync.

Commands:
  doctor    — check installation, config, auth, read/write capability (no writes)
  preview   — show the sync plan (no mutations)
  apply     — apply the sync plan after confirmation
  status    — compare plan vs sync history vs current diary
  resolve   — resolve unknown/ambiguous foods into the local catalog
  export    — export diary data to JSON/CSV (backend permitting)
  schema    — print the plan JSON Schema

Global flags:
  --backend {mock,myfitnesspal}   override configured backend
  --home PATH                     override state directory
  --json                          machine-readable output
  --non-interactive               never prompt (agent-friendly)
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .backends import build_backend
from .config import Settings, load_settings
from .errors import EXIT_CODES, ErrorCode, MfpSyncError
from .planner import load_plan, plan_json_schema
from .reconciliation import ActionType, PlannedAction, Reconciler, SyncPlan
from .resolver import Resolver
from .storage import Storage

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Prepopulate a MyFitnessPal diary from a weekly meal plan (local, idempotent).",
)
console = Console()
err_console = Console(stderr=True)

# Reusable argument spec (keeps command signatures within line length).
PlanArg = typer.Argument(..., help="Plan JSON/CSV")


@dataclass
class AppContext:
    settings: Settings
    json_output: bool
    non_interactive: bool


# -- global options -----------------------------------------------------------


@app.callback()
def main(
    ctx: typer.Context,
    backend: str | None = typer.Option(None, help="Override backend: mock|myfitnesspal"),
    home: str | None = typer.Option(None, help="Override local state directory"),
    on_conflict: str | None = typer.Option(
        None, "--on-conflict", help="Conflict policy: skip|ask|overwrite"
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable JSON output"),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Never prompt (agent workflows)"
    ),
    config: Path | None = typer.Option(None, "--config", help="Path to settings.toml"),
) -> None:
    try:
        settings = load_settings(config)
        settings = settings.with_overrides(backend=backend, home=home, on_conflict=on_conflict)
        settings.validate()
    except MfpSyncError as exc:
        _fail(exc, json_output)
    ctx.obj = AppContext(
        settings=settings, json_output=json_output, non_interactive=non_interactive
    )


# -- helpers ------------------------------------------------------------------


def _fail(exc: MfpSyncError, json_output: bool) -> None:
    if json_output:
        # Machine consumers read one stream — emit the error JSON on stdout.
        console.print_json(
            data={"ok": False, "error_code": exc.code.value, "error": exc.message}
        )
    else:
        err_console.print(f"[red]error[/red] [{exc.code.value}] {exc.message}")
    raise typer.Exit(code=exc.exit_code())


def _emit_json(data: dict) -> None:
    console.print_json(data=data)


def _open_backend_and_storage(app_ctx: AppContext):
    app_ctx.settings.home.mkdir(parents=True, exist_ok=True)
    storage = Storage(app_ctx.settings.db_path)
    backend = build_backend(app_ctx.settings)
    return backend, storage


def _action_style(action: ActionType) -> str:
    return {
        ActionType.ADD: "green",
        ActionType.RESTORE: "green",
        ActionType.SKIP: "dim",
        ActionType.DELETED_REMOTE: "yellow",
        ActionType.CONFLICT_EDIT: "red",
        ActionType.CONFLICT_PLAN: "red",
        ActionType.UNRESOLVED: "red",
        ActionType.UNCERTAIN: "yellow",
    }.get(action, "white")


def _plan_to_dict(sync_plan: SyncPlan) -> dict:
    # Canonical shape lives in serialize.py so the MCP server matches exactly.
    from .serialize import plan_to_dict

    return plan_to_dict(sync_plan)


def _render_plan_table(sync_plan: SyncPlan) -> None:
    table = Table(title=f"Sync plan: {sync_plan.plan_id}", show_lines=False)
    table.add_column("Date")
    table.add_column("Meal")
    table.add_column("Food")
    table.add_column("Action")
    table.add_column("Matched / detail")
    for a in sync_plan.actions:
        res = a.resolution
        matched = res.food.description if res and res.food else ""
        if res and res.selection:
            matched += f"  [{res.selection.servings:g} × {res.selection.serving.unit}]"
        detail = a.detail or matched
        warn = ""
        if a.nutrition and a.nutrition.has_warnings:
            warn = "  [yellow]⚠ nutrition[/yellow]"
        table.add_row(
            a.date.isoformat(), a.meal, a.food_name,
            f"[{_action_style(a.action)}]{a.action.value}[/]",
            (matched or detail) + warn,
        )
    console.print(table)

    # Daily nutrition summary.
    if sync_plan.daily_nutrition:
        nut = Table(title="Daily nutrition (planned vs matched)")
        nut.add_column("Date")
        nut.add_column("kcal (plan/actual)")
        nut.add_column("P")
        nut.add_column("C")
        nut.add_column("F")
        for date in sorted(sync_plan.daily_nutrition):
            report = sync_plan.daily_nutrition[date]
            cells = {c.name: c for c in report.comparisons}

            def fmt(name: str, cells=cells) -> str:
                c = cells.get(name)
                if not c or (c.planned is None and c.actual is None):
                    return "-"
                p = f"{c.planned:g}" if c.planned is not None else "?"
                a_ = f"{c.actual:g}" if c.actual is not None else "?"
                mark = "" if c.within_tolerance else " ⚠"
                return f"{p}/{a_}{mark}"

            nut.add_row(date.isoformat(), fmt("calories"), fmt("protein_g"),
                        fmt("carbs_g"), fmt("fat_g"))
        console.print(nut)


def _counts_line(sync_plan: SyncPlan) -> str:
    c = sync_plan.summary_counts()
    parts = [f"{v} {k}" for k, v in sorted(c.items())]
    return ", ".join(parts) if parts else "nothing to do"


# -- commands -----------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the version."""
    console.print(__version__)


@app.command()
def schema(ctx: typer.Context) -> None:
    """Print the weekly meal plan JSON Schema."""
    console.print_json(data=plan_json_schema())


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check installation, config, auth, and read/write capability (no writes)."""
    app_ctx: AppContext = ctx.obj
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python>=3.10", sys.version_info >= (3, 10), sys.version.split()[0]))
    checks.append(("state dir writable", _dir_writable(app_ctx.settings.home),
                   str(app_ctx.settings.home)))

    try:
        backend, storage = _open_backend_and_storage(app_ctx)
    except MfpSyncError as exc:
        _fail(exc, app_ctx.json_output)

    health = backend.health_check()
    checks.append((f"backend '{health.backend}' reachable", health.reachable, health.detail))
    checks.append(("authenticated", health.authenticated, ""))
    checks.append(("read access", health.can_read, ""))
    checks.append(("write capability", health.can_write,
                   "verified" if health.backend == "mock" else "inferred, not test-written"))
    checks.append(("stable entry IDs (idempotency)", health.supports_stable_ids, ""))
    backend.close()
    storage.close()

    all_ok = all(ok for _n, ok, _d in checks)
    if app_ctx.json_output:
        _emit_json({
            "ok": all_ok,
            "error_code": ErrorCode.OK.value if all_ok else ErrorCode.BACKEND_UNAVAILABLE.value,
            "backend": health.backend,
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks],
        })
    else:
        table = Table(title="mfp-sync doctor")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Detail")
        for name, ok, detail in checks:
            table.add_row(name, "[green]OK[/]" if ok else "[red]FAIL[/]", detail)
        console.print(table)
    raise typer.Exit(code=0 if all_ok else EXIT_CODES[ErrorCode.BACKEND_UNAVAILABLE])


@app.command()
def preview(ctx: typer.Context, plan_file: Path = PlanArg) -> None:
    """Show the sync plan without making any changes."""
    app_ctx: AppContext = ctx.obj
    sync_plan, backend, storage = _build_sync_plan(app_ctx, plan_file)
    try:
        if app_ctx.json_output:
            _emit_json({"ok": True, "error_code": ErrorCode.OK.value, **_plan_to_dict(sync_plan)})
        else:
            _render_plan_table(sync_plan)
            console.print(f"\n[bold]Summary:[/bold] {_counts_line(sync_plan)}")
            if sync_plan.unresolved():
                console.print(
                    "[yellow]Some foods are unresolved. "
                    "Run 'mfp-sync resolve' before applying.[/yellow]"
                )
    finally:
        backend.close()
        storage.close()


@app.command()
def apply(
    ctx: typer.Context,
    plan_file: Path = typer.Argument(..., help="Plan JSON/CSV"),
    restore_deleted: bool = typer.Option(False, "--restore-deleted",
                                         help="Re-add entries you deleted in MyFitnessPal"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
) -> None:
    """Apply the sync plan (writes to the diary) after confirmation."""
    app_ctx: AppContext = ctx.obj
    sync_plan, backend, storage = _build_sync_plan(
        app_ctx, plan_file, restore_deleted=restore_deleted
    )
    try:
        unresolved = sync_plan.unresolved()
        if unresolved:
            exc = MfpSyncError(
                f"{len(unresolved)} entries are unresolved; run 'mfp-sync resolve' first",
                code=ErrorCode.UNRESOLVED_FOODS,
            )
            _fail(exc, app_ctx.json_output)

        mutating = [a for a in sync_plan.actions if a.mutates]
        if not app_ctx.json_output:
            _render_plan_table(sync_plan)
            console.print(f"\n[bold]Will apply:[/bold] {_counts_line(sync_plan)}")

        if not mutating:
            msg = "nothing to apply; diary already matches the plan"
            if app_ctx.json_output:
                _emit_json({"ok": True, "error_code": ErrorCode.OK.value,
                            "message": msg, "result": _empty_result_dict()})
            else:
                console.print(f"[green]{msg}[/green]")
            return

        if not yes and not app_ctx.non_interactive:
            if not typer.confirm(f"Apply {len(mutating)} change(s) to the diary?"):
                exc = MfpSyncError("aborted by user", code=ErrorCode.USER_ABORTED)
                _fail(exc, app_ctx.json_output)

        reconciler = _make_reconciler(app_ctx, backend, storage)

        def confirm_conflict(action: PlannedAction) -> bool:
            if app_ctx.non_interactive:
                return False  # never overwrite silently in agent mode
            return typer.confirm(
                f"Overwrite manually-changed entry {action.source_entry_id} "
                f"({action.food_name} on {action.date})?"
            )

        result = reconciler.apply(sync_plan, confirm_conflict=confirm_conflict)
        _report_apply_result(app_ctx, result)
    finally:
        backend.close()
        storage.close()


@app.command()
def status(ctx: typer.Context, plan_file: Path = PlanArg) -> None:
    """Compare the plan, local sync history, and the current diary."""
    app_ctx: AppContext = ctx.obj
    sync_plan, backend, storage = _build_sync_plan(app_ctx, plan_file)
    try:
        rows = []
        for a in sync_plan.actions:
            rec = storage.get_sync_record(sync_plan.plan_id, a.source_entry_id)
            rows.append({
                "entry_id": a.source_entry_id,
                "date": a.date.isoformat(),
                "meal": a.meal,
                "food": a.food_name,
                "current_action": a.action.value,
                "history_status": rec.status if rec else "never_synced",
                "remote_entry_id": rec.remote_entry_id if rec else None,
            })
        if app_ctx.json_output:
            _emit_json({"ok": True, "error_code": ErrorCode.OK.value,
                        "plan_id": sync_plan.plan_id, "entries": rows})
        else:
            table = Table(title=f"Status: {sync_plan.plan_id}")
            for col in ("entry_id", "date", "meal", "food", "current_action", "history_status"):
                table.add_column(col)
            for r in rows:
                table.add_row(r["entry_id"], r["date"], r["meal"], r["food"],
                              r["current_action"], r["history_status"])
            console.print(table)
    finally:
        backend.close()
        storage.close()


@app.command()
def resolve(ctx: typer.Context, plan_file: Path = PlanArg) -> None:
    """Resolve unknown or ambiguous foods and save matches to the local catalog."""
    app_ctx: AppContext = ctx.obj
    try:
        plan = load_plan(plan_file)
    except MfpSyncError as exc:
        _fail(exc, app_ctx.json_output)

    backend, storage = _open_backend_and_storage(app_ctx)
    resolver = Resolver(backend, storage, app_ctx.settings.matching)
    try:
        unresolved_report = []
        for _date, _cat, entry in plan.all_entries():
            res = resolver.resolve(entry)
            if res.ok:
                continue
            if app_ctx.non_interactive or app_ctx.json_output:
                unresolved_report.append({
                    "entry_id": entry.entry_id, "food": entry.food,
                    "status": res.status.value, "message": res.message,
                    "candidates": [
                        {"food_id": c.food_id, "description": c.description, "brand": c.brand}
                        for c in res.candidates
                    ],
                })
                continue
            _interactive_resolve(entry, res, resolver, backend)
        if app_ctx.json_output:
            _emit_json({
                "ok": not unresolved_report,
                "error_code": (ErrorCode.OK.value if not unresolved_report
                               else ErrorCode.UNRESOLVED_FOODS.value),
                "unresolved": unresolved_report,
            })
        elif not app_ctx.non_interactive:
            console.print("[green]Resolution complete.[/green]")
        elif unresolved_report:
            console.print(
                f"[yellow]{len(unresolved_report)} unresolved (non-interactive).[/yellow]"
            )
    finally:
        backend.close()
        storage.close()


@app.command()
def export(
    ctx: typer.Context,
    from_date: str = typer.Option(..., "--from", help="Start date YYYY-MM-DD"),
    to_date: str = typer.Option(..., "--to", help="End date YYYY-MM-DD"),
    fmt: str = typer.Option("json", "--format", help="json|csv"),
    out: Path | None = typer.Option(None, "--out", help="Output file (default stdout)"),
) -> None:
    """Export diary data for a date range to JSON or CSV."""
    app_ctx: AppContext = ctx.obj
    try:
        start = dt.date.fromisoformat(from_date)
        end = dt.date.fromisoformat(to_date)
    except ValueError as exc:
        _fail(
            MfpSyncError(f"invalid date: {exc}", code=ErrorCode.INVALID_PLAN),
            app_ctx.json_output,
        )

    backend, storage = _open_backend_and_storage(app_ctx)
    try:
        days = []
        day = start
        while day <= end:
            entries = backend.get_diary(day)
            days.append({
                "date": day.isoformat(),
                "entries": [
                    {
                        "remote_id": e.remote_id, "meal": e.meal,
                        "food_id": e.food_id, "food": e.food_description,
                        "servings": e.servings, "serving_unit": e.serving_unit,
                        "calories": e.calories, "protein_g": e.protein_g,
                        "carbs_g": e.carbs_g, "fat_g": e.fat_g,
                    }
                    for e in entries
                ],
            })
            day += dt.timedelta(days=1)

        if fmt == "csv":
            text = _export_csv(days)
        else:
            text = json.dumps({"from": from_date, "to": to_date, "days": days}, indent=2)

        if out:
            out.write_text(text, encoding="utf-8")
            if not app_ctx.json_output:
                console.print(f"[green]wrote {out}[/green]")
        else:
            console.print(text)
    finally:
        backend.close()
        storage.close()


# -- command helpers ----------------------------------------------------------


def _build_sync_plan(app_ctx: AppContext, plan_file: Path, *, restore_deleted: bool = False):
    try:
        plan = load_plan(plan_file)
    except MfpSyncError as exc:
        _fail(exc, app_ctx.json_output)
    backend, storage = _open_backend_and_storage(app_ctx)
    reconciler = _make_reconciler(app_ctx, backend, storage)
    try:
        sync_plan = reconciler.build_plan(plan, restore_deleted=restore_deleted)
    except MfpSyncError as exc:
        backend.close()
        storage.close()
        _fail(exc, app_ctx.json_output)
    return sync_plan, backend, storage


def _make_reconciler(app_ctx: AppContext, backend, storage) -> Reconciler:
    resolver = Resolver(backend, storage, app_ctx.settings.matching)
    return Reconciler(backend, storage, resolver, app_ctx.settings)


def _interactive_resolve(entry, res, resolver: Resolver, backend) -> None:
    console.print(f"\n[bold]{entry.food}[/bold] ({entry.quantity} {entry.unit}) — {res.message}")
    if not res.candidates:
        console.print("  [red]no candidates found; try editing the food name[/red]")
        return
    for i, c in enumerate(res.candidates, start=1):
        brand = f" [{c.brand}]" if c.brand else ""
        console.print(f"  {i}. {c.description}{brand} (id={c.food_id})")
    choice = typer.prompt("  pick a number (or 's' to skip)", default="s")
    if choice.strip().lower() == "s":
        return
    try:
        idx = int(choice) - 1
        chosen = res.candidates[idx]
    except (ValueError, IndexError):
        console.print("  [red]invalid choice; skipped[/red]")
        return
    serving = chosen.serving_sizes[0] if chosen.serving_sizes else None
    if serving is None:
        console.print("  [yellow]chosen food has no serving sizes; skipped[/yellow]")
        return
    resolver.save_to_catalog(entry.food, chosen, serving)
    console.print(f"  [green]saved {chosen.description} to catalog[/green]")


def _report_apply_result(app_ctx: AppContext, result) -> None:
    from .serialize import apply_result_to_dict

    data = {
        "ok": result.ok,
        "error_code": (ErrorCode.OK.value if result.ok else ErrorCode.WRITE_UNCERTAIN.value),
        "result": apply_result_to_dict(result),
    }
    if app_ctx.json_output:
        _emit_json(data)
    else:
        console.print(
            f"\n[green]added {len(result.added)}[/green], "
            f"skipped {len(result.skipped)}, "
            f"restored {len(result.restored)}, "
            f"overwritten {len(result.overwritten)}, "
            f"[red]failed {len(result.failed)}[/red], "
            f"[yellow]uncertain {len(result.uncertain)}[/yellow]"
        )
        for entry, reason in result.failed:
            console.print(f"  [red]FAILED[/red] {entry}: {reason}")
        for entry, reason in result.uncertain:
            console.print(f"  [yellow]UNCERTAIN[/yellow] {entry}: {reason}")
    if not result.ok:
        raise typer.Exit(code=EXIT_CODES[ErrorCode.WRITE_UNCERTAIN])


def _empty_result_dict() -> dict:
    from .serialize import empty_result_dict

    return empty_result_dict()


def _export_csv(days: list[dict]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "meal", "food", "food_id", "servings", "serving_unit",
                     "calories", "protein_g", "carbs_g", "fat_g", "remote_id"])
    for day in days:
        for e in day["entries"]:
            writer.writerow([day["date"], e["meal"], e["food"], e["food_id"],
                             e["servings"], e["serving_unit"], e["calories"],
                             e["protein_g"], e["carbs_g"], e["fat_g"], e["remote_id"]])
    return buf.getvalue()


def _dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test = path / ".write_test"
        test.write_text("ok")
        test.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    app()
