"""Literal shared-account economics and the fixed S1/S2 research budget."""

from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from parity_fixture import AT, market
from pydantic import ValidationError
from test_causal_research import ZERO_COST, print_at, slow
from test_continuous_research import END, START, input_trades
from test_research_core import bars, rules
from test_research_gates import synthetic

from aca.accounting.ledger import balances
from aca.domain.market import MarketTrade, Symbol
from aca.domain.trading import TERMINAL
from aca.domain.values import FINANCIAL_CONTEXT
from aca.operations.config import RiskPolicy
from aca.portfolio.state import episode_risk, free_cash, market_exposure, nav
from aca.research.continuous import (
    ContinuousEvaluation,
    comparison_report,
    snapshot_identity,
)
from aca.research.diagnostics import describe
from aca.research.family import (
    FAMILY,
    JOINT,
    S0,
    S1,
    S2,
    PortfolioName,
    definition,
    portfolio_variant,
)
from aca.research.gates import assess, bindings_for, freeze
from aca.simulation.events import TradeSimulation
from aca.storage.strategy import PaperSelection
from aca.strategies.slow import S1_VARIANTS, ResearchVariant, signal

D = Decimal
SYMBOLS: tuple[Symbol, ...] = ("BTC/GBP", "ETH/GBP")


def shared(
    *, initial: Decimal = Decimal(1000), policy: RiskPolicy | None = None
) -> TradeSimulation:
    return TradeSimulation(
        market(AT).rules,
        additional_rules={"ETH/GBP": market(AT).rules.model_copy(update={"symbol": "ETH/GBP"})},
        start=AT,
        initial=initial,
        policy=policy or RiskPolicy(),
        costs=ZERO_COST,
    )


def tick(
    symbol: Symbol, ms: int, price: Decimal = Decimal(1000), quantity: Decimal = Decimal(1)
) -> MarketTrade:
    return print_at(ms, price, quantity).model_copy(
        update={"symbol": symbol, "trade_id": f"{symbol}:{ms}"}
    )


def publish(value: TradeSimulation, symbol: Symbol, *, desired: str = "long", ms: int = 0) -> None:
    value.publish(
        slow(desired=desired, at_ms=ms).model_copy(
            update={
                "symbol": symbol,
                "data_watermark": AT + timedelta(milliseconds=ms, seconds=-10),
            }
        ),
        AT + timedelta(milliseconds=ms),
    )


def enter_both(value: TradeSimulation) -> None:
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 0))
    for symbol in SYMBOLS:
        publish(value, symbol)
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 250))


def joint(name: PortfolioName = "joint-hysteresis") -> ContinuousEvaluation:
    # Two strong daily closes, then constant levels. Entirely synthetic, not a candidate result.
    source = bars([1000] * 362 + [1020, 1040] + [1040] * 61)
    data = {s: tuple(b.model_copy(update={"symbol": s}) for b in source) for s in SYMBOLS}
    return ContinuousEvaluation(
        data,
        input_trades(),
        {s: rules().model_copy(update={"symbol": s}) for s in SYMBOLS},
        symbol="BTC/GBP",
        variant=portfolio_variant(name),
        portfolio=name,
        start=START,
        end=END,
        costs=ZERO_COST,
    )


def test_exact_budget_and_preserved_original_definitions() -> None:
    assert len(S0) == 20 and len(S1) == 4 and len(S2) == 2
    assert len(set(FAMILY)) == 26
    assert all(definition(name)["pyramiding"] is False for name in FAMILY)
    assert definition(S0[0]) == {
        "trial": S0[0],
        "strategy_version": "slow-v2",
        "pyramiding": False,
        "protective_distance": "max(2*ATR14,5%entry); limit_1%below_trigger",
        "clock": "slow-v2; 10s_finalisation; 2s_dispatch; 250ms_latency; 60s_match",
        "stop_reentry": "next_fresh_closed_signal_only; latched_risk_remains",
    }
    changed = definition(S2[0])
    changed["members"] = {}
    assert definition(S2[0])["members"]  # Callers cannot mutate the registry.
    with pytest.raises(ValueError, match="26-definition"):
        definition("BTC/GBP:1440:sma200-band2")


