"""Independent arithmetic for descriptive legacy reporting, never promotion."""

import math

import pytest

from scripts.research_performance_report import series_metrics


def test_ratios_use_pooled_returns_and_drawdown_uses_bar_observations() -> None:
    # Daily endpoints recover to 1.08; the intra-day bar at .88 sets a 20% drawdown.
    daily = {"2020-01-01": 0.1, "2020-01-02": -0.1, "2020-01-03": 1.08 / 0.99 - 1}
    result = series_metrics(daily, [1.0, 1.1, 0.88, 0.99, 1.08], 3)
    annual = 1.08 ** (365.25 / 3) - 1
    mean = sum(daily.values()) / 3
    downside = math.sqrt(0.1**2 / 3 * 365.25)
    assert result["total_return"] == pytest.approx(0.08)
    assert result["annualised_return"] == pytest.approx(annual)
    assert result["max_drawdown"] == pytest.approx(0.2)
    assert result["calmar"] == pytest.approx(annual / 0.2)
    assert result["sortino"] == pytest.approx(mean * 365.25 / downside)


def test_zero_return_cash_has_undefined_ratios_and_zero_drawdown() -> None:
    result = series_metrics({"2020-01-01": 0, "2020-01-02": 0}, [1, 1, 1], 2)
    assert result["total_return"] == result["max_drawdown"] == 0
    assert result["sortino"] is result["calmar"] is result["sharpe_zero_cash_yield"] is None


def test_losses_chain_across_fold_boundaries_without_resetting_report_high_water() -> None:
    result = series_metrics({"2020-01-01": -0.1, "2020-01-02": -0.1}, [1, 0.9, 0.81], 2)
    assert result["total_return"] == pytest.approx(-0.19)
    assert result["max_drawdown"] == pytest.approx(0.19)
    assert result["annualised_return"] < 0
    assert result["sortino"] < 0
    assert result["calmar"] < 0


@pytest.mark.parametrize(
    "returns,units,days",
    [
        ({}, [1], 1),
        ({"a": 0}, [1], 0),
        ({"a": float("nan")}, [1], 1),
        ({"a": -1}, [1], 1),
        ({"a": 0}, [0], 1),
        ({"a": 0}, [1.1], 1),
    ],
)
def test_invalid_or_inconsistent_curves_fail(
    returns: dict[str, float], units: list[float], days: float
) -> None:
    with pytest.raises(ValueError):
        series_metrics(returns, units, days)
