"""Independent account/calendar oracles, prefix invariance and fair cash flows."""

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from parity_fixture import AT, market
from test_causal_research import engine, print_at, slow
from test_research_core import bars, rules

from aca.accounting.ledger import balances, stored
from aca.domain.market import Candle, MarketTrade, Symbol
from aca.domain.trading import TERMINAL
from aca.domain.values import FINANCIAL_CONTEXT
from aca.portfolio.state import unit_price
from aca.research.calendar import HOLDOUT_START, common_calendar, qualified_bars, reporting_windows
from aca.research.continuous import (
    ContinuousEvaluation,
    Scenario,
    comparison_report,
    contributions,
    economic_assessment,
    economic_frontier,
    scenario_grid,
    snapshot_identity,
)
from aca.simulation.events import EventResult
from aca.simulation.passive import PassiveAccount
from aca.simulation.reference import CostModel

D = Decimal
FIXTURE = Path(__file__).parent / "fixtures/parity/continuous-v1.json"
ORACLE = json.loads(FIXTURE.read_bytes())
NO_SPREAD = CostModel(spread_slippage_one_way=D(0))
START, END = datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 3, 1, tzinfo=UTC)
DEFAULT_SCENARIO = Scenario(monthly=D(100))
SYMBOLS: tuple[Symbol, ...] = ("BTC/GBP", "ETH/GBP")


def input_bars() -> dict[Symbol, tuple[Candle, ...]]:
    source = bars([1000] * 364 + [1001] * 61)
    return {
        symbol: tuple(b.model_copy(update={"symbol": symbol}) for b in source) for symbol in SYMBOLS
    }


def input_trades() -> tuple[MarketTrade, ...]:
    result = []
    day = START
    while day < END:
        for seconds in (0, 10, 10.25, 86399.75):
            at = day + timedelta(seconds=seconds)
            for symbol in SYMBOLS:
                result.append(
                    MarketTrade(
                        venue="fake",
                        symbol=symbol,
                        trade_id=f"{symbol}:{at.isoformat()}",
                        side="buy",
                        price=D(1000),
                        quantity=D(1),
                        event_at=at,
                        received_at=at,
                    )
                )
        day += timedelta(days=1)
    return tuple(result)


def evaluation(
    scenario: Scenario = DEFAULT_SCENARIO,
    *,
    trades: tuple[MarketTrade, ...] | None = None,
    scope: Literal["out_of_sample", "training", "validation"] = "out_of_sample",
) -> ContinuousEvaluation:
    metadata = {symbol: rules().model_copy(update={"symbol": symbol}) for symbol in SYMBOLS}
    return ContinuousEvaluation(
        input_bars(),
        input_trades() if trades is None else trades,
        metadata,
        symbol="BTC/GBP",
        variant="sma200",
        start=START,
        end=END,
        scenario=scenario,
        costs=NO_SPREAD,
        scope=scope,
    )


def test_common_calendar_includes_terminal_tail_without_moving_holdout() -> None:
    coverage = {
        (symbol, interval): (
            datetime(2014, 11, 6, tzinfo=UTC)
            if symbol == "BTC/GBP"
            else datetime(2015, 8, 7, tzinfo=UTC),
            datetime(2026, 10, 1, tzinfo=UTC),
        )
        for symbol in ("BTC/GBP", "ETH/GBP")
        for interval in (240, 1440)
    }
    calendar = common_calendar(coverage)  # type: ignore[arg-type]
    assert calendar.origin.isoformat() == ORACLE["common_origin"]
    assert calendar.oos_start.isoformat() == ORACLE["common_oos_start"]
    assert calendar.oos_end.isoformat() == ORACLE["common_oos_end"]
    assert len(calendar.windows) == ORACLE["common_windows"]
    assert calendar.windows[-1].partial_tail
    assert calendar.windows[-1].test_start.isoformat() == ORACLE["final_partial_start"]
    assert all(
        a.test_end == b.test_start
        for a, b in zip(calendar.windows, calendar.windows[1:], strict=False)
    )
    assert all(
        window.train_end <= window.test_start < window.test_end <= HOLDOUT_START
        for window in calendar.windows
    )
    with pytest.raises(ValueError, match="both assets"):
        common_calendar({("BTC/GBP", 1440): (START, HOLDOUT_START)})
    with pytest.raises(ValueError, match="warm-up"):
        common_calendar(coverage, origin=START.replace(year=2015))  # type: ignore[arg-type]


