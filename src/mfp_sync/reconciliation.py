"""The reconciliation engine: decide what to add/skip/flag, idempotently.

This is the safety-critical core. For each resolved plan entry it performs a
three-way comparison between:

  (L) the Local sync record  — what we previously logged for this plan entry
  (R) the current Remote state — what is in the diary now
  (P) the requested Plan state — what the plan says now

and chooses an action per the rules in the spec:

  * No L record, nothing matching in R   -> ADD (confirmed missing)
  * L record whose remote entry still in R, unchanged -> SKIP (idempotent)
  * L record, remote entry GONE from R    -> user DELETED it manually
        -> default: LEAVE DELETED (don't recreate); only ADD if --restore-deleted
  * L record present in R but EDITED (fingerprint differs) -> MANUAL EDIT
        -> on_conflict skip: SKIP; ask: ASK; overwrite: OVERWRITE
  * Plan entry changed vs L record (different food/qty) -> PLAN CHANGED
        -> treated like a conflict against the managed remote entry
  * Entries in R we never managed -> MANUAL ADDITION -> always PRESERVE

If the backend cannot provide stable IDs (supports_stable_ids == False), we
degrade to a conservative mode: we never delete, and we only add when there is
no plausible duplicate already present, flagging uncertainty instead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum

from .backends.base import DiaryBackend, DiaryEntry
from .config import Settings
from .models import MealPlan
from .nutrition import NutritionReport, actual_macros_for_selection, compare_macros
from .resolver import Resolution, Resolver
from .storage import (
    SYNC_STATUS_DELETED_REMOTE,
    SYNC_STATUS_FAILED,
    SYNC_STATUS_PENDING,
    SYNC_STATUS_UNCERTAIN,
    Storage,
    SyncRecord,
)


class ActionType(str, Enum):
    ADD = "add"
    SKIP = "skip"
    RESTORE = "restore"
    CONFLICT_EDIT = "conflict_edit"       # managed entry edited manually
    CONFLICT_PLAN = "conflict_plan"        # plan changed vs what we synced
    DELETED_REMOTE = "deleted_remote"      # user deleted; leave as-is by default
    UNRESOLVED = "unresolved"              # food not resolvable
    UNCERTAIN = "uncertain"                # no stable IDs; can't guarantee


@dataclass
class PlannedAction:
    date: dt.date
    meal: str
    source_entry_id: str
    food_name: str
    action: ActionType
    resolution: Resolution | None = None
    existing_remote_id: str | None = None
    nutrition: NutritionReport | None = None
    detail: str = ""

    @property
    def mutates(self) -> bool:
        return self.action in (ActionType.ADD, ActionType.RESTORE,
                               ActionType.CONFLICT_PLAN, ActionType.CONFLICT_EDIT)


@dataclass
class SyncPlan:
    plan_id: str
    actions: list[PlannedAction] = field(default_factory=list)
    daily_nutrition: dict[dt.date, NutritionReport] = field(default_factory=dict)

    def to_add(self) -> list[PlannedAction]:
        return [a for a in self.actions if a.action is ActionType.ADD]

    def conflicts(self) -> list[PlannedAction]:
        return [a for a in self.actions
                if a.action in (ActionType.CONFLICT_EDIT, ActionType.CONFLICT_PLAN)]

    def unresolved(self) -> list[PlannedAction]:
        return [a for a in self.actions
                if a.action in (ActionType.UNRESOLVED, ActionType.UNCERTAIN)]

    def summary_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.actions:
            counts[a.action.value] = counts.get(a.action.value, 0) + 1
        return counts


class Reconciler:
    def __init__(
        self,
        backend: DiaryBackend,
        storage: Storage,
        resolver: Resolver,
        settings: Settings,
    ):
        self._backend = backend
        self._storage = storage
        self._resolver = resolver
        self._settings = settings

    # -- planning (no mutations) ---------------------------------------------

    def build_plan(self, plan: MealPlan, *, restore_deleted: bool = False) -> SyncPlan:
        plan_id = plan.effective_plan_id()
        sync_plan = SyncPlan(plan_id=plan_id)

        # Pull remote state once per distinct date and index entries by remote id.
        dates = sorted({d for d, _c, _e in plan.all_entries()})
        remote_by_date: dict[dt.date, dict[str, DiaryEntry]] = {}
        for date in dates:
            entries = self._backend.get_diary(date)
            remote_by_date[date] = {e.remote_id: e for e in entries}

        # Remote entries already owned by some sync record. Recovery must not
        # adopt one of these as evidence that a different pending write landed.
        claimed = self._storage.claimed_remote_ids()

        for date, cat, entry in plan.all_entries():
            resolution = self._resolver.resolve(entry)
            action = self._decide(
                plan_id=plan_id,
                date=date,
                meal=cat.value,
                resolution=resolution,
                remote=remote_by_date.get(date, {}),
                restore_deleted=restore_deleted,
                claimed_remote_ids=claimed,
            )
            sync_plan.actions.append(action)

        self._attach_daily_nutrition(plan, sync_plan)
        return sync_plan

    def _decide(
        self,
        *,
        plan_id: str,
        date: dt.date,
        meal: str,
        resolution: Resolution,
        remote: dict[str, DiaryEntry],
        restore_deleted: bool,
        claimed_remote_ids: set[str] | None = None,
    ) -> PlannedAction:
        entry = resolution.entry
        base = dict(
            date=date, meal=meal, source_entry_id=entry.entry_id,
            food_name=entry.food, resolution=resolution,
        )
        claimed_remote_ids = claimed_remote_ids or set()

        if not resolution.ok:
            return PlannedAction(
                action=ActionType.UNRESOLVED, detail=resolution.message, **base
            )

        # Degraded mode: backend without stable IDs.
        if not self._backend.supports_stable_ids:
            return self._decide_no_stable_ids(base, resolution, remote)

        record = self._storage.get_sync_record(plan_id, entry.entry_id)
        nutrition = self._nutrition_for(resolution)

        # No prior record: this entry has never been synced for this plan.
        if record is None:
            return PlannedAction(action=ActionType.ADD, nutrition=nutrition, **base)

        # A record that never got a confirmed remote id (write failed, was
        # pending at a crash, or came back uncertain) is NOT a manual deletion.
        # Recovery must decide between re-adding and adopting an existing remote
        # entry — WITHOUT ever claiming a manually-created entry.
        #
        # Safe ownership evidence requires exactly ONE unclaimed remote entry
        # matching this plan item's fingerprint:
        #   * 0 matches            -> nothing landed; re-add.
        #   * 1 unclaimed match    -> adopt it (the pending write did land).
        #   * >1 unclaimed matches -> ambiguous; we cannot tell ours from a
        #                             manual duplicate. Report UNCERTAIN and do
        #                             NOT write (re-adding risks a duplicate,
        #                             adopting risks claiming a manual entry).
        if not record.remote_entry_id:
            if record.status in (SYNC_STATUS_FAILED, SYNC_STATUS_PENDING, SYNC_STATUS_UNCERTAIN):
                matches = self._matching_remote(
                    resolution, remote, base["meal"], claimed_remote_ids
                )
                if len(matches) == 1:
                    return PlannedAction(
                        action=ActionType.SKIP, nutrition=nutrition,
                        existing_remote_id=matches[0].remote_id,
                        detail=f"prior write was {record.status}; exactly one unclaimed "
                               "matching entry found remotely, adopting it as synced",
                        **base,
                    )
                if len(matches) > 1:
                    return PlannedAction(
                        action=ActionType.UNCERTAIN,
                        detail=(
                            f"prior write was {record.status} and {len(matches)} "
                            "identical unclaimed entries exist remotely — cannot tell "
                            "which (if any) is ours without a stable id. Not writing, "
                            "to avoid duplicating or claiming a manual entry. Resolve "
                            "by hand in MyFitnessPal, then re-run."
                        ),
                        **base,
                    )
                return PlannedAction(
                    action=ActionType.ADD, nutrition=nutrition,
                    detail=f"retrying prior {record.status} write "
                           "(no matching remote entry found)", **base,
                )

        # We have a record with a remote id. Where is its remote entry now?
        if record.remote_entry_id and record.remote_entry_id in remote:
            current = remote[record.remote_entry_id]
            # Did the user edit our managed entry in the app?
            if record.fingerprint and current.fingerprint() != record.fingerprint:
                return self._conflict(
                    base, nutrition, record.remote_entry_id,
                    ActionType.CONFLICT_EDIT,
                    f"managed entry was edited manually in MyFitnessPal "
                    f"(expected {record.fingerprint}, found {current.fingerprint()})",
                )
            # Did the plan itself change vs what we synced?
            if self._plan_changed(record, resolution):
                return self._conflict(
                    base, nutrition, record.remote_entry_id,
                    ActionType.CONFLICT_PLAN,
                    "plan entry changed since last sync",
                )
            # Unchanged, present, managed -> idempotent skip.
            return PlannedAction(
                action=ActionType.SKIP, nutrition=nutrition,
                existing_remote_id=record.remote_entry_id,
                detail="already synced and unchanged", **base,
            )

        # Record had a confirmed remote id but it's gone now -> user deleted it.
        if record.status != SYNC_STATUS_DELETED_REMOTE:
            if restore_deleted:
                return PlannedAction(
                    action=ActionType.RESTORE, nutrition=nutrition,
                    detail="previously deleted in MyFitnessPal; restoring on request", **base,
                )
            return PlannedAction(
                action=ActionType.DELETED_REMOTE,
                detail="deleted in MyFitnessPal; left as-is (use --restore-deleted to re-add)",
                **base,
            )
        # Already recorded as remotely deleted: honor the deletion.
        return PlannedAction(
            action=ActionType.DELETED_REMOTE,
            detail="previously deleted in MyFitnessPal; honoring deletion", **base,
        )

    def _matching_remote(
        self,
        resolution: Resolution,
        remote: dict[str, DiaryEntry],
        meal: str,
        claimed_remote_ids: set[str],
    ) -> list[DiaryEntry]:
        """All remote entries that plausibly match this resolved plan item AND
        are not already owned by another sync record.

        Match = same meal, food id, servings, and serving unit. Claimed entries
        (tracked by some sync record) are excluded: such an entry belongs to a
        different managed item and is never evidence that THIS write landed.
        Returns every match so the caller can require an unambiguous single one.
        """
        food = resolution.food
        sel = resolution.selection
        if food is None or sel is None:
            return []
        out: list[DiaryEntry] = []
        for e in remote.values():
            if e.remote_id in claimed_remote_ids:
                continue
            if (
                e.meal == meal
                and e.food_id == food.food_id
                and abs(e.servings - sel.servings) <= 1e-6
                and e.serving_unit == sel.serving.unit
            ):
                out.append(e)
        return out

    def _decide_no_stable_ids(
        self, base: dict, resolution: Resolution, remote: dict[str, DiaryEntry]
    ) -> PlannedAction:
        """Conservative strategy when the backend has no stable entry IDs."""
        # Look for a plausible duplicate already present.
        for e in remote.values():
            if (
                e.meal == base["meal"]
                and resolution.food is not None
                and e.food_id == resolution.food.food_id
            ):
                return PlannedAction(
                    action=ActionType.UNCERTAIN,
                    detail=(
                        "a matching entry may already exist, but this backend exposes "
                        "no stable entry id, so idempotency cannot be guaranteed; "
                        "skipping to avoid a possible duplicate"
                    ),
                    **base,
                )
        return PlannedAction(
            action=ActionType.ADD,
            detail="no stable ids: adding (no plausible duplicate found)",
            **base,
        )

    def _conflict(self, base, nutrition, remote_id, action, detail) -> PlannedAction:
        on_conflict = self._settings.sync.on_conflict
        resolved_action = action
        if on_conflict == "skip":
            resolved_action = ActionType.SKIP
            detail = f"{detail} -> skipped (on_conflict=skip)"
        # ask/overwrite keep the conflict action; the writer handles prompting.
        return PlannedAction(
            action=resolved_action, nutrition=nutrition,
            existing_remote_id=remote_id, detail=detail, **base,
        )

    def _plan_changed(self, record: SyncRecord, resolution: Resolution) -> bool:
        sel = resolution.selection
        food = resolution.food
        if sel is None or food is None:
            return False
        if record.food_id != food.food_id:
            return True
        if abs(record.servings - sel.servings) > 1e-6:
            return True
        if record.serving_unit != sel.serving.unit:
            return True
        return False

    def _nutrition_for(self, resolution: Resolution) -> NutritionReport | None:
        if resolution.food is None or resolution.selection is None:
            return None
        planned = resolution.entry.planned_macros()
        if planned.is_empty():
            return None
        actual = actual_macros_for_selection(resolution.food, resolution.selection)
        return compare_macros(planned, actual, self._settings.nutrition)

    def _attach_daily_nutrition(self, plan: MealPlan, sync_plan: SyncPlan) -> None:
        from .nutrition import Macros, actual_macros_for_selection, sum_macros

        by_date: dict[dt.date, list[Macros]] = {}
        planned_by_date: dict[dt.date, list[Macros]] = {}
        for action in sync_plan.actions:
            res = action.resolution
            if res and res.ok and res.food and res.selection:
                by_date.setdefault(action.date, []).append(
                    actual_macros_for_selection(res.food, res.selection)
                )
            if res:
                planned_by_date.setdefault(action.date, []).append(res.entry.planned_macros())

        for date in by_date:
            planned_total = sum_macros(planned_by_date.get(date, []))
            actual_total = sum_macros(by_date.get(date, []))
            sync_plan.daily_nutrition[date] = compare_macros(
                planned_total, actual_total, self._settings.nutrition
            )

    # -- application (mutations) ---------------------------------------------

    def apply(
        self,
        sync_plan: SyncPlan,
        *,
        confirm_conflict=None,
    ) -> ApplyResult:
        """Execute a SyncPlan. `confirm_conflict(action) -> bool` is called for
        each conflict when on_conflict == 'ask'; return True to overwrite."""
        from .writer import DiaryWriter

        writer = DiaryWriter(self._backend, self._storage, self._settings)
        return writer.apply(sync_plan, confirm_conflict=confirm_conflict)


@dataclass
class ApplyResult:
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    uncertain: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.uncertain
