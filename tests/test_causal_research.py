"""Independent event economics and prefix/clock invariants for Phase 4a Batch B."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from parity_fixture import AT, EXPECTED, StopCase, decision, market
from test_research_core import bars

from aca.accounting.ledger import balances
from aca.domain.market import MarketTrade
from aca.domain.timing import CLOCK_VERSION
from aca.domain.trading import TERMINAL
from aca.domain.values import FINANCIAL_CONTEXT
from aca.research.statistics import daily_returns, event_metrics, metrics
from aca.simulation.events import TradeSimulation, simulate_trades
from aca.simulation.reference import CostModel
from aca.storage.strategy import PaperSelection
from aca.strategies.slow import StrategySignal, signal

D = Decimal
ZERO_COST = CostModel(spread_slippage_one_way=D(0))


def print_at(
    ms: int, price: Decimal = Decimal(1000), quantity: Decimal = Decimal(1)
) -> MarketTrade:
    at = AT + timedelta(milliseconds=ms)
    return MarketTrade(
        venue=market(AT).rules.venue,
        symbol="BTC/GBP",
        trade_id=f"print-{ms}",
        side="buy",
        price=price,
        quantity=quantity,
        event_at=at,
        received_at=at,
    )


def slow(*, desired: str = "long", at_ms: int = 0) -> StrategySignal:
    value = decision().model_dump(mode="json")
    value.update(
        strategy_version="slow-v2",
        desired=desired,
        decision_at=(AT + timedelta(milliseconds=at_ms)).isoformat(),
        expires_at=(AT + timedelta(seconds=60)).isoformat(),
    )
    return StrategySignal.model_validate_json(json.dumps(value))


def engine(*, latency: int = 250, delay: int = 0) -> TradeSimulation:
    return TradeSimulation(
        market(AT).rules,
        start=AT,
        initial=D(1000),
        costs=ZERO_COST.model_copy(update={"latency_ms": latency}),
        dispatch_delay_ms=delay,
    )


def enter(value: TradeSimulation) -> None:
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(249))
    assert not value.fills
    value.trade(print_at(250))
    assert value.fills[0].quantity == EXPECTED.entry_quantity
    assert value.fills[0].price == EXPECTED.entry_price
    assert value.state.quantity("GBP") == EXPECTED.entry_cash


@pytest.mark.parametrize("case", EXPECTED.cases, ids=lambda case: case.name)
def test_trade_stops_match_same_independent_oracle_as_postgres_paper(case: StopCase) -> None:
    value = engine()
    enter(value)
    original = value.fills[0]
    for step in case.steps:
        trade = print_at(step.ms, step.bid, step.depth)
        value.trade(trade)
        result = value.result()
        assert result.state.quantity("GBP") == step.cash
        assert result.state.quantity("BTC") == EXPECTED.entry_quantity - step.filled
        assert result.backtest.observations[-1].nav == step.nav
        assert result.backtest.fees == step.fees
        assert len(result.fills) == step.fill_count
        assert balances(result.journals) == result.state.balances
        assert result.attribution["ending_nav_gbp"] == (
            result.attribution["opening_nav_gbp"]
            + result.attribution["realised_trading_pnl_gbp"]
            + result.attribution["unrealised_mark_pnl_gbp"]
            - result.attribution["fees_gbp"]
        )
        assert all(f.price >= EXPECTED.protective_limit for f in result.fills if f.side == "sell")
        assert value.fills[0] == original
        if result.state.quantity("BTC"):
            protection = [
                o
                for o in result.state.orders
                if o.intent.purpose == "protection" and o.state not in TERMINAL
            ]
            assert sum(o.remaining for o in protection) == result.state.quantity("BTC")
            assert result.open_episode is not None
            assert result.open_episode.id == protection[0].intent.episode_id
        value.trade(trade)
        assert value.result() == result  # Duplicate liquidity cannot be consumed again.
        a = result.attribution
        assert (
            a["opening_nav_gbp"]
            + a["gross_marked_pnl_gbp"]
            - a["fees_gbp"]
            - a["spread_slippage_rounding_gbp"]
            - a["terminal_mark_discount_gbp"]
            + a["functional_currency_rounding_gbp"]
            == a["ending_nav_gbp"]
        )
    assert len(value.episodes) == int(not value.state.quantity("BTC"))
    assert (value.open_episode is None) == (not value.state.quantity("BTC"))


@pytest.mark.parametrize("delay,allowed", [(1999, True), (2000, False), (2500, False)])
def test_two_second_approval_is_a_pre_dispatch_clock(delay: int, allowed: bool) -> None:
    value = engine(delay=delay)
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(delay + 250))
    assert bool(value.fills) is allowed
    command = value.result().commands[0]
    assert (command.dispatched_at is not None) is allowed
    if not allowed:
        assert any("stale or mismatched" in reason for reason in value.skips)
        assert value.state.quantity("GBP") == 1000


def test_intervening_state_change_invalidates_a_queued_approval() -> None:
    value = engine(delay=1999)
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(1000))
    value.trade(print_at(2249))
    assert not value.fills
    assert value.result().commands[0].dispatched_at is None
    assert any("stale or mismatched" in reason for reason in value.skips)


@pytest.mark.parametrize("latency", [250, 2000, 5000, 59_999])
def test_matching_delay_does_not_expire_a_consumed_dispatch(latency: int) -> None:
    value = engine(latency=latency)
    value.trade(print_at(0))
    value.publish(slow(), AT)
    original = value.result().commands[0]
    value.trade(print_at(latency - 1))
    assert not value.fills
    value.trade(print_at(latency))
    assert len(value.fills) == 1
    assert value.result().commands[0] == original
    assert value.fills[0].client_id == original.client_id


def test_matching_deadline_is_disclosed_separately_from_approval_expiry() -> None:
    value = engine(latency=60_000)
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(60_000))
    assert not value.fills
    assert value.result().commands[0].dispatched_at == AT
    assert value.skips == {"matching_deadline": 1}


def test_delayed_old_trade_cannot_fill_even_after_receipt_latency() -> None:
    value = engine()
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(250).model_copy(update={"event_at": AT}))
    assert not value.fills
    value.trade(print_at(500))
    assert len(value.fills) == 1


def test_distinct_equal_timestamp_trades_keep_ids_and_individual_liquidity() -> None:
    value = engine()
    enter(value)
    value.trade(print_at(500, D(950)))
    first = print_at(750, D(960), D("0.4"))
    second = first.model_copy(update={"trade_id": "different-public-trade"})
    value.trade(first)
    value.trade(second)
    assert len(value.fills) == 3
    assert value.state.quantity("BTC") == D("0.002")
    with pytest.raises(ValueError, match="conflicting"):
        value.trade(first.model_copy(update={"price": D(970)}))


def test_signal_deadline_is_fixed_and_cannot_be_renewed_by_late_evaluation() -> None:
    data = bars(list(range(100, 300)))
    end = data[-1].end
    first = signal(data, "sma200", end + timedelta(seconds=10), version="slow-v2")
    late = signal(data, "sma200", end + timedelta(seconds=69, milliseconds=999), version="slow-v2")
    assert first.expires_at == late.expires_at == end + timedelta(seconds=70)
    with pytest.raises(ValueError, match="expired"):
        signal(data, "sma200", end + timedelta(seconds=70), version="slow-v2")
    assert signal(data, "sma200", end + timedelta(seconds=10)).strategy_version == "slow-v1"
    assert CLOCK_VERSION == "three-clocks-v1"


def test_legacy_selection_and_signal_roundtrip_keep_their_original_meaning() -> None:
    value = engine()
    selection = PaperSelection(
        account_id=value.state.account_id,
        strategy="sma200",
        symbol="BTC/GBP",
        interval_minutes=240,
        selected_at=AT,
        policy_hash="0" * 64,
        application_version="0.4.21",
    )
    assert selection.strategy_version == "slow-v1"
    document = selection.document()
    assert PaperSelection.restore(document).document() == document
    original = decision()
    restored = StrategySignal.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.expires_at == AT + timedelta(seconds=2)


def test_stale_signal_cannot_be_resurrected_with_a_new_print() -> None:
    value = engine()
    value.publish(slow(), AT)
    value.trade(print_at(60_000))
    value.publish(slow(), AT + timedelta(seconds=60))
    assert not value.commands
    assert value.skips == {"signal_expired": 1}


def test_successful_signal_is_not_renewed_after_stop_or_reporting_snapshot() -> None:
    value = engine()
    enter(value)
    value.trade(print_at(500, D(950)))
    value.trade(print_at(750, D(960)))
    assert len(value.episodes) == 1
    original = value.result()
    value.publish(slow(at_ms=800), AT + timedelta(milliseconds=800))
    assert len(value.commands) == len(original.commands)
    assert len(value.fills) == len(original.fills)


def test_future_prints_cannot_rewrite_prior_fills_signals_or_approval() -> None:
    value = engine()
    enter(value)
    prefix = value.result()
    value.trade(print_at(500, D(800), D(100)))
    value.trade(print_at(750, D(700), D(100)))
    extended = value.result()
    assert extended.fills[: len(prefix.fills)] == prefix.fills
    assert extended.state.orders[0].intent == prefix.state.orders[0].intent
    assert extended.state.orders[0].approval == prefix.state.orders[0].approval
    assert (
        extended.backtest.observations[: len(prefix.backtest.observations)]
        == prefix.backtest.observations
    )
    assert not extended.backtest.episodes


def test_finalised_prefix_executes_inside_next_bar_without_future_bar_liquidity() -> None:
    data = bars([1000] * 199 + [1100] * 4)
    start = data[199].end
    rules = market(AT).rules.model_copy(update={"venue": "fake"})

    def observed(offset: int, price: Decimal = Decimal(1100)) -> MarketTrade:
        at = start + timedelta(milliseconds=offset)
        return MarketTrade(
            venue="fake",
            symbol="BTC/GBP",
            trade_id=str(offset),
            side="buy",
            price=price,
            quantity=D(1),
            event_at=at,
            received_at=at,
        )

    trades = (observed(9999), observed(10_000), observed(10_249), observed(10_250))
    result = simulate_trades(
        data,
        trades,
        rules,
        variant="sma200",
        start=start,
        end=start + timedelta(seconds=11),
        initial=D(1000),
        costs=ZERO_COST,
    )
    assert len(result.fills) == 1
    assert result.fills[0].occurred_at == start + timedelta(milliseconds=10_250)
    assert result.fills[0].occurred_at < data[200].end
    # Altering future OHLC and volume cannot change the complete prior result.
    changed = (
        *data[:200],
        *(
            b.model_copy(
                update={
                    "open": D(1),
                    "low": D(1),
                    "high": D(1),
                    "close": D(1),
                    "volume": D("999999"),
                }
            )
            for b in data[200:]
        ),
    )
    assert (
        simulate_trades(
            changed,
            trades,
            rules,
            variant="sma200",
            start=start,
            end=start + timedelta(seconds=11),
            initial=D(1000),
            costs=ZERO_COST,
        )
        == result
    )
    assert result.fills[0].quantity <= D("0.01")


def test_no_print_liquidity_never_becomes_a_fill_or_a_maker_award() -> None:
    value = engine()
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(250, quantity=D("0.0000001")))
    assert not value.fills
    assert value.skips["ioc_unfilled_residual"] == 1
    value.trade(print_at(500, quantity=D(1000)))
    assert not value.fills  # No retry/chasing the consumed action.


def test_gap_bad_history_and_stale_terminal_evidence_are_explicit() -> None:
    value = engine()
    enter(value)
    assert value.result(AT + timedelta(seconds=6)).stale_terminal_mark
    with pytest.raises(ValueError, match="regressed"):
        value.trade(print_at(500))
    with pytest.raises(ValueError, match="stale/future"):
        engine().trade(print_at(6000).model_copy(update={"event_at": AT}))
    data = bars([1000] * 202)
    with pytest.raises(ValueError, match="discontinuous"):
        simulate_trades(
            (*data[:10], *data[11:]),
            (),
            market(AT).rules,
            variant="sma200",
            start=data[200].start,
            end=data[-1].end,
        )


def test_partial_strategy_exit_reprotects_only_the_confirmed_remainder() -> None:
    value = engine()
    enter(value)
    at = AT + timedelta(seconds=1)
    value.trade(print_at(1000, D(1100)))
    flat = slow(desired="flat", at_ms=1000).model_copy(
        update={
            "data_watermark": AT - timedelta(seconds=9),
            "expires_at": AT + timedelta(seconds=61),
        }
    )
    value.publish(flat, at)
    value.trade(print_at(1250, D(1100), D("0.4")))
    result = value.result()
    assert result.state.quantity("BTC") == D("0.006")
    assert not result.backtest.episodes
    assert result.open_episode is not None and result.open_episode.sold == D("0.004")
    protected = [
        o
        for o in result.state.orders
        if o.intent.purpose == "protection" and o.state not in TERMINAL
    ]
    assert sum(o.remaining for o in protected) == D("0.006")
    assert all(o.intent.episode_id == result.open_episode.id for o in protected)
    value.trade(print_at(1500, D(950)))
    value.trade(print_at(1750, D(960)))
    final = value.result()
    assert final.state.quantity("BTC") == 0
    assert len(final.backtest.episodes) == 1
    assert final.backtest.episodes[0].quantity == D("0.01")
    assert final.backtest.episodes[0].pnl == final.attribution["ending_nav_gbp"] - D(1000)
    assert balances(final.journals) == final.state.balances


def test_halt_duration_and_open_episode_report_preserve_protective_exit() -> None:
    value = engine()
    enter(value)
    value.trade(print_at(500, D(15_000)))
    value.trade(print_at(750, D(100)))
    assert value.state.kill.hard
    gap = event_metrics(value.result())
    assert gap["open_episode"] is not None
    assert gap["completed_episodes"] == 0
    assert gap["unfilled_stop_observations"] == 0  # This observation only triggered.
    assert gap["max_drawdown"] > 0.1  # type: ignore[operator]
    value.trade(print_at(1000, D(960)))
    result = value.result()
    report = event_metrics(result)
    assert result.state.kill.hard
    assert len(result.backtest.episodes) == 1
    assert result.halt_seconds == D("0.25")
    assert result.exposure_seconds == D("0.75")
    assert report["open_episode"] is None
    assert report["drawdown_includes_inception"] is True
    assert report["clock_version"] == CLOCK_VERSION
    assert report["promotion_evidence"] is False
    assert report["live_execution_available"] is False
    assert "unfilled_stop_bars" not in report


def test_external_flows_reconcile_without_releasing_a_hard_latch() -> None:
    value = engine()
    enter(value)
    value.trade(print_at(500, D(15_000)))
    value.trade(print_at(750, D(100)))
    epoch, latch = value.state.kill.policy_epoch, value.state.kill
    at = AT + timedelta(milliseconds=750)
    value.cashflow("deposit", D("99.092"), at)
    assert value.state.units == D(1100)
    assert value.result().backtest.observations[-1].nav == D("1090.012")
    value.cashflow("withdrawal", D("-49.546"), at)
    result = value.result()
    assert value.state.units == D(1050)
    assert result.backtest.observations[-1].nav == D("1040.466")
    assert result.backtest.observations[-1].unit_price == D("0.99092")
    assert value.state.kill == latch and latch.policy_epoch == epoch and latch.hard
    a = result.attribution
    assert a["external_flows_gbp"] == D("49.546")
    assert a["gross_marked_pnl_gbp"] == D(-9)
    assert a["ending_nav_gbp"] == D(1000) + D("49.546") - D(9) - D("0.08")
    assert balances(result.journals) == result.state.balances
    value.cashflow("deposit", D("99.092"), at)
    assert value.result() == result
    with pytest.raises(ValueError, match="conflicting"):
        value.cashflow("deposit", D("99.093"), at)


@pytest.mark.parametrize("peak,hard", [(6000, False), (15_000, True)])
def test_soft_pause_permits_strategy_reduction_while_hard_halt_requires_review(
    peak: int,
    hard: bool,
) -> None:
    value = engine()
    enter(value)
    value.trade(print_at(500, D(peak)))
    value.trade(print_at(750, D(100)))
    assert value.state.kill.soft and value.state.kill.hard is hard
    at = AT + timedelta(milliseconds=750)
    flat = slow(desired="flat", at_ms=750).model_copy(
        update={
            "data_watermark": at - timedelta(seconds=10),
            "expires_at": at + timedelta(seconds=60),
        }
    )
    value.publish(flat, at)
    active = [o for o in value.state.orders if o.state not in TERMINAL]
    assert len(active) == 1
    assert active[0].intent.purpose == ("protection" if hard else "exit")
    value.trade(print_at(1000, D(960) if hard else D(100)))
    assert value.state.quantity("BTC") == 0
    assert len(value.episodes) == 1


@settings(max_examples=1000, derandomize=True, print_blob=True, deadline=None)
@given(price=st.integers(100, 2000), depth=st.integers(1, 100), spread=st.integers(0, 10))
def test_generated_attribution_rebuilds_with_open_and_partial_inventory(
    price: int, depth: int, spread: int
) -> None:
    value = TradeSimulation(
        market(AT).rules,
        start=AT,
        initial=D(1000),
        costs=CostModel(spread_slippage_one_way=D(spread) / 10000),
    )
    value.trade(print_at(0))
    value.publish(slow(), AT)
    value.trade(print_at(250))
    value.trade(print_at(500, D(price), D(depth) / 100))
    value.trade(print_at(750, D(price), D(depth) / 100))
    result = value.result()
    with localcontext(FINANCIAL_CONTEXT):
        a = result.attribution
        reconstructed = (
            a["opening_nav_gbp"]
            + a["gross_marked_pnl_gbp"]
            - a["fees_gbp"]
            - a["spread_slippage_rounding_gbp"]
            - a["terminal_mark_discount_gbp"]
            + a["functional_currency_rounding_gbp"]
        )
        assert reconstructed == a["ending_nav_gbp"]
        assert balances(result.journals) == result.state.balances
        assert result.state.quantity("GBP") >= 0 and result.state.quantity("BTC") >= 0
        assert all(f.liquidity == "taker" for f in result.fills)


def test_drawdown_uses_inception_all_events_while_statistics_keep_daily_sampling() -> None:
    value = engine()
    enter(value)
    snapshot = value.result().backtest
    first = snapshot.observations[0]
    result = replace(
        snapshot,
        observations=(
            first,
            replace(first, at=AT + timedelta(hours=4), nav=D(900), unit_price=D("0.9")),
            replace(first, at=AT + timedelta(hours=23), nav=D(1000), unit_price=D(1)),
        ),
    )
    report = metrics(result, initial=1000)
    assert report["max_drawdown"] == pytest.approx(0.1)
    assert daily_returns(result) == {"2020-01-01": 0.0}
    assert report["annualised_volatility"] is None
    assert report["drawdown_includes_inception"] is True
