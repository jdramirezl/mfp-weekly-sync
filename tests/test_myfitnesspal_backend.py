"""Regression tests for the live MyFitnessPal backend adapter.

These never touch the network or a real account. They exercise the dict→value
translation, error mapping/redaction, and the health-check branching (including
the credential-free identity-API probe added during live validation).

They are skipped only if `mfp_api` is not importable, so they run in any env
where the live dependency is installed.
"""

from __future__ import annotations

import datetime as dt

import pytest

from mfp_sync.backends.myfitnesspal import (
    MyFitnessPalBackend,
    _energy,
    _redact,
    _translate,
    _unwrap_item,
)
from mfp_sync.config import Settings
from mfp_sync.errors import (
    AuthFailedError,
    BackendUnavailableError,
    SessionExpiredError,
)

mfp_api = pytest.importorskip("mfp_api")


# -- pure helpers (no network) ------------------------------------------------


def test_unwrap_item_handles_both_shapes():
    assert _unwrap_item({"item": {"id": "1"}}) == {"id": "1"}
    assert _unwrap_item({"id": "2"}) == {"id": "2"}


def test_energy_from_dict_and_scalar():
    assert _energy({"energy": {"value": 150.0, "unit": "calories"}}) == 150.0
    assert _energy({"energy": 120}) == 120.0
    assert _energy({"calories": 90}) == 90.0
    assert _energy({}) is None


def test_redact_strips_bearer_and_password():
    assert "<redacted>" in _redact("Authorization: Bearer abc.def.ghi")
    red = _redact('{"password": "hunter2"}')
    assert "hunter2" not in red


def test_translate_maps_statuses():
    assert isinstance(_translate(Exception("HTTP 401 Unauthorized")), SessionExpiredError)
    assert isinstance(_translate(Exception("session expired")), SessionExpiredError)
    assert isinstance(_translate(Exception("login failed: 403")), AuthFailedError)
    assert isinstance(_translate(Exception("connection reset")), BackendUnavailableError)


# -- translation of API dict shapes ------------------------------------------


def _backend() -> MyFitnessPalBackend:
    return MyFitnessPalBackend(Settings(backend="myfitnesspal"))


def test_to_candidate_translates_search_shape():
    raw = {
        "id": 123,
        "description": "Banana, raw",
        "brand_name": "Generic",
        "version": "7",
        "serving_sizes": [
            {"unit": "g", "value": 1.0, "nutrition_multiplier": 1.0,
             "gram_weight": 1.0, "id": "sg"},
            {"unit": "medium", "value": 1.0, "nutrition_multiplier": 118.0,
             "gram_weight": 118.0, "id": "sm"},
        ],
        "nutritional_contents": {
            "energy": {"value": 0.89, "unit": "calories"},
            "protein": 0.0109, "carbohydrates": 0.228, "fat": 0.0033,
        },
    }
    cand = _backend()._to_candidate(raw)
    assert cand.food_id == "123"
    assert cand.description == "Banana, raw"
    assert cand.brand == "Generic"
    assert cand.version == "7"
    assert len(cand.serving_sizes) == 2
    assert cand.serving_sizes[1].unit == "medium"
    assert cand.serving_sizes[1].gram_weight == 118.0
    assert cand.nutrition.calories == 0.89
    assert cand.nutrition.protein_g == 0.0109


def test_to_entry_translates_diary_shape_with_stable_id():
    raw = {
        "id": "uuid-abc-123",
        "type": "food_entry",
        "meal_name": "Snacks",
        "servings": 2.0,
        "food": {"id": "f99", "description": "Banana, raw"},
        "serving_size": {"unit": "medium", "value": 1.0, "id": "sm"},
        "nutritional_contents": {
            "energy": {"value": 210.0}, "protein": 2.6, "carbohydrates": 54.0, "fat": 0.8,
        },
    }
    entry = _backend()._to_entry(raw, dt.date(2026, 10, 19))
    assert entry.remote_id == "uuid-abc-123"   # stable id preserved
    assert entry.meal == "snack"               # MFP "Snacks" -> our "snack"
    assert entry.food_id == "f99"
    assert entry.servings == 2.0
    assert entry.serving_unit == "medium"
    assert entry.calories == 210.0
    # fingerprint is stable and excludes nutrition
    assert "banana" not in entry.fingerprint().lower()
    assert entry.fingerprint() == entry.fingerprint()


# -- health check branching (no network for the no-creds path) ----------------


def test_health_without_credentials_probes_identity_api(monkeypatch):
    backend = _backend()
    # Stub the identity probe so this test needs no network.
    monkeypatch.setattr(
        backend, "_probe_identity_api", lambda: (True, "stubbed probe OK")
    )
    health = backend.health_check()
    assert health.authenticated is False
    assert health.can_read is False
    assert health.reachable is True
    assert "credentials not configured" in health.detail
    assert "stubbed probe OK" in health.detail


def test_health_without_credentials_reports_probe_failure(monkeypatch):
    backend = _backend()
    monkeypatch.setattr(
        backend, "_probe_identity_api", lambda: (False, "simulated API down")
    )
    health = backend.health_check()
    assert health.reachable is False
    assert "mobile identity API probe failed" in health.detail
    assert "simulated API down" in health.detail
