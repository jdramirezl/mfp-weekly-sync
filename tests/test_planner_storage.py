"""Planner (JSON/CSV loading) and storage persistence tests."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from mfp_sync.errors import InvalidPlanError, PlanNotFoundError
from mfp_sync.models import MealCategory
from mfp_sync.planner import load_plan, plan_json_schema
from mfp_sync.storage import CatalogItem, Storage


def test_load_json_plan(tmp_path: Path):
    p = tmp_path / "plan.json"
    p.write_text(
        '{"schema_version":1,"days":[{"date":"2026-10-19",'
        '"meals":{"breakfast":[{"entry_id":"a","food":"Oats","quantity":40,"unit":"g"}]}}]}'
    )
    plan = load_plan(p)
    assert plan.plan_id == "plan"  # from filename stem
    assert plan.days[0].date == dt.date(2026, 10, 19)


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(PlanNotFoundError):
        load_plan(tmp_path / "nope.json")


def test_load_bad_json_raises(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(InvalidPlanError):
        load_plan(p)


def test_load_csv_plan(tmp_path: Path):
    p = tmp_path / "plan.csv"
    p.write_text(
        "date,meal,food,quantity,unit,expected_calories\n"
        "2026-10-19,breakfast,Oatmeal dry,40,g,156\n"
        "2026-10-19,lunch,Chicken breast,125,g,\n"
        "2026-10-20,snack,Banana,1,medium,105\n"
    )
    plan = load_plan(p)
    assert len(plan.days) == 2
    day0 = next(d for d in plan.days if d.date == dt.date(2026, 10, 19))
    assert len(day0.meals[MealCategory.breakfast]) == 1
    assert day0.meals[MealCategory.breakfast][0].expected_calories == 156


def test_csv_missing_columns_raises(tmp_path: Path):
    p = tmp_path / "bad.csv"
    p.write_text("date,meal\n2026-10-19,breakfast\n")
    with pytest.raises(InvalidPlanError):
        load_plan(p)


def test_json_schema_is_well_formed():
    schema = plan_json_schema()
    assert schema["title"] == "MyFitnessPal Weekly Meal Plan"
    assert "$defs" in schema
    assert "properties" in schema


def test_storage_catalog_roundtrip(tmp_path: Path):
    s = Storage(tmp_path / "s.sqlite")
    item = CatalogItem(
        name="oatmeal, dry", food_id="mock-oatmeal", food_description="Oatmeal, dry",
        brand=None, serving_id="oat-g", serving_unit="g", serving_value=1.0,
        nutrition_multiplier=1.0, gram_weight=1.0, calories=3.89, protein_g=0.169,
        carbs_g=0.663, fat_g=0.069, verified_at="2026-10-15T00:00:00+00:00",
    )
    s.upsert_catalog(item)
    got = s.get_catalog("Oatmeal, Dry")  # case-insensitive lookup
    assert got is not None
    assert got.food_id == "mock-oatmeal"
    # upsert again updates in place (no duplicate rows)
    s.upsert_catalog(item)
    assert len(s.all_catalog()) == 1
    s.close()


def test_storage_sync_record_roundtrip(tmp_path: Path):
    s = Storage(tmp_path / "s.sqlite")
    rec = s.new_record(
        plan_id="p", source_entry_id="e", date=dt.date(2026, 10, 19), meal="breakfast",
        food_id="f", serving_id="sid", servings=40.0, serving_unit="g", serving_value=1.0,
    )
    s.upsert_sync_record(rec)
    got = s.get_sync_record("p", "e")
    assert got is not None
    assert got.servings == 40.0
    s.touch(got, status="synced", remote_entry_id="r123")
    s.upsert_sync_record(got)
    again = s.get_sync_record("p", "e")
    assert again.status == "synced"
    assert again.remote_entry_id == "r123"
    assert len(s.records_for_plan("p")) == 1
    s.close()
