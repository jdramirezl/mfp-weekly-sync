"""Shared fixtures. All tests run against the deterministic mock backend —
never against a real MyFitnessPal account."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from mfp_sync.backends.mock import MockBackend
from mfp_sync.config import Settings, SyncConfig
from mfp_sync.reconciliation import Reconciler
from mfp_sync.resolver import Resolver
from mfp_sync.storage import Storage


@pytest.fixture
def mock_backend() -> MockBackend:
    # Zero rate-limit wait in tests.
    return MockBackend()


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    s = Storage(tmp_path / "state.sqlite")
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        backend="mock",
        home=tmp_path,
        sync=SyncConfig(on_conflict="ask", max_retries=3,
                        retry_base_delay_seconds=0.0, min_write_interval_seconds=0.0),
    )


@pytest.fixture
def resolver(mock_backend, storage, settings) -> Resolver:
    return Resolver(mock_backend, storage, settings.matching)


@pytest.fixture
def reconciler(mock_backend, storage, resolver, settings) -> Reconciler:
    return Reconciler(mock_backend, storage, resolver, settings)


@pytest.fixture
def monday() -> dt.date:
    return dt.date(2026, 10, 19)