@pytest.mark.parametrize("variant", S1_VARIANTS)
def test_research_definitions_do_not_expand_existing_operational_paper_selection(
    variant: ResearchVariant,
) -> None:
    with pytest.raises(ValidationError, match="literal_error"):
        PaperSelection.model_validate(
            {
                "account_id": uuid4(),
                "strategy": variant,
                "symbol": "BTC/GBP",
                "interval_minutes": 1440,
                "selected_at": AT,
                "policy_hash": "a" * 64,
            }
        )


@pytest.mark.parametrize("variant", S1_VARIANTS)
def test_daily_signal_prefix_is_causal_and_cannot_consume_future_bars(
    variant: ResearchVariant,
) -> None:
    prefix = bars([1000] * 199 + [1020, 1040])
    at = prefix[-1].end + timedelta(seconds=10)
    value = signal(prefix, variant, at, version="slow-v2")
    assert value.desired == "long"
    assert value.stop_distance >= prefix[-1].close * D(".05")
    with pytest.raises(ValueError, match="prefix"):
        signal((*prefix, *bars([10])[:1]), variant, at, version="slow-v2")
    with pytest.raises(ValueError, match="daily"):
        signal(
            tuple(b.model_copy(update={"interval_minutes": 240}) for b in prefix),
            variant,
            at,
            version="slow-v2",
        )
    with pytest.raises(ValueError, match="daily"):
        signal(prefix, variant, at)


@pytest.mark.parametrize(
    "values,desired",
    [([1000, 1000], "hold"), ([1020, 1040], "long"), ([980, 960], "flat"), ([1020, 990], "hold")],
)
def test_confirmation_uses_two_own_averages_and_strict_equality(
    values: list[int], desired: str
) -> None:
    data = bars([1000] * 199 + values)
    assert (
        signal(
            data, "sma200-confirm2", data[-1].end + timedelta(seconds=10), version="slow-v2"
        ).desired
        == desired
    )


def test_hysteresis_band_equality_is_hold_literal_oracle() -> None:
    base = bars([100] * 200)
    # Use an exactly summable history: 198*100 + 99 + 101 = 20000.
    data = (
        *(
            b.model_copy(update={"open": D(100), "high": D(101), "low": D(99), "close": D(100)})
            for b in base[:-2]
        ),
        base[-2].model_copy(update={"open": D(99), "high": D(101), "low": D(99), "close": D(99)}),
        base[-1].model_copy(update={"open": D(101), "high": D(101), "low": D(99), "close": D(101)}),
    )
    assert (
        signal(
            data, "sma200-band1", data[-1].end + timedelta(seconds=10), version="slow-v2"
        ).desired
        == "hold"
    )


def test_one_cash_ledger_two_distinct_episodes_and_literal_fee_economics() -> None:
    value = shared()
    enter_both(value)
    result = value.result()
    # Each 1%-of-print fill buys .01 units @1000 with .08 GBP actual fees.
    assert result.state.quantity("BTC") == result.state.quantity("ETH") == D(".01")
    assert result.state.quantity("GBP") == D("979.84")
    assert result.attribution["ending_nav_gbp"] == D("999.84")
    assert result.state.units == 1000
    assert len(result.open_episodes) == 2 and result.open_episode is None
    assert len({e.id for e in result.open_episodes}) == 2
    assert len([j for j in result.journals if j.source == "research-opening"]) == 1
    assert balances(result.journals) == result.state.balances
    assert episode_risk(result.state) <= nav(result.state, AT + timedelta(milliseconds=250)) * D(
        ".01"
    )
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 250))
    assert value.result() == result  # Duplicates cannot supply liquidity twice.


def test_exit_cancels_only_own_protection_and_keeps_other_asset_inventory() -> None:
    value = shared()
    enter_both(value)
    publish(value, "BTC/GBP", desired="flat", ms=500)
    eth_protection = [
        o
        for o in value.state.orders
        if o.intent.symbol == "ETH/GBP" and o.intent.purpose == "protection"
    ]
    assert len(eth_protection) == 1 and eth_protection[0].state == "OPEN"
    value.trade(tick("BTC/GBP", 750))
    assert value.state.quantity("BTC") == 0 and value.state.quantity("ETH") == D(".01")
    assert value.state.order(eth_protection[0].intent.client_id).state == "OPEN"
    assert value.state.quantity("GBP") == D("989.76")
    assert len(value.result().open_episodes) == 1


