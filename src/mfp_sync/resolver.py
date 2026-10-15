"""Food resolution: turn a plan entry's free-text food name into a concrete
MyFitnessPal food id + serving, safely.

Resolution order:
  1. Plan already carries mfp_food_id       -> use it (fetch details to confirm).
  2. Local catalog has a verified match      -> reuse it (the key win for re-runs).
  3. Backend food search                     -> only auto-accept a candidate whose
                                                fuzzy score clears the configured
                                                threshold AND is unambiguous.
  4. Otherwise                               -> UNRESOLVED. We never silently pick
                                                an arbitrary search result; the
                                                `resolve` command surfaces candidates
                                                for an explicit human choice.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum

from .backends.base import DiaryBackend, FoodCandidate, ServingSize
from .config import MatchingConfig
from .models import PlanEntry, normalize_unit
from .nutrition import ConversionError, ServingSelection, select_serving
from .storage import CatalogItem, Storage


class ResolutionStatus(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    UNCONVERTIBLE = "unconvertible"


@dataclass
class Resolution:
    entry: PlanEntry
    status: ResolutionStatus
    food: FoodCandidate | None = None
    selection: ServingSelection | None = None
    candidates: tuple[FoodCandidate, ...] = ()
    source: str = ""  # "plan" | "catalog" | "search"
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED


def _score(query: str, candidate: str) -> float:
    """Fuzzy similarity in [0,1]. Token-overlap-biased ratio."""
    q = query.strip().lower()
    c = candidate.strip().lower()
    ratio = SequenceMatcher(None, q, c).ratio()
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if q_tokens:
        overlap = len(q_tokens & c_tokens) / len(q_tokens)
    else:
        overlap = 0.0
    return max(ratio, 0.5 * ratio + 0.5 * overlap)


class Resolver:
    def __init__(self, backend: DiaryBackend, storage: Storage, config: MatchingConfig):
        self._backend = backend
        self._storage = storage
        self._config = config

    # -- catalog <-> candidate glue ------------------------------------------

    def _candidate_from_catalog(self, item: CatalogItem) -> FoodCandidate:
        from .backends.base import FoodNutrition

        serving = ServingSize(
            unit=item.serving_unit,
            value=item.serving_value,
            nutrition_multiplier=item.nutrition_multiplier,
            gram_weight=item.gram_weight,
            serving_id=item.serving_id,
        )
        return FoodCandidate(
            food_id=item.food_id,
            description=item.food_description,
            brand=item.brand,
            serving_sizes=(serving,),
            nutrition=FoodNutrition(
                calories=item.calories,
                protein_g=item.protein_g,
                carbs_g=item.carbs_g,
                fat_g=item.fat_g,
            ),
        )

    def save_to_catalog(self, name: str, food: FoodCandidate, serving: ServingSize) -> None:
        item = CatalogItem(
            name=name.strip().lower(),
            food_id=food.food_id,
            food_description=food.description,
            brand=food.brand,
            serving_id=serving.serving_id,
            serving_unit=serving.unit,
            serving_value=serving.value,
            nutrition_multiplier=serving.nutrition_multiplier,
            gram_weight=serving.gram_weight,
            calories=food.nutrition.calories,
            protein_g=food.nutrition.protein_g,
            carbs_g=food.nutrition.carbs_g,
            fat_g=food.nutrition.fat_g,
            verified_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )
        self._storage.upsert_catalog(item)

    # -- resolution -----------------------------------------------------------

    def resolve(self, entry: PlanEntry) -> Resolution:
        # 1. explicit food id on the plan
        if entry.mfp_food_id:
            food = self._backend.get_food(entry.mfp_food_id)
            if food is None:
                # Build a minimal candidate so serving selection can still work
                # if the plan also specified serving details.
                food = FoodCandidate(food_id=entry.mfp_food_id, description=entry.food)
            return self._finalize(entry, food, source="plan")

        # 2. local catalog
        cat = self._storage.get_catalog(entry.food)
        if cat is not None:
            food = self._candidate_from_catalog(cat)
            return self._finalize(entry, food, source="catalog")

        # 3. backend search
        candidates = self._backend.search_food(entry.food, limit=self._config.max_candidates)
        if not candidates:
            return Resolution(
                entry, ResolutionStatus.NOT_FOUND, source="search",
                message=f"no food search results for {entry.food!r}",
            )

        scored = sorted(
            ((_score(entry.food, c.description), c) for c in candidates),
            key=lambda t: t[0],
            reverse=True,
        )
        top_score, top = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0

        # Auto-accept only if confident AND clearly ahead of the runner-up.
        if top_score >= self._config.auto_accept_score and (top_score - second_score) >= 0.05:
            return self._finalize(entry, top, source="search")

        return Resolution(
            entry,
            ResolutionStatus.AMBIGUOUS,
            candidates=tuple(c for _s, c in scored),
            source="search",
            message=(
                f"best match {top.description!r} scored {top_score:.2f}, "
                f"below auto-accept {self._config.auto_accept_score:.2f} "
                "or too close to runner-up"
            ),
        )

    def _finalize(self, entry: PlanEntry, food: FoodCandidate, *, source: str) -> Resolution:
        """Pick a serving for a chosen food, or report it's unconvertible."""
        # If the plan or catalog gives explicit serving details and the food has
        # none, synthesize a serving from the plan so conversion can proceed.
        if not food.serving_sizes and entry.serving_id is None:
            # Create a pass-through serving matching the requested unit.
            synthesized = ServingSize(
                unit=normalize_unit(entry.unit), value=1.0, nutrition_multiplier=1.0
            )
            food = FoodCandidate(
                food_id=food.food_id,
                description=food.description,
                brand=food.brand,
                serving_sizes=(synthesized,),
                nutrition=food.nutrition,
                version=food.version,
            )
        try:
            selection = select_serving(entry, food)
        except ConversionError as exc:
            return Resolution(
                entry, ResolutionStatus.UNCONVERTIBLE, food=food, source=source,
                message=str(exc),
            )
        return Resolution(
            entry, ResolutionStatus.RESOLVED, food=food, selection=selection, source=source,
        )