def test_common_prefix_rejects_unknown_gaps_and_ignores_holdout_contents() -> None:
    data = input_bars()["BTC/GBP"]
    assert len(qualified_bars(data, "BTC/GBP", START, END)) == 260
    with pytest.raises(ValueError, match="discontinuous"):
        qualified_bars(data[:390] + data[391:], "BTC/GBP", START, END)
    with pytest.raises(ValueError, match="holdout"):
        qualified_bars(data, "BTC/GBP", START, HOLDOUT_START + timedelta(days=1))
    later = data[-1].model_copy(
        update={
            "start": HOLDOUT_START,
            "end": HOLDOUT_START + timedelta(days=1),
            "complete": False,
            "quality": ("disputed_trade_evidence",),
        }
    )
    assert qualified_bars((*data, later), "BTC/GBP", START, END) == qualified_bars(
        data, "BTC/GBP", START, END
    )
    with pytest.raises(ValueError, match="reporting"):
        reporting_windows(START, END, (START,))


@pytest.mark.parametrize("monthly", [D(100), D(250), D(400)])
def test_month_end_flow_calendar_leap_year_and_exclusive_terminal(monthly: Decimal) -> None:
    scenario = Scenario(monthly=monthly)
    flows = contributions(START, END, scenario)
    assert len(flows) == 1
    assert flows[0].at == datetime(2020, 2, 1, tzinfo=UTC)
    assert flows[0].closing_month == "2020-01"
    assert flows[0].amount == monthly
    extended = contributions(START, END + timedelta(microseconds=1), scenario)
    assert extended[1].closing_month == "2020-02"
    assert extended[1].at == END


def test_reporting_splits_preserve_complete_economics_and_open_episode() -> None:
    whole = evaluation().run()
    split = evaluation().run(
        (
            START + timedelta(seconds=10.125),
            START + timedelta(seconds=10.5),
            datetime(2020, 2, 1, tzinfo=UTC),
        )
    )
    assert split.final == whole.final
    assert snapshot_identity(split.final) == snapshot_identity(whole.final)
    assert split.final.candidate.open_episode is not None
    assert split.reports[1].candidate.open_episode is not None
    assert split.reports[1].candidate.open_episode.id == split.final.candidate.open_episode.id
    assert split.reports[0].candidate.commands[0].dispatched_at is not None
    assert not split.reports[0].candidate.fills  # Consumed pending command survives split.
    assert split.reports[1].candidate.fills == split.final.candidate.fills
    assert (
        split.reports[1].candidate.state.kill.policy_epoch
        == split.final.candidate.state.kill.policy_epoch
    )
    assert split.final.candidate.state.units > split.reports[1].candidate.state.units
    assert balances(split.final.candidate.journals) == split.final.candidate.state.balances


@pytest.mark.parametrize("capital", [D(100), D(250), D(400)])
def test_all_accounts_receive_same_flows_and_cash_benchmark_has_no_return(capital: Decimal) -> None:
    result = evaluation(Scenario(initial=capital, monthly=capital)).run()
    flows = result.flows
    assert len(flows) == 1
    assert result.final.candidate.attribution["external_flows_gbp"] == capital
    for account in result.final.comparators.values():
        assert account.attribution["external_flows_gbp"] == capital
        assert balances(account.journals) == account.state.balances
        posted = [j for j in account.journals if j.source == flows[0].id]
        assert len(posted) == 1
        assert posted[0].occurred_at == flows[0].at
        assert posted[0].movements[0].quantity == capital
    cash = result.final.comparators["cash"]
    assert cash.state.quantity("GBP") == cash.state.units == 2 * capital
    assert all(row.unit_price == 1 for row in cash.backtest.observations)
    report = comparison_report(result)
    assert report["promotion_evidence"] is False
    assert set(result.final.comparators) >= {
        "cash",
        "passive10",
        "passive30",
        "buy_hold",
        "monthly_purchases",
        "btc_buy_hold",
        "eth_buy_hold",
        "blend50_50",
        "past_exposure_matched",
        "past_risk_matched",
    }
    assert result.final.always_long.state.account_id != result.final.candidate.state.account_id