def test_partial_exit_deadline_restores_only_its_remaining_protection() -> None:
    value = shared()
    enter_both(value)
    publish(value, "BTC/GBP", desired="flat", ms=500)
    value.trade(tick("BTC/GBP", 750, quantity=D(".4")))
    result = value.result(AT + timedelta(seconds=61))
    assert result.state.quantity("BTC") == D(".006")
    assert result.state.quantity("ETH") == D(".01")
    for symbol, expected in (("BTC/GBP", D(".006")), ("ETH/GBP", D(".01"))):
        protection = [
            o
            for o in result.state.orders
            if o.intent.symbol == symbol
            and o.intent.purpose == "protection"
            and o.state not in TERMINAL
        ]
        assert sum((o.remaining for o in protection), D(0)) == expected
    assert balances(result.journals) == result.state.balances


def test_hold_after_completed_exit_does_not_reenter_and_episode_report_is_literal() -> None:
    value = shared()
    enter_both(value)
    publish(value, "BTC/GBP", desired="flat", ms=500)
    value.trade(tick("BTC/GBP", 750))
    publish(value, "BTC/GBP", desired="hold", ms=1000)
    value.trade(tick("BTC/GBP", 1250))
    assert len(value.fills) == 3
    report = describe(value.result(), (), {}, AT + timedelta(seconds=2))
    episodes = report["episodes"]
    assert isinstance(episodes, list)
    completed = next(e for e in episodes if e["symbol"] == "BTC/GBP")
    assert D(completed["completed_pnl_gbp"]) == D("-.16")
    assert completed["whipsaw"] is True
    assert next(e for e in episodes if e["symbol"] == "ETH/GBP")["closed_at"] is None


def test_descriptive_missed_trend_is_separate_from_causal_signal_and_joint_loss_days() -> None:
    data = bars([1000] * 201 + [1001, 999])
    at = data[200].end + timedelta(seconds=10)
    decision = signal(data[:201], "sma200-confirm2", at, version="slow-v2")
    assert decision.desired == "hold"
    report = describe(
        shared().result(),
        ((decision, D(0)),),
        {s: tuple(b.model_copy(update={"symbol": s}) for b in data) for s in SYMBOLS},
        data[-1].end,
    )
    assert report["selection_input"] is False
    missed = report["missed_trend_observations"]
    assert isinstance(missed, list) and len(missed) == 1
    assert D(missed[0]["subsequent_close_return"]) == D(".001")


def test_pending_reservations_share_budget_and_signal_priority_is_deterministic() -> None:
    value = shared()
    value.state = value.state.model_copy(update={"tax_reserve_gbp": D("989.9")})
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 0))
    publish(value, "BTC/GBP")
    first = free_cash(value.state)
    publish(value, "ETH/GBP")
    # First pending entry reserves nearly all spendable cash after the tax reserve.
    assert D(0) < first < D(".02")
    assert len(value.commands) == 1
    assert next(iter(value.commands.values())).client_id == value.state.orders[0].intent.client_id
    assert value.state.orders[0].intent.symbol == "BTC/GBP"
    assert value.skips["below_minimum"] == 1
    assert episode_risk(value.state) <= D(10)
    assert market_exposure(value.state, AT) <= D(300)


def test_stale_other_asset_blocks_entry_and_flow_without_fabricated_mark() -> None:
    value = shared()
    value.trade(tick("BTC/GBP", 0))
    publish(value, "BTC/GBP")
    value.trade(tick("BTC/GBP", 250))
    value.trade(tick("ETH/GBP", 6000))
    publish(value, "ETH/GBP", ms=6000)
    assert value.skips["unsafe_shared_valuation"] == 1
    assert not value.state.quantity("ETH")
    assert value.result().stale_terminal_mark
    with pytest.raises(ValueError, match="valuation"):
        value.cashflow("missing-mark", D(100), AT + timedelta(seconds=6))


def test_correlated_losses_latch_once_and_preserve_both_protections() -> None:
    value = shared()
    enter_both(value)
    # Both prices rise: shared peak 1289.84. Correlated reversal loses >10%.
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 500, D(15500)))
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 750, D(1000)))
    result = value.result()
    assert result.state.kill.hard
    assert result.state.inception_high_water > D("1.28")
    protections = [
        o
        for o in result.state.orders
        if o.intent.purpose == "protection" and o.state not in TERMINAL
    ]
    assert len(protections) == 2
    before = result.state.kill
    value.cashflow("deposit", D(100), AT + timedelta(milliseconds=1000))
    assert value.state.kill == before
    for symbol in SYMBOLS:
        publish(value, symbol, ms=1000)
    assert len(value.fills) == 2


