"""Disposable test process. Never imported by the application or used on retained accounts."""

import asyncio
import json
import signal as os_signal
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.domain.market import InstrumentRules, Level, MarketTrade, OrderBook
from aca.domain.trading import Fill, Journal, OrderIntent, PortfolioSnapshot, Valuation, VenueOrder
from aca.execution.paper import PaperExecution
from aca.operations.config import DatabaseSettings, Settings
from aca.portfolio.reserves import TaxReserveRecord
from aca.portfolio.state import reserved_cash
from aca.risk.execution import DispatchDenied, MarketEvidence
from aca.simulation.paper import PaperExchange
from aca.storage.database import create_engine, verify_runtime
from aca.storage.paper import PaperStore, PaperWriter, fills
from aca.strategies.slow import StrategySignal

AT = datetime(2026, 10, 19, tzinfo=UTC)
D = Decimal
EPOCH = UUID("29dba699-ec2a-43ac-9bda-25220b7b564f")


def emit(event: str, **detail: object) -> None:
    print(json.dumps({"event": event, **detail}, sort_keys=True), flush=True)


def gate(point: str) -> None:
    # A synchronous gate deliberately prevents any more work on the writer loop.
    # SIGKILL bypasses all finally blocks; the parent must observe return code -9.
    emit("paused", point=point)
    if sys.stdin.readline() != "resume\n":
        raise RuntimeError("test gate requires an explicit resume")


def market(at: datetime = AT, *, depth: str = "10") -> MarketEvidence:
    rules = InstrumentRules(
        venue="process-test-venue",
        symbol="BTC/GBP",
        venue_symbol="Bitcoin-Pounds",
        rest_pair="test-only",
        price_increment=D("0.1"),
        quantity_increment=D("0.00000001"),
        minimum_quantity=D("0.00006"),
        minimum_cost=D("0.43"),
        status="online",
        observed_at=AT,
    )
    book = OrderBook(
        venue=rules.venue,
        symbol=rules.symbol,
        bids=(Level(price=D(999), quantity=D(depth)),),
        asks=(Level(price=D(1000), quantity=D(depth)),),
        checksum_valid=True,
        event_at=at,
        received_at=at,
        epoch=EPOCH,
    )
    return MarketEvidence(book=book, rules=rules)


def proposal(at: datetime = AT) -> StrategySignal:
    return StrategySignal(
        strategy="sma200",
        symbol="BTC/GBP",
        decision_at=at,
        data_watermark=at - timedelta(seconds=10),
        desired="long",
        stop_distance=D(50),
        expires_at=at + timedelta(seconds=2),
    )


class GateStore(PaperStore):
    stop_kind: str | None = None

    async def append(
        self,
        connection: AsyncConnection,
        previous: PortfolioSnapshot | None,
        state: PortfolioSnapshot,
        *,
        source: str,
        kind: str,
        journal: Journal | None = None,
        alert: bool = False,
        tax_reserve: TaxReserveRecord | None = None,
    ) -> None:
        await super().append(
            connection,
            previous,
            state,
            source=source,
            kind=kind,
            journal=journal,
            alert=alert,
            tax_reserve=tax_reserve,
        )
        if kind == self.stop_kind:
            gate("uncommitted_" + kind)


class ObservedVenue(PaperExchange):
    fill_before_reply: bool = False
    partial: bool = False
    race_cancel: bool = False
    pause_cancel: bool = False
    forbid_commands: bool = False

    async def advance_fixture(self, *, partial: bool) -> None:
        at = AT + timedelta(milliseconds=300)
        evidence = market(at, depth="0.01" if partial else "10")
        await self.advance(evidence.book, evidence.rules, at)
        if partial:
            at += timedelta(milliseconds=100)
            evidence = market(at, depth="0.01")
            trade = MarketTrade(
                venue=evidence.rules.venue,
                symbol="BTC/GBP",
                trade_id="process-test-aggression",
                side="sell",
                price=D(999),
                quantity=D("0.11"),
                event_at=at,
                received_at=at,
            )
            await self.advance(evidence.book, evidence.rules, at, trade)

    async def submit(self, intent: OrderIntent, at: datetime) -> VenueOrder:
        emit("submit_attempt", client_id=str(intent.client_id))
        if self.forbid_commands:
            raise AssertionError("recovery must not submit")
        order = await super().submit(intent, at)
        if self.fill_before_reply:
            await self.advance_fixture(partial=self.partial)
        return order

    async def cancel(self, client_id: UUID, at: datetime) -> VenueOrder:
        emit("cancel_attempt", client_id=str(client_id))
        if self.forbid_commands:
            raise AssertionError("fixture recovery must not cancel")
        if self.pause_cancel:
            gate("cancel_before_send")
        if self.race_cancel:
            await self.advance_fixture(partial=False)
        return await super().cancel(client_id, at)


class GateExecution(PaperExecution):
    stop_posted: bool = False
    stop_halt: bool = False

    async def _post_fill(self, fill: Fill, at: datetime) -> None:
        await super()._post_fill(fill, at)
        if self.stop_posted:
            gate("committed_fill")

    async def cancel_entries(self, at: datetime) -> None:
        if self.stop_halt:
            gate("committed_halt")
        await super().cancel_entries(at)


