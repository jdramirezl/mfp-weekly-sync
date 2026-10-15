"""Canonical machine-readable serialization + a reusable engine session.

Both the CLI (`--json`) and the MCP server import these so the structured
contract (field names, error-code envelope) has a single source of truth.

Nothing here prints or depends on Typer/Rich — it returns plain dicts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backends import build_backend
from .config import Settings, load_settings
from .errors import ErrorCode, MfpSyncError
from .planner import load_plan
from .reconciliation import ApplyResult, Reconciler, SyncPlan
from .resolver import Resolver
from .storage import Storage

# -- engine session -----------------------------------------------------------


class EngineSession:
    """Owns a backend + storage + reconciler for the lifetime of one operation.

    Use as a context manager so the backend and storage are always closed:

        with EngineSession(settings) as eng:
            plan = eng.load("plan.json")
            sync_plan = eng.reconciler.build_plan(plan)
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        settings.home.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(settings.db_path)
        self.backend = build_backend(settings)
        self.resolver = Resolver(self.backend, self.storage, settings.matching)
        self.reconciler = Reconciler(self.backend, self.storage, self.resolver, settings)

    def load(self, plan_path: str | Path):
        return load_plan(plan_path)

    def load_dict(self, plan_obj: Any):
        """Validate a plan supplied as a dict (for the MCP *_json tools)."""
        from .planner import load_plan_dict

        return load_plan_dict(plan_obj)

    def close(self) -> None:
        try:
            self.backend.close()
        finally:
            self.storage.close()

    def __enter__(self) -> EngineSession:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def build_settings(
    *,
    backend: str | None = None,
    home: str | None = None,
    on_conflict: str | None = None,
    config_path: Path | None = None,
) -> Settings:
    """Build + validate Settings with the same precedence the CLI uses."""
    settings = load_settings(config_path)
    settings = settings.with_overrides(backend=backend, home=home, on_conflict=on_conflict)
    settings.validate()
    return settings


# -- serializers (identical to the CLI's --json output) -----------------------


def plan_to_dict(sync_plan: SyncPlan) -> dict[str, Any]:
    actions = []
    for a in sync_plan.actions:
        res = a.resolution
        actions.append({
            "date": a.date.isoformat(),
            "meal": a.meal,
            "entry_id": a.source_entry_id,
            "food": a.food_name,
            "action": a.action.value,
            "detail": a.detail,
            "matched_food": (res.food.description if res and res.food else None),
            "matched_food_id": (res.food.food_id if res and res.food else None),
            "servings": (res.selection.servings if res and res.selection else None),
            "serving_unit": (res.selection.serving.unit if res and res.selection else None),
            "nutrition_warnings": (
                [c.name for c in a.nutrition.warnings()] if a.nutrition else []
            ),
        })
    daily = {}
    for date, report in sync_plan.daily_nutrition.items():
        daily[date.isoformat()] = {
            c.name: {"planned": c.planned, "actual": c.actual, "ok": c.within_tolerance}
            for c in report.comparisons
        }
    return {
        "plan_id": sync_plan.plan_id,
        "counts": sync_plan.summary_counts(),
        "actions": actions,
        "daily_nutrition": daily,
    }


def apply_result_to_dict(result: ApplyResult) -> dict[str, Any]:
    return {
        "added": result.added,
        "skipped": result.skipped,
        "restored": result.restored,
        "overwritten": result.overwritten,
        "failed": [{"entry": e, "reason": r} for e, r in result.failed],
        "uncertain": [{"entry": e, "reason": r} for e, r in result.uncertain],
    }


def empty_result_dict() -> dict[str, Any]:
    return {"added": [], "skipped": [], "restored": [], "overwritten": [],
            "failed": [], "uncertain": []}


def ok_envelope(**fields: Any) -> dict[str, Any]:
    return {"ok": True, "error_code": ErrorCode.OK.value, **fields}


def error_envelope(exc: MfpSyncError) -> dict[str, Any]:
    return {"ok": False, "error_code": exc.code.value, "error": exc.message}