def test_train_validation_and_oos_are_isolated_accounts() -> None:
    accounts = [
        evaluation(scope=scope).candidate.state.account_id
        for scope in ("out_of_sample", "training", "validation")
    ]
    assert len(set(accounts)) == 3
    training = evaluation(scope="training").run()
    assert training.scope == "training"
    assert not training.promotion_evidence


def test_past_matched_diagnostics_are_prefix_invariant_and_not_mandatory_replacements() -> None:
    data = input_trades()
    change = datetime(2020, 2, 15, tzinfo=UTC)
    changed = tuple(
        t.model_copy(update={"price": D(100_000)}) if t.received_at >= change else t for t in data
    )
    left, right = evaluation(), evaluation(trades=changed)
    before = datetime(2020, 2, 2, tzinfo=UTC)
    assert left.advance(before) == right.advance(before)
    assert left.diagnostic_targets == right.diagnostic_targets
    assert len(left.diagnostic_targets) == 2
    assert all(0 <= D(item["target_allocation"]) <= D(".15") for item in left.diagnostic_targets)
    assert left.comparators["passive10"].weights == {"BTC/GBP": D(".1")}


def test_missing_flow_valuation_blocks_comparison_without_moving_flow_or_dropping_day() -> None:
    gap = datetime(2020, 2, 1, tzinfo=UTC)
    data = tuple(
        t for t in input_trades() if not gap - timedelta(seconds=10) <= t.received_at <= gap
    )
    value = evaluation(trades=data)
    with pytest.raises(ValueError, match="valuation"):
        value.run()
    with pytest.raises(ValueError, match="failed continuous"):
        value.advance(END)
    assert value.flows[0].at == gap
    sparse = evaluation(Scenario(monthly=D(0)), trades=data).run()
    report = comparison_report(sparse)
    calendar = report["calendar"]
    assert isinstance(calendar, dict)
    assert len(calendar["dates"]) == 60
    assert "2020-01-31" in calendar["missing_days"]["candidate"]
    assert not calendar["statistical_sample_complete"]
    assert (
        sparse.final.candidate.state.kill.policy_epoch
        == sparse.reports[0].candidate.state.kill.policy_epoch
    )


def test_passive_literal_budget_latency_fees_and_unitisation_oracle() -> None:
    value = PassiveAccount(
        "literal",
        {"BTC/GBP": market(AT).rules},
        {"BTC/GBP": D(".1")},
        start=AT,
        initial=D(1000),
        costs=NO_SPREAD,
    )
    value.trade(print_at(0))
    value.trade(print_at(249))
    assert not value.fills
    value.trade(print_at(250))
    assert value.state.quantity("GBP") == D("989.92")
    assert value.state.quantity("BTC") == D(ORACLE["entry_quantity"])
    assert unit_price(value.state, AT + timedelta(milliseconds=250)) == D(
        ORACLE["pre_flow_unit_price"]
    )
    at = AT + timedelta(milliseconds=500)
    value.cashflow("literal-flow", D(ORACLE["contribution"]), at)
    assert value.state.units == D(ORACLE["post_flow_units"])
    assert value.state.quantity("GBP") == D(ORACLE["post_flow_cash"])
    value.trade(print_at(500, quantity=D(".1")))
    value.trade(print_at(750, quantity=D(".1")))
    result = value.result(AT + timedelta(milliseconds=1000))
    assert result.fills[1].quantity == D(ORACLE["passive_second_quantity"])
    assert result.fills[1].fees[0].quantity == D(ORACLE["passive_second_fee"])
    assert result.state.quantity("GBP") == D(ORACLE["passive_ending_cash"])
    assert result.attribution["ending_nav_gbp"] == D(ORACLE["passive_ending_nav"])
    assert result.state.units == D(ORACLE["passive_ending_units"])
    assert result.budgets[0].spent == D("10.08")
    assert result.budgets[0].amount == 100
    # More qualifying prints cannot chase the original unfilled budget.
    value.trade(print_at(1250, quantity=D(100)))
    assert len(value.fills) == 2


