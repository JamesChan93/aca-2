"""Real PostgreSQL paper commands, crash boundaries and reconciliation faults."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from aca.domain.market import InstrumentRules, Level, MarketTrade, OrderBook
from aca.domain.trading import OrderIntent, PortfolioSnapshot, Valuation, VenueOrder, VenueSnapshot
from aca.execution.paper import PaperExecution, SimulatedCrash
from aca.operations.config import Settings
from aca.portfolio.state import reserved_cash
from aca.risk.execution import DispatchDenied, MarketEvidence, execution_guard
from aca.simulation.paper import PaperExchange
from aca.storage.database import create_engine, verify_runtime
from aca.storage.paper import (
    PaperStore,
    PaperWriter,
    WriterUnavailable,
    journals,
)
from aca.strategies.slow import StrategySignal

pytestmark = pytest.mark.integration
D = Decimal
AT = datetime(2026, 10, 18, tzinfo=UTC)
EPOCH = uuid4()


def market(
    at: datetime = AT, *, bid: str = "999", ask: str = "1000", depth: str = "10"
) -> MarketEvidence:
    rules = InstrumentRules(
        venue="fake-venue",
        symbol="BTC/GBP",
        venue_symbol="Bitcoin-Pounds",
        rest_pair="not-a-kraken-symbol",
        price_increment=D("0.1"),
        quantity_increment=D("0.00000001"),
        minimum_quantity=D("0.00006"),
        minimum_cost=D("0.43"),
        status="online",
        observed_at=AT,
    )
    book = OrderBook(
        venue="fake-venue",
        symbol="BTC/GBP",
        bids=(Level(price=D(bid), quantity=D(depth)),),
        asks=(Level(price=D(ask), quantity=D(depth)),),
        checksum_valid=True,
        event_at=at,
        received_at=at,
        epoch=EPOCH,
    )
    return MarketEvidence(book=book, rules=rules)


def signal(at: datetime = AT) -> StrategySignal:
    return StrategySignal(
        strategy="sma200",
        symbol="BTC/GBP",
        decision_at=at,
        data_watermark=at - timedelta(seconds=10),
        desired="long",
        stop_distance=D(50),
        expires_at=at + timedelta(seconds=2),
    )


@pytest.fixture
async def paper(database_settings: Settings) -> AsyncIterator[tuple[PaperExecution, PaperExchange]]:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as writer:
            venue = PaperExchange(store)
            service = PaperExecution(writer, venue)
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            await service.mark(
                (Valuation(asset="BTC", bid_gbp=D(999), observed_at=AT, source="fixture"),), AT
            )
            assert (await service.recover(AT)).complete
            yield service, venue
    finally:
        await engine.dispose()


async def filled_position(service: PaperExecution, venue: PaperExchange) -> UUID:
    order = await service.queue(signal(), market(), AT, time_in_force="FOK")
    await service.dispatch(order.intent.client_id, market(), AT)
    at = AT + timedelta(milliseconds=300)
    m = market(at)
    await venue.advance(m.book, m.rules, at)
    await service.mark(
        (Valuation(asset="BTC", bid_gbp=D(999), observed_at=at, source="fixture"),), at
    )
    result = await service.reconcile(at)
    assert result.complete, result.differences
    return order.intent.client_id


async def test_paper_full_lifecycle_fee_posting_and_restart(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    client = await filled_position(service, venue)
    state = await service._state()
    assert state.order(client).state == "FILLED"
    assert state.quantity("BTC") > 0
    assert len([o for o in state.orders if o.intent.purpose == "protection"]) == 1
    assert reserved_cash(state) == 0
    async with service.writer.transaction() as c:
        count = len(await service.store.history(c))
    await service.reconcile(state.as_of)
    async with service.writer.transaction() as c:
        assert len(await service.store.history(c)) == count
    at = AT + timedelta(milliseconds=400)
    exit_order = await service.exit_position("BTC/GBP", market(at), at)
    assert exit_order is not None
    later = AT + timedelta(milliseconds=700)
    await venue.advance(market(later).book, market(later).rules, later)
    assert (await service.reconcile(later)).complete
    final = await service._state()
    assert final.quantity("BTC") == 0
    assert all(o.state in {"FILLED", "CANCELLED"} for o in final.orders)
    assert final.quantity("GBP") < 1000
    # A fresh recovery changes operational readiness, never financial history.
    assert (await PaperExecution(service.writer, venue).recover(later)).complete
    assert (await service._state()).balances == final.balances


@pytest.mark.parametrize(
    "point,accepted", [("after_submitting_before_send", False), ("after_send_before_ack", True)]
)
async def test_consumed_command_crash_never_replayed(
    paper: tuple[PaperExecution, PaperExchange], point: str, accepted: bool
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT)

    def crash(where: str) -> None:
        if where == point:
            raise SimulatedCrash()

    service.fault = crash
    with pytest.raises(SimulatedCrash):
        await service.dispatch(order.intent.client_id, market(), AT)
    assert (await service._state()).order(order.intent.client_id).state == "SUBMITTING"
    result = await PaperExecution(service.writer, venue).recover(AT)
    assert result.complete is accepted
    state = await service._state()
    assert state.order(order.intent.client_id).state == ("OPEN" if accepted else "UNKNOWN")
    assert reserved_cash(state) > 0
    with pytest.raises(DispatchDenied):
        await service.dispatch(order.intent.client_id, market(), AT)
    assert len((await venue.snapshot(AT)).orders) == int(accepted)


async def test_crash_before_submitting_expires_unconsumed_work(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper

    def crash(where: str) -> None:
        if where == "before_submitting":
            raise SimulatedCrash()

    service.fault = crash
    with pytest.raises(SimulatedCrash):
        await service.queue(signal(), market(), AT)
    assert (await PaperExecution(service.writer, venue).recover(AT)).complete
    assert (await service._state()).orders[-1].state == "EXPIRED"
    assert reserved_cash(await service._state()) == 0
    assert not (await venue.snapshot(AT)).orders


class LostResponse:
    def __init__(self, venue: PaperExchange) -> None:
        self.venue, self.calls = venue, 0

    async def submit(self, intent: OrderIntent, at: datetime) -> VenueOrder:
        self.calls += 1
        await self.venue.submit(intent, at)
        raise TimeoutError("synthetic lost response")

    async def cancel(self, client_id: UUID, at: datetime) -> VenueOrder:
        result = await self.venue.cancel(client_id, at)
        return result

    async def snapshot(self, at: datetime) -> VenueSnapshot:
        return await self.venue.snapshot(at)


async def test_timeout_reconciles_closed_id_without_resubmission(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    lost = LostResponse(venue)
    service.venue = lost
    order = await service.queue(signal(), market(), AT, time_in_force="FOK")
    assert (await service.dispatch(order.intent.client_id, market(), AT)).state == "UNKNOWN"
    at = AT + timedelta(milliseconds=300)
    await venue.advance(market(at).book, market(at).rules, at)
    assert (await service.recover(at)).complete
    assert (await service._state()).order(order.intent.client_id).state == "FILLED"
    assert lost.calls == 1
    with pytest.raises(DispatchDenied):
        await service.dispatch(order.intent.client_id, market(at), at)
    assert lost.calls == 1


async def test_partial_fill_cancel_preserves_protection_and_reservation(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT)
    await service.dispatch(order.intent.client_id, market(), AT)
    at = AT + timedelta(milliseconds=300)
    await venue.advance(market(at, depth="0.01").book, market().rules, at)
    at += timedelta(milliseconds=100)
    # Consume displayed queue, then 1% participation in the remaining aggression.
    trade = MarketTrade(
        venue="fake-venue",
        symbol="BTC/GBP",
        trade_id="aggressive-sell",
        side="sell",
        price=D(999),
        quantity=D("0.11"),
        event_at=at,
        received_at=at,
    )
    m = market(at, depth="0.01")
    await venue.advance(m.book, m.rules, at, trade)
    assert (await service.reconcile(at)).complete
    partial = await service._state()
    assert 0 < partial.order(order.intent.client_id).filled < order.intent.quantity
    assert reserved_cash(partial) > 0
    await service.cancel(order.intent.client_id, at)
    final = await service._state()
    assert final.order(order.intent.client_id).state == "CANCELLED"
    assert final.quantity("BTC") == partial.quantity("BTC")
    assert reserved_cash(final) == 0
    assert sum(
        o.remaining for o in final.orders if o.intent.purpose == "protection"
    ) == final.quantity("BTC")
    # Exact event replay cannot create another economic posting or trade fill.
    snapshot = await venue.advance(m.book, m.rules, at, trade)
    assert len(snapshot.fills) == 1
    assert (await service.reconcile(at)).complete


async def test_hard_halt_preserves_exit_and_requires_review(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    await filled_position(service, venue)
    at = AT + timedelta(milliseconds=400)
    await service.halt("Synthetic incident review", at)
    state = await service._state()
    assert state.kill.hard
    assert any(o.intent.purpose == "protection" and o.state == "OPEN" for o in state.orders)
    await service.cashflow("deposit-after-halt", D(100), at)
    await service.recover(at)
    assert (await service._state()).kill.hard
    with pytest.raises(DispatchDenied):
        await service.queue(signal(at), market(at), at)
    with pytest.raises(DispatchDenied):
        await service.exit_position("BTC/GBP", market(at), at)
    epoch = (await service._state()).kill.policy_epoch
    await service.review("Reviewed synthetic fault and all remaining protection", at)
    reviewed = await service._state()
    assert not reviewed.kill.hard and reviewed.kill.policy_epoch != epoch
    assert reviewed.inception_high_water >= reviewed.high_water


async def test_stop_limit_gap_does_not_receive_fictitious_fill(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    await filled_position(service, venue)
    at = AT + timedelta(seconds=1)
    gap = market(at, bid="800", ask="801")
    snapshot = await venue.advance(gap.book, gap.rules, at)
    assert len(snapshot.fills) == 1
    assert next(o for o in snapshot.orders if o.intent.purpose == "protection").triggered
    later = at + timedelta(milliseconds=300)
    gap = market(later, bid="800", ask="801")
    assert len((await venue.advance(gap.book, gap.rules, later)).fills) == 1
    recover = later + timedelta(milliseconds=300)
    m = market(recover, bid="950", ask="951")
    assert len((await venue.advance(m.book, m.rules, recover)).fills) == 2
    await service.mark(
        (Valuation(asset="BTC", bid_gbp=D(950), observed_at=recover, source="fixture"),), recover
    )
    assert (await service.reconcile(recover)).complete
    assert (await service._state()).quantity("BTC") == 0


@pytest.mark.parametrize(
    "problem",
    [
        "stale_quote",
        "stale_account",
        "stale_reconciliation",
        "clock",
        "rules",
        "offline",
        "state_version",
        "price",
        "approval_expiry",
    ],
)
async def test_execution_guard_independent_vetoes(
    paper: tuple[PaperExecution, PaperExchange], problem: str
) -> None:
    service, _ = paper
    order = await service.queue(signal(), market(), AT)
    state = await service._state()
    evidence = market()
    at = AT
    if problem == "stale_quote":
        at += timedelta(seconds=6)
    elif problem == "stale_account":
        state = state.model_copy(update={"account_evidence_at": AT - timedelta(seconds=31)})
    elif problem == "stale_reconciliation":
        state = state.model_copy(update={"reconciled_at": AT - timedelta(seconds=61)})
    elif problem == "clock":
        evidence = MarketEvidence(book=evidence.book, rules=evidence.rules, clock_offset_ms=D(501))
    elif problem == "rules":
        evidence = MarketEvidence(
            book=evidence.book, rules=evidence.rules.model_copy(update={"minimum_quantity": D(1)})
        )
    elif problem == "offline":
        evidence = MarketEvidence(book=evidence.book, rules=evidence.rules, system_online=False)
    elif problem == "state_version":
        state = state.model_copy(update={"version": state.version + 1})
    elif problem == "price":
        order = order.model_copy(
            update={"intent": order.intent.model_copy(update={"limit_price": D(2000)})}
        )
    else:
        at += timedelta(seconds=2)
    with pytest.raises(DispatchDenied):
        execution_guard(state, order, evidence, service.policy, at)


async def test_writer_exclusion_connection_loss_and_runtime_restrictions(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    await verify_runtime(service.store.engine)
    with pytest.raises(WriterUnavailable):
        async with PaperWriter(service.store):
            pass
    for table in (
        "paper_events",
        "paper_journals",
        "paper_journal_lines",
        "paper_movements",
        "paper_fills",
        "paper_venue_events",
        "paper_alerts",
    ):
        async with service.store.engine.begin() as c:
            with pytest.raises(DBAPIError):
                await c.execute(
                    text("DELETE FROM aca." + table)  # noqa: S608 - fixed test allowlist
                )
    assert service.writer.connection is not None
    await service.writer.connection.invalidate()
    with pytest.raises(WriterUnavailable):
        await service.queue(signal(), market(), AT)


async def test_balanced_journal_constraint_and_duplicate_flow(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    before = await service._state()
    await service.cashflow("opening", D(1000), AT)
    assert await service._state() == before
    with pytest.raises(ValueError, match="conflicting"):
        await service.cashflow("opening", D(2000), AT)
    # The database independently refuses a journal without balanced lines.
    with pytest.raises(DBAPIError):
        async with service.store.engine.begin() as c:
            await c.execute(
                journals.insert().values(
                    id=uuid4(),
                    account_id=service.store.account_id,
                    source="unbalanced-injected",
                    occurred_at=AT,
                    document={},
                )
            )
    async with service.store.engine.connect() as c:
        assert (
            await c.scalar(
                select(func.count())
                .select_from(journals)
                .where(journals.c.source == "unbalanced-injected")
            )
            == 0
        )


async def test_unknown_snapshot_and_missing_protection_fail_closed(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    await filled_position(service, venue)
    protective = next(
        o for o in (await venue.snapshot(AT)).orders if o.intent.purpose == "protection"
    )
    at = AT + timedelta(milliseconds=400)
    await venue.cancel(protective.intent.client_id, at)
    result = await service.reconcile(at)
    assert not result.complete
    assert "unprotected_or_overreserved_inventory" in result.differences
    assert not (await service._state()).ready
    with pytest.raises(DispatchDenied):
        await service.queue(signal(at), market(at), at)


async def test_ledger_projection_mismatch_is_a_blocker(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, _ = paper
    state = await service._state()
    bad = state.model_copy(update={"units": D(10000)})
    with pytest.raises(ValueError, match="units"):
        await service._save(state, bad, at=AT, kind="injected")
    assert await service._state() == state


async def test_demo_replay_uses_shared_signals_and_accounting(
    paper: tuple[PaperExecution, PaperExchange], database_settings: Settings
) -> None:
    from aca.simulation.replay import ReplayPlan, demo_plan, run_replay

    plan = demo_plan(uuid4())
    assert ReplayPlan.restore(plan.document()) == plan
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as writer:
            venue = PaperExchange(store)
            result = await run_replay(PaperExecution(writer, venue), venue, plan)
        assert result["live_execution_available"] is False
        assert result["promotion_evidence"] is False
        assert result["blocked_steps"] == 0
        document = result["state"]
        assert isinstance(document, dict)
        state = PortfolioSnapshot.restore(document)
        assert state.quantity("BTC") == 0 and state.quantity("GBP") < 250
        assert len(state.orders) == 3
    finally:
        await engine.dispose()


async def test_connection_terminated_before_dispatch_cannot_transmit(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT)
    async with service.store.engine.begin() as c:
        await c.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": service.writer.pid})
    with pytest.raises(DBAPIError):
        await service.dispatch(order.intent.client_id, market(), AT)
    assert not (await venue.snapshot(AT)).orders


async def test_cancel_fill_race_imports_economics_before_release(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT, time_in_force="FOK")
    await service.dispatch(order.intent.client_id, market(), AT)
    at = AT + timedelta(milliseconds=300)
    # Fill is authoritative but local financial posting has not happened.
    await venue.advance(market(at).book, market(at).rules, at)
    assert (await service._state()).quantity("BTC") == 0
    assert reserved_cash(await service._state()) > 0
    await service.cancel(order.intent.client_id, at)
    state = await service._state()
    assert state.order(order.intent.client_id).state == "FILLED"
    assert state.quantity("BTC") == order.intent.quantity
    assert reserved_cash(state) == 0


async def test_incomplete_snapshot_and_conflicting_fill_block(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    await filled_position(service, venue)

    class EvidenceFault(LostResponse):
        incomplete = True

        async def snapshot(self, at: datetime) -> VenueSnapshot:
            original = await self.venue.snapshot(at)
            if self.incomplete:
                return original.model_copy(update={"complete": False})
            altered = original.fills[0].model_copy(update={"price": D(1)})
            return original.model_copy(update={"fills": (altered,)})

    bad = EvidenceFault(venue)
    service.venue = bad
    at = AT + timedelta(milliseconds=400)
    initial = await service._state()
    assert not (await service.reconcile(at)).complete
    assert not (await service._state()).ready
    bad.incomplete = False
    assert not (await service.reconcile(at)).complete
    assert (await service._state()).balances == initial.balances


async def test_consumption_transaction_failure_has_zero_venue_calls(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT)
    # An injected read-only transaction behaves like a failed durable write.
    assert service.writer.connection is not None
    await service.writer.connection.execute(text("SET default_transaction_read_only=on"))
    await service.writer.connection.commit()
    try:
        with pytest.raises(DBAPIError):
            await service.dispatch(order.intent.client_id, market(), AT)
        assert not (await venue.snapshot(AT)).orders
    finally:
        await service.writer.connection.execute(text("SET default_transaction_read_only=off"))
        await service.writer.connection.commit()


async def test_existing_journal_cannot_gain_additional_lines_or_movements(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    from aca.storage.paper import lines, movements

    service, _ = paper
    async with service.writer.transaction() as c:
        journal = (await service.store.history(c))[0]
    with pytest.raises(DBAPIError):
        async with service.store.engine.begin() as c:
            await c.execute(
                lines.insert().values(
                    journal_id=journal.id, number=99, account="asset:GBP", gbp=D(0)
                )
            )
    with pytest.raises(DBAPIError):
        async with service.store.engine.begin() as c:
            await c.execute(
                movements.insert().values(
                    journal_id=journal.id, number=99, asset="BTC", quantity=D(0)
                )
            )


async def test_tiny_partial_does_not_relax_protective_minimum(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT)
    await service.dispatch(order.intent.client_id, market(), AT)
    at = AT + timedelta(milliseconds=300)
    await venue.advance(market(at, depth="0.0001").book, market().rules, at)
    at += timedelta(milliseconds=100)
    m = market(at, depth="0.0001")
    trade = MarketTrade(
        venue="fake-venue",
        symbol="BTC/GBP",
        trade_id="dust",
        side="sell",
        price=D(999),
        quantity=D("0.00011"),
        event_at=at,
        received_at=at,
    )
    await venue.advance(m.book, m.rules, at, trade)
    result = await service.reconcile(at)
    state = await service._state()
    assert not result.complete and not state.ready
    assert 0 < state.quantity("BTC") < m.rules.minimum_quantity
    assert state.order(order.intent.client_id).state == "CANCELLED"
    assert not any(o.intent.purpose == "protection" for o in state.orders)


async def test_positive_rejection_releases_reservation_without_retry(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    from aca.ports.execution import DefinitiveRejection

    service, venue = paper

    class Rejected(LostResponse):
        async def submit(self, intent: OrderIntent, at: datetime) -> VenueOrder:
            self.calls += 1
            raise DefinitiveRejection("post_only")

    rejected = Rejected(venue)
    service.venue = rejected
    order = await service.queue(signal(), market(), AT)
    assert (await service.dispatch(order.intent.client_id, market(), AT)).state == "REJECTED"
    assert reserved_cash(await service._state()) == 0
    assert rejected.calls == 1
    assert not (await venue.snapshot(AT)).orders


async def test_late_partial_cannot_erase_uncertain_cancellation(
    paper: tuple[PaperExecution, PaperExchange],
) -> None:
    service, venue = paper
    order = await service.queue(signal(), market(), AT)
    await service.dispatch(order.intent.client_id, market(), AT)
    at = AT + timedelta(milliseconds=300)
    await venue.advance(market(at, depth="0.01").book, market().rules, at)

    class LostCancel(LostResponse):
        async def cancel(self, client_id: UUID, at: datetime) -> VenueOrder:
            self.calls += 1
            raise TimeoutError("synthetic uncertain cancellation")

    lost = LostCancel(venue)
    service.venue = lost
    await service.cancel(order.intent.client_id, at)
    at += timedelta(milliseconds=100)
    trade = MarketTrade(
        venue="fake-venue",
        symbol="BTC/GBP",
        trade_id="racing-fill",
        side="sell",
        price=D(999),
        quantity=D("0.11"),
        event_at=at,
        received_at=at,
    )
    await venue.advance(market(at, depth="0.01").book, market().rules, at, trade)
    result = await service.reconcile(at)
    state = await service._state()
    local = state.order(order.intent.client_id)
    assert not result.complete and "unresolved_cancellation" in result.differences
    assert local.filled > 0 and local.state == "UNKNOWN" and local.pending_operation == "cancel"
    assert lost.calls == 1 and reserved_cash(state) > 0
