"""Diary backends: an abstract contract plus a mock and a live implementation."""

from __future__ import annotations

from ..config import Settings
from ..errors import BackendUnavailableError
from .base import DiaryBackend, DiaryEntry, FoodCandidate, ServingSize
from .mock import MockBackend

__all__ = [
    "DiaryBackend",
    "DiaryEntry",
    "FoodCandidate",
    "ServingSize",
    "MockBackend",
    "build_backend",
]


def build_backend(settings: Settings) -> DiaryBackend:
    """Instantiate the configured backend.

    The mock backend is always available. The live MyFitnessPal backend is
    imported lazily so the package works without `mfp-api` installed.
    """
    if settings.backend == "mock":
        return MockBackend(db_path=settings.home / "mock_backend.sqlite")
    if settings.backend == "myfitnesspal":
        try:
            from .myfitnesspal import MyFitnessPalBackend
        except ImportError as exc:  # pragma: no cover - env dependent
            raise BackendUnavailableError(
                "the 'myfitnesspal' backend requires the mfp-api package:\n"
                '  pip install "git+https://github.com/Rift-Walker/mfp-api"'
            ) from exc
        return MyFitnessPalBackend(settings)
    if settings.backend == "cookie-session":
        try:
            from .cookie_session import CookieSessionBackend
        except ImportError as exc:  # pragma: no cover - env dependent
            raise BackendUnavailableError(
                "the 'cookie-session' backend requires cryptography + httpx:\n"
                '  pip install -e ".[cookie]"'
            ) from exc
        return CookieSessionBackend(settings)
    raise BackendUnavailableError(f"unknown backend {settings.backend!r}")