async def recover(settings: Settings, account: UUID) -> None:
    engine = create_engine(settings)
    try:
        await verify_runtime(engine)
        store = PaperStore(engine, account)
        async with PaperWriter(store) as writer:
            venue = ObservedVenue(store)
            venue.forbid_commands = True
            execution = PaperExecution(writer, venue)
            before = await execution._state()
            at = AT + timedelta(seconds=1)
            result = await execution.recover(at)
            state = await execution._state()
            # Even a resolved OPEN/FILLED/CANCELLED ID must never be dispatchable.
            if state.orders:
                try:
                    original = next(o for o in state.orders if o.intent.purpose == "entry")
                    await execution.dispatch(original.intent.client_id, market(at), at)
                except DispatchDenied:
                    pass
                else:
                    raise AssertionError("recovered command was dispatchable")
            if state.kill.hard or not result.complete:
                try:
                    await execution.queue(proposal(at), market(at), at)
                except DispatchDenied:
                    pass
                else:
                    raise AssertionError("halted or uncertain account admitted a new entry")
            # A denied admission may append its risk-rejection audit event.
            state = await execution._state()
            async with writer.transaction() as c:
                assert await store.audit(c) == state
                journal_docs = [j.document() for j in await store.history(c)]
                fill_docs = list(
                    await c.scalars(select(fills.c.document).where(fills.c.account_id == account))
                )
            emit(
                "recovered",
                before=before.document(),
                state=state.document(),
                result=result.document(),
                reservation=str(reserved_cash(state)),
                journals=journal_docs,
                fills=fill_docs,
                venue=(await venue.snapshot(at)).document(),
            )
    finally:
        await engine.dispose()


async def exercise(settings: Settings, account: UUID, scenario: str) -> None:
    engine = create_engine(settings)
    try:
        await verify_runtime(engine)
        store = GateStore(engine, account)
        async with PaperWriter(store) as writer:
            venue = ObservedVenue(store)
            execution = GateExecution(writer, venue)
            await execution.initialise(AT)
            await execution.cashflow("opening", D(1000), AT)
            await execution.mark(
                (Valuation(asset="BTC", bid_gbp=D(999), observed_at=AT, source="process-test"),),
                AT,
            )
            assert (await execution.recover(AT)).complete
            emit("writer", backend_pid=writer.pid)
            if scenario == "uncommitted_intent":
                store.stop_kind = "intent_reserved"
            elif scenario in {
                "before_submitting",
                "after_submitting_before_send",
                "after_send_before_ack",
            }:
                execution.fault = lambda point: gate(point) if point == scenario else None
            elif scenario in {"filled_before_ack", "partial_before_ack"}:
                venue.fill_before_reply = True
                venue.partial = scenario == "partial_before_ack"
                execution.fault = lambda point: (
                    gate(scenario) if point == "after_send_before_ack" else None
                )
            passive = scenario in {"partial_before_ack", "cancel_partial", "cancel_before_send"}
            order = await execution.queue(
                proposal(), market(), AT, time_in_force="GTD" if passive else "FOK"
            )
            emit("intent", client_id=str(order.intent.client_id))
            try:
                await execution.dispatch(order.intent.client_id, market(), AT)
            except Exception as exc:
                # Used only when the parent terminates this writer's backend and
                # resumes it. A physical connection loss must not silently rearm.
                emit("dispatch_failed", error_type=type(exc).__name__)
                return
            if scenario == "cancel_racing_fill":
                venue.race_cancel = True
            else:
                await venue.advance_fixture(partial=passive)
            at = AT + timedelta(milliseconds=500)
            if scenario == "uncommitted_fill":
                store.stop_kind = "confirmed_fill"
            elif scenario == "committed_fill":
                execution.stop_posted = True
            if scenario == "cancel_racing_fill":
                execution.fault = lambda point: (
                    gate(scenario) if point == "after_cancel_before_ack" else None
                )
                await execution.cancel(order.intent.client_id, at)
            else:
                assert (await execution.reconcile(at)).complete
                if scenario in {"cancel_partial", "cancel_before_send"}:
                    venue.pause_cancel = scenario == "cancel_before_send"
                    execution.fault = lambda point: (
                        gate(scenario) if point == "after_cancel_before_ack" else None
                    )
                    await execution.cancel(order.intent.client_id, at)
                elif scenario == "committed_halt":
                    execution.stop_halt = True
                    await execution.halt("Process-kill recovery fixture", at)
            raise AssertionError("fixture failed to reach its requested crash point")
    finally:
        await engine.dispose()


def main() -> int:
    # No admin URL, passwords or engine objects are passed through argv/environment.
    # Settings contain a private file path for the restricted test runtime role.
    os_signal.alarm(60)
    settings = Settings(database=DatabaseSettings.model_validate_json(sys.stdin.readline()))
    if settings.database.host not in {"127.0.0.1", "localhost", "::1"} or not (
        settings.database.name.startswith("aca_test_")
    ):
        raise ValueError("worker only supports a disposable loopback test database")
    scenario, account = sys.argv[1], UUID(sys.argv[2])
    asyncio.run(
        recover(settings, account)
        if scenario == "recover"
        else exercise(settings, account, scenario)
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        emit("failed", error_type=type(exc).__name__)
        raise SystemExit(1) from None
