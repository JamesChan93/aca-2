"""Research invariants and independent financial/timing fixtures."""

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aca.domain.market import Candle, InstrumentRules
from aca.domain.values import FINANCIAL_CONTEXT
from aca.features.indicators import atr, sma
from aca.operations.config import RiskPolicy
from aca.research.economics import fixed_cost_gate, project
from aca.research.protocol import TRIALS, months, walk_forward
from aca.simulation.reference import CostModel, simulate
from aca.sizing.long_spot import round_increment, size_long
from aca.strategies.slow import StrategySignal, signal

D = Decimal
START = datetime(2019, 1, 1, tzinfo=UTC)
OBSERVED = datetime(2030, 1, 1, tzinfo=UTC)


def bars(prices: list[int], *, interval: int = 1440) -> tuple[Candle, ...]:
    return tuple(
        Candle.model_validate(
            {
                "venue": "fake",
                "symbol": "BTC/GBP",
                "interval_minutes": interval,
                "start": START + timedelta(minutes=index * interval),
                "end": START + timedelta(minutes=(index + 1) * interval),
                "open": str(price),
                "high": str(price + 1),
                "low": str(price - 1),
                "close": str(price),
                "volume": "10000",
                "trade_count": 100,
                "complete": True,
                "quality": (),
                "source": "synthetic_test",
                "observed_at": OBSERVED,
            }
        )
        for index, price in enumerate(prices)
    )


def rules() -> InstrumentRules:
    return InstrumentRules(
        venue="fake",
        symbol="BTC/GBP",
        venue_symbol="FAKE-BTCGBP",
        rest_pair="fake",
        price_increment=D(".01"),
        quantity_increment=D(".00000001"),
        minimum_quantity=D(".00006"),
        minimum_cost=D(".43"),
        status="online",
        observed_at=START,
    )


def always_long(prefix: tuple[Candle, ...], variant: str, at: datetime) -> StrategySignal:
    return StrategySignal.model_validate(
        {
            "strategy": variant,
            "symbol": "BTC/GBP",
            "decision_at": at,
            "data_watermark": prefix[-1].end,
            "desired": "long",
            "stop_distance": D(5),
            "expires_at": at + timedelta(seconds=2),
        }
    )


def test_indicators_and_prior_only_channel() -> None:
    data = bars(list(range(100, 300)))
    assert sma(data, 200) == D("199.5")
    assert atr(data) == D(2)
    at = data[-1].end + timedelta(seconds=10)
    assert signal(data, "donchian55-20", at).desired == "hold"
    last = data[-1].model_copy(update={"close": D(400), "high": D(500)})
    assert signal((*data[:-1], last), "donchian55-20", at).desired == "long"


@pytest.mark.parametrize("variant", ["sma200", "sma100", "momentum90", "momentum60"])
def test_pure_signals_and_venue_independence(variant: str) -> None:
    data = bars(list(range(100, 300)))
    at = data[-1].end + timedelta(seconds=10)
    original = signal(data, variant, at)  # type: ignore[arg-type]
    other = tuple(bar.model_copy(update={"venue": "other"}) for bar in data)
    assert signal(other, variant, at) == original  # type: ignore[arg-type]
    assert original.desired == "long"
    with pytest.raises(ValueError, match="unsafe"):
        signal(data, variant, data[-1].end)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="warm-up"):
        signal(data[:199], variant, at)  # type: ignore[arg-type]


def test_future_candles_do_not_change_past_signal() -> None:
    data = bars(list(range(100, 302)))
    at = data[199].end + timedelta(seconds=10)
    with pytest.raises(ValueError, match="unsafe"):
        signal(data, "sma200", at)
    assert signal(data[:200], "sma200", at) == signal(data[:200], "sma200", at)


def test_strategy_import_boundaries() -> None:
    allowed = {"collections", "datetime", "decimal", "typing", "pydantic"}
    for directory in ("src/aca/strategies", "src/aca/features"):
        for path in Path(directory).glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    assert module.split(".")[0] in allowed or module.startswith(
                        ("aca.domain.", "aca.features.")
                    )
                elif isinstance(node, ast.Import):
                    assert all(item.name.split(".")[0] in allowed for item in node.names)


@settings(max_examples=1000, derandomize=True)
@given(
    equity=st.integers(100, 100_000),
    price=st.integers(100, 100_000),
    stop_percent=st.integers(5, 80),
)
def test_sizing_never_relaxes_risk_or_minimums(equity: int, price: int, stop_percent: int) -> None:
    entry, capital = D(price), D(equity)
    limit = entry * (1 - D(stop_percent) / 100)
    result = size_long(
        equity=capital,
        free_cash=capital,
        entry=entry,
        protective_limit=limit,
        entry_fee=D(".008"),
        exit_fee=D(".008"),
        allowance_per_unit=entry * D(".002"),
        liquidity_quantity=D(100),
        rules=rules(),
        policy=RiskPolicy(),
    )
    assert result.quantity * entry <= capital * D(".15")
    assert result.quantity * entry * D("1.008") <= capital
    assert result.planned_loss <= capital * D(".005")
    assert result.quantity % D(".00000001") == 0
    if result.quantity:
        assert result.quantity >= rules().minimum_quantity
        assert result.quantity * limit >= rules().minimum_cost


def test_minimums_floats_and_rounding() -> None:
    assert round_increment(D(1) / 3, D(".01")) == D(".33")
    assert round_increment(D(1) / 3, D(".01"), up=True) == D(".34")
    with pytest.raises(ValueError, match="Decimal"):
        round_increment(1.2, D(".01"))  # type: ignore[arg-type]
    result = size_long(
        equity=D(100),
        free_cash=D(100),
        entry=D(60_000),
        protective_limit=D(48_000),
        entry_fee=D(".008"),
        exit_fee=D(".008"),
        allowance_per_unit=D(120),
        liquidity_quantity=D(1),
        rules=rules(),
        policy=RiskPolicy(),
    )
    assert result.quantity == 0 and result.reason == "below_minimum"


