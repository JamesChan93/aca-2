"""Independent economics, ordered paper probes and named legacy-model differences.

These fixtures establish Batch A verification. They do not claim corrected-model
parity: P4A-01/02/04/05/06 still have explicit baseline discrepancies below.
"""

import ast
import json
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import uuid4

import pytest
from parity_fixture import AT, EXPECTED, StopCase, decision, market
from test_research_core import bars

from aca.accounting.ledger import apply_journals, balances, cash_flow, fill_journal
from aca.domain.market import Candle
from aca.domain.trading import Fee, Fill, KillSwitchState, PortfolioSnapshot, Valuation
from aca.domain.values import FINANCIAL_CONTEXT
from aca.execution.paper import PaperExecution
from aca.operations.config import RiskPolicy, Settings
from aca.portfolio.state import entries_paused, flow_units, nav, observe_risk, unit_price
from aca.research.statistics import metrics
from aca.risk.execution import DispatchDenied, execution_guard
from aca.simulation.assumptions import BOUNDS, LEGACY_BAR_MODEL, PAPER_MODEL
from aca.simulation.paper import PaperExchange
from aca.simulation.reference import Backtest, CostModel, Observation, simulate
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter
from aca.strategies.slow import StrategySignal, Variant, signal

D = Decimal


@pytest.fixture
async def account(
    database_settings: Settings,
) -> AsyncIterator[tuple[PaperExecution, PaperExchange]]:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as writer:
            venue = PaperExchange(store)
            service = PaperExecution(writer, venue)
            await service.initialise(AT)
            await service.cashflow("parity-opening", EXPECTED.opening_gbp, AT)
            await service.mark(
                (Valuation(asset="BTC", bid_gbp=D(999), observed_at=AT, source="parity"),), AT
            )
            assert (await service.recover(AT)).complete
            yield service, venue
    finally:
        await engine.dispose()


async def enter(service: PaperExecution, venue: PaperExchange) -> None:
    order = await service.queue(decision(), market(AT), AT, time_in_force="FOK")
    assert order.intent.quantity == EXPECTED.entry_quantity
    assert order.intent.limit_price == EXPECTED.entry_limit
    assert order.intent.stop_trigger == EXPECTED.stop_trigger
    assert order.intent.protective_limit == EXPECTED.protective_limit
    await service.dispatch(order.intent.client_id, market(AT), AT)
    before = AT + timedelta(milliseconds=249)
    m = market(before)
    assert not (await venue.advance(m.book, m.rules, before)).fills
    at = AT + timedelta(milliseconds=250)
    m = market(at)
    snapshot = await venue.advance(m.book, m.rules, at)
    assert len(snapshot.fills) == 1
    fill = snapshot.fills[0]
    assert fill.client_id == order.intent.client_id
    assert fill.quantity == EXPECTED.entry_quantity
    assert fill.price == EXPECTED.entry_price
    assert fill.fees[0].quantity == EXPECTED.entry_fee
    await service.mark(
        (Valuation(asset="BTC", bid_gbp=D(999), observed_at=at, source="parity"),), at
    )
    assert (await service.reconcile(at)).complete
    assert (await service._state()).quantity("GBP") == EXPECTED.entry_cash


@pytest.mark.integration
async def test_continuous_account_and_paper_share_independent_flow_oracle(
    account: tuple[PaperExecution, PaperExchange],
) -> None:
    from test_causal_research import engine, print_at, slow

    oracle = json.loads((Path(__file__).parent / "fixtures/parity/continuous-v1.json").read_bytes())
    service, venue = account
    await enter(service, venue)
    research = engine()
    research.trade(print_at(0))
    research.publish(slow(), AT)
    research.trade(print_at(250))
    at = AT + timedelta(milliseconds=500)
    await service.mark(
        (Valuation(asset="BTC", bid_gbp=D(1000), observed_at=at, source="parity"),), at
    )
    before = await service._state()
    assert (
        nav(before, at)
        == research.result().attribution["ending_nav_gbp"]
        == D(oracle["pre_flow_nav"])
    )
    assert unit_price(before, at) == D(oracle["pre_flow_unit_price"])
    epoch, inception = before.kill.policy_epoch, before.inception_high_water
    # Internal query/report snapshots are read-only. Neither resets the account.
    assert (await service._state()).document() == before.document()
    await service.cashflow("independent-month-end", D(oracle["contribution"]), at)
    research.cashflow("independent-month-end", D(oracle["contribution"]), at)
    after = await service._state()
    assert after.quantity("GBP") == research.state.quantity("GBP") == D(oracle["post_flow_cash"])
    assert after.units == research.state.units == D(oracle["post_flow_units"])
    assert (
        nav(after, at)
        == research.result().attribution["ending_nav_gbp"]
        == D(oracle["post_flow_nav"])
    )
    assert unit_price(after, at) == unit_price(before, at)
    assert after.orders == before.orders
    assert after.kill.policy_epoch == epoch and after.inception_high_water == inception
    assert (await service._state()).quantity("BTC") == D(oracle["entry_quantity"])