@pytest.mark.parametrize("name", ["joint-hysteresis", "joint-confirmation"])
def test_joint_continuous_splits_common_flows_and_whole_account_calendar(
    name: PortfolioName,
) -> None:
    whole = joint(name).run()
    split = joint(name).run((START + timedelta(seconds=10.125), START + timedelta(seconds=10.5)))
    assert whole.final == split.final
    assert snapshot_identity(whole.final) == snapshot_identity(split.final)
    candidate = whole.final.candidate
    assert candidate.attribution["opening_nav_gbp"] == 250
    assert candidate.attribution["external_flows_gbp"] == 250
    assert len([j for j in candidate.journals if j.source.startswith("month-end:")]) == 1
    assert balances(candidate.journals) == candidate.state.balances
    report = comparison_report(whole)
    assert report["joint_account"] is True and report["symbol"] == JOINT
    assert report["candidate_definition"] == definition(f"{JOINT}:1440:{name}")
    assert len(report["calendar"]["dates"]) == 60  # type: ignore[index]
    assert joint(name).comparators["passive10"].weights == {s: D(".05") for s in SYMBOLS}


def test_joint_future_prices_cannot_change_prior_snapshot() -> None:
    left, right = joint(), joint()
    # Alter future bars only, keeping the required prefix unchanged.
    cutoff = START + timedelta(days=15)
    right.bars = {
        s: tuple(
            b.model_copy(update={"close": D(99999), "high": D(99999)}) if b.start >= cutoff else b
            for b in source
        )
        for s, source in right.bars.items()
    }
    assert left.advance(cutoff) == right.advance(cutoff)


@pytest.mark.parametrize("name", (*S1, *S2))
def test_new_definitions_use_same_rechecked_gates_and_cannot_promote_by_label(
    name: str, tmp_path: Path
) -> None:
    report, _, evidence, calibrated, reviews = synthetic()
    symbol, interval, variant = name.split(":")
    report.update(
        symbol=symbol,
        interval_minutes=int(interval),
        variant=variant,
        candidate_definition=definition(name),
        joint_account=name in S2,
    )
    bound = bindings_for(report, evidence, definition(name))
    # Wholly synthetic positive gate, same 104 claims, no actual selection.
    assert (
        assess(report, bound, evidence, calibration=calibrated, reviews=reviews).status
        == "freeze_eligible"
    )
    report["candidate"]["completed_episodes"] = 0
    with pytest.raises(ValueError, match="complete passing"):
        freeze(
            tmp_path,
            report,
            bound,
            evidence,
            calibrated,
            actor="synthetic",
            reason="Synthetic short-sample rejection fixture",
            reviews=reviews,
        )
    with pytest.raises(ValueError, match="changed preregistered"):
        bindings_for(report, evidence, {**definition(name), "pyramiding": True})


@settings(max_examples=1000, derandomize=True, deadline=None)
@given(capital=st.integers(100, 10000), depth=st.integers(1, 100), price=st.integers(500, 10000))
def test_generated_shared_funding_risk_and_journal_invariants(
    capital: int, depth: int, price: int
) -> None:
    value = shared(initial=D(capital))
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 0, D(price), D(depth)))
    for symbol in SYMBOLS:
        publish(value, symbol)
    assert episode_risk(value.state) <= D(capital) * D(".01")
    assert market_exposure(value.state, AT) <= D(capital) * D(".3")
    assert free_cash(value.state) >= 0
    for symbol in SYMBOLS:
        value.trade(tick(symbol, 250, D(price), D(depth)))
    result = value.result()
    assert balances(result.journals) == result.state.balances
    assert free_cash(result.state) >= 0
    # New commitments were checked against shared equity before each actual fill.
    assert all(f.quantity <= D(depth) * D(".01") for f in result.fills)
    assert len(result.open_episodes) <= 2
    with localcontext(FINANCIAL_CONTEXT):
        a = result.attribution
        assert abs(
            a["opening_nav_gbp"]
            + a["gross_marked_pnl_gbp"]
            - a["fees_gbp"]
            - a["spread_slippage_rounding_gbp"]
            - a["terminal_mark_discount_gbp"]
            + a["functional_currency_rounding_gbp"]
            - a["ending_nav_gbp"]
        ) < D("1e-14")