def test_bar_execution_waits_for_finalisation_and_does_not_pyramid() -> None:
    data = bars(list(range(100, 330)))
    result = simulate(data, rules(), variant="sma200", start=data[200].start, end=data[-1].end)
    assert result.observations[0].exposure == 0
    assert result.observations[1].exposure > 0
    assert result.open_quantity > 0
    assert result.turnover < D(40)
    stressed = simulate(
        data,
        rules(),
        variant="sma200",
        start=data[200].start,
        end=data[-1].end,
        costs=CostModel(latency_ms=2000),
    )
    assert stressed.turnover == 0


def test_gapped_stop_can_remain_unfilled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aca.simulation.reference.signal", always_long)
    data = bars([100] * 202 + [80] * 4)
    result = simulate(data, rules(), variant="sma200", start=data[200].start, end=data[-1].end)
    assert result.open_quantity > 0
    assert result.unfilled_stop_bars >= 2
    assert result.episodes == ()


def test_partial_exit_finishes_one_episode_without_oversell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = list(bars([100] * 208))
    for index in range(203, 207):
        data[index] = data[index].model_copy(update={"volume": D(1)})

    def exiting(prefix: tuple[Candle, ...], variant: str, at: datetime) -> StrategySignal:
        result = always_long(prefix, variant, at)
        return result.model_copy(update={"desired": "flat"}) if at >= data[203].start else result

    monkeypatch.setattr("aca.simulation.reference.signal", exiting)
    result = simulate(
        tuple(data), rules(), variant="sma200", start=data[200].start, end=data[-1].end
    )
    assert len(result.episodes) == 1
    assert result.open_quantity == 0
    assert result.episodes[0].quantity > D(".04")
    assert result.episodes[0].fees == result.fees
    with localcontext(FINANCIAL_CONTEXT):
        assert abs(result.observations[-1].nav - D(250) - result.episodes[0].pnl) < D("1e-40")


def test_deposits_do_not_clear_a_drawdown_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[datetime] = []

    def recorded(prefix: tuple[Candle, ...], variant: str, at: datetime) -> StrategySignal:
        dispatched.append(at)
        return always_long(prefix, variant, at)

    monkeypatch.setattr("aca.simulation.reference.signal", recorded)
    data = bars([100] * 202 + [3000] * 3 + [30] * 5 + [100] * 90)
    result = simulate(
        data, rules(), variant="sma200", start=data[200].start, end=data[-1].end, monthly=D(250)
    )
    assert "hard_drawdown" in result.halts
    assert "soft_drawdown" in result.halts
    assert len(result.episodes) == 1
    assert result.open_quantity == 0
    assert any(row.contribution for row in result.observations)
    assert max(dispatched) < data[205].start


def test_deposits_do_not_create_returns() -> None:
    data = bars([100] * 300)
    result = simulate(
        data,
        rules(),
        variant=None,
        benchmark_allocation=D(0),
        start=data[200].start,
        end=data[-1].end,
        monthly=D(250),
    )
    assert all(row.unit_price == 1 for row in result.observations)
    assert result.observations[-1].nav > D(250)
    assert result.fees == 0


def test_gaps_never_become_executable_history() -> None:
    data = bars([100] * 220)
    with pytest.raises(ValueError, match="gaps"):
        simulate(
            (*data[:210], *data[211:]),
            rules(),
            variant="sma200",
            start=data[200].start,
            end=data[-1].end,
        )


def test_benchmark_actual_fee_arithmetic() -> None:
    data = bars([100] * 2)
    result = simulate(
        data,
        rules(),
        variant=None,
        benchmark_allocation=D(1),
        start=data[0].start,
        end=data[-1].end,
    )
    expected_quantity = D("2.47768104")  # floor(250 / (100.10 * 1.008), 8dp)
    assert result.open_quantity == expected_quantity
    assert result.fees == expected_quantity * D("100.10") * D(".008")
    assert result.observations[-1].nav == (
        D(250) - expected_quantity * D("100.10") * D("1.008") + expected_quantity * D("99.9")
    )


def test_predeclared_family_and_walk_forward_holdout() -> None:
    assert len(set(TRIALS)) == 20
    assert months(datetime(2020, 2, 29, tzinfo=UTC), 12).day == 28
    end = datetime(2026, 1, 1, tzinfo=UTC)
    holdout = months(end, -12)
    folds = walk_forward(START, holdout)
    assert folds[0].train_end == datetime(2021, 1, 1, tzinfo=UTC)
    assert all(fold.test_end <= holdout for fold in folds)
    assert all(right.validation_end == left.test_end for left, right in pairwise(folds))


@pytest.mark.parametrize("gross,expected", [(".01", "29594"), (".04", "33400")])
def test_published_central_economic_projection(gross: str, expected: str) -> None:
    result = project(D(250), D(250), D(gross), D(".0144"))
    assert abs(result.ending_nav - D(expected)) < 1
    with localcontext(FINANCIAL_CONTEXT):
        attribution = (
            result.contributions
            + result.gross_performance
            - result.variable_costs
            - result.modelled_tax
        )
        assert abs(result.ending_nav - attribution) < D("1e-40")
    assert result.separate_infrastructure == D(2400)


def test_fixed_cost_gate_equality() -> None:
    assert fixed_cost_gate(D(9600), D(".05"))["passes_arithmetic_hurdle"]
    assert not fixed_cost_gate(D(9599), D(".05"))["passes_arithmetic_hurdle"]
