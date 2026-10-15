"""End-to-end CLI tests via Typer's CliRunner, all on the mock backend."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mfp_sync.cli import app

runner = CliRunner()

PLAN = {
    "schema_version": 1,
    "plan_id": "cli-test",
    "days": [
        {
            "date": "2026-10-19",
            "meals": {
                "breakfast": [
                    {"entry_id": "b1", "food": "Oatmeal, dry", "quantity": 40, "unit": "g",
                     "expected_calories": 156}
                ],
                "lunch": [
                    {"entry_id": "l1", "food": "Chicken breast, cooked", "quantity": 125,
                     "unit": "g", "mfp_food_id": "mock-chicken-cooked"}
                ],
            },
        }
    ],
}


def _write_plan(tmp_path: Path, data=PLAN) -> Path:
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(data))
    return p


def _base(tmp_path: Path):
    return ["--backend", "mock", "--home", str(tmp_path / "state"), "--json"]


def test_version():
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0
    assert "0.1.0" in r.stdout


def test_schema_outputs_json():
    r = runner.invoke(app, ["schema"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["title"] == "MyFitnessPal Weekly Meal Plan"


def test_doctor_mock_ok(tmp_path):
    r = runner.invoke(app, _base(tmp_path) + ["doctor"])
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["ok"] is True
    assert data["backend"] == "mock"


def test_preview_json(tmp_path):
    plan = _write_plan(tmp_path)
    r = runner.invoke(app, _base(tmp_path) + ["preview", str(plan)])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    assert data["counts"].get("add") == 2
    assert all(a["action"] == "add" for a in data["actions"])


def test_apply_then_idempotent_rerun(tmp_path):
    plan = _write_plan(tmp_path)
    # apply with --yes to skip the prompt (and --json for machine output)
    r1 = runner.invoke(app, _base(tmp_path) + ["apply", str(plan), "--yes"])
    assert r1.exit_code == 0, r1.stdout
    d1 = json.loads(r1.stdout)
    assert len(d1["result"]["added"]) == 2

    # second apply: nothing to do
    r2 = runner.invoke(app, _base(tmp_path) + ["apply", str(plan), "--yes"])
    assert r2.exit_code == 0, r2.stdout
    d2 = json.loads(r2.stdout)
    # Either reported as "nothing to apply" or zero adds.
    added = d2.get("result", {}).get("added", [])
    assert added == []


def test_invalid_plan_exit_code(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 1, "days": [
        {"date": "not-a-date", "meals": {}}]}))
    r = runner.invoke(app, _base(tmp_path) + ["preview", str(bad)])
    assert r.exit_code == 2  # INVALID_PLAN
    data = json.loads(r.stdout)
    assert data["error_code"] == "INVALID_PLAN"


def test_plan_not_found_exit_code(tmp_path):
    r = runner.invoke(app, _base(tmp_path) + ["preview", str(tmp_path / "nope.json")])
    assert r.exit_code == 3  # PLAN_NOT_FOUND
    data = json.loads(r.stdout)
    assert data["error_code"] == "PLAN_NOT_FOUND"


def test_status_reports_history(tmp_path):
    plan = _write_plan(tmp_path)
    runner.invoke(app, _base(tmp_path) + ["apply", str(plan), "--yes"])
    r = runner.invoke(app, _base(tmp_path) + ["status", str(plan)])
    assert r.exit_code == 0, r.stdout
    data = json.loads(r.stdout)
    statuses = {e["history_status"] for e in data["entries"]}
    assert "synced" in statuses


def test_resolve_non_interactive_reports_ambiguous(tmp_path):
    data = {
        "schema_version": 1, "plan_id": "amb",
        "days": [{"date": "2026-10-19", "meals": {"lunch": [
            {"entry_id": "x", "food": "chicken breast", "quantity": 100, "unit": "g"}]}}],
    }
    plan = _write_plan(tmp_path, data)
    r = runner.invoke(app, _base(tmp_path) + ["--non-interactive", "resolve", str(plan)])
    out = json.loads(r.stdout)
    assert out["ok"] is False
    assert out["error_code"] == "UNRESOLVED_FOODS"
    assert out["unresolved"][0]["food"] == "chicken breast"


def test_export_json(tmp_path):
    plan = _write_plan(tmp_path)
    runner.invoke(app, _base(tmp_path) + ["apply", str(plan), "--yes"])
    r = runner.invoke(
        app, ["--backend", "mock", "--home", str(tmp_path / "state"),
              "export", "--from", "2026-10-19", "--to", "2026-10-19", "--format", "json"]
    )
    assert r.exit_code == 0, r.stdout
    assert "2026-10-19" in r.stdout


def test_apply_blocks_on_unresolved(tmp_path):
    data = {
        "schema_version": 1, "plan_id": "amb2",
        "days": [{"date": "2026-10-19", "meals": {"lunch": [
            {"entry_id": "x", "food": "chicken breast", "quantity": 100, "unit": "g"}]}}],
    }
    plan = _write_plan(tmp_path, data)
    r = runner.invoke(app, _base(tmp_path) + ["apply", str(plan), "--yes"])
    assert r.exit_code == 4  # UNRESOLVED_FOODS
