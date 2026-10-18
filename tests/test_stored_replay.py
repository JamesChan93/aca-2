"""Real PostgreSQL ordering, replay crashes, liquidity and cursor boundaries."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, insert, select, text
from sqlalchemy.exc import DBAPIError
from test_paper_execution import market, signal

from aca.app.paper_supervisor import PaperSupervisor, SupervisorStatus
from aca.domain.market import MarketTrade, Symbol
from aca.domain.trading import Valuation, fingerprint
from aca.execution.paper import PaperExecution, SimulatedCrash
from aca.market_data.store import MarketStore
from aca.operations.config import Settings
from aca.ports.execution import UnknownOutcome
from aca.risk.execution import MarketEvidence
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.database import create_engine
from aca.storage.models import MarketReceipt
from aca.storage.paper import PaperStore, PaperWriter, journals, venue_events
from aca.storage.replay import PUBLIC_REPLAY_LOCK, cursors, public_events

pytestmark = pytest.mark.integration
AT = datetime(2026, 10, 18, tzinfo=UTC)
D = Decimal


def evidence(
    at: datetime,
    epoch: UUID,
    symbol: Symbol = "BTC/GBP",
    depth: str = "0.01",
    bid: str = "999",
    ask: str = "1000",
) -> MarketEvidence:
    m = market(at, depth=depth, bid=bid, ask=ask)
    return MarketEvidence(
        book=m.book.model_copy(update={"venue": "kraken", "symbol": symbol, "epoch": epoch}),
        rules=m.rules.model_copy(update={"venue": "kraken", "symbol": symbol}),
    )


class Feed:
    def __init__(self, replay: StoredReplay) -> None:
        self.replay, self.store, self.epoch = replay, MarketStore(replay.store.engine), uuid4()

    async def marker(
        self,
        channel: str,
        at: datetime,
        normalized: dict[str, object] | None = None,
        *,
        valid: bool = True,
    ) -> None:
        await self.store.append(
            epoch=self.epoch,
            channel=channel,
            payload="{}",
            received_at=at,
            normalized=normalized,
            valid=valid,
        )

    async def book(
        self,
        at: datetime,
        symbol: Symbol = "BTC/GBP",
        *,
        bid: str = "999",
        ask: str = "1000",
        depth: str = "0.01",
    ) -> None:
        m = evidence(at, self.epoch, symbol, bid=bid, ask=ask, depth=depth)
        await self.marker("book", at, {"books": [m.book.model_dump(mode="json")]})

    async def bootstrap(self, at: datetime = AT) -> None:
        await self.marker("epoch_started", at)
        rules = [
            evidence(at, self.epoch, s).rules.model_dump(mode="json")
            for s in ("BTC/GBP", "ETH/GBP")
        ]
        await self.marker("instrument_rules", at, {"rules": rules})
        await self.book(at)
        await self.book(at, "ETH/GBP")
        await self.marker("capture_health", at, {"market_ready": True})
        result = await self.replay.batch(at=at)
        assert result.processed == 5 and not result.more

    async def trade(
        self,
        at: datetime,
        *,
        id: str | None = None,
        size: str = "0.11",
        event_at: datetime | None = None,
        snapshot: bool = False,
        channel: str = "trade",
    ) -> MarketTrade:
        t = MarketTrade(
            venue="kraken",
            symbol="BTC/GBP",
            trade_id=id or uuid4().hex,
            side="sell",
            price=D(999),
            quantity=D(size),
            received_at=at,
            event_at=event_at or at,
        )
        await self.store.append(
            epoch=self.epoch,
            channel=channel,
            payload="{}",
            received_at=at,
            normalized={"snapshot": snapshot},
            trades=(t,),
        )
        return t


@pytest.fixture
async def replay(database_settings: Settings) -> AsyncIterator[tuple[StoredReplay, Feed]]:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            await service.mark(
                (Valuation(asset="BTC", bid_gbp=D(999), observed_at=AT, source="fixture"),), AT
            )
            replay = StoredReplay(service, venue)
            await replay.enrol()
            feed = Feed(replay)
            await feed.bootstrap()
            assert (await service.recover(AT)).complete
            yield replay, feed
    finally:
        await engine.dispose()


async def passive(replay: StoredReplay, feed: Feed) -> UUID:
    service = replay.service
    initial = evidence(AT, feed.epoch, depth="10")
    order = await service.queue(signal(), initial, AT)
    await service.dispatch(order.intent.client_id, initial, AT)
    at = AT + timedelta(milliseconds=300)
    await feed.book(at)
    await replay.batch(at=at)
    assert (await service.reconcile(at)).complete
    return order.intent.client_id


@pytest.mark.parametrize("point", ["before_cursor", "before_commit", "after_commit"])
async def test_replay_cursor_fill_and_protection_are_atomic_across_crashes(
    replay: tuple[StoredReplay, Feed],
    point: str,
) -> None:
    runner, feed = replay
    ident = await passive(runner, feed)
    at = AT + timedelta(milliseconds=400)
    await feed.trade(at)
    before = await runner.audit()

    def crash(where: str) -> None:
        if where == point:
            raise SimulatedCrash()

    runner.fault = crash
    with pytest.raises(SimulatedCrash):
        await runner.batch(at=at)
    snapshot = await runner.venue.snapshot(at)
    assert len(snapshot.fills) == int(point == "after_commit")
    assert sum(o.intent.purpose == "protection" for o in snapshot.orders) == len(snapshot.fills)
    assert ((await runner.audit()).number > before.number) == (point == "after_commit")
    # Reconstruct the consumer after a process-stop analogue. Only committed
    # venue effects survive, even when financial import has not happened yet.
    runner = StoredReplay(runner.service, PaperExchange(runner.store))
    await runner.audit()
    result = await runner.batch(at=at)
    assert result.processed == int(point != "after_commit")
    assert (await runner.service.recover(at)).complete
    state = await runner.service._state()
    assert state.order(ident).filled > 0
    assert state.quantity("BTC") == state.order(ident).filled
    async with runner.service.writer.transaction() as c:
        count = await c.scalar(
            select(func.count())
            .select_from(journals)
            .where(journals.c.account_id == runner.store.account_id)
        )
        assert count == 2  # One contribution, one economic fill.
    assert (await runner.service.reconcile(at)).complete
    assert len((await runner.venue.snapshot(at)).fills) == 1


async def test_duplicate_snapshots_recovery_and_late_trades_never_add_queue_liquidity(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    ident = await passive(runner, feed)
    at = AT + timedelta(milliseconds=400)
    await feed.trade(at, snapshot=True, size="100")
    await feed.trade(at, channel="rest:normalized_trades", size="100")
    await feed.trade(at, event_at=AT, size="100")
    result = await runner.batch(at=at)
    assert result.skipped_trades == 1
    assert not (await runner.venue.snapshot(at)).fills
    assert not next(
        o for o in (await runner.venue.snapshot(at)).orders if o.intent.client_id == ident
    ).queue_initialized
    at += timedelta(milliseconds=100)
    await feed.book(at)  # Reinitialise queue after the late-trade interruption.
    await runner.batch(at=at)
    at += timedelta(milliseconds=100)
    trade = await feed.trade(at)
    await runner.batch(at=at)
    first = await runner.venue.snapshot(at)
    assert len(first.fills) == 1
    await feed.trade(at, id=trade.trade_id)
    assert (await runner.batch(at=at)).market_observations == 0
    assert await runner.venue.snapshot(at) == first


@pytest.mark.parametrize(
    "fault",
    [
        "coverage_gap",
        "capture_stopped",
        "new_epoch",
        "stale",
        "missing_pair",
        "future",
        "bad_checksum",
        "regressed_book",
        "unknown_channel",
    ],
)
async def test_replay_faults_preserve_cursor_or_reset_queue_without_filling(
    replay: tuple[StoredReplay, Feed],
    fault: str,
) -> None:
    runner, feed = replay
    await passive(runner, feed)
    at = AT + timedelta(milliseconds=400)
    cursor = await runner.audit()
    invalid = fault in {"future", "bad_checksum", "unknown_channel", "regressed_book"}
    if fault in {"coverage_gap", "capture_stopped"}:
        await feed.marker(fault, at, valid=False)
    elif fault in {"new_epoch", "missing_pair"}:
        feed.epoch = uuid4()
        await feed.marker("epoch_started", at)
        if fault == "missing_pair":
            await feed.marker("capture_health", at, {"market_ready": True})
            await feed.book(at)
    elif fault == "stale":
        at = AT + timedelta(seconds=11)
    elif fault == "future":
        await feed.book(at + timedelta(seconds=1))
    elif fault == "bad_checksum":
        doc = evidence(at, feed.epoch).book.model_dump(mode="json")
        doc["checksum_valid"] = False
        await feed.marker("book", at, {"books": [doc]})
    elif fault == "regressed_book":
        doc = evidence(at, feed.epoch).book.model_dump(mode="json")
        doc["event_at"] = (AT + timedelta(milliseconds=200)).isoformat()
        await feed.marker("book", at, {"books": [doc]})
    else:
        await feed.marker("unknown_critical", at)
    await feed.trade(at, size="100")
    if invalid:
        if fault == "future":
            result = await runner.batch(at=at)
            assert result.deferred and not result.more and result.processed == 0
        else:
            with pytest.raises(ValueError):
                await runner.batch(at=at)
        assert await runner.audit() == cursor
    else:
        await runner.batch(at=at)
    assert not (await runner.venue.snapshot(at)).fills
    assert not (await runner.service._state()).ready


async def test_commands_cannot_overtake_cursor_or_bypass_enrolled_replay(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    ident = await passive(runner, feed)
    at = AT + timedelta(milliseconds=400)
    await feed.trade(at)
    with pytest.raises(UnknownOutcome, match="unconsumed"):
        await runner.venue.cancel(ident, at)
    with pytest.raises(ValueError, match="durable replay cursor"):
        await runner.venue.advance(
            evidence(at, feed.epoch).book, evidence(at, feed.epoch).rules, at
        )
    await runner.batch(at=at)
    await runner.venue.cancel(ident, at)
    assert (await runner.service.reconcile(at)).complete
    assert (await runner.service._state()).quantity("BTC") > 0


async def test_bound_batch_and_fresh_supervisor_recover_cursor(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    for i in range(70):
        await feed.marker("heartbeat", AT + timedelta(microseconds=i))
    at = AT + timedelta(seconds=1)
    result = await runner.batch(limit=32, at=at)
    assert result.processed == 32 and result.more

    async def marks(now: datetime) -> tuple[Valuation, ...]:
        return (Valuation(asset="BTC", bid_gbp=D(999), observed_at=now, source="fixture"),)

    async def capacity() -> None:
        pass

    status = SupervisorStatus()
    supervisor = PaperSupervisor(
        runner.service, status, replay=runner, marks=marks, capacity=capacity, clock=lambda: at
    )
    await supervisor.start()
    await supervisor.tick()
    assert status.supervision_ready and status.fill_replay_enabled
    assert status.replay_processed == 38
    assert status.document()["strategy_dispatch_enabled"] is False
    assert status.document()["promotion_evidence"] is False
    assert status.document()["trading_ready"] is False


async def test_public_commit_order_lock_prevents_cursor_skipping_inflight_receipt(
    replay: tuple[StoredReplay, Feed],
    database_settings: Settings,
) -> None:
    runner, feed = replay
    engine = runner.store.engine
    # The owner holds one pool slot. Use a separate engine for the waiting writer.
    independent_engine = create_engine(database_settings)
    independent = MarketStore(independent_engine)
    async with engine.connect() as c:
        async with c.begin():
            await c.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": PUBLIC_REPLAY_LOCK})
            receipt_id = uuid4()
            await c.execute(
                insert(MarketReceipt).values(
                    id=receipt_id,
                    epoch=feed.epoch,
                    schema_version=1,
                    received_at=AT,
                    channel="heartbeat",
                    valid=True,
                    reason=None,
                    payload="{}",
                    payload_hash=hashlib.sha256(b"{}").hexdigest(),
                    normalized={},
                )
            )
            await c.execute(
                public_events.insert().values(
                    receipt_id=receipt_id, document={}, fingerprint=fingerprint({})
                )
            )
            # Release this connection's pool slot only after the deliberate rollback;
            # the pending writer cannot publish a later number in the meantime.
            task = asyncio.create_task(
                independent.append(
                    epoch=feed.epoch, channel="heartbeat", payload="{}", received_at=AT
                )
            )
            waiting = False
            for _ in range(100):
                waiting = bool(
                    await c.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks "
                            "WHERE locktype='advisory' AND NOT granted "
                            "AND classid=:hi AND objid=:lo"
                        ),
                        {"hi": PUBLIC_REPLAY_LOCK >> 32, "lo": PUBLIC_REPLAY_LOCK & 0xFFFFFFFF},
                    )
                )
                if waiting:
                    break
                await asyncio.sleep(0.01)
            assert waiting and not task.done()
            await c.rollback()
    await task
    await independent_engine.dispose()
    result = await runner.batch(at=AT)
    assert result.processed == 1  # The rolled-back sequence gap is harmless.


async def test_replay_history_append_only_and_corrupt_cursor_blocks_restart(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, _ = replay
    for table in ("public_replay_events", "paper_replay_cursors"):
        for mutation in ("DELETE FROM", "TRUNCATE"):
            async with runner.store.engine.begin() as c:
                with pytest.raises(DBAPIError):
                    await c.execute(text(f"{mutation} aca.{table}"))
    current = await runner.audit()
    async with runner.store.engine.begin() as c:
        await c.execute(
            cursors.insert().values(
                account_id=runner.store.account_id,
                number=current.number + 100000,
                previous_hash="0" * 64,
                fingerprint="f" * 64,
                document=current.document(),
            )
        )
    with pytest.raises(ValueError, match="corrupt replay cursor"):
        await runner.audit()


async def test_venue_projection_does_not_read_public_prefix(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await passive(runner, feed)
    at = AT + timedelta(milliseconds=400)
    await feed.trade(at)
    await runner.batch(at=at)
    queries: list[str] = []

    def observe(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        queries.append(statement)

    event.listen(runner.store.engine.sync_engine, "before_cursor_execute", observe)
    try:
        snapshot = await runner.venue.snapshot(at)
    finally:
        event.remove(runner.store.engine.sync_engine, "before_cursor_execute", observe)
    assert len(snapshot.orders) == 2 and len(snapshot.fills) == 1
    assert all("DISTINCT ON" in q or "LIMIT" in q or "SET " in q for q in queries)
    assert not any("market_book" in q for q in queries)


async def test_replay_trade_watermark_rejects_late_queue_credit_and_keeps_equal_time_ids(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await passive(runner, feed)
    at = AT + timedelta(milliseconds=500)
    await feed.trade(at, size="0.01")
    await runner.batch(at=at)
    later = at + timedelta(milliseconds=100)
    await feed.trade(later, event_at=at - timedelta(milliseconds=100), size="100")
    assert (await runner.batch(at=later)).skipped_trades == 1
    assert not (await runner.venue.snapshot(later)).fills
    await feed.book(later)
    await runner.batch(at=later)
    at = later + timedelta(milliseconds=100)
    await feed.trade(at, size="0.06")
    await feed.trade(at, size="0.06")
    await runner.batch(at=at)
    snapshot = await runner.venue.snapshot(at)
    assert len(snapshot.fills) == 2
    assert sum(f.quantity for f in snapshot.fills) == D("0.0011")
    assert (await runner.service.reconcile(at)).complete


async def test_stored_replay_fok_then_stop_limit_gap_and_rebound(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    initial = evidence(AT, feed.epoch, depth="10")
    order = await runner.service.queue(signal(), initial, AT, time_in_force="FOK")
    await runner.service.dispatch(order.intent.client_id, initial, AT)
    at = AT + timedelta(milliseconds=300)
    await feed.book(at, depth="10")
    await runner.batch(at=at)
    assert (await runner.service.reconcile(at)).complete
    assert (await runner.service._state()).quantity("BTC") == order.intent.quantity
    at += timedelta(milliseconds=300)
    await feed.book(at, bid="800", ask="801", depth="10")
    await runner.batch(at=at)
    await feed.book(at + timedelta(milliseconds=300), bid="800", ask="801", depth="10")
    await runner.batch(at=at + timedelta(milliseconds=300))
    snapshot = await runner.venue.snapshot(at)
    assert len(snapshot.fills) == 1
    assert next(o for o in snapshot.orders if o.intent.purpose == "protection").triggered
    at += timedelta(milliseconds=600)
    await feed.book(at, bid="970", ask="971", depth="10")
    await runner.batch(at=at)
    await runner.service.mark(
        (Valuation(asset="BTC", bid_gbp=D(970), observed_at=at, source="fixture"),), at
    )
    assert (await runner.service.reconcile(at)).complete
    assert (await runner.service._state()).quantity("BTC") == 0
    assert len((await runner.venue.snapshot(at)).fills) == 2


async def test_replay_enrolment_requires_flat_account_and_is_idempotent(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    try:
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            await service.initialise(AT)
            await service.cashflow("opening", D(1000), AT)
            await service.mark(
                (Valuation(asset="BTC", bid_gbp=D(999), observed_at=AT, source="fixture"),), AT
            )
            await service.recover(AT)
            order = await service.queue(signal(), evidence(AT, uuid4(), depth="10"), AT)
            runner = StoredReplay(service, venue)
            with pytest.raises(ValueError, match="flat account"):
                await runner.enrol()
            assert (await service._state()).order(order.intent.client_id).state == "VALIDATED"
            await service.cancel_entries(AT)
            first = await runner.enrol()
            assert await runner.enrol() == first
            assert await runner.audit() == first
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "corruption", ["hash", "source", "float", "too_many_trades", "future_publication"]
)
async def test_malformed_replay_source_never_advances_cursor_or_posts_a_fill(
    replay: tuple[StoredReplay, Feed],
    corruption: str,
) -> None:
    runner, feed = replay
    await passive(runner, feed)
    before = await runner.audit()
    at = AT + timedelta(milliseconds=400)
    receipt_id = uuid4()
    book = evidence(at, feed.epoch).book.model_dump(mode="json")
    if corruption == "float":
        book["bids"][0]["price"] = 999.0
    doc: dict[str, object] = dict(
        schema_version=1,
        receipt_id=str(receipt_id),
        epoch=str(feed.epoch),
        received_at=at.isoformat(),
        channel="book",
        valid=True,
        payload_hash=hashlib.sha256(b"{}").hexdigest(),
        normalized={"books": [book]},
        trades=[],
    )
    if corruption == "too_many_trades":
        t = MarketTrade(
            venue="kraken",
            symbol="BTC/GBP",
            trade_id="oversized",
            side="sell",
            price=D(999),
            quantity=D(1),
            received_at=at,
            event_at=at,
        )
        doc["trades"] = [t.model_dump(mode="json")] * 257
    async with runner.store.engine.begin() as c:
        await c.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": PUBLIC_REPLAY_LOCK})
        await c.execute(
            insert(MarketReceipt).values(
                id=receipt_id,
                schema_version=1,
                epoch=feed.epoch,
                received_at=at,
                channel="trade" if corruption == "source" else "book",
                valid=True,
                payload="{}",
                payload_hash=hashlib.sha256(b"{}").hexdigest(),
                normalized={},
                reason=None,
            )
        )
        await c.execute(
            public_events.insert().values(
                receipt_id=receipt_id,
                document=doc,
                fingerprint="f" * 64 if corruption == "hash" else fingerprint(doc),
                published_at=AT - timedelta(seconds=1)
                if corruption == "future_publication"
                else at,
            )
        )
    with pytest.raises(ValueError):
        await runner.batch(at=at)
    assert await runner.audit() == before
    assert not (await runner.venue.snapshot(at)).fills
    assert not (await runner.service._state()).ready


async def test_cancel_that_overtakes_a_delayed_market_receipt_requires_review(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    ident = await passive(runner, feed)
    at = AT + timedelta(milliseconds=600)
    await runner.venue.cancel(ident, at)
    assert (await runner.service.reconcile(at)).complete
    before = await runner.audit()
    await feed.book(at - timedelta(milliseconds=200))
    with pytest.raises(ValueError, match="cancellation overtook"):
        await runner.batch(at=at)
    assert await runner.audit() == before
    assert not (await runner.venue.snapshot(at)).fills
    assert not (await runner.service._state()).ready


async def test_distinct_book_receipts_with_equal_timestamp_are_not_collapsed(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await passive(runner, feed)
    at = AT + timedelta(milliseconds=400)
    await feed.book(at)
    await feed.book(at, bid="998", ask="999")
    assert (await runner.batch(at=at)).market_observations == 2
    async with runner.service.writer.transaction() as c:
        count = await c.scalar(
            select(func.count())
            .select_from(venue_events)
            .where(
                venue_events.c.account_id == runner.store.account_id,
                venue_events.c.kind == "market_book",
                venue_events.c.document["received_at"].astext == at.isoformat(),
            )
        )
    assert count == 2
    assert not (await runner.venue.snapshot(at)).fills


async def test_arrival_after_decision_cutoff_defers_entire_suffix_until_next_tick(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    await passive(runner, feed)
    before = await runner.audit()
    arrival = AT + timedelta(milliseconds=400)
    await feed.book(arrival)
    await feed.marker("heartbeat", AT + timedelta(milliseconds=300))
    cutoff = AT + timedelta(milliseconds=350)
    result = await runner.batch(at=cutoff)
    assert result.processed == 0 and result.deferred and not result.more
    assert await runner.audit() == before
    result = await runner.batch(at=arrival)
    assert result.processed == 2 and not result.more
    assert not (await runner.venue.snapshot(arrival)).fills
    assert (await runner.batch(at=arrival)).processed == 0


async def test_flat_batch_reads_venue_projection_once_without_writing_observations(
    replay: tuple[StoredReplay, Feed],
) -> None:
    runner, feed = replay
    for i in range(64):
        await feed.book(AT + timedelta(microseconds=i))
    queries: list[str] = []

    def observe(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "aca.paper_venue_events" in statement:
            queries.append(statement)

    event.listen(runner.store.engine.sync_engine, "before_cursor_execute", observe)
    try:
        result = await runner.batch(limit=64, at=AT + timedelta(seconds=1))
    finally:
        event.remove(runner.store.engine.sync_engine, "before_cursor_execute", observe)
    assert result.processed == 64 and not result.more
    assert len(queries) == 2
    assert not (await runner.venue.snapshot(AT)).orders
    async with runner.service.writer.transaction() as c:
        assert (
            await c.scalar(
                select(func.count())
                .select_from(venue_events)
                .where(venue_events.c.account_id == runner.store.account_id)
            )
            == 0
        )
