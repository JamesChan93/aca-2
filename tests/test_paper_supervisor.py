"""Supervision uses real persistence; unsafe evidence never grants readiness."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from aca.app.paper_supervisor import (
    PaperSupervisor,
    SupervisorHealthServer,
    SupervisorMetrics,
    SupervisorStatus,
)
from aca.domain.market import InstrumentRules, Level, OrderBook
from aca.domain.trading import Valuation
from aca.execution.paper import PaperExecution
from aca.market_data.paper_feed import FeedUnavailable, StoredPaperFeed
from aca.market_data.store import MarketStore
from aca.operations.config import Settings
from aca.simulation.paper import PaperExchange
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter, events, venue_events

AT = datetime(2026, 10, 18, tzinfo=UTC)
D = Decimal
_times = count(30)


@pytest.fixture
async def service(database_settings: Settings) -> AsyncIterator[PaperExecution]:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as owner:
            execution = PaperExecution(owner, PaperExchange(store))
            await execution.initialise(AT)
            await execution.cashflow("opening", D(250), AT)
            yield execution
    finally:
        await engine.dispose()


async def capacity_ok() -> None:
    pass


async def no_feed(at: datetime) -> tuple[Valuation, ...]:
    raise FeedUnavailable("fixture")


async def fresh_marks(at: datetime) -> tuple[Valuation, ...]:
    return (Valuation(asset="BTC", bid_gbp=D(60000), observed_at=at, source="fixture"),)


@pytest.mark.integration
async def test_supervisor_stale_feed_is_durable_deduplicated_and_recovers(
    service: PaperExecution,
) -> None:
    status = SupervisorStatus()
    supervisor = PaperSupervisor(
        service,
        status,
        marks=no_feed,
        capacity=capacity_ok,
        clock=lambda: AT,
    )
    await supervisor.start()
    await supervisor.tick()
    state = await service._state()
    assert not state.ready and state.incident == "paper_feed_unavailable"
    assert not status.supervision_ready
    for _ in range(3):
        await supervisor.tick()
    assert await service._state() == state  # No per-second duplicate faults or alerts.
    supervisor.marks = fresh_marks
    await supervisor.tick()
    assert status.supervision_ready
    assert (await service._state()).ready
    assert status.document()["trading_ready"] is False
    assert status.document()["strategy_dispatch_enabled"] is False
    assert status.document()["fill_replay_enabled"] is False
    async with service.writer.transaction() as c:
        assert (
            await c.scalar(
                select(func.count())
                .select_from(venue_events)
                .where(venue_events.c.account_id == service.store.account_id)
            )
            == 0
        )  # No fabricated fills, orders or public book touches.


@pytest.mark.integration
async def test_supervisor_resource_failure_stops_and_disarms(service: PaperExecution) -> None:
    async def exhausted() -> None:
        raise ValueError("injected disk/database capacity failure")

    status = SupervisorStatus()
    supervisor = PaperSupervisor(
        service,
        status,
        marks=fresh_marks,
        capacity=exhausted,
        clock=lambda: AT,
    )
    with pytest.raises(ValueError, match="capacity"):
        await supervisor.run(asyncio.Event(), SupervisorMetrics())
    state = await service._state()
    assert not state.ready and state.incident == "paper_supervisor_fault"
    assert status.shutting_down and not status.supervision_ready
    assert not state.orders


@pytest.mark.integration
@pytest.mark.parametrize("jump", [-1, 2])
async def test_supervisor_clock_jumps_fail_before_marking(
    service: PaperExecution,
    jump: int,
) -> None:
    now = AT
    status = SupervisorStatus()
    supervisor = PaperSupervisor(
        service,
        status,
        marks=fresh_marks,
        capacity=capacity_ok,
        clock=lambda: now,
        timer=lambda: 0.0,
    )
    await supervisor.start()
    await supervisor.tick()
    before = await service._state()
    now += timedelta(seconds=jump)
    with pytest.raises(ValueError, match="clock discontinuity"):
        await supervisor.tick()
    assert await service._state() == before
    assert not status.supervision_ready


@pytest.mark.integration
async def test_supervisor_stop_wakes_immediately_and_blocks_ready(service: PaperExecution) -> None:
    stop = asyncio.Event()
    status = SupervisorStatus()
    supervisor = PaperSupervisor(
        service,
        status,
        marks=fresh_marks,
        capacity=capacity_ok,
        clock=lambda: AT,
    )
    task = asyncio.create_task(supervisor.run(stop, SupervisorMetrics()))
    try:
        async with asyncio.timeout(3):
            while not status.ticks:  # noqa: ASYNC110 - observe task's first completed real tick
                await asyncio.sleep(0.01)
        began = monotonic()
        stop.set()
        await task
        assert monotonic() - began < 1
        state = await service._state()
        assert not state.ready and state.incident == "paper_supervisor_stopped"
    finally:
        stop.set()
        await task


@pytest.mark.integration
async def test_slow_dependency_exceeds_tick_deadline_and_disarms(service: PaperExecution) -> None:
    async def stuck(at: datetime) -> tuple[Valuation, ...]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    status = SupervisorStatus()
    supervisor = PaperSupervisor(
        service,
        status,
        marks=stuck,
        capacity=capacity_ok,
        clock=lambda: AT,
    )
    began = monotonic()
    with pytest.raises(TimeoutError):
        await supervisor.run(asyncio.Event(), SupervisorMetrics())
    assert monotonic() - began < 8
    assert not status.supervision_ready
    assert (await service._state()).incident == "paper_supervisor_fault"


def test_supervision_health_and_metrics_cannot_grant_trading_permission() -> None:
    status, metrics = SupervisorStatus(), SupervisorMetrics()
    server = SupervisorHealthServer(status, metrics)
    assert server.response("GET", "/health/paper")[0] == 503
    status.database_ready = status.schema_ready = status.market_ready = status.reconciled = True
    status.reason, status.last_success = None, monotonic()
    assert server.response("GET", "/health/paper")[0] == 200
    assert server.response("GET", "/health/trading")[0] == 503
    assert server.response("POST", "/health/paper")[0] == 405
    assert b"aca_paper_supervision_ready 1.0" in metrics.render(status)
    assert b"aca_trading_ready 0.0" in metrics.render(status)
    status.last_success -= 3
    assert server.response("GET", "/health/paper")[0] == 503


@pytest.mark.integration
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "stale_health",
        "missing_pair",
        "new_epoch",
        "capture_fault",
        "capture_stopped",
        "quarantine",
        "coverage_gap",
        "offline",
        "no_health",
        "stale_book",
        "bad_checksum",
        "future_book",
        "future_arrival",
    ],
)
async def test_stored_public_feed_requires_complete_current_epoch(
    database_settings: Settings,
    fault: str | None,
) -> None:
    engine = create_engine(database_settings)
    at = AT + timedelta(days=next(_times))
    epoch = uuid4()
    store = MarketStore(engine)
    try:

        async def receipt(
            channel: str,
            data: dict[str, object],
            *,
            at_time: datetime = at,
        ) -> None:
            await store.append(
                epoch=epoch,
                channel=channel,
                payload="{}",
                received_at=at_time,
                normalized=data,
            )

        await receipt("epoch_started", {}, at_time=at - timedelta(seconds=20))
        rules = tuple(
            InstrumentRules(
                venue="kraken",
                symbol=symbol,
                venue_symbol=symbol,
                rest_pair=symbol,
                price_increment=D("0.01"),
                quantity_increment=D("0.00000001"),
                minimum_quantity=D("0.00006"),
                minimum_cost=D("0.43"),
                status="offline" if fault == "offline" else "online",
                observed_at=at,
            )
            for symbol in ("BTC/GBP", "ETH/GBP")
        )
        await receipt("instrument_rules", {"rules": [r.model_dump(mode="json") for r in rules]})
        if fault != "no_health":
            await receipt(
                "capture_health",
                {"market_ready": True},
                at_time=at - timedelta(seconds=11) if fault == "stale_health" else at,
            )
        book_time = at - timedelta(seconds=6) if fault == "stale_book" else at
        if fault == "future_book":
            book_time = at + timedelta(seconds=1)
        books = [
            OrderBook(
                venue="kraken",
                symbol=symbol,
                bids=(Level(price=D(999), quantity=D(1)),),
                asks=(Level(price=D(1000), quantity=D(1)),),
                checksum_valid=True,
                received_at=book_time,
                event_at=book_time,
                epoch=epoch,
            ).model_dump(mode="json")
            for symbol in ("BTC/GBP", "ETH/GBP")
        ]
        if fault == "missing_pair":
            books.pop()
        if fault == "bad_checksum":
            books[0]["checksum_valid"] = False
        await receipt("book", {"books": books}, at_time=book_time)
        if fault == "future_arrival":
            future = at + timedelta(milliseconds=100)
            later_books = [
                dict(b, received_at=future.isoformat(), event_at=future.isoformat()) for b in books
            ]
            await receipt("book", {"books": later_books}, at_time=future)
        if fault == "new_epoch":
            epoch = uuid4()
            await receipt("epoch_started", {}, at_time=at + timedelta(microseconds=1))
        if fault in {"capture_fault", "capture_stopped", "quarantine", "coverage_gap"}:
            await receipt(fault, {})
        feed = StoredPaperFeed(PaperStore(engine, uuid4()))
        decision = at + timedelta(microseconds=1) if fault == "new_epoch" else at
        if fault and fault != "future_arrival":
            with pytest.raises(FeedUnavailable):
                await feed.marks(decision)
        else:
            marks = await feed.marks(at)
            assert {m.asset for m in marks} == {"BTC", "ETH"}
            assert all(m.bid_gbp == D(999) and m.observed_at == at for m in marks)
            # Do not persist a paper account merely by inspecting public evidence.
            async with engine.connect() as c:
                assert (
                    await c.scalar(
                        select(func.count())
                        .select_from(events)
                        .where(events.c.account_id == feed.store.account_id)
                    )
                    == 0
                )
    finally:
        await engine.dispose()
