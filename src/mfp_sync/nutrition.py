"""Unit conversion and planned-vs-matched nutrition validation.

Two responsibilities:

1. Convert a requested (quantity, unit) into a concrete serving selection for a
   matched food, *honestly*. We convert among mass units freely (g/kg/oz/lb).
   We convert a mass request into "number of servings" ONLY when the serving's
   gram_weight is known. We never invent a gram-weight for a volume unit or a
   count/"serving" unit, and we never assume a cup/scoop/piece weighs some
   default — if the conversion isn't defined, we say so.

2. Compare the plan's *expected* nutrition against the matched food's *actual*
   nutrition and flag discrepancies beyond configurable tolerances. Planned
   values and MyFitnessPal's stored values are kept strictly separate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backends.base import FoodCandidate, ServingSize
from .config import NutritionConfig
from .models import GRAMS_PER_MASS_UNIT, MASS_UNITS, Macros, PlanEntry, normalize_unit


class ConversionError(Exception):
    """Raised when a requested unit cannot be honestly converted to a serving."""


@dataclass(frozen=True)
class ServingSelection:
    """The concrete serving + count to log for a requested quantity."""

    serving: ServingSize
    servings: float
    note: str = ""


def grams(quantity: float, unit: str) -> float | None:
    """Return the gram-equivalent of a mass quantity, or None if `unit` is not mass."""
    u = normalize_unit(unit)
    if u in MASS_UNITS:
        return quantity * GRAMS_PER_MASS_UNIT[u]
    return None


def select_serving(entry: PlanEntry, food: FoodCandidate) -> ServingSelection:
    """Choose which serving definition to use and how many of it to log.

    Strategy, in order:
      1. If the plan names a serving_id that the food has, use it directly with
         servings = quantity.
      2. If the requested unit matches a serving's unit exactly, use that serving
         with servings = quantity / serving.value.
      3. If the request is a mass and the food has a gram-denominated serving
         (serving.unit in mass units, or gram_weight known), convert via grams.
      4. Otherwise refuse — do not guess.
    """
    requested_unit = normalize_unit(entry.unit)
    qty = entry.quantity

    # 1. explicit serving_id
    if entry.serving_id:
        for s in food.serving_sizes:
            if s.serving_id == entry.serving_id:
                # The serving already represents `s.value` of `s.unit`. If the
                # request is in the same unit, the number of servings is
                # qty / s.value (requesting 118 g of a "118 g" serving == 1
                # serving, NOT 118). Only fall back to treating qty as a raw
                # serving count when the units genuinely differ (e.g. the plan
                # asked for "2 servings" of a serving defined in grams).
                if normalize_unit(s.unit) == requested_unit and s.value:
                    return ServingSelection(
                        s, qty / s.value,
                        note=f"serving_id {entry.serving_id}: "
                             f"{qty}{requested_unit} / {s.value}{s.unit} per serving",
                    )
                if requested_unit in {"serving", "servings"}:
                    return ServingSelection(
                        s, qty, note=f"serving_id {entry.serving_id}: {qty} servings"
                    )
                # Units differ and request isn't "servings" — try a mass
                # conversion against this serving's gram weight if possible.
                req_g = grams(qty, requested_unit)
                per_g = s.gram_weight or (
                    GRAMS_PER_MASS_UNIT.get(normalize_unit(s.unit), 0.0) * s.value
                )
                if req_g is not None and per_g and per_g > 0:
                    return ServingSelection(
                        s, req_g / per_g,
                        note=f"serving_id {entry.serving_id}: "
                             f"{req_g:g} g / {per_g:g} g per serving",
                    )
                # Can't reconcile the requested unit with this serving honestly.
                raise ConversionError(
                    f"serving_id {entry.serving_id} is defined in {s.unit!r} but the "
                    f"plan requested {entry.unit!r}; cannot convert without a known "
                    "gram weight. Refusing to invent a conversion."
                )

    # 2. exact unit match
    for s in food.serving_sizes:
        if normalize_unit(s.unit) == requested_unit:
            return ServingSelection(
                s, qty / s.value if s.value else qty,
                note=f"unit match on {s.unit!r}",
            )

    # 3. mass request -> gram-weighted serving
    requested_g = grams(qty, requested_unit)
    if requested_g is not None:
        # Prefer a per-gram serving (gram_weight == 1 and unit is a mass unit).
        gram_serving = _find_gram_serving(food)
        if gram_serving is not None:
            per_serving_g = gram_serving.gram_weight or (
                GRAMS_PER_MASS_UNIT.get(normalize_unit(gram_serving.unit), 0.0)
                * gram_serving.value
            )
            if per_serving_g and per_serving_g > 0:
                return ServingSelection(
                    gram_serving,
                    requested_g / per_serving_g,
                    note=f"{requested_g:g} g / {per_serving_g:g} g per serving",
                )

    # 4. refuse to guess
    available = ", ".join(sorted({s.unit for s in food.serving_sizes})) or "(none)"
    raise ConversionError(
        f"cannot convert {qty} {entry.unit!r} of {entry.food!r} to a serving of "
        f"{food.description!r}. Available serving units: {available}. "
        "Refusing to invent a conversion."
    )


def _find_gram_serving(food: FoodCandidate) -> ServingSize | None:
    """Return a serving whose gram-weight is known, preferring a per-gram one."""
    best: ServingSize | None = None
    for s in food.serving_sizes:
        u = normalize_unit(s.unit)
        if u in MASS_UNITS:
            # per-gram serving is ideal
            if u in {"g", "gram", "grams"} and s.value == 1.0:
                return s
            best = best or s
        elif s.gram_weight:
            best = best or s
    return best


# -- nutrition validation -----------------------------------------------------


@dataclass(frozen=True)
class NutrientComparison:
    name: str
    planned: float | None
    actual: float | None
    tolerance: float
    within_tolerance: bool
    note: str = ""

    @property
    def diff_fraction(self) -> float | None:
        if self.planned is None or self.actual is None:
            return None
        if self.planned == 0:
            return None if self.actual == 0 else float("inf")
        return abs(self.actual - self.planned) / self.planned


@dataclass(frozen=True)
class NutritionReport:
    comparisons: tuple[NutrientComparison, ...]

    @property
    def has_warnings(self) -> bool:
        return any(not c.within_tolerance for c in self.comparisons)

    def warnings(self) -> list[NutrientComparison]:
        return [c for c in self.comparisons if not c.within_tolerance]


def actual_macros_for_selection(
    food: FoodCandidate, selection: ServingSelection
) -> Macros:
    """Compute the matched food's actual macros for the chosen serving/count."""
    factor = selection.servings * selection.serving.nutrition_multiplier
    n = food.nutrition
    return Macros(
        calories=_mul(n.calories, factor),
        protein_g=_mul(n.protein_g, factor),
        carbs_g=_mul(n.carbs_g, factor),
        fat_g=_mul(n.fat_g, factor),
    )


