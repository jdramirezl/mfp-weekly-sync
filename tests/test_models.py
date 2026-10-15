"""Model validation: dates, meal categories, quantities, duplicate IDs."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from mfp_sync.models import DayPlan, MealCategory, MealPlan, PlanEntry


def _entry(entry_id="e1", food="Banana, raw", quantity=1.0, unit="medium", **kw):
    return {"entry_id": entry_id, "food": food, "quantity": quantity, "unit": unit, **kw}


def test_valid_minimal_plan():
    plan = MealPlan.model_validate({
        "schema_version": 1,
        "days": [{"date": "2026-10-19", "meals": {"breakfast": [_entry()]}}],
    })
    assert plan.days[0].date == dt.date(2026, 10, 19)
    assert plan.days[0].meals[MealCategory.breakfast][0].food == "Banana, raw"


def test_rejects_bad_date():
    with pytest.raises(ValidationError) as ei:
        DayPlan.model_validate({"date": "2026-13-40", "meals": {}})
    assert "invalid date" in str(ei.value) or "date" in str(ei.value)


def test_date_is_local_not_utc_shifted():
    # A plain date string must map to that exact calendar day — no tz math.
    day = DayPlan.model_validate({"date": "2026-10-19", "meals": {}})
    assert day.date == dt.date(2026, 10, 19)
    assert not isinstance(day.date, dt.datetime)


def test_rejects_zero_and_negative_quantity():
    for bad in (0, -5):
        with pytest.raises(ValidationError):
            PlanEntry.model_validate(_entry(quantity=bad))


def test_rejects_blank_unit():
    with pytest.raises(ValidationError):
        PlanEntry.model_validate(_entry(unit="   "))


def test_accepts_named_serving_unit():
    e = PlanEntry.model_validate(_entry(unit="Medium"))
    assert e.unit == "medium"  # normalized to lowercase


def test_rejects_unknown_meal_category():
    with pytest.raises(ValidationError) as ei:
        DayPlan.model_validate({"date": "2026-10-19", "meals": {"brunch": [_entry()]}})
    assert "unknown meal category" in str(ei.value)


def test_snacks_alias_maps_to_snack():
    day = DayPlan.model_validate({"date": "2026-10-19", "meals": {"Snacks": [_entry()]}})
    assert MealCategory.snack in day.meals


def test_rejects_duplicate_entry_ids():
    with pytest.raises(ValidationError) as ei:
        MealPlan.model_validate({
            "schema_version": 1,
            "days": [
                {"date": "2026-10-19", "meals": {"breakfast": [_entry(entry_id="dup")]}},
                {"date": "2026-10-20", "meals": {"lunch": [_entry(entry_id="dup")]}},
            ],
        })
    assert "duplicate entry_id" in str(ei.value)


def test_rejects_duplicate_dates():
    with pytest.raises(ValidationError) as ei:
        MealPlan.model_validate({
            "schema_version": 1,
            "days": [
                {"date": "2026-10-19", "meals": {"breakfast": [_entry(entry_id="a")]}},
                {"date": "2026-10-19", "meals": {"lunch": [_entry(entry_id="b")]}},
            ],
        })
    assert "duplicate date" in str(ei.value)


def test_rejects_unknown_schema_version():
    with pytest.raises(ValidationError):
        MealPlan.model_validate({"schema_version": 99, "days": [
            {"date": "2026-10-19", "meals": {"breakfast": [_entry()]}}]})


def test_rejects_extra_fields():
    with pytest.raises(ValidationError):
        PlanEntry.model_validate(_entry(surprise="nope"))


def test_planned_macros_merges_expected_calories():
    e = PlanEntry.model_validate(_entry(expected_calories=100,
                                        expected_macros={"protein_g": 5}))
    m = e.planned_macros()
    assert m.calories == 100
    assert m.protein_g == 5


def test_mfp_meal_name_mapping():
    assert MealCategory.snack.mfp_meal_name == "Snacks"
    assert MealCategory.breakfast.mfp_meal_name == "Breakfast"
