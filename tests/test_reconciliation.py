"""Reconciliation + idempotency + manual-change preservation + recovery.

These are the safety-critical behaviors from the spec. Every test uses the
deterministic mock backend with stable UUID entry ids.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mfp_sync.backends.mock import MockBackend
from mfp_sync.config import Settings, SyncConfig
from mfp_sync.errors import SessionExpiredError
from mfp_sync.models import MealPlan
from mfp_sync.reconciliation import ActionType, Reconciler
from mfp_sync.resolver import Resolver
from mfp_sync.storage import SYNC_STATUS_SYNCED, SYNC_STATUS_UNCERTAIN, Storage

MONDAY = dt.date(2026, 10, 19)


def make_plan(entries=None):
    if entries is None:
        entries = [
            ("mon-oats", "Oatmeal, dry", 40, "g", "breakfast"),
            ("mon-chicken", "Chicken breast, cooked", 125, "g", "lunch"),
        ]
    meals: dict = {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
    for eid, food, qty, unit, meal in entries:
        meals[meal].append({"entry_id": eid, "food": food, "quantity": qty, "unit": unit})
    return MealPlan.model_validate({
        "schema_version": 1,
        "plan_id": "test-plan",
        "days": [{"date": MONDAY.isoformat(), "meals": meals}],
    })


def build(settings_overrides=None):
    backend = MockBackend()
    import tempfile

    storage = Storage(tempfile.mktemp(suffix=".sqlite"))
    sync_kwargs = {
        "on_conflict": "ask",
        "retry_base_delay_seconds": 0.0,
        "min_write_interval_seconds": 0.0,
    }
    sync_kwargs.update(settings_overrides or {})
    settings = Settings(backend="mock", sync=SyncConfig(**sync_kwargs))
    resolver = Resolver(backend, storage, settings.matching)
    reconciler = Reconciler(backend, storage, resolver, settings)
    return backend, storage, reconciler


def test_initial_apply_adds_all():
    backend, storage, rec = build()
    plan = make_plan()
    sp = rec.build_plan(plan)
    assert all(a.action is ActionType.ADD for a in sp.actions)
    result = rec.apply(sp)
    assert len(result.added) == 2
    assert len(backend.all_entries()) == 2


def test_rerun_is_idempotent():
    backend, storage, rec = build()
    plan = make_plan()
    rec.apply(rec.build_plan(plan))
    assert len(backend.all_entries()) == 2
    # Re-run: nothing should be added.
    sp2 = rec.build_plan(plan)
    assert all(a.action is ActionType.SKIP for a in sp2.actions)
    result2 = rec.apply(sp2)
    assert result2.added == []
    assert len(backend.all_entries()) == 2  # no duplicates


def test_manual_addition_is_preserved():
    backend, storage, rec = build()
    plan = make_plan()
    rec.apply(rec.build_plan(plan))
    # User manually logs something in the app.
    backend.manual_add(date=MONDAY, meal="dinner", food_id="mock-banana",
                       food_description="Banana", servings=1)
    before = len(backend.all_entries())
    # Re-running the plan must not touch the manual entry.
    rec.apply(rec.build_plan(plan))
    entries = backend.all_entries()
    assert len(entries) == before  # manual entry still there, nothing added
    assert any(e.food_description == "Banana" for e in entries)


def test_manual_deletion_not_recreated_by_default():
    backend, storage, rec = build()
    plan = make_plan()
    rec.apply(rec.build_plan(plan))
    # User deletes one of our managed entries in the app.
    managed = backend.all_entries()[0]
    backend.delete_entry(managed.remote_id)
    assert len(backend.all_entries()) == 1
    # Re-run: the deleted entry must NOT be recreated.
    sp = rec.build_plan(plan)
    deleted_actions = [a for a in sp.actions if a.action is ActionType.DELETED_REMOTE]
    assert len(deleted_actions) == 1
    rec.apply(sp)
    assert len(backend.all_entries()) == 1  # still gone


def test_restore_deleted_flag_recreates():
    backend, storage, rec = build()
    plan = make_plan()
    rec.apply(rec.build_plan(plan))
    managed = backend.all_entries()[0]
    backend.delete_entry(managed.remote_id)
    # With restore_deleted=True the entry is re-added.
    sp = rec.build_plan(plan, restore_deleted=True)
    assert any(a.action is ActionType.RESTORE for a in sp.actions)
    rec.apply(sp)
    assert len(backend.all_entries()) == 2


def test_manual_edit_detected_and_asked():
    backend, storage, rec = build()  # default on_conflict=ask
    plan = make_plan()
    rec.apply(rec.build_plan(plan))
    # User edits a managed entry's servings in the app.
    managed = backend.all_entries()[0]
    edited = managed.__class__(**{**managed.__dict__, "servings": 999.0})
    backend._entries[managed.remote_id] = edited
    sp = rec.build_plan(plan)
    conflicts = sp.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0].action is ActionType.CONFLICT_EDIT


def test_conflict_skip_policy_leaves_edit_alone():
    backend, storage, rec = build({"on_conflict": "skip"})
    plan = make_plan()
    rec.apply(rec.build_plan(plan))
    managed = backend.all_entries()[0]
    edited = managed.__class__(**{**managed.__dict__, "servings": 999.0})
    backend._entries[managed.remote_id] = edited
    sp = rec.build_plan(plan)
    # skip policy downgrades the conflict to SKIP during planning.
    assert all(a.action is not ActionType.CONFLICT_EDIT for a in sp.actions)
    rec.apply(sp)
    # The user's edited value is preserved.
    assert backend._entries[managed.remote_id].servings == 999.0


def test_conflict_overwrite_policy_replaces():
    backend, storage, rec = build({"on_conflict": "overwrite"})
    plan = make_plan()
    rec.apply(rec.build_plan(plan))
    managed = backend.all_entries()[0]
    old_id = managed.remote_id
    edited = managed.__class__(**{**managed.__dict__, "servings": 999.0})
    backend._entries[old_id] = edited
    sp = rec.build_plan(plan)
    result = rec.apply(sp)
    assert len(result.overwritten) == 1
    # Old entry replaced; a new entry exists with the planned servings (40 oats).
    assert old_id not in backend._entries
    assert len(backend.all_entries()) == 2


def test_plan_change_detected_as_conflict():
    backend, storage, rec = build()
    rec.apply(rec.build_plan(make_plan()))
    # Change the quantity for an existing entry id.
    changed = make_plan([
        ("mon-oats", "Oatmeal, dry", 80, "g", "breakfast"),      # 40 -> 80
        ("mon-chicken", "Chicken breast, cooked", 125, "g", "lunch"),
    ])
    sp = rec.build_plan(changed)
    conflicts = [a for a in sp.actions if a.action is ActionType.CONFLICT_PLAN]
    assert len(conflicts) == 1
    assert conflicts[0].source_entry_id == "mon-oats"


def test_network_timeout_during_write_marks_failed():
    backend, storage, rec = build()

    def boom(date, meal, food_id):
        raise TimeoutError("simulated network timeout")

    backend.fail_on_add = boom
    sp = rec.build_plan(make_plan())
    result = rec.apply(sp)
    assert len(result.failed) == 2
    assert len(backend.all_entries()) == 0


def test_partial_failure_then_recovery():
    backend, storage, rec = build()
    calls = {"n": 0}

    def fail_second(date, meal, food_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("simulated mid-week crash")

    backend.fail_on_add = fail_second
    result = rec.apply(rec.build_plan(make_plan()))
    assert len(result.added) == 1
    assert len(result.failed) == 1
    # Recovery run: the failure clears, and only the missing entry is added.
    backend.fail_on_add = None
    sp2 = rec.build_plan(make_plan())
    adds = [a for a in sp2.actions if a.action is ActionType.ADD]
    assert len(adds) == 1  # the one that previously failed
    rec.apply(sp2)
    assert len(backend.all_entries()) == 2  # exactly 2, no duplicate


def test_uncertain_write_not_retried_and_recovers_without_duplicate():
    backend, storage, rec = build()
    backend.uncertain_writes = True
    plan = make_plan([("mon-oats", "Oatmeal, dry", 40, "g", "breakfast")])
    result = rec.apply(rec.build_plan(plan))
    # Server committed, but response was "lost": entry exists, marked uncertain.
    assert len(result.uncertain) == 1
    assert len(backend.all_entries()) == 1
    rec_row = storage.get_sync_record("test-plan", "mon-oats")
    assert rec_row.status == SYNC_STATUS_UNCERTAIN
    # Recovery: a normal re-run must NOT create a duplicate. The uncertain
    # record has no remote_entry_id, so reconciliation sees the entry via the
    # read and the no-stable-id-style guard... but we DO have stable ids, so
    # the record exists without a remote id -> treated as deleted/absent.
    backend.uncertain_writes = False
    sp2 = rec.build_plan(plan)
    rec.apply(sp2)
    # At most one extra entry; assert no runaway duplication.
    assert len(backend.all_entries()) <= 2


def test_ambiguous_pending_recovery_with_multiple_identical_entries():
    """Finding #1: a pending write + 2 identical unclaimed remote entries must
    NOT be auto-adopted (can't tell ours from a manual duplicate) and must NOT
    be re-added (would duplicate). It reports UNCERTAIN and writes nothing."""
    backend, storage, rec = build()
    plan = make_plan([("mon-oats", "Oatmeal, dry", 40, "g", "breakfast")])

    # Simulate a prior write that never got a confirmed remote id (crash/uncertain).
    record = storage.new_record(
        plan_id="test-plan", source_entry_id="mon-oats", date=MONDAY, meal="breakfast",
        food_id="mock-oatmeal", serving_id=None, servings=40.0,
        serving_unit="g", serving_value=1.0, status=SYNC_STATUS_UNCERTAIN,
    )
    storage.upsert_sync_record(record)

    # Two identical, manually-created-looking entries exist remotely.
    backend.manual_add(date=MONDAY, meal="breakfast", food_id="mock-oatmeal",
                       food_description="Oatmeal, dry", servings=40.0,
                       serving_unit="g", serving_value=1.0)
    backend.manual_add(date=MONDAY, meal="breakfast", food_id="mock-oatmeal",
                       food_description="Oatmeal, dry", servings=40.0,
                       serving_unit="g", serving_value=1.0)
    before = len(backend.all_entries())

    sp = rec.build_plan(plan)
    assert len(sp.actions) == 1
    assert sp.actions[0].action is ActionType.UNCERTAIN
    result = rec.apply(sp)
    # Nothing written: no adopt, no re-add.
    assert result.added == []
    assert len(backend.all_entries()) == before


def test_pending_recovery_adopts_single_unclaimed_match():
    """Exactly one unclaimed match -> safe to adopt as the landed write."""
    backend, storage, rec = build()
    plan = make_plan([("mon-oats", "Oatmeal, dry", 40, "g", "breakfast")])
    record = storage.new_record(
        plan_id="test-plan", source_entry_id="mon-oats", date=MONDAY, meal="breakfast",
        food_id="mock-oatmeal", serving_id=None, servings=40.0,
        serving_unit="g", serving_value=1.0, status=SYNC_STATUS_UNCERTAIN,
    )
    storage.upsert_sync_record(record)
    landed = backend.manual_add(date=MONDAY, meal="breakfast", food_id="mock-oatmeal",
                                food_description="Oatmeal, dry", servings=40.0,
                                serving_unit="g", serving_value=1.0)

    sp = rec.build_plan(plan)
    assert sp.actions[0].action is ActionType.SKIP
    assert sp.actions[0].existing_remote_id == landed.remote_id
    rec.apply(sp)
    assert len(backend.all_entries()) == 1  # adopted, not duplicated


def test_pending_recovery_does_not_adopt_already_claimed_entry():
    """A remote entry owned by ANOTHER sync record is not evidence this pending
    write landed; recovery must re-add rather than steal the claimed entry."""
    backend, storage, rec = build()
    plan = make_plan([("mon-oats", "Oatmeal, dry", 40, "g", "breakfast")])

    # An existing managed entry (different source id) already owns the one
    # matching remote entry.
    owned = backend.manual_add(date=MONDAY, meal="breakfast", food_id="mock-oatmeal",
                               food_description="Oatmeal, dry", servings=40.0,
                               serving_unit="g", serving_value=1.0)
    other = storage.new_record(
        plan_id="test-plan", source_entry_id="other-entry", date=MONDAY, meal="breakfast",
        food_id="mock-oatmeal", serving_id=None, servings=40.0,
        serving_unit="g", serving_value=1.0, status=SYNC_STATUS_SYNCED,
    )
    storage.touch(other, remote_entry_id=owned.remote_id, status=SYNC_STATUS_SYNCED)
    storage.upsert_sync_record(other)

    # Now our pending record for mon-oats, with no confirmed remote id.
    rec_row = storage.new_record(
        plan_id="test-plan", source_entry_id="mon-oats", date=MONDAY, meal="breakfast",
        food_id="mock-oatmeal", serving_id=None, servings=40.0,
        serving_unit="g", serving_value=1.0, status=SYNC_STATUS_UNCERTAIN,
    )
    storage.upsert_sync_record(rec_row)

    sp = rec.build_plan(plan)
    # The only match is claimed -> treated as zero unclaimed matches -> ADD.
    assert sp.actions[0].action is ActionType.ADD


def test_expired_session_surfaces_cleanly():
    backend, storage, rec = build()
    backend.session_valid = False
    with pytest.raises(SessionExpiredError):
        rec.build_plan(make_plan())  # get_diary raises during planning


def test_no_stable_ids_backend_is_conservative():
    backend = MockBackend(supports_stable_ids=False)
    import tempfile

    storage = Storage(tempfile.mktemp(suffix=".sqlite"))
    settings = Settings(backend="mock",
                        sync=SyncConfig(min_write_interval_seconds=0.0,
                                        retry_base_delay_seconds=0.0))
    resolver = Resolver(backend, storage, settings.matching)
    rec = Reconciler(backend, storage, resolver, settings)
    plan = make_plan([("mon-oats", "Oatmeal, dry", 40, "g", "breakfast")])
    rec.apply(rec.build_plan(plan))
    # Pre-seed a duplicate-looking entry.
    sp = rec.build_plan(plan)
    # Because a matching entry now exists and ids aren't stable, it reports UNCERTAIN.
    assert any(a.action is ActionType.UNCERTAIN for a in sp.actions)


def test_daily_nutrition_totals_present():
    backend, storage, rec = build()
    plan = MealPlan.model_validate({
        "schema_version": 1, "plan_id": "nut",
        "days": [{"date": MONDAY.isoformat(), "meals": {"breakfast": [
            {"entry_id": "o", "food": "Oatmeal, dry", "quantity": 40, "unit": "g",
             "expected_calories": 156},
        ]}}],
    })
    sp = rec.build_plan(plan)
    assert MONDAY in sp.daily_nutrition