@pytest.mark.integration
@pytest.mark.parametrize("case", EXPECTED.cases, ids=lambda case: case.name)
async def test_ordered_stops_match_independent_cash_inventory_and_fees(
    account: tuple[PaperExecution, PaperExchange], case: StopCase
) -> None:
    service, venue = account
    await enter(service, venue)
    original_fill: Fill | None = None
    for step in case.steps:
        at = AT + timedelta(milliseconds=step.ms)
        m = market(at, bid=step.bid, depth=step.depth)
        snapshot = await venue.advance(m.book, m.rules, at)
        protection = next(o for o in snapshot.orders if o.intent.purpose == "protection")
        assert protection.triggered
        assert protection.triggered_at == AT + timedelta(milliseconds=500)
        assert protection.filled == step.filled
        assert len(snapshot.fills) == step.fill_count
        assert all(
            f.price >= EXPECTED.protective_limit
            and f.occurred_at >= protection.triggered_at + timedelta(milliseconds=250)
            for f in snapshot.fills
            if f.side == "sell"
        )
        if original_fill is None:
            original_fill = snapshot.fills[0]
        assert snapshot.fills[0] == original_fill  # Later evidence cannot rewrite a fill.
        await service.mark(
            (Valuation(asset="BTC", bid_gbp=step.bid, observed_at=at, source="parity"),), at
        )
        assert (await service.reconcile(at)).complete
        state = await service._state()
        assert state.quantity("GBP") == step.cash
        assert state.quantity("BTC") == EXPECTED.entry_quantity - step.filled
        assert nav(state, at) == step.nav
        assert sum((fee.quantity for fill in snapshot.fills for fee in fill.fees), D(0)) == (
            step.fees
        )
        if state.quantity("BTC"):
            assert protection.state in {"OPEN", "PARTIALLY_FILLED"}
            assert protection.intent.quantity - protection.filled == state.quantity("BTC")
        # Repeating an observation/reconciliation cannot consume depth or post again.
        async with service.writer.transaction() as connection:
            count = len(await service.store.history(connection))
        assert await venue.advance(m.book, m.rules, at) == snapshot
        assert (await service.reconcile(at)).complete
        repeated = await service._state()
        assert (repeated.balances, repeated.units, repeated.orders, repeated.kill) == (
            state.balances,
            state.units,
            state.orders,
            state.kill,
        )
        async with service.writer.transaction() as connection:
            assert len(await service.store.history(connection)) == count


