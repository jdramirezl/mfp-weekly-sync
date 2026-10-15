"""Regression tests for the cookie-session backend and Chrome cookie reader.

No network and no real Keychain access: the cookie extraction and the HTTP
client are stubbed. These lock in the token-exchange logic, the v2 translation
reuse, stable-id round-tripping, and health-check branching that live
validation relied on.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mfp_sync.browser_cookies import has_mfp_session
from mfp_sync.config import Settings

pytest.importorskip("httpx")
import httpx  # noqa: E402

from mfp_sync.backends.cookie_session import CookieSessionBackend  # noqa: E402


def test_has_mfp_session_detects_markers():
    assert has_mfp_session({"__Secure-next-auth.session-token": "x"})
    assert has_mfp_session({"_mfp_session": "x"})
    assert not has_mfp_session({"__Host-next-auth.csrf-token": "x", "_ga": "y"})


class _StubResponse:
    def __init__(self, status=200, json_data=None, content_type="application/json", text=""):
        self.status_code = status
        self._json = json_data or {}
        self.headers = {"Content-Type": content_type}
        self.text = text

    def json(self):
        return self._json


class _StubHttp:
    """Mimics the httpx.Client surface the backend uses."""

    def __init__(self, routes):
        # routes: dict[(method, url_substr)] -> _StubResponse or callable
        self.routes = routes
        self.cookies = httpx.Cookies()
        self.calls = []

    def _match(self, method, url):
        for (m, sub), resp in self.routes.items():
            if m == method and sub in url:
                return resp
        raise AssertionError(f"no stub route for {method} {url}")

    def get(self, url, params=None, headers=None):
        self.calls.append(("GET", url))
        r = self._match("GET", url)
        return r(url, params) if callable(r) else r

    def post(self, url, json=None, headers=None):
        self.calls.append(("POST", url, json))
        r = self._match("POST", url)
        return r(url, json) if callable(r) else r

    def delete(self, url, headers=None):
        self.calls.append(("DELETE", url))
        return self._match("DELETE", url)

    def close(self):
        pass


def _backend_with_session(monkeypatch, http: _StubHttp) -> CookieSessionBackend:
    b = CookieSessionBackend(Settings(backend="cookie-session"))
    # Skip real cookie extraction.
    monkeypatch.setattr(
        "mfp_sync.backends.cookie_session.extract_mfp_cookies",
        lambda browser="Chrome": {"_mfp_session": "stub", "__Secure-next-auth.session-token": "s"},
    )
    b._http = http
    return b


def test_token_exchange_then_read(monkeypatch):
    diary_entry = {
        "id": "uuid-live-1", "meal_name": "Snacks", "servings": 1.0,
        "food": {"id": "98000210537901", "description": "Banana (One)"},
        "serving_size": {"unit": "g", "value": 118.0, "id": "53772637115901"},
        "nutritional_contents": {"energy": {"value": 105.0}, "protein": 1.0,
                                 "carbohydrates": 27.0, "fat": 0.4},
    }
    http = _StubHttp({
        ("GET", "/user/auth_token"): _StubResponse(
            json_data={"access_token": "tok-abc", "user_id": "42", "token_type": "Bearer"}),
        ("GET", "/v2/diary"): _StubResponse(json_data={"items": [diary_entry]}),
    })
    b = _backend_with_session(monkeypatch, http)
    entries = b.get_diary(dt.date(2026, 10, 19))
    assert len(entries) == 1
    e = entries[0]
    assert e.remote_id == "uuid-live-1"       # stable id preserved
    assert e.meal == "snack"
    assert e.calories == 105.0
    # token + user id were captured from the exchange
    assert b._access_token == "tok-abc"
    assert b._user_id == "42"


def test_stale_session_returns_html_raises_auth_failed(monkeypatch):
    http = _StubHttp({
        ("GET", "/user/auth_token"): _StubResponse(
            status=200, content_type="text/html", text="<html>login</html>"),
    })
    b = _backend_with_session(monkeypatch, http)
    from mfp_sync.errors import AuthFailedError

    with pytest.raises(AuthFailedError):
        b.get_diary(dt.date(2026, 10, 19))


def test_add_entry_round_trips_stable_id(monkeypatch):
    created = {
        "id": "uuid-new-9", "meal_name": "Snacks", "servings": 1.0,
        "food": {"id": "98000210537901", "description": "Banana (One)"},
        "serving_size": {"unit": "g", "value": 118.0},
        "nutritional_contents": {"energy": {"value": 105.0}, "protein": 1.0},
    }
    food_record = {
        "item": {
            "id": "98000210537901", "description": "Banana (One)", "version": "99",
            "serving_sizes": [{"unit": "g", "value": 118.0, "id": "S118",
                               "gram_weight": 118.0}],
            "nutritional_contents": {"energy": {"value": 105.0}, "protein": 1.0},
        }
    }
    http = _StubHttp({
        ("GET", "/user/auth_token"): _StubResponse(
            json_data={"access_token": "t", "user_id": "1"}),
        ("GET", "/v2/users/"): _StubResponse(json_data={
            "item": {"diary_preferences": {"meal_names": [
                "Breakfast", "Lunch", "Dinner",
                "Snacks Morning", "Snacks Noon", "Snacks Night"]}}}),
        ("GET", "/v2/foods/"): _StubResponse(json_data=food_record),
        ("POST", "/v2/diary"): _StubResponse(status=201, json_data={"items": [created]}),
    })
    b = _backend_with_session(monkeypatch, http)
    from mfp_sync.backends.base import ServingSize

    entry = b.add_entry(
        date=dt.date(2026, 10, 19), meal="snack", food_id="98000210537901",
        serving=ServingSize(unit="g", value=118.0, nutrition_multiplier=1.0),
        servings=1.0,
    )
    assert entry.remote_id == "uuid-new-9"
    # Regression (live bugs): the web/v2 client must send type "food_entry"
    # (not "food") WITH the food version, and the meal_name must be resolved
    # against the ACCOUNT's configured meal names — this account has three
    # snack slots, so logical "snack" must map to "Snacks Morning", not the
    # literal "Snacks" (which the server would coerce to Dinner).
    post_call = next(c for c in http.calls if c[0] == "POST")
    body = post_call[2]["items"][0]
    assert body["type"] == "food_entry"
    assert body["meal_name"] == "Snacks Morning"
    assert body["food"] == {"id": "98000210537901", "version": "99"}
    assert body["serving_size"] == {"unit": "g", "value": 118.0, "nutrition_multiplier": 1.0}


def test_meal_name_resolution_against_account(monkeypatch):
    http = _StubHttp({
        ("GET", "/user/auth_token"): _StubResponse(
            json_data={"access_token": "t", "user_id": "1"}),
        ("GET", "/v2/users/"): _StubResponse(json_data={
            "item": {"diary_preferences": {"meal_names": [
                "Breakfast", "Lunch", "Dinner",
                "Snacks Morning", "Snacks Noon", "Snacks Night"]}}}),
    })
    b = _backend_with_session(monkeypatch, http)
    assert b._resolve_meal_name("breakfast") == "Breakfast"
    assert b._resolve_meal_name("lunch") == "Lunch"
    assert b._resolve_meal_name("dinner") == "Dinner"
    assert b._resolve_meal_name("snack") == "Snacks Morning"  # first snack slot


def test_meal_name_resolution_default_account(monkeypatch):
    http = _StubHttp({
        ("GET", "/user/auth_token"): _StubResponse(
            json_data={"access_token": "t", "user_id": "1"}),
        ("GET", "/v2/users/"): _StubResponse(json_data={
            "item": {"diary_preferences": {"meal_names": [
                "Breakfast", "Lunch", "Dinner", "Snacks"]}}}),
    })
    b = _backend_with_session(monkeypatch, http)
    assert b._resolve_meal_name("snack") == "Snacks"


def test_401_maps_to_session_expired(monkeypatch):
    http = _StubHttp({
        ("GET", "/user/auth_token"): _StubResponse(
            json_data={"access_token": "t", "user_id": "1"}),
        ("GET", "/v2/diary"): _StubResponse(status=401, text="unauthorized"),
    })
    b = _backend_with_session(monkeypatch, http)
    from mfp_sync.errors import SessionExpiredError

    with pytest.raises(SessionExpiredError):
        b.get_diary(dt.date(2026, 10, 19))
