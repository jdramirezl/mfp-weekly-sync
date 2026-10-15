"""Pydantic models for the weekly meal plan and shared value objects.

The meal plan is the user-authored (or ChatGPT-authored) input. These models
enforce every validation rule in the spec: ISO dates, known meal categories,
supported units, positive quantities, and unique entry IDs.

Dates are parsed as *local calendar dates* (datetime.date). We never convert
them through UTC — a plan date of 2026-10-19 means that calendar day in the
plan's timezone, full stop.
"""

from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 1


class MealCategory(str, Enum):
    """The four meal categories the free MyFitnessPal tier exposes.

    MyFitnessPal's API uses capitalized names (Breakfast/Lunch/Dinner/Snacks).
    We normalize to lowercase internally and map at the backend boundary.
    """

    breakfast = "breakfast"
    lunch = "lunch"
    dinner = "dinner"
    snack = "snack"

    @property
    def mfp_meal_name(self) -> str:
        """The meal_name string MyFitnessPal's v2 API expects."""
        return {
            MealCategory.breakfast: "Breakfast",
            MealCategory.lunch: "Lunch",
            MealCategory.dinner: "Dinner",
            MealCategory.snack: "Snacks",
        }[self]


# Units we understand. Mass units convert among themselves; "serving" and the
# volume units are passed through to MFP's serving definitions (we never invent
# a gram-weight for a volume or a serving — see resolver.py).
MASS_UNITS = {"g", "gram", "grams", "kg", "oz", "ounce", "ounces", "lb", "lbs", "pound", "pounds"}
VOLUME_UNITS = {"ml", "milliliter", "milliliters", "l", "liter", "liters", "cup", "cups",
                "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
                "fl oz", "fl_oz"}
COUNT_UNITS = {"serving", "servings", "piece", "pieces", "item", "items", "each", "unit", "units"}

SUPPORTED_UNITS = MASS_UNITS | VOLUME_UNITS | COUNT_UNITS

# Canonical grams per mass unit (exact where defined by law/convention).
GRAMS_PER_MASS_UNIT: dict[str, float] = {
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0,
    "oz": 28.349523125, "ounce": 28.349523125, "ounces": 28.349523125,
    "lb": 453.59237, "lbs": 453.59237, "pound": 453.59237, "pounds": 453.59237,
}


def normalize_unit(unit: str) -> str:
    """Lowercase + strip a unit string. Does not validate."""
    return unit.strip().lower()


class Macros(BaseModel):
    """Optional per-entry nutrition the plan author expects (for validation only).

    These are the *planned* values. They are kept strictly separate from what
    MyFitnessPal actually stores for the matched food (see nutrition.py).
    """

    model_config = ConfigDict(extra="forbid")

    calories: float | None = Field(default=None, ge=0)
    protein_g: float | None = Field(default=None, ge=0)
    carbs_g: float | None = Field(default=None, ge=0)
    fat_g: float | None = Field(default=None, ge=0)

    def is_empty(self) -> bool:
        return all(
            v is None for v in (self.calories, self.protein_g, self.carbs_g, self.fat_g)
        )


class PlanEntry(BaseModel):
    """A single planned food item within a meal."""

    model_config = ConfigDict(extra="forbid")

    entry_id: str = Field(..., min_length=1, description="Stable ID unique within the plan.")
    food: str = Field(..., min_length=1, description="Display name used for searching/matching.")
    quantity: float = Field(..., gt=0, description="Amount in `unit`.")
    unit: str = Field(default="serving", description="Measurement unit.")

    # Optional pre-resolved identity. When present, matching is skipped.
    mfp_food_id: str | None = Field(default=None)
    serving_id: str | None = Field(default=None)
    serving_description: str | None = Field(default=None)
    brand: str | None = Field(default=None)

    # Optional expected nutrition for validation.
    expected_calories: float | None = Field(default=None, ge=0)
    expected_macros: Macros | None = Field(default=None)

    @field_validator("unit")
    @classmethod
    def _validate_unit(cls, v: str) -> str:
        norm = normalize_unit(v)
        if not norm:
            raise ValueError("unit must not be blank")
        # We accept any non-blank unit. Mass/volume/count units convert
        # automatically; a *named serving unit* (e.g. "medium", "slice",
        # "scoop", "bottle") is passed through to MyFitnessPal's serving
        # definitions. The resolver refuses to invent a conversion when a
        # named unit has no matching serving (see nutrition.select_serving),
        # so an unknown unit fails loudly at resolve time, not here — this
        # keeps real MFP serving labels usable without an exhaustive list.
        return norm

    @field_validator("entry_id", "food")
    @classmethod
    def _strip_nonempty(cls, v: str) -> str:
        v2 = v.strip()
        if not v2:
            raise ValueError("must not be blank")
        return v2

    def planned_macros(self) -> Macros:
        """Unify expected_calories + expected_macros into one Macros object."""
        base = self.expected_macros or Macros()
        if base.calories is None and self.expected_calories is not None:
            base = base.model_copy(update={"calories": self.expected_calories})
        return base


