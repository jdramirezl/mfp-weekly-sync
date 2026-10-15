"""Configuration loading and precedence.

Precedence (highest wins): CLI flags > environment variables > settings.toml >
built-in defaults. CLI flags are applied by the caller after load_settings()
returns, by calling Settings.with_overrides(...).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore

from .errors import ConfigError

DEFAULT_HOME = ".mfp-sync"
VALID_BACKENDS = {"mock", "myfitnesspal", "cookie-session"}
VALID_CONFLICT = {"skip", "ask", "overwrite"}


@dataclass(frozen=True)
class NutritionConfig:
    calorie_tolerance: float = 0.15
    protein_tolerance: float = 0.20
    carb_tolerance: float = 0.20
    fat_tolerance: float = 0.20


@dataclass(frozen=True)
class MatchingConfig:
    auto_accept_score: float = 0.92
    max_candidates: int = 8


@dataclass(frozen=True)
class SyncConfig:
    on_conflict: str = "ask"
    max_retries: int = 3
    retry_base_delay_seconds: float = 1.0
    min_write_interval_seconds: float = 0.5


@dataclass(frozen=True)
class Settings:
    backend: str = "mock"
    home: Path = Path(DEFAULT_HOME)
    nutrition: NutritionConfig = NutritionConfig()
    matching: MatchingConfig = MatchingConfig()
    sync: SyncConfig = SyncConfig()

    # Credentials are read from the environment only; never persisted here.
    mfp_username: str | None = None
    mfp_password: str | None = None

    def validate(self) -> None:
        if self.backend not in VALID_BACKENDS:
            raise ConfigError(
                f"invalid backend {self.backend!r}; expected one of {sorted(VALID_BACKENDS)}"
            )
        if self.sync.on_conflict not in VALID_CONFLICT:
            raise ConfigError(
                f"invalid on_conflict {self.sync.on_conflict!r}; "
                f"expected one of {sorted(VALID_CONFLICT)}"
            )

    @property
    def db_path(self) -> Path:
        return self.home / "state.sqlite"

    @property
    def session_path(self) -> Path:
        return self.home / "session.json"

    def with_overrides(
        self,
        *,
        backend: str | None = None,
        home: str | None = None,
        on_conflict: str | None = None,
    ) -> Settings:
        new = self
        if backend is not None:
            new = replace(new, backend=backend)
        if home is not None:
            new = replace(new, home=Path(home).expanduser())
        if on_conflict is not None:
            new = replace(new, sync=replace(new.sync, on_conflict=on_conflict))
        return new


def _load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:  # type: ignore[attr-defined]
        raise ConfigError(f"could not parse {path}: {exc}") from exc


def load_settings(config_path: Path | None = None) -> Settings:
    """Load settings from (optional) TOML, then layer environment variables."""
    data: dict = {}
    if config_path is not None:
        data = _load_toml(config_path)
    else:
        # Try the conventional location, but it's entirely optional.
        default_path = Path("config/settings.toml")
        if default_path.exists():
            data = _load_toml(default_path)

    nut = data.get("nutrition", {})
    match = data.get("matching", {})
    syn = data.get("sync", {})

    backend = os.environ.get("MFP_SYNC_BACKEND", data.get("backend", "mock"))
    home_raw = os.environ.get("MFP_SYNC_HOME", data.get("home", DEFAULT_HOME))

    settings = Settings(
        backend=backend,
        home=Path(home_raw).expanduser(),
        nutrition=NutritionConfig(
            calorie_tolerance=float(nut.get("calorie_tolerance", 0.15)),
            protein_tolerance=float(nut.get("protein_tolerance", 0.20)),
            carb_tolerance=float(nut.get("carb_tolerance", 0.20)),
            fat_tolerance=float(nut.get("fat_tolerance", 0.20)),
        ),
        matching=MatchingConfig(
            auto_accept_score=float(match.get("auto_accept_score", 0.92)),
            max_candidates=int(match.get("max_candidates", 8)),
        ),
        sync=SyncConfig(
            on_conflict=syn.get("on_conflict", "ask"),
            max_retries=int(syn.get("max_retries", 3)),
            retry_base_delay_seconds=float(syn.get("retry_base_delay_seconds", 1.0)),
            min_write_interval_seconds=float(syn.get("min_write_interval_seconds", 0.5)),
        ),
        mfp_username=os.environ.get("MFP_USERNAME"),
        mfp_password=os.environ.get("MFP_PASSWORD"),
    )
    settings.validate()
    return settings
