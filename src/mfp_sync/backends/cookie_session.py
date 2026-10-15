"""Live backend driven by a browser session cookie (for SSO/Google-login accounts).

Flow:
  1. Extract the authenticated myfitnesspal.com cookies from Chrome (macOS).
  2. Exchange them for a bearer token:
        GET https://www.myfitnesspal.com/user/auth_token?refresh=true
     -> {"access_token": ..., "user_id": ..., "token_type": "Bearer", ...}
  3. Call the SAME v2 JSON API (api.myfitnesspal.com) the other live backend
     uses, with Authorization: Bearer <token> + mfp-user-id + mfp-client-id.

This reuses the dict<->value translation helpers from the myfitnesspal backend
so diary entries still carry stable UUID IDs (idempotency works identically).

The web client id "mfp-main-js" is used (this is the web session's client),
matching myfitnesspal-mcp-python.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..browser_cookies import CookieExtractionError, extract_mfp_cookies
from ..config import Settings
from ..errors import (
    AuthFailedError,
    AuthRequiredError,
    BackendUnavailableError,
    SessionExpiredError,
)
from .base import BackendHealth, DiaryEntry, FoodCandidate, ServingSize
from .myfitnesspal import (
    _MEAL_NAME,
    _redact,
    _translate,
    _unwrap_item,
)

API_BASE = "https://api.myfitnesspal.com"
WWW_BASE = "https://www.myfitnesspal.com"
AUTH_TOKEN_PATH = "/user/auth_token?refresh=true"
WEB_CLIENT_ID = "mfp-main-js"


class CookieSessionBackend:
    name = "cookie-session"
    supports_stable_ids = True

    def __init__(self, settings: Settings, browser: str = "Chrome"):
        self._settings = settings
        self._browser = browser
        self._http: Any = None
        self._access_token: str | None = None
        self._user_id: str | None = None
        self._meal_names: list[str] | None = None
        # Reuse the translation methods from the mobile backend without its auth.
        from .myfitnesspal import MyFitnessPalBackend

        self._xlate = MyFitnessPalBackend.__new__(MyFitnessPalBackend)

    # -- session establishment ------------------------------------------------

    def _ensure_session(self) -> None:
        if self._access_token and self._user_id:
            return
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise BackendUnavailableError(
                "the cookie-session backend needs httpx: pip install -e \".[cookie]\""
            ) from exc

        try:
            cookies = extract_mfp_cookies(self._browser)
        except CookieExtractionError as exc:
            raise AuthRequiredError(str(exc)) from exc

        if self._http is None:
            self._http = httpx.Client(timeout=30.0, follow_redirects=True)
        self._http.cookies.update(cookies)

        # Exchange the session cookies for a bearer token.
        try:
            resp = self._http.get(
                WWW_BASE + AUTH_TOKEN_PATH,
                headers={"Accept": "application/json"},
            )
        except Exception as exc:  # network
            raise BackendUnavailableError(_redact(str(exc))) from exc

        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and ctype.startswith("application/json"):
            data = resp.json()
            self._access_token = data.get("access_token")
            self._user_id = str(data.get("user_id")) if data.get("user_id") else None
            if not self._access_token or not self._user_id:
                raise AuthFailedError(
                    "auth_token response missing access_token/user_id; "
                    "the browser session may be stale — log in again."
                )
            return

        # A non-JSON (usually HTML login page) response means the cookies are
        # not a valid logged-in session.
        raise AuthFailedError(
            "could not exchange browser cookies for a token "
            f"(status {resp.status_code}, content-type {ctype!r}). "
            "The session may be expired — log into myfitnesspal.com in "
            f"{self._browser} again."
        )

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "mfp-user-id": str(self._user_id),
            "mfp-client-id": WEB_CLIENT_ID,
            "Accept": "application/json",
        }
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _account_meal_names(self) -> list[str]:
        """Fetch this account's configured meal names (cached).

        MyFitnessPal lets users rename/add meals (e.g. three snack slots), so
        the diary API resolves meal_name against the account's own list. Sending
        a name that isn't configured (e.g. "Snacks" on an account whose snack
        slots are "Snacks Morning/Noon/Night") makes the server silently coerce
        the entry to Dinner — observed in live testing. We read the real list
        and map to it.
        """
        if self._meal_names is not None:
            return self._meal_names
        try:
            data = self._get(f"/v2/users/{self._user_id}",
                             {"fields[]": ["diary_preferences"]})
            prefs = (data.get("item") or {}).get("diary_preferences") or {}
            names = prefs.get("meal_names") or []
        except Exception:
            names = []
        self._meal_names = [str(n) for n in names] or list(_MEAL_NAME.values())
        return self._meal_names

    def _resolve_meal_name(self, meal: str) -> str:
        """Map our logical category to one of the account's real meal names."""
        names = self._account_meal_names()
        lower = {n.lower(): n for n in names}
        if meal == "breakfast":
            return lower.get("breakfast", names[0] if names else "Breakfast")
        if meal == "lunch":
            return lower.get("lunch", _nth(names, 1, "Lunch"))
        if meal == "dinner":
            return lower.get("dinner", _nth(names, 2, "Dinner"))
        # snack -> first configured name that looks like a snack slot.
        for n in names:
            if n.lower().startswith("snack"):
                return n
        # No snack slot configured: fall back to the last meal (closest to a
        # catch-all) rather than a wrong fixed name.
        return names[-1] if names else "Snacks"

    # -- health ---------------------------------------------------------------

    def health_check(self) -> BackendHealth:
        try:
            import httpx  # noqa: F401
        except ImportError:
            return BackendHealth(
                backend=self.name, reachable=False, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids,
                detail="httpx not installed (pip install -e \".[cookie]\")",
            )
        try:
            self._ensure_session()
        except AuthRequiredError as exc:
            return BackendHealth(
                backend=self.name, reachable=True, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids, detail=exc.message,
            )
        except (AuthFailedError, SessionExpiredError) as exc:
            return BackendHealth(
                backend=self.name, reachable=True, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids, detail=exc.message,
            )
        except Exception as exc:  # pragma: no cover - network dependent
            return BackendHealth(
                backend=self.name, reachable=False, authenticated=False,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids, detail=_redact(str(exc)),
            )
        # Verify a read without mutating.
        try:
            self.get_diary(dt.date.today())
        except Exception as exc:  # pragma: no cover - network dependent
            return BackendHealth(
                backend=self.name, reachable=True, authenticated=True,
                can_read=False, can_write=False,
                supports_stable_ids=self.supports_stable_ids,
                detail=f"token OK but diary read failed: {_redact(str(exc))}",
            )
        return BackendHealth(
            backend=self.name, reachable=True, authenticated=True,
            can_read=True, can_write=True,
            supports_stable_ids=self.supports_stable_ids,
            detail=f"browser session ({self._browser}) OK; read verified; "
                   "write capability not test-exercised",
        )

    # -- v2 API calls ---------------------------------------------------------

    def _get(self, path: str, params: dict) -> Any:
        self._ensure_session()
        try:
            resp = self._http.get(API_BASE + path, params=params, headers=self._headers())
        except Exception as exc:
            raise BackendUnavailableError(_redact(str(exc))) from exc
        if resp.status_code == 401:
            raise SessionExpiredError("v2 API returned 401 — browser session expired")
        if resp.status_code >= 400:
            raise _translate(Exception(f"{resp.status_code}: {resp.text[:300]}"))
        return resp.json()

    def search_food(self, query: str, limit: int = 25) -> list[FoodCandidate]:
        data = self._get(
            "/v2/search/nutrition",
            {
                "q": query, "scope": "all", "max_items": limit,
                "resource[]": "foods",
                "fields[]": ["id", "nutritional_contents", "serving_sizes",
                             "version", "brand_name", "description"],
            },
        )
        items = data.get("items", data.get("foods", []))
        return [self._xlate._to_candidate(_unwrap_item(r)) for r in items]

    def get_food(self, food_id: str) -> FoodCandidate | None:
        try:
            data = self._get(f"/v2/foods/{food_id}", {})
        except Exception:
            return None
        item = data.get("item") or data
        return self._xlate._to_candidate(item)

    def get_diary(self, date: dt.date) -> list[DiaryEntry]:
        data = self._get(
            "/v2/diary",
            {
                "entry_date": date.isoformat(),
                "types": "food_entry",
                "fields[]": ["food", "nutritional_contents", "serving_size",
                             "meal_name", "servings"],
            },
        )
        return [self._xlate._to_entry(r, date) for r in data.get("items", [])]

    def add_entry(
        self, *, date: dt.date, meal: str, food_id: str,
        serving: ServingSize, servings: float,
    ) -> DiaryEntry:
        self._ensure_session()
        # The web/v2 client ("mfp-main-js") expects type "food_entry" (NOT the
        # mobile client's "food") and the food's current version. Live testing
        # showed that sending type "food" caused the server to silently coerce
        # the meal to Dinner regardless of meal_name. We fetch the food to get
        # its version, then post the web-client shape.
        version = None
        food = self.get_food(food_id)
        if food is not None:
            version = food.version
        food_ref: dict[str, Any] = {"id": food_id}
        if version is not None:
            food_ref["version"] = version
        item = {
            "type": "food_entry",
            "date": date.isoformat(),
            "meal_name": self._resolve_meal_name(meal),
            "servings": servings,
            "food": food_ref,
            "serving_size": {
                "unit": serving.unit, "value": serving.value,
                "nutrition_multiplier": serving.nutrition_multiplier,
            },
        }
        try:
            resp = self._http.post(
                API_BASE + "/v2/diary", json={"items": [item]},
                headers=self._headers(json_body=True),
            )
        except Exception as exc:
            raise BackendUnavailableError(_redact(str(exc))) from exc
        if resp.status_code == 401:
            raise SessionExpiredError("v2 API returned 401 on write — session expired")
        if resp.status_code >= 400:
            raise _translate(Exception(f"{resp.status_code}: {resp.text[:300]}"))
        raw = resp.json()["items"][0]
        return self._xlate._to_entry(raw, date)

    def delete_entry(self, remote_id: str) -> None:
        self._ensure_session()
        try:
            resp = self._http.delete(API_BASE + f"/v2/diary/{remote_id}",
                                     headers=self._headers())
        except Exception as exc:
            raise BackendUnavailableError(_redact(str(exc))) from exc
        if resp.status_code == 401:
            raise SessionExpiredError("v2 API returned 401 on delete — session expired")
        if resp.status_code >= 400:
            raise _translate(Exception(f"{resp.status_code}: {resp.text[:300]}"))

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass
            self._http = None


def _nth(seq: list[str], idx: int, default: str) -> str:
    return seq[idx] if len(seq) > idx else default
