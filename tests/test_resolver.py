"""Food resolution: catalog reuse, ambiguity, explicit ids, unconvertible units."""

from __future__ import annotations

from mfp_sync.models import PlanEntry
from mfp_sync.resolver import ResolutionStatus


def _entry(food, quantity=100, unit="g", **kw):
    return PlanEntry.model_validate(
        {"entry_id": "e", "food": food, "quantity": quantity, "unit": unit, **kw}
    )


def test_resolves_known_food_by_search(resolver):
    res = resolver.resolve(_entry("Oatmeal, dry", 40, "g"))
    assert res.status is ResolutionStatus.RESOLVED
    assert res.food.food_id == "mock-oatmeal"
    assert res.selection.servings == 40


def test_ambiguous_when_low_confidence(resolver):
    # "chicken breast" matches both cooked and raw with similar scores.
    res = resolver.resolve(_entry("chicken breast", 100, "g"))
    assert res.status is ResolutionStatus.AMBIGUOUS
    assert len(res.candidates) >= 2


def test_not_found(resolver):
    res = resolver.resolve(_entry("unicorn steak", 100, "g"))
    assert res.status is ResolutionStatus.NOT_FOUND


def test_explicit_food_id_bypasses_search(resolver):
    res = resolver.resolve(_entry("whatever", 100, "g", mfp_food_id="mock-banana"))
    assert res.status is ResolutionStatus.RESOLVED
    assert res.food.food_id == "mock-banana"
    assert res.source == "plan"


def test_catalog_reuse_after_save(resolver, storage):
    # Ambiguous first.
    res = resolver.resolve(_entry("chicken breast", 100, "g"))
    assert res.status is ResolutionStatus.AMBIGUOUS
    # Resolve it manually: pick the cooked candidate and save.
    cooked = next(c for c in res.candidates if c.food_id == "mock-chicken-cooked")
    resolver.save_to_catalog("chicken breast", cooked, cooked.serving_sizes[0])
    # Now it resolves from the catalog, unambiguously.
    res2 = resolver.resolve(_entry("chicken breast", 100, "g"))
    assert res2.status is ResolutionStatus.RESOLVED
    assert res2.source == "catalog"
    assert res2.food.food_id == "mock-chicken-cooked"


def test_unconvertible_unit_reported(resolver):
    # Oatmeal only has a gram serving; requesting cups cannot be converted.
    res = resolver.resolve(_entry("Oatmeal, dry", 1, "cup"))
    assert res.status is ResolutionStatus.UNCONVERTIBLE
    assert "Refusing to invent" in res.message


def test_never_silently_picks_arbitrary_result(resolver):
    # A vague query with multiple plausible matches must not auto-resolve.
    res = resolver.resolve(_entry("chicken", 100, "g"))
    assert res.status is ResolutionStatus.AMBIGUOUS
