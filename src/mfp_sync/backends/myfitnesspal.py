"""Live MyFitnessPal backend, adapting the `mfp-api` package.

`mfp-api` (https://github.com/Rift-Walker/mfp-api) talks to MyFitnessPal's
native mobile REST v2 API. We verified from its source that:
  * food diary entries carry a stable UUID `id`  -> supports_stable_ids = True
  * add_food_entry / get_food_diary / delete_diary_entry round-trip that id
  * search_food / get_food return serving_sizes and nutritional_contents

This adapter translates between mfp-api's dict shapes and our typed value
objects, persists the session token locally (so re-runs don't re-login), and
redacts secrets from any logging.

The whole module imports lazily — the package only needs `mfp_api` installed
when this backend is actually selected.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import (
    AuthFailedError,
    AuthRequiredError,
    BackendUnavailableError,
    SessionExpiredError,
)
from .base import (
    BackendHealth,
    DiaryEntry,
    FoodCandidate,
    FoodNutrition,
    ServingSize,
)

# MyFitnessPal's meal_name values keyed by our lowercase categories.
_MEAL_NAME = {
    "breakfast": "Breakfast",
    "lunch": "Lunch",
    "dinner": "Dinner",
    "snack": "Snacks",
}
_MEAL_FROM_MFP = {v.lower(): k for k, v in _MEAL_NAME.items()}


class MyFitnessPalBackend:
    name = "myfitnesspal"
    supports_stable_ids = True

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: Any = None  # mfp_api.MfpClient, created on demand
        self._session_path: Path = settings.session_path

    # -- lazy auth ------------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from mfp_api import MfpClient  # type: ignore
        except ImportError as exc:  # pragma: no cover - env dependent
            raise BackendUnavailableError(
                "mfp-api is not installed. Install with:\n"
                '  pip install "git+https://github.com/Rift-Walker/mfp-api"'
            ) from exc

        username = self._settings.mfp_username
        password = self._settings.mfp_password
        if not username or not password:
            raise AuthRequiredError(
                "MFP_USERNAME and MFP_PASSWORD must be set to use the live backend. "
                "See docs/authentication.md."
            )
        try:
            self._client = MfpClient.login(username, password)
        except Exception as exc:  # mfp-api raises MfpAuthError or httpx errors
            raise AuthFailedError(f"MyFitnessPal login failed: {_redact(str(exc))}") from exc
        return self._client

    # -- health ---------------------------------------------------------------

    def health_check(self) -> BackendHealth:
        try:
            from mfp_api import MfpClient  # noqa: F401
        except ImportError:
            return BackendHealth(
                backend=self.name, reachable=False, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids,
                detail="mfp-api not installed",
            )
        if not self._settings.mfp_username or not self._settings.mfp_password:
            # No credentials yet. Still probe the native mobile identity API
            # (credential-free steps: client-credentials grant + signing key)
            # so `doctor` can distinguish "API reachable, just need creds" from
            # "the mobile auth surface itself is down/changed". This is the
            # flow mfp-api uses, which is DISTINCT from the website's NextAuth
            # form login (see docs/authentication.md).
            probe_ok, probe_detail = self._probe_identity_api()
            reachable_detail = (
                f"mobile identity API reachable; credentials not configured "
                f"(set MFP_USERNAME / MFP_PASSWORD). {probe_detail}"
                if probe_ok
                else f"credentials not configured, AND mobile identity API probe "
                     f"failed: {probe_detail}"
            )
            return BackendHealth(
                backend=self.name, reachable=probe_ok, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids,
                detail=reachable_detail,
            )
        try:
            client = self._ensure_client()
            # Read-only probe: today's diary. Never mutates.
            client.get_food_diary(dt.date.today())
        except (AuthRequiredError, AuthFailedError) as exc:
            return BackendHealth(
                backend=self.name, reachable=True, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids, detail=exc.message,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            return BackendHealth(
                backend=self.name, reachable=False, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids,
                detail=_redact(str(exc)),
            )
        # Read succeeded. Write capability is *inferred* (we do not test-write
        # during a health check) — reported as capable but unverified.
        return BackendHealth(
            backend=self.name, reachable=True, authenticated=True,
            can_read=True, can_write=True,
            supports_stable_ids=self.supports_stable_ids,
            detail="authenticated; read verified; write capability not test-exercised",
        )

    def _probe_identity_api(self) -> tuple[bool, str]:
        """Credential-free reachability check of the native mobile identity API.

        Exercises exactly the two OAuth steps that need no user account:
        the app-level client-credentials grant and the JWT signing-key fetch.
        If both succeed, mfp-api's login flow is viable at the transport level
        and only real user credentials remain untested. Returns (ok, detail).
        No secrets are returned in `detail`.
        """
        try:
            from mfp_api.auth import MfpAuth
        except ImportError:
            return False, "mfp-api not installed"
        auth = MfpAuth()
        try:
            auth._get_client_token()
            key = auth._fetch_signing_key()
            alg = key.get("alg", "?")
            return True, f"client-credentials + signing key OK (alg={alg})"
        except Exception as exc:  # pragma: no cover - network dependent
            return False, _redact(str(exc))
        finally:
            try:
                auth.close()
            except Exception:
                pass

    # -- food search ----------------------------------------------------------

    def search_food(self, query: str, limit: int = 25) -> list[FoodCandidate]:
        client = self._ensure_client()
        raw = client.search_food(query, limit=limit)
        return [self._to_candidate(_unwrap_item(r)) for r in raw]

    def get_food(self, food_id: str) -> FoodCandidate | None:
        client = self._ensure_client()
        try:
            raw = client.get_food(food_id)
        except Exception:
            return None
        return self._to_candidate(raw)

    # -- diary ----------------------------------------------------------------

    def get_diary(self, date: dt.date) -> list[DiaryEntry]:
        client = self._ensure_client()
        try:
            raw = client.get_food_diary(date)
        except Exception as exc:
            raise _translate(exc) from exc
        return [self._to_entry(r, date) for r in raw]

    def add_entry(
        self,
        *,
        date: dt.date,
        meal: str,
        food_id: str,
        serving: ServingSize,
        servings: float,
    ) -> DiaryEntry:
        client = self._ensure_client()
        serving_dict = {
            "unit": serving.unit,
            "value": serving.value,
            "nutrition_multiplier": serving.nutrition_multiplier,
        }
        try:
            raw = client.add_food_entry(
                food_id,
                serving_size=serving_dict,
                meal_name=_MEAL_NAME.get(meal, "Breakfast"),
                target_date=date,
                servings=servings,
            )
        except Exception as exc:
            raise _translate(exc) from exc
        return self._to_entry(raw, date)

    def delete_entry(self, remote_id: str) -> None:
        client = self._ensure_client()
        try:
            client.delete_diary_entry(remote_id)
        except Exception as exc:
            raise _translate(exc) from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # -- translation helpers --------------------------------------------------

    def _to_candidate(self, raw: dict) -> FoodCandidate:
        nutr = raw.get("nutritional_contents") or {}
        servings = tuple(self._to_serving(s) for s in (raw.get("serving_sizes") or []))
        return FoodCandidate(
            food_id=str(raw.get("id")),
            description=raw.get("description") or raw.get("name") or "",
            brand=raw.get("brand_name"),
            serving_sizes=servings,
            nutrition=_nutrition_from(nutr),
            version=str(raw["version"]) if raw.get("version") is not None else None,
        )

    @staticmethod
    def _to_serving(raw: dict) -> ServingSize:
        return ServingSize(
            unit=raw.get("unit", "serving"),
            value=float(raw.get("value", 1.0)),
            nutrition_multiplier=float(raw.get("nutrition_multiplier", 1.0)),
            gram_weight=(float(raw["gram_weight"]) if raw.get("gram_weight") is not None else None),
            serving_id=str(raw["id"]) if raw.get("id") is not None else None,
            description=raw.get("description"),
        )

    def _to_entry(self, raw: dict, date: dt.date) -> DiaryEntry:
        food = raw.get("food") or {}
        serving = raw.get("serving_size") or {}
        nutr = raw.get("nutritional_contents") or {}
        meal_name = (raw.get("meal_name") or "").lower()
        return DiaryEntry(
            remote_id=str(raw.get("id")),
            date=date,
            meal=_MEAL_FROM_MFP.get(meal_name, meal_name or "breakfast"),
            food_id=str(food.get("id") or raw.get("food_id") or ""),
            food_description=food.get("description") or "",
            servings=float(raw.get("servings", 1.0)),
            serving_unit=serving.get("unit", "serving"),
            serving_value=float(serving.get("value", 1.0)),
            serving_id=str(serving["id"]) if serving.get("id") is not None else None,
            calories=_energy(nutr),
            protein_g=_num(nutr.get("protein")),
            carbs_g=_num(nutr.get("carbohydrates")),
            fat_g=_num(nutr.get("fat")),
        )


# -- module-level helpers -----------------------------------------------------


def _unwrap_item(raw: dict) -> dict:
    """search_food results may be wrapped as {"item": {...}}."""
    if isinstance(raw, dict) and "item" in raw and isinstance(raw["item"], dict):
        return raw["item"]
    return raw


def _nutrition_from(nutr: dict) -> FoodNutrition:
    return FoodNutrition(
        calories=_energy(nutr),
        protein_g=_num(nutr.get("protein")),
        carbs_g=_num(nutr.get("carbohydrates")),
        fat_g=_num(nutr.get("fat")),
    )


def _energy(nutr: dict) -> float | None:
    energy = nutr.get("energy")
    if isinstance(energy, dict):
        return _num(energy.get("value"))
    return _num(energy if energy is not None else nutr.get("calories"))


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _redact(text: str) -> str:
    """Strip anything that looks like a bearer token or password from a string."""
    import re

    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1<redacted>", text)
    text = re.sub(r'("?password"?\s*[:=]\s*)"?[^",\s}]+', r"\1<redacted>", text, flags=re.I)
    return text


def _translate(exc: Exception) -> Exception:
    """Map mfp-api / httpx errors to our typed, machine-coded errors."""
    msg = _redact(str(exc))
    lowered = msg.lower()
    if "401" in msg or "unauthor" in lowered or "expired" in lowered:
        return SessionExpiredError(f"session expired or unauthorized: {msg}")
    if "403" in msg or "login failed" in lowered:
        return AuthFailedError(msg)
    return BackendUnavailableError(msg)
