"""The backend contract.

A DiaryBackend abstracts whatever actually stores diary entries. The real one
talks to MyFitnessPal's native API via mfp-api; the mock one keeps an in-memory
/ SQLite store for deterministic tests and offline use.

Design rules enforced by this contract:
  * Every created diary entry has a stable, backend-assigned `remote_id`.
    This is what makes idempotent reconciliation possible. A backend that
    cannot provide stable IDs must say so via `supports_stable_ids == False`,
    and the reconciliation engine then refuses ambiguous destructive actions.
  * Reads return the full current state for a date so the engine can do its
    three-way comparison.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ServingSize:
    """A serving definition from the food database.

    `unit` is the serving's unit label (e.g. "g", "cup", "medium banana").
    `value` is how many of that unit this serving represents.
    `gram_weight` is the mass of one serving in grams, when the backend knows
    it; None means "unknown — do not invent a gram conversion".
    """

    unit: str
    value: float
    nutrition_multiplier: float = 1.0
    gram_weight: float | None = None
    serving_id: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class FoodNutrition:
    """Per-serving nutrition as stored by the backend's food database."""

    calories: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None


@dataclass(frozen=True)
class FoodCandidate:
    """A single food-search result."""

    food_id: str
    description: str
    brand: str | None = None
    serving_sizes: tuple[ServingSize, ...] = field(default_factory=tuple)
    # Nutrition for the *default* (first) serving size.
    nutrition: FoodNutrition = field(default_factory=FoodNutrition)
    version: str | None = None


@dataclass(frozen=True)
class DiaryEntry:
    """A single logged diary line item as it currently exists remotely."""

    remote_id: str
    date: dt.date
    meal: str  # lowercase meal category: breakfast/lunch/dinner/snack
    food_id: str
    food_description: str
    servings: float
    serving_unit: str
    serving_value: float
    serving_id: str | None = None
    calories: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None

    def fingerprint(self) -> str:
        """A content fingerprint used to detect manual edits to a managed entry.

        Deliberately excludes remote_id (identity) and nutrition (derived) —
        it captures *what was logged*: food, quantity, serving, meal, date.
        """
        parts = [
            self.date.isoformat(),
            self.meal,
            self.food_id,
            f"{self.servings:.6g}",
            self.serving_unit,
            f"{self.serving_value:.6g}",
            self.serving_id or "",
        ]
        return "|".join(parts)


@runtime_checkable
class DiaryBackend(Protocol):
    """Abstract diary backend."""

    name: str
    supports_stable_ids: bool

    def health_check(self) -> BackendHealth:
        """Report connectivity, read access, and write capability without mutating."""
        ...

    def search_food(self, query: str, limit: int = 25) -> list[FoodCandidate]:
        ...

    def get_food(self, food_id: str) -> FoodCandidate | None:
        ...

    def get_diary(self, date: dt.date) -> list[DiaryEntry]:
        ...

    def add_entry(
        self,
        *,
        date: dt.date,
        meal: str,
        food_id: str,
        serving: ServingSize,
        servings: float,
    ) -> DiaryEntry:
        """Create a diary entry and return it, including its stable remote_id."""
        ...

    def delete_entry(self, remote_id: str) -> None:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True)
class BackendHealth:
    """Result of DiaryBackend.health_check()."""

    backend: str
    reachable: bool
    authenticated: bool
    can_read: bool
    can_write: bool
    supports_stable_ids: bool
    detail: str = ""