def test_passive_missing_liquidity_and_expired_budgets_stay_cash() -> None:
    value = PassiveAccount(
        "tiny",
        {"BTC/GBP": market(AT).rules},
        {"BTC/GBP": D(1)},
        start=AT,
        initial=D(1000),
        costs=NO_SPREAD,
    )
    value.trade(print_at(0, quantity=D(".0000001")))
    value.trade(print_at(250, quantity=D(1000)))
    assert not value.fills
    assert value.state.quantity("GBP") == 1000
    expired = PassiveAccount(
        "expired",
        {"BTC/GBP": market(AT).rules},
        {"BTC/GBP": D(1)},
        start=AT,
        initial=D(1000),
        costs=NO_SPREAD,
    )
    expired.trade(print_at(60_000))
    assert not expired.fills
    assert expired.skips["budget_deadline_residual_cash"] == 1


def test_explicit_epoch_review_preserves_daily_and_inception_and_is_idempotent() -> None:
    value = engine()
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(250))
    value.trade(print_at(500, D(15000)))
    value.trade(print_at(750, D(100)))
    before = value.result()
    assert before.state.kill.hard
    value.review_epoch(
        "human-review",
        "Reviewed and reconciled the known protected position",
        "a" * 64,
        before.state.kill.policy_epoch,
        AT + timedelta(milliseconds=1000),
    )
    after = value.result()
    assert not after.state.kill.hard
    assert after.state.kill.daily_day == before.state.kill.daily_day
    assert after.state.inception_high_water == before.state.inception_high_water
    assert after.state.high_water == after.state.last_unit_price
    assert after.state.kill.policy_epoch != before.state.kill.policy_epoch
    assert after.open_episode == before.open_episode
    assert after.fills == before.fills
    value.review_epoch(
        "human-review",
        "Reviewed and reconciled the known protected position",
        "a" * 64,
        before.state.kill.policy_epoch,
        AT + timedelta(milliseconds=1000),
    )
    assert value.result() == after
    with pytest.raises(ValueError, match="conflicting"):
        value.review_epoch(
            "human-review",
            "A changed reason is not the same audit record",
            "a" * 64,
            before.state.kill.policy_epoch,
            AT + timedelta(milliseconds=1000),
        )
    with pytest.raises(ValueError, match="known account"):
        value.review_epoch(
            "wrong-epoch",
            "A substantive but mismatched manual approval",
            "b" * 64,
            uuid4(),
            AT + timedelta(milliseconds=1000),
        )


@pytest.mark.parametrize("boundary_ms", [125, 250, 375, 500, 625, 875])
def test_split_preserves_pending_triggered_partial_and_halted_episode(boundary_ms: int) -> None:
    def run(split: bool) -> EventResult:
        value = engine()
        value.trade(print_at(0))
        value.publish(slow(), AT)
        for ms, price, quantity in (
            (250, 1000, "1"),
            (375, 15000, "1"),
            (500, 950, "1"),
            (750, 960, ".4"),
            (1000, 800, "1"),
        ):
            if split and value.at < AT + timedelta(milliseconds=boundary_ms) < AT + timedelta(
                milliseconds=ms
            ):
                value.result(AT + timedelta(milliseconds=boundary_ms))
            value.trade(print_at(ms, D(price), D(quantity)))
        return value.result(AT + timedelta(milliseconds=1250))

    assert run(True) == run(False)
    assert run(True).open_episode is not None
    assert run(True).state.quantity("BTC") == D(".006")
    assert run(True).state.kill.hard


def test_matching_timer_expires_without_another_print_and_never_replays() -> None:
    value = engine()
    value.trade(print_at(0))
    value.publish(slow(), AT)
    first = value.result(AT + timedelta(seconds=90))
    assert not first.fills
    assert first.state.orders[0].state == "EXPIRED"
    assert first.skip_reasons["matching_deadline"] == 1
    value.trade(print_at(90_000))
    assert not value.fills
    assert value.result().commands == first.commands


def test_trade_iterator_is_consumed_incrementally_without_future_price_decisions() -> None:
    data = input_trades()
    count = 0

    def source() -> Iterator[MarketTrade]:
        nonlocal count
        for trade in data:
            count += 1
            yield trade

    base = evaluation()
    value = ContinuousEvaluation(
        input_bars(),
        source(),
        {s: rules().model_copy(update={"symbol": s}) for s in SYMBOLS},
        symbol="BTC/GBP",
        variant="sma200",
        start=START,
        end=END,
        scenario=DEFAULT_SCENARIO,
        costs=NO_SPREAD,
    )
    assert count <= 1
    first = value.advance(START + timedelta(seconds=11))
    assert first.candidate.fills
    assert count < 10
    final = value.advance(END)
    assert final == base.advance(END)
    assert count == len(data)


