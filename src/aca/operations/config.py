"""Explicit TOML configuration with a small, fail-closed environment surface."""

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from aca.domain.values import FinancialDecimal


class ConfigurationError(ValueError):
    """Safe-to-display configuration failure; never embeds input values."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class RiskPolicy(StrictModel):
    """Provisional policy v1 is fixed until a separately reviewed version is implemented."""

    version: Literal["provisional-v1"] = "provisional-v1"
    soft_drawdown: FinancialDecimal = Decimal("0.05")
    hard_drawdown: FinancialDecimal = Decimal("0.10")
    daily_loss: FinancialDecimal = Decimal("0.02")
    planned_risk_per_trade: FinancialDecimal = Decimal("0.005")
    total_exposure: FinancialDecimal = Decimal("0.30")
    asset_exposure: FinancialDecimal = Decimal("0.15")
    aggregate_planned_risk: FinancialDecimal = Decimal("0.01")
    max_positions: Literal[2] = 2
    leverage: Literal[0] = 0
    automatic_hard_halt_restart: Literal[False] = False
    global_cancel_timer: Literal[False] = False

    @field_validator("leverage", "max_positions", mode="before")
    @classmethod
    def exact_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("integer policy fields require integers")
        return value

    @field_validator("automatic_hard_halt_restart", "global_cancel_timer", mode="before")
    @classmethod
    def exact_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("boolean policy fields require booleans")
        return value

    @model_validator(mode="after")
    def fixed_policy(self) -> Self:
        expected = {
            "soft_drawdown": "0.05",
            "hard_drawdown": "0.10",
            "daily_loss": "0.02",
            "planned_risk_per_trade": "0.005",
            "total_exposure": "0.30",
            "asset_exposure": "0.15",
            "aggregate_planned_risk": "0.01",
        }
        if any(getattr(self, key) != Decimal(value) for key, value in expected.items()):
            raise ValueError(
                "policy v1 limits are immutable; a policy change needs milestone review"
            )
        return self


class DatabaseSettings(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = Field(default="aca", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    runtime_user: Literal["aca_runtime"] = "aca_runtime"
    migration_user: Literal["aca_migrator"] = "aca_migrator"
    admin_user: Literal["aca_admin"] = "aca_admin"
    runtime_password_file: str = ".secrets/runtime_password"  # noqa: S105 - path, not a secret
    migration_password_file: str = ".secrets/migration_password"  # noqa: S105 - path, not a secret
    connect_timeout_seconds: int = Field(default=3, ge=1, le=10)


class HealthSettings(StrictModel):
    host: Literal["127.0.0.1", "0.0.0.0", "::1"] = "127.0.0.1"  # noqa: S104
    port: int = Field(default=8000, ge=1, le=65535)
    database_check_interval_seconds: int = Field(default=2, ge=1, le=30)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )
    schema_version: Literal[1] = 1
    mode: Literal["paper"] = "paper"
    environment: Literal["development", "paper"] = "development"
    base_currency: Literal["GBP"] = "GBP"
    instruments: tuple[Literal["BTC/GBP", "ETH/GBP"], ...] = ("BTC/GBP", "ETH/GBP")
    log_level: Literal["INFO", "WARNING", "ERROR"] = "INFO"
    paper_storage: Literal["bounded-2gb", "observation-8gb"] = Field(
        default="bounded-2gb", exclude_if=lambda v: v == "bounded-2gb"
    )

    @property
    def paper_database_budget(self) -> int:
        return 8_000_000_000 if self.paper_storage == "observation-8gb" else 2_000_000_000

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    health: HealthSettings = Field(default_factory=HealthSettings)
    risk: RiskPolicy = Field(default_factory=RiskPolicy)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version requires an integer")
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Only load_settings may interpret environment or files. No hidden precedence.
        return (init_settings,)

    @model_validator(mode="after")
    def approved_instruments(self) -> Self:
        if not self.instruments or len(set(self.instruments)) != len(self.instruments):
            raise ValueError("instrument list must be nonempty and contain no duplicates")
        return self

    def document(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def fingerprint(self) -> str:
        payload = json.dumps(self.document(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_settings(
    path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    permitted = {"ACA_CONFIG", "ACA_MODE"}
    if any(key.startswith("ACA_") and key not in permitted for key in env):
        raise ConfigurationError("unsupported ACA_ environment variable")
    configured_path = path or (Path(env["ACA_CONFIG"]) if "ACA_CONFIG" in env else None)
    data: dict[str, object] = {}
    if configured_path is not None:
        try:
            with configured_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError("configuration file is unavailable or invalid TOML") from exc
    # TOML arrays have no immutable tuple representation; this is the only collection conversion.
    instruments = data.get("instruments")
    if isinstance(instruments, list):
        data["instruments"] = tuple(instruments)
    if "ACA_MODE" in env:
        data["mode"] = env["ACA_MODE"]
    return Settings.model_validate(data)
