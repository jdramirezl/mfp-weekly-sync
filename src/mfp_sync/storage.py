"""Local SQLite persistence: the food catalog and the sync-history ledger.

Two tables:

  catalog         — verified food matches. Maps a food name/alias to a concrete
                    MyFitnessPal food id + serving. Once a food is resolved it is
                    reused for all future plans, satisfying "future weekly plans
                    should reuse the verified match."

  sync_record     — one row per (plan_id, source entry_id). Records what this
                    application put into the diary and the remote entry id it got
                    back. This is the ledger the reconciliation engine uses to
                    tell *our* entries apart from manual ones, and to achieve
                    idempotency across re-runs.

All timestamps are UTC ISO strings. No secrets are ever stored here.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

SYNC_STATUS_PENDING = "pending"
SYNC_STATUS_SYNCED = "synced"
SYNC_STATUS_UNCERTAIN = "uncertain"
SYNC_STATUS_FAILED = "failed"
SYNC_STATUS_DELETED_REMOTE = "deleted_remote"  # user deleted it in MFP


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class CatalogItem:
    name: str  # normalized food name / alias (the lookup key)
    food_id: str
    food_description: str
    brand: str | None
    serving_id: str | None
    serving_unit: str
    serving_value: float
    nutrition_multiplier: float
    gram_weight: float | None
    calories: float | None
    protein_g: float | None
    carbs_g: float | None
    fat_g: float | None
    verified_at: str


@dataclass
class SyncRecord:
    plan_id: str
    source_entry_id: str
    date: dt.date
    meal: str
    food_id: str
    serving_id: str | None
    servings: float
    serving_unit: str
    serving_value: float
    remote_entry_id: str | None
    fingerprint: str | None
    status: str
    created_at: str
    updated_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    name                 TEXT PRIMARY KEY,
    food_id              TEXT NOT NULL,
    food_description     TEXT NOT NULL,
    brand                TEXT,
    serving_id           TEXT,
    serving_unit         TEXT NOT NULL,
    serving_value        REAL NOT NULL,
    nutrition_multiplier REAL NOT NULL DEFAULT 1.0,
    gram_weight          REAL,
    calories             REAL,
    protein_g            REAL,
    carbs_g              REAL,
    fat_g                REAL,
    verified_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_record (
    plan_id         TEXT NOT NULL,
    source_entry_id TEXT NOT NULL,
    date            TEXT NOT NULL,
    meal            TEXT NOT NULL,
    food_id         TEXT NOT NULL,
    serving_id      TEXT,
    servings        REAL NOT NULL,
    serving_unit    TEXT NOT NULL,
    serving_value   REAL NOT NULL,
    remote_entry_id TEXT,
    fingerprint     TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (plan_id, source_entry_id)
);

CREATE INDEX IF NOT EXISTS idx_sync_remote ON sync_record (remote_entry_id);
CREATE INDEX IF NOT EXISTS idx_sync_date ON sync_record (date);
"""