def compare_macros(
    planned: Macros, actual: Macros, config: NutritionConfig
) -> NutritionReport:
    """Compare planned vs actual macros; flag items exceeding tolerance."""
    comparisons = [
        _compare("calories", planned.calories, actual.calories, config.calorie_tolerance),
        _compare("protein_g", planned.protein_g, actual.protein_g, config.protein_tolerance),
        _compare("carbs_g", planned.carbs_g, actual.carbs_g, config.carb_tolerance),
        _compare("fat_g", planned.fat_g, actual.fat_g, config.fat_tolerance),
    ]
    return NutritionReport(tuple(comparisons))


def _compare(
    name: str, planned: float | None, actual: float | None, tol: float
) -> NutrientComparison:
    if planned is None:
        # Nothing to validate against — not a warning.
        return NutrientComparison(name, planned, actual, tol, within_tolerance=True,
                                  note="no planned value")
    if actual is None:
        return NutrientComparison(name, planned, actual, tol, within_tolerance=True,
                                  note="matched food has no value for this nutrient")
    if planned == 0:
        within = actual == 0
    else:
        within = abs(actual - planned) / planned <= tol
    return NutrientComparison(name, planned, actual, tol, within_tolerance=within)


def sum_macros(items: list[Macros]) -> Macros:
    def s(attr: str) -> float | None:
        vals = [getattr(m, attr) for m in items if getattr(m, attr) is not None]
        return round(sum(vals), 2) if vals else None

    return Macros(
        calories=s("calories"),
        protein_g=s("protein_g"),
        carbs_g=s("carbs_g"),
        fat_g=s("fat_g"),
    )


def _mul(value: float | None, factor: float) -> float | None:
    return None if value is None else round(value * factor, 4)