def test_economics_keeps_infrastructure_outside_nav_and_hurdle_independent() -> None:
    result = evaluation().run().final.candidate
    base = economic_assessment(
        result,
        Scenario(monthly=D(100)),
        incremental_lower_bound=D(".05"),
        statistical_eligible=False,
    )
    assert not base["economic_eligible"]
    assert base["statistical_eligible"] is False
    assert D(str(base["trading_nav_gbp"])) == result.attribution["ending_nav_gbp"]
    assert D(str(base["trading_nav_gbp"])) - D(str(base["household_wealth_before_tax_gbp"])) == D(
        str(base["separate_infrastructure_gbp"])
    )
    unknown = economic_assessment(
        result, Scenario(), incremental_lower_bound=None, statistical_eligible=None
    )
    assert unknown["economic_arithmetic"] is None
    assert not unknown["economic_eligible"]


def test_frontier_requires_comparable_cases_and_cannot_grant_statistical_eligibility() -> None:
    grid = scenario_grid()
    assert len(grid) == 9
    assert {(s.initial, s.monthly) for s in grid} == {
        (D(i), D(m)) for i in (100, 250, 400) for m in (100, 250, 400)
    }
    cases = [evaluation(s).run() for s in grid]
    frontier = economic_frontier(
        cases, evidence={snapshot_identity(case.final): (D(".05"), False) for case in cases}
    )
    rows = frontier["cases"]
    assert isinstance(rows, list)
    assert len(rows) == 9
    assert all(not row["economic_eligible"] for row in rows)
    assert all(D(row["required_average_equity_at_supplied_bound_gbp"]) == 9600 for row in rows)
    with pytest.raises(ValueError, match="noncomparable"):
        economic_frontier([cases[0], cases[0]])
    unknown = economic_frontier(cases)
    unknown_rows = unknown["cases"]
    assert isinstance(unknown_rows, list)
    assert all(row["statistical_eligible"] is None for row in unknown_rows)
    with pytest.raises(ValueError, match="different account"):
        economic_frontier(cases, evidence={"unknown": (D(".05"), True)})


@settings(max_examples=1000, derandomize=True, deadline=None)
@given(
    initial=st.integers(100, 10000),
    allocation=st.integers(0, 100),
    price=st.integers(50, 10000),
    depth=st.integers(1, 100),
)
def test_generated_passive_journal_attribution_and_no_overspend(
    initial: int, allocation: int, price: int, depth: int
) -> None:
    value = PassiveAccount(
        "generated",
        {"BTC/GBP": market(AT).rules},
        {"BTC/GBP": D(allocation) / 100},
        start=AT,
        initial=D(initial),
        costs=NO_SPREAD,
    )
    value.trade(print_at(0, D(price), D(depth)))
    value.trade(print_at(250, D(price), D(depth)))
    result = value.result(AT + timedelta(milliseconds=500))
    assert result.state.quantity("GBP") >= 0
    assert balances(result.journals) == result.state.balances
    a = result.attribution
    with localcontext(FINANCIAL_CONTEXT):
        assert abs(
            a["opening_nav_gbp"]
            + a["external_flows_gbp"]
            + a["gross_marked_pnl_gbp"]
            - a["fees_gbp"]
            - a["spread_slippage_rounding_gbp"]
            - a["terminal_mark_discount_gbp"]
            + a["functional_currency_rounding_gbp"]
            - a["ending_nav_gbp"]
        ) < D("1e-15")
        assert sum((b.spent for b in result.budgets), D(0)) <= stored(
            D(initial) * D(allocation) / 100
        )
    assert all(f.quantity <= D(depth) * D(".01") for f in result.fills)
    assert not any(o.state not in TERMINAL for o in result.state.orders)


def test_scenario_boundaries_reject_floats_and_undeclared_flows() -> None:
    with pytest.raises(ValueError, match="decimal"):
        Scenario(initial=250.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scenario"):
        Scenario(monthly=D(200))
    with pytest.raises(ValueError, match="scenario"):
        replace(Scenario(), infrastructure_monthly=D(36))
