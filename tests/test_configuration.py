from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

from aca.domain.values import (
    FINANCIAL_CONTEXT,
    FinancialDecimal,
    UtcDateTime,
    parse_decimal,
    utc_datetime,
)
from aca.operations.cli import main
from aca.operations.config import ConfigurationError, RiskPolicy, Settings, load_settings


def test_defaults_cannot_enable_trading() -> None:
    config = load_settings(environ={})
    assert config.mode == "paper"
    assert config.risk.planned_risk_per_trade == Decimal("0.005")
    assert config.risk.automatic_hard_halt_restart is False
    assert config.risk.global_cancel_timer is False


@pytest.mark.parametrize("value", ["live", "production", "true", "1", "PAPER"])
def test_mode_environment_fails_closed(value: str) -> None:
    with pytest.raises(ValidationError):
        load_settings(environ={"ACA_MODE": value})


@pytest.mark.parametrize("key", ["ACA_LIVE", "ACA_TRADING_ENABLED", "ACA_RISK__TOTAL_EXPOSURE"])
def test_unknown_environment_rejected(key: str) -> None:
    with pytest.raises(ConfigurationError):
        load_settings(environ={key: "true"})


@pytest.mark.parametrize(
    "config",
    [
        {"mode": "live"},
        {"live_enabled": True},
        {"base_currency": "USD"},
        {"instruments": ("SOL/GBP",)},
        {"instruments": ("BTC/GBP", "BTC/GBP")},
        {"instruments": ()},
        {"environment": "production"},
    ],
)
def test_invalid_configuration_rejected(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(config)


@pytest.mark.parametrize(
    "changes",
    [
        {"planned_risk_per_trade": "0.006"},
        {"total_exposure": "1"},
        {"hard_drawdown": "0.11"},
        {"soft_drawdown": "0.04"},
        {"daily_loss": "0.03"},
        {"asset_exposure": "0.20"},
        {"aggregate_planned_risk": "0.02"},
        {"leverage": 2},
        {"max_positions": 3},
        {"automatic_hard_halt_restart": True},
        {"global_cancel_timer": True},
        {"planned_risk_per_trade": 0.005},
        {"leverage": False},
        {"automatic_hard_halt_restart": 0},
        {"global_cancel_timer": 0},
    ],
)
def test_policy_requires_reviewed_version(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RiskPolicy.model_validate(changes)


def test_policy_and_nested_settings_are_immutable() -> None:
    config = Settings()
    with pytest.raises(ValidationError):
        config.risk.total_exposure = Decimal("1")
    with pytest.raises(ValidationError):
        config.database.host = "elsewhere"


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_schema_version_does_not_coerce_equal_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"schema_version": value})


def test_toml_load_and_stable_fingerprint() -> None:
    config = load_settings(Path("configs/development.toml"), environ={})
    assert config.fingerprint() == Settings().fingerprint()
    assert config.document()["mode"] == "paper"


def test_explicit_path_beats_config_environment(configuration_file: Path) -> None:
    config = load_settings(configuration_file, environ={"ACA_CONFIG": "/does-not-exist"})
    assert config.mode == "paper"


def test_base_settings_has_no_hidden_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv("DATABASE", '{"host":"wrong"}')
    assert Settings().database.host == "127.0.0.1"


@pytest.mark.parametrize("value", [1, True, 0.1, float("nan"), "NaN", "Infinity", "1e20", "1e-19"])
def test_inexact_or_nonfinite_financial_inputs(value: object) -> None:
    with pytest.raises(ValueError):
        parse_decimal(value)


@settings(max_examples=1000, derandomize=True, print_blob=True)
@given(
    st.decimals(
        min_value="-9999999999",
        max_value="9999999999",
        places=18,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_decimal_round_trip_is_exact(value: Decimal) -> None:
    assert parse_decimal(str(value)) == value


@settings(max_examples=1000, derandomize=True, print_blob=True)
@given(st.floats())
def test_every_float_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        parse_decimal(value)


@settings(max_examples=1000, derandomize=True, print_blob=True)
@given(
    sign=st.integers(0, 1),
    digits=st.lists(st.integers(0, 9), min_size=1, max_size=200),
    exponent=st.integers(-200, 200),
)
def test_decimal_representation_check_matches_independent_quantization(
    sign: int, digits: list[int], exponent: int
) -> None:
    value = Decimal((sign, tuple(digits), exponent))
    with localcontext(FINANCIAL_CONTEXT):
        try:
            valid = abs(value) < Decimal("1e20") and value == value.quantize(Decimal("1e-18"))
        except InvalidOperation:
            valid = False
    # The result does not depend on the ambient precision or exponent limits.
    with localcontext() as ambient:
        ambient.prec = 3
        ambient.Emin, ambient.Emax = -9, 9
        if valid:
            assert parse_decimal(value).as_tuple() == value.as_tuple()
            assert parse_decimal(str(value)).as_tuple() == value.as_tuple()
        else:
            with pytest.raises(ValueError):
                parse_decimal(value)


@pytest.mark.parametrize(
    "text",
    [
        "99999999999999999999.9999999999999999990000",
        "-99999999999999999999.9999999999999999990000",
        "0e-999999999",
        "-0e999999999",
        "1000e-21",
    ],
)
def test_decimal_range_accepts_exact_trailing_zeroes_without_rounding(text: str) -> None:
    assert parse_decimal(text).as_tuple() == Decimal(text).as_tuple()


@pytest.mark.parametrize(
    "text",
    ["1e999999999", "1e-999999999", "1.0000000000000000001", "-1e20", "NaN", "sNaN"],
)
def test_decimal_range_rejects_extreme_or_inexact_representations(text: str) -> None:
    with pytest.raises(ValueError):
        parse_decimal(text)


def test_domain_validation_rejects_naive_time_and_normalises_aware_time() -> None:
    class Boundary(BaseModel):
        amount: FinancialDecimal
        at: UtcDateTime

    with pytest.raises(ValidationError):
        Boundary.model_validate({"amount": "1.00", "at": datetime(2026, 1, 1)})
    at = datetime(2026, 7, 1, 12, tzinfo=timezone(timedelta(hours=1)))
    result = Boundary.model_validate({"amount": "1.00", "at": at})
    assert result.at == datetime(2026, 7, 1, 11, tzinfo=UTC)
    assert result.amount == Decimal("1.00")
    with pytest.raises(ValueError):
        utc_datetime("2026-01-01")


def test_cli_never_echoes_invalid_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "bad.toml"
    config.write_text('password = "sensitive-input-must-not-appear"\nmode = "live"')
    assert main(["--config", str(config), "config", "check"]) == 2
    output = capsys.readouterr()
    assert "sensitive-input" not in output.out + output.err


def test_no_live_command_registered() -> None:
    with pytest.raises(SystemExit) as error:
        main(["live"])
    assert error.value.code == 2
