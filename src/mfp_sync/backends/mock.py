"""A deterministic, in-memory/SQLite-free mock diary backend.

Used by every automated test and by `--backend mock` for offline dry runs. It
assigns stable UUID remote IDs just like MyFitnessPal does, so reconciliation
behaves identically against it.

It ships a small built-in food database and supports fault injection so tests
can simulate timeouts, partial failures, and expired sessions deterministically.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from ..errors import SessionExpiredError, WriteUncertainError
from .base import (
    BackendHealth,
    DiaryEntry,
    FoodCandidate,
    FoodNutrition,
    ServingSize,
)


def _banana() -> FoodCandidate:
    return FoodCandidate(
        food_id="mock-banana",
        description="Banana, raw",
        brand=None,
        serving_sizes=(
            ServingSize(unit="g", value=1.0, nutrition_multiplier=1.0, gram_weight=1.0,
                        serving_id="banana-g", description="1 g"),
            ServingSize(unit="medium", value=1.0, nutrition_multiplier=118.0,
                        gram_weight=118.0, serving_id="banana-medium",
                        description="1 medium (118 g)"),
        ),
        nutrition=FoodNutrition(calories=0.89, protein_g=0.0109, carbs_g=0.228, fat_g=0.0033),
        version="1",
    )


def _oatmeal() -> FoodCandidate:
    return FoodCandidate(
        food_id="mock-oatmeal",
        description="Oatmeal, dry",
        brand=None,
        serving_sizes=(
            ServingSize(unit="g", value=1.0, nutrition_multiplier=1.0, gram_weight=1.0,
                        serving_id="oat-g", description="1 g"),
        ),
        nutrition=FoodNutrition(calories=3.89, protein_g=0.169, carbs_g=0.663, fat_g=0.069),
        version="1",
    )


def _chicken_cooked() -> FoodCandidate:
    return FoodCandidate(
        food_id="mock-chicken-cooked",
        description="Chicken breast, cooked",
        brand=None,
        serving_sizes=(
            ServingSize(unit="g", value=1.0, nutrition_multiplier=1.0, gram_weight=1.0,
                        serving_id="chk-g", description="1 g"),
        ),
        nutrition=FoodNutrition(calories=1.65, protein_g=0.31, carbs_g=0.0, fat_g=0.036),
        version="1",
    )


def _chicken_raw() -> FoodCandidate:
    return FoodCandidate(
        food_id="mock-chicken-raw",
        description="Chicken breast, raw",
        brand=None,
        serving_sizes=(
            ServingSize(unit="g", value=1.0, nutrition_multiplier=1.0, gram_weight=1.0,
                        serving_id="chkraw-g", description="1 g"),
        ),
        nutrition=FoodNutrition(calories=1.20, protein_g=0.228, carbs_g=0.0, fat_g=0.026),
        version="1",
    )


def _rice_cooked() -> FoodCandidate:
    return FoodCandidate(
        food_id="mock-rice-cooked",
        description="White rice, cooked",
        brand=None,
        serving_sizes=(
            ServingSize(unit="g", value=1.0, nutrition_multiplier=1.0, gram_weight=1.0,
                        serving_id="rice-g", description="1 g"),
            ServingSize(unit="cup", value=1.0, nutrition_multiplier=158.0,
                        gram_weight=158.0, serving_id="rice-cup",
                        description="1 cup (158 g)"),
        ),
        nutrition=FoodNutrition(calories=1.30, protein_g=0.027, carbs_g=0.28, fat_g=0.003),
        version="1",
    )


DEFAULT_FOODS: list[FoodCandidate] = [
    _banana(), _oatmeal(), _chicken_cooked(), _chicken_raw(), _rice_cooked(),
]


@dataclass
class MockBackend:
    """A diary store with a tiny food DB and fault injection hooks.

    If `db_path` is provided, entries persist to a JSON file so the diary
    survives across process invocations (mirroring a real backend). With no
    path it is purely in-memory, which is what the unit tests use.
    """

    name: str = "mock"
    supports_stable_ids: bool = True
    db_path: object | None = None  # when set, entries persist to this JSON file

    foods: list[FoodCandidate] = field(default_factory=lambda: list(DEFAULT_FOODS))
    _entries: dict[str, DiaryEntry] = field(default_factory=dict)

    # Fault injection ---------------------------------------------------------
    # If set, called at the start of add_entry with the pending (date, meal,
    # food_id). Raise to simulate a failure. Return None to proceed.
    fail_on_add: Callable[[dt.date, str, str], None] | None = None
    session_valid: bool = True
    # When True, add_entry writes the entry *then* raises WriteUncertainError,
    # simulating a response lost after the server already committed.
    uncertain_writes: bool = False

    def __post_init__(self) -> None:
        self._load()

    # Persistence -------------------------------------------------------------

    def _path(self):
        if self.db_path is None:
            return None
        from pathlib import Path

        return Path(str(self.db_path))

    def _load(self) -> None:
        import json

        p = self._path()
        if p is None or not p.exists():
            return
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            return
        for row in data.get("entries", []):
            row = dict(row)
            row["date"] = dt.date.fromisoformat(row["date"])
            self._entries[row["remote_id"]] = DiaryEntry(**row)

    def _save(self) -> None:
        import json

        p = self._path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for e in self._entries.values():
            d = dict(e.__dict__)
            d["date"] = e.date.isoformat()
            rows.append(d)
        p.write_text(json.dumps({"entries": rows}, indent=2))

    # Backend protocol --------------------------------------------------------

    def health_check(self) -> BackendHealth:
        return BackendHealth(
            backend=self.name,
            reachable=True,
            authenticated=self.session_valid,
            can_read=True,
            can_write=self.session_valid,
            supports_stable_ids=self.supports_stable_ids,
            detail="in-memory mock backend",
        )

    def search_food(self, query: str, limit: int = 25) -> list[FoodCandidate]:
        q = query.strip().lower()
        scored: list[tuple[float, FoodCandidate]] = []
        for food in self.foods:
            hay = f"{food.description} {food.brand or ''}".lower()
            if all(tok in hay for tok in q.split()):
                # crude ordering: shorter description == closer match
                scored.append((len(hay), food))
            elif q in hay:
                scored.append((len(hay) + 100, food))
        scored.sort(key=lambda t: t[0])
        return [f for _s, f in scored[:limit]]

    def get_food(self, food_id: str) -> FoodCandidate | None:
        for food in self.foods:
            if food.food_id == food_id:
                return food
        return None

    def get_diary(self, date: dt.date) -> list[DiaryEntry]:
        self._require_session()
        return [e for e in self._entries.values() if e.date == date]

    def add_entry(
        self,
        *,
        date: dt.date,
        meal: str,
        food_id: str,
        serving: ServingSize,
        servings: float,
    ) -> DiaryEntry:
        self._require_session()
        if self.fail_on_add is not None:
            self.fail_on_add(date, meal, food_id)

        food = self.get_food(food_id)
        nutrition = food.nutrition if food else FoodNutrition()
        # Scale per-unit nutrition by servings * serving multiplier.
        factor = servings * serving.nutrition_multiplier
        entry = DiaryEntry(
            remote_id=str(uuid.uuid4()),
            date=date,
            meal=meal,
            food_id=food_id,
            food_description=food.description if food else food_id,
            servings=servings,
            serving_unit=serving.unit,
            serving_value=serving.value,
            serving_id=serving.serving_id,
            calories=_scale(nutrition.calories, factor),
            protein_g=_scale(nutrition.protein_g, factor),
            carbs_g=_scale(nutrition.carbs_g, factor),
            fat_g=_scale(nutrition.fat_g, factor),
        )
        self._entries[entry.remote_id] = entry
        if self.uncertain_writes:
            self._save()
            raise WriteUncertainError(
                "simulated: response lost after server committed the write"
            )
        self._save()
        return entry

    def delete_entry(self, remote_id: str) -> None:
        self._require_session()
        self._entries.pop(remote_id, None)
        self._save()

    def close(self) -> None:
        pass

    # Test helpers ------------------------------------------------------------

    def _require_session(self) -> None:
        if not self.session_valid:
            raise SessionExpiredError("mock session marked expired")

    def manual_add(
        self,
        *,
        date: dt.date,
        meal: str,
        food_id: str,
        food_description: str = "manual food",
        servings: float = 1.0,
        serving_unit: str = "g",
        serving_value: float = 1.0,
    ) -> DiaryEntry:
        """Simulate a user adding an entry directly in the MyFitnessPal app."""
        entry = DiaryEntry(
            remote_id=str(uuid.uuid4()),
            date=date,
            meal=meal,
            food_id=food_id,
            food_description=food_description,
            servings=servings,
            serving_unit=serving_unit,
            serving_value=serving_value,
        )
        self._entries[entry.remote_id] = entry
        self._save()
        return entry

    def all_entries(self) -> list[DiaryEntry]:
        return list(self._entries.values())


def _scale(value: float | None, factor: float) -> float | None:
    return None if value is None else round(value * factor, 4)
