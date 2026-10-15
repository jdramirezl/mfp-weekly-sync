"""Unit conversion honesty and planned-vs-matched nutrition validation."""

from __future__ import annotations

import pytest

from mfp_sync.backends.base import FoodCandidate, FoodNutrition, ServingSize
from mfp_sync.config import NutritionConfig
from mfp_sync.models import Macros, PlanEntry
from mfp_sync.nutrition import (
    ConversionError,
    actual_macros_for_selection,
    compare_macros,
    grams,
    select_serving,
    sum_macros,
)


def _food_grams_only():
    return FoodCandidate(
        food_id="f1", description="Chicken breast, cooked",
        serving_sizes=(ServingSize(unit="g", value=1.0, gram_weight=1.0, serving_id="g"),),
        nutrition=FoodNutrition(calories=1.65, protein_g=0.31, carbs_g=0.0, fat_g=0.036),
    )


def _food_with_named_serving():
    return FoodCandidate(
        food_id="f2", description="Banana, raw",
        serving_sizes=(
            ServingSize(unit="g", value=1.0, gram_weight=1.0, serving_id="g"),
            ServingSize(unit="medium", value=1.0, nutrition_multiplier=118.0,
                        gram_weight=118.0, serving_id="m"),
        ),
        nutrition=FoodNutrition(calories=0.89, protein_g=0.0109),
    )


def _entry(quantity, unit, **kw):
    return PlanEntry.model_validate(
        {"entry_id": "e", "food": "x", "quantity": quantity, "unit": unit, **kw}
    )


def test_grams_conversion_table():
    assert grams(1, "kg") == 1000.0
    assert grams(1, "oz") == pytest.approx(28.3495, rel=1e-4)
    assert grams(1, "lb") == pytest.approx(453.592, rel=1e-4)
    assert grams(1, "cup") is None  # volume is not mass


def test_exact_unit_match():
    sel = select_serving(_entry(125, "g"), _food_grams_only())
    assert sel.serving.unit == "g"
    assert sel.servings == 125


def test_named_serving_match():
    sel = select_serving(_entry(2, "medium"), _food_with_named_serving())
    assert sel.serving.unit == "medium"
    assert sel.servings == 2


def test_mass_request_converts_via_gram_serving():
    # Requesting oz against a gram-only food should convert through grams.
    sel = select_serving(_entry(1, "oz"), _food_grams_only())
    assert sel.serving.unit == "g"
    assert sel.servings == pytest.approx(28.3495, rel=1e-4)


def test_refuses_to_invent_volume_conversion():
    # A gram-only food has no cup serving and no way to know cup->grams.
    with pytest.raises(ConversionError):
        select_serving(_entry(1, "cup"), _food_grams_only())


def test_refuses_unknown_named_serving():
    with pytest.raises(ConversionError):
        select_serving(_entry(1, "scoop"), _food_grams_only())


def test_raw_vs_cooked_are_distinct_foods():
    # The resolver/catalog distinguishes these; here we assert the mock DB keeps
    # separate nutrition so a wrong pick would be caught by validation.
    from mfp_sync.backends.mock import _chicken_cooked, _chicken_raw

    cooked = _chicken_cooked().nutrition.calories
    raw = _chicken_raw().nutrition.calories
    assert cooked != raw


def test_explicit_serving_id_same_unit_divides_by_serving_value():
    # Regression (live bug): requesting 118 g against a serving defined as
    # "118 g" must yield servings=1, NOT servings=118. Previously the explicit
    # serving_id branch blindly used qty as the serving count -> 118x = wrong.
    food = FoodCandidate(
        food_id="banana", description="Banana (One)",
        serving_sizes=(
            ServingSize(unit="g", value=118.0, nutrition_multiplier=1.0,
                        gram_weight=118.0, serving_id="S118"),
        ),
        nutrition=FoodNutrition(calories=105.0, protein_g=1.0),
    )
    entry = _entry(118, "g", serving_id="S118")
    sel = select_serving(entry, food)
    assert sel.serving.serving_id == "S118"
    assert sel.servings == pytest.approx(1.0)
    actual = actual_macros_for_selection(food, sel)
    assert actual.calories == pytest.approx(105.0)  # not 12390


def test_explicit_serving_id_servings_unit_uses_count():
    food = FoodCandidate(
        food_id="x", description="X",
        serving_sizes=(ServingSize(unit="g", value=118.0, gram_weight=118.0,
                                   serving_id="S"),),
        nutrition=FoodNutrition(calories=105.0),
    )
    sel = select_serving(_entry(2, "servings", serving_id="S"), food)
    assert sel.servings == 2.0


def test_explicit_serving_id_incompatible_unit_refuses():
    food = FoodCandidate(
        food_id="x", description="X",
        serving_sizes=(ServingSize(unit="slice", value=1.0, serving_id="S"),),  # no gram weight
        nutrition=FoodNutrition(calories=50.0),
    )
    with pytest.raises(ConversionError):
        select_serving(_entry(100, "g", serving_id="S"), food)
    # Plan expects 206 kcal for 125 g; matched food yields 175 kcal.
    planned = Macros(calories=206, protein_g=38.8)
    food = FoodCandidate(
        food_id="f", description="Chicken",
        serving_sizes=(ServingSize(unit="g", value=1.0, gram_weight=1.0),),
        nutrition=FoodNutrition(calories=1.40, protein_g=0.31),  # 125*1.40 = 175
    )
    sel = select_serving(_entry(125, "g"), food)
    actual = actual_macros_for_selection(food, sel)
    assert actual.calories == pytest.approx(175.0)
    report = compare_macros(planned, actual, NutritionConfig(calorie_tolerance=0.15))
    cal = next(c for c in report.comparisons if c.name == "calories")
    assert not cal.within_tolerance  # 175 vs 206 is ~15% low -> flagged
    assert report.has_warnings


def test_nutrition_within_tolerance_no_warning():
    planned = Macros(calories=200)
    actual = Macros(calories=190)
    report = compare_macros(planned, actual, NutritionConfig(calorie_tolerance=0.15))
    assert not report.has_warnings


def test_missing_planned_value_is_not_a_warning():
    report = compare_macros(Macros(), Macros(calories=500),
                            NutritionConfig())
    assert not report.has_warnings


def test_sum_macros():
    total = sum_macros([Macros(calories=100, protein_g=5), Macros(calories=50, protein_g=2)])
    assert total.calories == 150
    assert total.protein_g == 7