class Storage:
    """Thin SQLite wrapper. Not thread-safe; one instance per process is fine."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- catalog --------------------------------------------------------------

    def upsert_catalog(self, item: CatalogItem) -> None:
        self._conn.execute(
            """
            INSERT INTO catalog (name, food_id, food_description, brand, serving_id,
                serving_unit, serving_value, nutrition_multiplier, gram_weight,
                calories, protein_g, carbs_g, fat_g, verified_at)
            VALUES (:name, :food_id, :food_description, :brand, :serving_id,
                :serving_unit, :serving_value, :nutrition_multiplier, :gram_weight,
                :calories, :protein_g, :carbs_g, :fat_g, :verified_at)
            ON CONFLICT(name) DO UPDATE SET
                food_id=excluded.food_id,
                food_description=excluded.food_description,
                brand=excluded.brand,
                serving_id=excluded.serving_id,
                serving_unit=excluded.serving_unit,
                serving_value=excluded.serving_value,
                nutrition_multiplier=excluded.nutrition_multiplier,
                gram_weight=excluded.gram_weight,
                calories=excluded.calories,
                protein_g=excluded.protein_g,
                carbs_g=excluded.carbs_g,
                fat_g=excluded.fat_g,
                verified_at=excluded.verified_at
            """,
            item.__dict__,
        )
        self._conn.commit()

    def get_catalog(self, name: str) -> CatalogItem | None:
        row = self._conn.execute(
            "SELECT * FROM catalog WHERE name = ?", (name.strip().lower(),)
        ).fetchone()
        return _row_to_catalog(row) if row else None

    def all_catalog(self) -> list[CatalogItem]:
        rows = self._conn.execute("SELECT * FROM catalog ORDER BY name").fetchall()
        return [_row_to_catalog(r) for r in rows]

    # -- sync records ---------------------------------------------------------

    def upsert_sync_record(self, rec: SyncRecord) -> None:
        payload = {
            **rec.__dict__,
            "date": rec.date.isoformat(),
        }
        self._conn.execute(
            """
            INSERT INTO sync_record (plan_id, source_entry_id, date, meal, food_id,
                serving_id, servings, serving_unit, serving_value, remote_entry_id,
                fingerprint, status, created_at, updated_at)
            VALUES (:plan_id, :source_entry_id, :date, :meal, :food_id,
                :serving_id, :servings, :serving_unit, :serving_value, :remote_entry_id,
                :fingerprint, :status, :created_at, :updated_at)
            ON CONFLICT(plan_id, source_entry_id) DO UPDATE SET
                date=excluded.date, meal=excluded.meal, food_id=excluded.food_id,
                serving_id=excluded.serving_id, servings=excluded.servings,
                serving_unit=excluded.serving_unit, serving_value=excluded.serving_value,
                remote_entry_id=excluded.remote_entry_id, fingerprint=excluded.fingerprint,
                status=excluded.status, updated_at=excluded.updated_at
            """,
            payload,
        )
        self._conn.commit()

    def get_sync_record(self, plan_id: str, source_entry_id: str) -> SyncRecord | None:
        row = self._conn.execute(
            "SELECT * FROM sync_record WHERE plan_id = ? AND source_entry_id = ?",
            (plan_id, source_entry_id),
        ).fetchone()
        return _row_to_sync(row) if row else None

    def records_for_plan(self, plan_id: str) -> list[SyncRecord]:
        rows = self._conn.execute(
            "SELECT * FROM sync_record WHERE plan_id = ? ORDER BY date, meal", (plan_id,)
        ).fetchall()
        return [_row_to_sync(r) for r in rows]

    def claimed_remote_ids(self) -> set[str]:
        """Remote diary entry IDs already owned by SOME sync record (any plan).

        Used by recovery to avoid adopting a remote entry that is already
        tracked as belonging to a different managed entry — such an entry is
        not evidence that *this* pending write succeeded.
        """
        rows = self._conn.execute(
            "SELECT remote_entry_id FROM sync_record "
            "WHERE remote_entry_id IS NOT NULL AND remote_entry_id != ''"
        ).fetchall()
        return {r["remote_entry_id"] for r in rows}

    def new_record(
        self,
        *,
        plan_id: str,
        source_entry_id: str,
        date: dt.date,
        meal: str,
        food_id: str,
        serving_id: str | None,
        servings: float,
        serving_unit: str,
        serving_value: float,
        status: str = SYNC_STATUS_PENDING,
    ) -> SyncRecord:
        now = _utcnow()
        return SyncRecord(
            plan_id=plan_id,
            source_entry_id=source_entry_id,
            date=date,
            meal=meal,
            food_id=food_id,
            serving_id=serving_id,
            servings=servings,
            serving_unit=serving_unit,
            serving_value=serving_value,
            remote_entry_id=None,
            fingerprint=None,
            status=status,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def touch(rec: SyncRecord, **updates) -> SyncRecord:
        for key, value in updates.items():
            setattr(rec, key, value)
        rec.updated_at = _utcnow()
        return rec


def _row_to_catalog(row: sqlite3.Row) -> CatalogItem:
    return CatalogItem(
        name=row["name"],
        food_id=row["food_id"],
        food_description=row["food_description"],
        brand=row["brand"],
        serving_id=row["serving_id"],
        serving_unit=row["serving_unit"],
        serving_value=row["serving_value"],
        nutrition_multiplier=row["nutrition_multiplier"],
        gram_weight=row["gram_weight"],
        calories=row["calories"],
        protein_g=row["protein_g"],
        carbs_g=row["carbs_g"],
        fat_g=row["fat_g"],
        verified_at=row["verified_at"],
    )


def _row_to_sync(row: sqlite3.Row) -> SyncRecord:
    return SyncRecord(
        plan_id=row["plan_id"],
        source_entry_id=row["source_entry_id"],
        date=dt.date.fromisoformat(row["date"]),
        meal=row["meal"],
        food_id=row["food_id"],
        serving_id=row["serving_id"],
        servings=row["servings"],
        serving_unit=row["serving_unit"],
        serving_value=row["serving_value"],
        remote_entry_id=row["remote_entry_id"],
        fingerprint=row["fingerprint"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
