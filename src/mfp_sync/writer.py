"""Executes a SyncPlan's mutations safely.

Safety properties implemented here:
  * Rate limiting between writes.
  * Bounded retries with exponential backoff for *transient* errors only.
  * No blind retry of a write whose outcome is uncertain (WriteUncertainError):
    we record the record as `uncertain` and stop touching it — the next run's
    read-based reconciliation resolves it without risking a duplicate.
  * A pending sync record is written BEFORE the remote call and updated to
    `synced` with the returned remote id AFTER. If the process crashes between,
    the record stays `pending`; the next run reconciles by reading the diary and
    will not blindly re-add (it checks for the entry first).
  * Read-after-write verification: after add, we confirm the entry is present
    remotely before marking it synced.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .backends.base import DiaryBackend
from .config import Settings
from .errors import (
    BackendUnavailableError,
    SessionExpiredError,
    WriteUncertainError,
)
from .nutrition import ServingSelection
from .reconciliation import ActionType, ApplyResult, PlannedAction, SyncPlan
from .storage import (
    SYNC_STATUS_FAILED,
    SYNC_STATUS_SYNCED,
    SYNC_STATUS_UNCERTAIN,
    Storage,
)

ConfirmFn = Callable[[PlannedAction], bool]


class DiaryWriter:
    def __init__(self, backend: DiaryBackend, storage: Storage, settings: Settings):
        self._backend = backend
        self._storage = storage
        self._settings = settings
        self._last_write = 0.0

    def apply(
        self, sync_plan: SyncPlan, *, confirm_conflict: ConfirmFn | None = None
    ) -> ApplyResult:
        result = ApplyResult()
        for action in sync_plan.actions:
            self._apply_one(sync_plan.plan_id, action, result, confirm_conflict)
        return result

    def _apply_one(
        self,
        plan_id: str,
        action: PlannedAction,
        result: ApplyResult,
        confirm_conflict: ConfirmFn | None,
    ) -> None:
        label = f"{action.date.isoformat()}/{action.meal}/{action.source_entry_id}"

        if action.action in (ActionType.SKIP, ActionType.DELETED_REMOTE):
            result.skipped.append(label)
            return
        if action.action in (ActionType.UNRESOLVED, ActionType.UNCERTAIN):
            result.uncertain.append((label, action.detail))
            return

        if action.action in (ActionType.CONFLICT_EDIT, ActionType.CONFLICT_PLAN):
            policy = self._settings.sync.on_conflict
            if policy == "ask":
                approved = bool(confirm_conflict and confirm_conflict(action))
                if not approved:
                    result.skipped.append(label)
                    return
            elif policy != "overwrite":
                result.skipped.append(label)
                return
            # overwrite: delete the existing managed entry, then re-add.
            if action.existing_remote_id:
                try:
                    self._rate_limit()
                    self._backend.delete_entry(action.existing_remote_id)
                except Exception as exc:  # noqa: BLE001
                    result.failed.append((label, f"delete-before-overwrite failed: {exc}"))
                    return
            self._do_add(plan_id, action, result, label, overwrite=True)
            return

        if action.action in (ActionType.ADD, ActionType.RESTORE):
            self._do_add(plan_id, action, result, label, overwrite=False)
            return

    def _do_add(
        self,
        plan_id: str,
        action: PlannedAction,
        result: ApplyResult,
        label: str,
        *,
        overwrite: bool,
    ) -> None:
        resolution = action.resolution
        assert resolution is not None and resolution.selection is not None
        sel: ServingSelection = resolution.selection
        food = resolution.food
        assert food is not None

        # 1. Write a pending record FIRST (crash-safety).
        record = self._storage.new_record(
            plan_id=plan_id,
            source_entry_id=action.source_entry_id,
            date=action.date,
            meal=action.meal,
            food_id=food.food_id,
            serving_id=sel.serving.serving_id,
            servings=sel.servings,
            serving_unit=sel.serving.unit,
            serving_value=sel.serving.value,
        )
        self._storage.upsert_sync_record(record)

        # 2. Perform the remote write with bounded retries for transient errors.
        try:
            entry = self._write_with_retries(action)
        except WriteUncertainError as exc:
            self._storage.upsert_sync_record(
                self._storage.touch(record, status=SYNC_STATUS_UNCERTAIN)
            )
            result.uncertain.append((label, str(exc)))
            return
        except (SessionExpiredError, BackendUnavailableError) as exc:
            self._storage.upsert_sync_record(
                self._storage.touch(record, status=SYNC_STATUS_FAILED)
            )
            result.failed.append((label, str(exc)))
            return
        except Exception as exc:  # noqa: BLE001
            self._storage.upsert_sync_record(
                self._storage.touch(record, status=SYNC_STATUS_FAILED)
            )
            result.failed.append((label, str(exc)))
            return

        # 3. Read-after-write verification.
        verified = self._verify(action, entry.remote_id)
        if not verified:
            self._storage.upsert_sync_record(
                self._storage.touch(record, status=SYNC_STATUS_UNCERTAIN,
                                    remote_entry_id=entry.remote_id,
                                    fingerprint=entry.fingerprint())
            )
            result.uncertain.append((label, "write not confirmed by read-back"))
            return

        # 4. Mark synced with the stable remote id + fingerprint.
        self._storage.upsert_sync_record(
            self._storage.touch(
                record,
                status=SYNC_STATUS_SYNCED,
                remote_entry_id=entry.remote_id,
                fingerprint=entry.fingerprint(),
            )
        )
        if overwrite:
            result.overwritten.append(label)
        elif action.action is ActionType.RESTORE:
            result.restored.append(label)
        else:
            result.added.append(label)

    def _write_with_retries(self, action: PlannedAction):
        resolution = action.resolution
        sel = resolution.selection
        food = resolution.food
        attempts = self._settings.sync.max_retries
        delay = self._settings.sync.retry_base_delay_seconds
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                self._rate_limit()
                return self._backend.add_entry(
                    date=action.date,
                    meal=action.meal,
                    food_id=food.food_id,
                    serving=sel.serving,
                    servings=sel.servings,
                )
            except WriteUncertainError:
                # NEVER retry an uncertain write — it may have committed.
                raise
            except SessionExpiredError as exc:
                # Not transient in the retry sense; surface immediately.
                raise exc
            except BackendUnavailableError as exc:
                last_exc = exc
                if attempt < attempts:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        if last_exc:
            raise last_exc
        raise BackendUnavailableError("write failed with no exception captured")

    def _verify(self, action: PlannedAction, remote_id: str) -> bool:
        """Read the diary back and confirm the new entry id is present."""
        try:
            entries = self._backend.get_diary(action.date)
        except Exception:  # noqa: BLE001
            return False
        return any(e.remote_id == remote_id for e in entries)

    def _rate_limit(self) -> None:
        interval = self._settings.sync.min_write_interval_seconds
        now = time.monotonic()
        wait = interval - (now - self._last_write)
        if wait > 0:
            time.sleep(wait)
        self._last_write = time.monotonic()