@pytest.mark.integration
async def test_common_executed_trade_matches_bar_research_paper_and_journal(
    account: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = account
    await enter(service, venue)
    at = AT + timedelta(milliseconds=250)
    state = await service.mark(
        (Valuation(asset="BTC", bid_gbp=D(1000), observed_at=at, source="parity"),), at
    )
    data = bars([1000])
    result = simulate(
        data,
        market(AT).rules.model_copy(update={"venue": "fake"}),
        variant=None,
        benchmark_allocation=D("0.01008"),  # £10.08 pays £10 + £0.08, not £10.08 notional.
        start=data[0].start,
        end=data[-1].end,
        initial=D(1000),
        costs=CostModel(spread_slippage_one_way=D(0)),
    )
    assert result.open_quantity == state.quantity("BTC") == D("0.01")
    assert result.fees == D("0.08")
    assert result.observations[-1].nav == nav(state, at) == D("999.92")
    assert result.observations[-1].unit_price == unit_price(state, at) == D("0.99992")
    assert result.episodes == ()  # Open inventory is not a completed episode.
    journal = fill_journal(
        (await venue.snapshot(at)).fills[0], balances([cash_flow("opening", D(1000), AT)])
    )
    assert sum(line.gbp for line in journal.lines) == 0
    assert {
        b.asset: b.quantity for b in balances([cash_flow("opening", D(1000), AT), journal])
    } == {
        "GBP": D("989.92"),
        "BTC": D("0.01"),
        "ETH": D(0),
    }


@pytest.mark.integration
@pytest.mark.parametrize("delay_ms,accepted", [(1999, True), (2000, False)])
async def test_dispatch_approval_boundary_keeps_independent_veto(
    account: tuple[PaperExecution, PaperExchange], delay_ms: int, accepted: bool
) -> None:
    service, venue = account
    order = await service.queue(decision(), market(AT), AT, time_in_force="FOK")
    queued = await service._state()
    assert order.approval is not None and order.approval.allowed
    with pytest.raises(DispatchDenied, match="account paused or uncertain"):
        execution_guard(
            queued.model_copy(update={"ready": False}), order, market(AT), service.policy, AT
        )
    forged = order.model_copy(
        update={"approval": order.approval.model_copy(update={"planned_loss": D(0)})}
    )
    with pytest.raises(DispatchDenied, match="independent planned-loss veto"):
        execution_guard(queued, forged, market(AT), service.policy, AT)
    at = AT + timedelta(milliseconds=delay_ms)
    if accepted:
        await service.dispatch(order.intent.client_id, market(at), at)
        snapshot = await venue.snapshot(at)
        assert len(snapshot.orders) == 1
        assert snapshot.orders[0].intent.client_id == order.intent.client_id
    else:
        with pytest.raises(DispatchDenied, match="stale or mismatched"):
            await service.dispatch(order.intent.client_id, market(at), at)
        assert not (await venue.snapshot(at)).orders


@pytest.mark.integration
async def test_v2_paper_dispatch_can_match_after_its_approval_expires(
    account: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = account
    venue = PaperExchange(service.store, latency_ms=2000)
    service.venue = venue
    value = decision().model_copy(
        update={"strategy_version": "slow-v2", "expires_at": AT + timedelta(seconds=60)}
    )
    # A slow signal remains fresh at +3s. Admission creates a NEW two-second
    # approval, rather than extending an existing approval or replaying a command.
    at = AT + timedelta(seconds=3)
    order = await service.queue(value, market(at), at, time_in_force="FOK")
    assert order.approval is not None
    assert order.approval.expires_at == AT + timedelta(seconds=5)
    assert order.intent.expires_at == AT + timedelta(seconds=8)
    await service.dispatch(order.intent.client_id, market(at), at)
    at = AT + timedelta(seconds=5)
    m = market(at)
    snapshot = await venue.advance(m.book, m.rules, at)
    assert len(snapshot.fills) == 1
    assert snapshot.fills[0].client_id == order.intent.client_id
    assert snapshot.fills[0].quantity == EXPECTED.entry_quantity
    assert snapshot.fills[0].occurred_at == order.approval.expires_at
    assert (await service.reconcile(at)).complete
    assert (await service._state()).quantity("GBP") == EXPECTED.entry_cash
    at = AT + timedelta(seconds=6)
    m = market(at, bid=D(1100))
    await service.mark(
        (Valuation(asset="BTC", bid_gbp=D(1100), observed_at=at, source="parity"),), at
    )
    exit_order = await service.exit_position("BTC/GBP", m, at)
    assert exit_order is not None
    assert exit_order.intent.timing_version == "three-clocks-v1"
    at = AT + timedelta(seconds=8)
    m = market(at, bid=D(1100))
    snapshot = await venue.advance(m.book, m.rules, at)
    assert len(snapshot.fills) == 2
    assert snapshot.fills[-1].client_id == exit_order.intent.client_id
    assert (await service.reconcile(at)).complete
    assert (await service._state()).quantity("BTC") == 0


def test_actual_base_asset_fee_and_partial_disposal_have_independent_basis() -> None:
    opening = cash_flow("opening", D(1000), AT)
    buy = Fill(
        id="independent-buy",
        client_id=uuid4(),
        symbol="BTC/GBP",
        side="buy",
        quantity=D("0.01"),
        price=D(1000),
        occurred_at=AT,
        liquidity="taker",
        fees=(
            Fee(
                asset="BTC",
                quantity=D("0.00001"),
                rate_gbp=D(1000),
                valued_at=AT,
                valuation_source="independent-example",
            ),
        ),
    )
    purchase = fill_journal(buy, balances([opening]))
    initial = balances([opening, purchase])
    assert next(b for b in initial if b.asset == "BTC").quantity == D("0.00999")
    assert next(b for b in initial if b.asset == "BTC").cost_gbp == D("9.99")
    assert next(b for b in initial if b.asset == "GBP").quantity == D(990)
    sell = Fill(
        id="independent-partial",
        client_id=uuid4(),
        symbol="BTC/GBP",
        side="sell",
        quantity=D("0.004"),
        price=D(1200),
        occurred_at=AT,
        liquidity="taker",
        fees=(
            Fee(
                asset="GBP",
                quantity=D("0.0384"),
                rate_gbp=D(1),
                valued_at=AT,
                valuation_source="independent-example",
            ),
        ),
    )
    sale = fill_journal(sell, initial)
    final = apply_journals(initial, (sale,))
    assert next(b for b in final if b.asset == "BTC").quantity == D("0.00599")
    assert next(b for b in final if b.asset == "BTC").cost_gbp == D("5.99")
    assert next(b for b in final if b.asset == "GBP").quantity == D("994.7616")
    assert sum(line.gbp for line in sale.lines if line.account == "pnl:realised") == D("-0.8")
    assert sum(line.gbp for line in sale.lines if line.account == "expense:fees") == D("0.0384")
    assert sum(line.gbp for line in sale.lines) == 0


def test_flows_reporting_split_and_restore_cannot_hide_ten_percent_loss() -> None:
    # A reporting fixture starts with an existing holding; this is not an admitted new order.
    opening = cash_flow("opening", D(1000), AT)
    acquisition = Fill(
        id="holding",
        client_id=uuid4(),
        symbol="BTC/GBP",
        side="buy",
        quantity=D(1),
        price=D(1000),
        occurred_at=AT,
        liquidity="taker",
    )
    history = [opening, fill_journal(acquisition, balances([opening]))]
    state = PortfolioSnapshot(
        account_id=uuid4(),
        version=1,
        as_of=AT,
        balances=balances(history),
        units=D(1000),
        kill=KillSwitchState(policy_epoch=uuid4()),
        daily_day=AT.date(),
        marks=(Valuation(asset="BTC", bid_gbp=D(1000), observed_at=AT, source="parity"),),
    )
    at = AT + timedelta(hours=4)
    state = observe_risk(
        state.model_copy(
            update={
                "marks": (Valuation(asset="BTC", bid_gbp=D(900), observed_at=at, source="parity"),)
            }
        ),
        at,
        RiskPolicy(),
    )
    assert unit_price(state, at) == D("0.9")
    assert state.kill.hard and state.kill.soft and state.kill.daily_day == at.date()
    policy_epoch, latch = state.kill.policy_epoch, state.kill
    for amount, units, value in [(D(90), D(1100), D(990)), (D(-45), D(1050), D(945))]:
        journal = cash_flow(f"flow-{amount}", amount, at)
        issued = flow_units(state, amount, at)
        history.append(journal)
        state = observe_risk(
            state.model_copy(
                update={
                    "balances": apply_journals(state.balances, (journal,)),
                    "units": issued,
                }
            ),
            at,
            RiskPolicy(),
        )
        assert state.units == units
        assert nav(state, at) == value and unit_price(state, at) == D("0.9")
        assert state.kill == latch
        assert state.balances == balances(history)
        # A report/restart serialisation is lossless, including the original episode epoch.
        state = PortfolioSnapshot.restore(state.document())
        assert state.kill.policy_epoch == policy_epoch and entries_paused(state, at)
    tomorrow = AT + timedelta(days=1)
    state = observe_risk(
        state.model_copy(
            update={
                "marks": (
                    Valuation(asset="BTC", bid_gbp=D(1000), observed_at=tomorrow, source="parity"),
                )
            }
        ),
        tomorrow,
        RiskPolicy(),
    )
    assert state.kill.hard and state.kill.soft and state.kill.daily_day is None
    assert state.kill.policy_epoch == policy_epoch  # UTC reset cannot rearm a hard halt.


def test_timed_slow_signal_uses_only_finalised_prefix() -> None:
    data = bars(list(range(100, 301)))
    close = data[199].end
    with pytest.raises(ValueError, match="unsafe"):
        signal(data[:200], "sma200", close + timedelta(seconds=9, milliseconds=999))
    actual = signal(data[:200], "sma200", close + timedelta(seconds=10))
    assert actual.desired == "long"
    assert actual.data_watermark == close
    assert actual.expires_at == close + timedelta(seconds=12)
    assert actual.stop_distance == D("14.95")  # max(2 * ATR 2, 5% of close 299).
    with pytest.raises(ValueError, match="unsafe"):
        signal(data, "sma200", close + timedelta(seconds=10))


def test_p4a05_all_observation_metric_recovers_intraday_drawdown() -> None:
    result = Backtest(
        observations=(
            Observation(AT + timedelta(hours=4), D(900), D("0.9"), D(0), D(0)),
            Observation(AT + timedelta(days=1), D(1000), D(1), D(0), D(0)),
        ),
        episodes=(),
        fees=D(0),
        turnover=D(0),
        skips=0,
        halts=("hard_drawdown",),
        open_quantity=D(0),
        unfilled_stop_bars=0,
        interval_minutes=240,
    )
    # The original daily-only calculation returned zero for this path.
    assert metrics(result, initial=1000)["max_drawdown"] == pytest.approx(0.1)
    with localcontext(FINANCIAL_CONTEXT):
        required = max(D(1) - observation.unit_price / D(1) for observation in result.observations)
    assert required == D("0.1")


def test_p4a01_baseline_probe_detects_extra_bar_before_long_entry() -> None:
    data = bars([1000] * 199 + [1100] * 4)
    at = data[199].end + timedelta(seconds=10)
    actual = signal(data[:200], "sma200", at)
    assert actual.desired == "long"
    assert data[200].start < at < data[200].end
    first = simulate(
        data[:201],
        market(AT).rules,
        variant="sma200",
        start=data[200].start,
        end=data[200].end,
    )
    extended = simulate(
        data,
        market(AT).rules,
        variant="sma200",
        start=data[200].start,
        end=data[-1].end,
    )
    # Legacy research has no post-finalisation observation inside this bar. The
    # ordered paper path can match after 250ms; that difference remains open.
    assert first.open_quantity == 0
    assert first.observations == extended.observations[:1]
    assert extended.open_quantity > 0


def test_p4a06_baseline_probe_names_minimum_price_exit_stress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = list(bars([1000] * 204))
    data = [bar.model_copy(update={"volume": D(1)}) for bar in data]
    data[202] = data[202].model_copy(update={"low": D(950)})
    data[203] = data[203].model_copy(
        update={"open": D(960), "high": D(961), "low": D(959), "close": D(960)}
    )

    def prescribed(prefix: tuple[Candle, ...], variant: Variant, at: datetime) -> StrategySignal:
        # Isolate execution using the same declared long decision and £50 stop
        # distance as paper. This is not a newly evaluated trading definition.
        return StrategySignal(
            strategy=variant,
            symbol=prefix[-1].symbol,
            decision_at=at,
            data_watermark=prefix[-1].end,
            desired=decision().desired,
            stop_distance=decision().stop_distance,
            expires_at=at + timedelta(seconds=2),
        )

    monkeypatch.setattr("aca.simulation.reference.signal", prescribed)
    result = simulate(
        tuple(data),
        market(AT).rules,
        variant="sma200",
        start=data[201].start,
        end=data[-1].end,
        initial=D(1000),
        costs=CostModel(spread_slippage_one_way=D(0)),
    )
    assert len(result.episodes) == 1 and result.open_quantity == 0
    assert result.episodes[0].quantity == D("0.01")
    assert result.episodes[0].exit_price == D("940.5")  # Legacy stop 950, 1% offset.
    assert result.observations[-1].nav == D("999.24976")
    # Paper's recovered-bid fixture receives the later executable bid 960. The
    # difference remains a named pessimistic stress, not corrected event replay.
    assert result.observations[-1].nav < EXPECTED.cases[0].steps[-1].cash


def test_versioned_bounds_are_shared_without_importing_into_independent_risk() -> None:
    assert BOUNDS.version == "simulation-bounds-v1"
    assert (BOUNDS.minimum_latency_ms, BOUNDS.maximum_latency_ms) == (250, 60_000)
    assert BOUNDS.maximum_participation == D("0.01")
    assert (LEGACY_BAR_MODEL, PAPER_MODEL) == (
        "legacy-flat-window-bar-taker-v2",
        "paper-ordered-depth-queue-v1",
    )
    with pytest.raises(FrozenInstanceError):
        BOUNDS.minimum_latency_ms = 0  # type: ignore[misc]  # intentional frozen mutation
    for path in Path("src/aca/risk").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("aca.simulation")
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("aca.simulation") for alias in node.names)
