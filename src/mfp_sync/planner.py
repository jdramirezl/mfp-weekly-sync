"""Load, validate, and introspect meal plan files (JSON and CSV)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import InvalidPlanError, PlanNotFoundError
from .models import MealPlan


def load_plan(path: str | Path) -> MealPlan:
    """Load and validate a plan from a .json or .csv file."""
    p = Path(path)
    if not p.exists():
        raise PlanNotFoundError(f"plan file not found: {p}")

    suffix = p.suffix.lower()
    try:
        if suffix == ".csv":
            data = _csv_to_plan_dict(p)
        else:
            data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidPlanError(f"{p} is not valid JSON: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise InvalidPlanError(f"could not read {p}: {exc}") from exc

    # Default plan_id from filename stem if not provided.
    if isinstance(data, dict) and not data.get("plan_id"):
        data["plan_id"] = p.stem

    return load_plan_dict(data)


def load_plan_dict(data: Any) -> MealPlan:
    """Validate a plan already parsed into a dict (same rules as load_plan).

    Used by the file loader above and by the MCP *_json tools, which receive a
    complete plan object directly rather than a file path.
    """
    if not isinstance(data, dict):
        raise InvalidPlanError("plan must be a JSON object")
    try:
        return MealPlan.model_validate(data)
    except ValidationError as exc:
        raise InvalidPlanError(_format_validation_error(exc)) from exc


def _csv_to_plan_dict(path: Path) -> dict[str, Any]:
    """Convert a flat CSV into the nested plan dict.

    Expected columns (header row required):
      date, meal, food, quantity, unit,
      [entry_id, mfp_food_id, serving_id, brand, expected_calories]
    """
    days: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"date", "meal", "food", "quantity", "unit"}
        missing = required - set(h.strip() for h in (reader.fieldnames or []))
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(sorted(missing))}")
        for i, row in enumerate(reader, start=2):
            date = (row.get("date") or "").strip()
            meal = (row.get("meal") or "").strip().lower()
            if not date or not meal:
                raise ValueError(f"row {i}: date and meal are required")
            day = days.setdefault(date, {"date": date, "meals": {}})
            meals = day["meals"].setdefault(meal, [])
            entry: dict[str, Any] = {
                "entry_id": (row.get("entry_id") or f"{date}-{meal}-{len(meals)+1}").strip(),
                "food": (row.get("food") or "").strip(),
                "quantity": float(row["quantity"]),
                "unit": (row.get("unit") or "serving").strip(),
            }
            for opt in ("mfp_food_id", "serving_id", "brand"):
                val = (row.get(opt) or "").strip()
                if val:
                    entry[opt] = val
            exp_cal = (row.get("expected_calories") or "").strip()
            if exp_cal:
                entry["expected_calories"] = float(exp_cal)
            meals.append(entry)
    return {"schema_version": 1, "days": list(days.values())}


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["meal plan failed validation:"]
    for err in exc.errors():
        loc = " -> ".join(str(x) for x in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


# -- JSON Schema generation ---------------------------------------------------


def plan_json_schema() -> dict[str, Any]:
    """Return the JSON Schema for the weekly meal plan (agent integration)."""
    schema = MealPlan.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "MyFitnessPal Weekly Meal Plan"
    return schema