class DayPlan(BaseModel):
    """One calendar day with its four meal buckets."""

    model_config = ConfigDict(extra="forbid")

    date: dt.date
    meals: dict[MealCategory, list[PlanEntry]] = Field(default_factory=dict)

    @field_validator("date", mode="before")
    @classmethod
    def _parse_local_date(cls, v: object) -> dt.date:
        """Parse YYYY-MM-DD as a local calendar date with no timezone math."""
        if isinstance(v, dt.date) and not isinstance(v, dt.datetime):
            return v
        if isinstance(v, dt.datetime):
            # A datetime slipped in; take its calendar date, ignore tz/time.
            return v.date()
        if isinstance(v, str):
            s = v.strip()
            try:
                return dt.date.fromisoformat(s)
            except ValueError as exc:
                raise ValueError(
                    f"invalid date {v!r}: expected ISO 8601 YYYY-MM-DD"
                ) from exc
        raise ValueError(f"invalid date {v!r}: expected ISO 8601 YYYY-MM-DD string")

    @model_validator(mode="before")
    @classmethod
    def _coerce_meal_keys(cls, data: object) -> object:
        """Allow meal keys to arrive as any case; reject unknown names clearly."""
        if not isinstance(data, dict):
            return data
        meals = data.get("meals")
        if isinstance(meals, dict):
            coerced: dict = {}
            for key, value in meals.items():
                k = key.strip().lower() if isinstance(key, str) else key
                # Map common synonyms.
                if k == "snacks":
                    k = "snack"
                if isinstance(k, str) and k not in {m.value for m in MealCategory}:
                    raise ValueError(
                        f"unknown meal category {key!r}. "
                        f"Expected one of: breakfast, lunch, dinner, snack"
                    )
                coerced[k] = value
            data = {**data, "meals": coerced}
        return data

    def entries(self) -> list[tuple[MealCategory, PlanEntry]]:
        """Flatten to (category, entry) pairs in a stable order."""
        out: list[tuple[MealCategory, PlanEntry]] = []
        for cat in MealCategory:
            for entry in self.meals.get(cat, []):
                out.append((cat, entry))
        return out


class MealPlan(BaseModel):
    """The top-level weekly plan document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION)
    timezone: str | None = Field(default=None)
    plan_id: str | None = Field(
        default=None,
        description="Stable identifier for this plan. Derived from the filename if omitted.",
    )
    days: list[DayPlan] = Field(default_factory=list, min_length=1)

    @field_validator("schema_version")
    @classmethod
    def _check_schema(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {v}; this build supports {SCHEMA_VERSION}"
            )
        return v

    @model_validator(mode="after")
    def _validate_uniqueness(self) -> MealPlan:
        # Duplicate entry IDs anywhere in the plan are a hard error — they would
        # make reconciliation ambiguous.
        seen: dict[str, str] = {}
        for day in self.days:
            for _cat, entry in day.entries():
                if entry.entry_id in seen:
                    raise ValueError(
                        f"duplicate entry_id {entry.entry_id!r} "
                        f"(first seen on {seen[entry.entry_id]}, "
                        f"again on {day.date.isoformat()})"
                    )
                seen[entry.entry_id] = day.date.isoformat()

        # Duplicate dates would make a day ambiguous.
        dates_seen: set[dt.date] = set()
        for day in self.days:
            if day.date in dates_seen:
                raise ValueError(f"duplicate date {day.date.isoformat()} in plan")
            dates_seen.add(day.date)
        return self

    def all_entries(self) -> list[tuple[dt.date, MealCategory, PlanEntry]]:
        out: list[tuple[dt.date, MealCategory, PlanEntry]] = []
        for day in sorted(self.days, key=lambda d: d.date):
            for cat, entry in day.entries():
                out.append((day.date, cat, entry))
        return out

    def effective_plan_id(self) -> str:
        return self.plan_id or "plan"


MealPlanType = Annotated[MealPlan, "Validated weekly meal plan"]
