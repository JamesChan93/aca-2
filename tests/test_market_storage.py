import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_market_processor import rules, trade_message

from aca.adapters.kraken.wire import MarketDataError
from aca.market_data.capture import CaptureStatus, PublicCapture
from aca.market_data.processor import PublicProcessor
from aca.market_data.store import MarketStore
from aca.operations.config import Settings
from aca.storage.database import create_engine
from aca.storage.models import MarketReceipt, PublicTradeRecord

pytestmark = pytest.mark.integration


async def test_trade_deduplication_conflict_atomicity_and_restart(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    epoch = uuid4()
    first = (
        PublicProcessor(rules(), epoch)
        .process(trade_message(trade_id=12345), datetime.now(UTC))
        .trades[0]
    )
    try:
        store = MarketStore(engine)
        receipt = dict(
            epoch=epoch,
            channel="trade",
            payload=trade_message(trade_id=12345),
            received_at=datetime.now(UTC),
            trades=(first,),
        )
        assert len(await store.append(**receipt)) == 1  # type: ignore[arg-type]
        assert await store.append(**receipt) == ()  # type: ignore[arg-type]
        async with store.sessions() as session:
            before = await session.scalar(
                select(func.count()).select_from(MarketReceipt).where(MarketReceipt.epoch == epoch)
            )
        with pytest.raises(MarketDataError, match="conflicting"):
            await store.append(
                epoch=epoch,
                channel="trade",
                payload="{}",
                received_at=datetime.now(UTC),
                trades=(first.model_copy(update={"side": "sell"}),),
            )
        async with store.sessions() as session:
            after = await session.scalar(
                select(func.count()).select_from(MarketReceipt).where(MarketReceipt.epoch == epoch)
            )
        assert before == after == 2
    finally:
        await engine.dispose()
    engine = create_engine(database_settings)
    try:
        assert (
            await MarketStore(engine).append(
                epoch=epoch,
                channel="trade",
                payload="{}",
                received_at=datetime.now(UTC),
                trades=(first,),
            )
            == ()
        )
        async with MarketStore(engine).sessions() as session:
            existing = await session.get(PublicTradeRecord, ("kraken", "BTC/GBP", "12345"))
            assert existing and existing.price == first.price
    finally:
        await engine.dispose()


async def test_duplicate_batch_and_different_ids_do_not_collapse_volume(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    try:
        store = MarketStore(engine)
        p = PublicProcessor(rules(), uuid4())
        first = p.process(trade_message(trade_id=88881), datetime.now(UTC)).trades[0]
        second = p.process(trade_message(trade_id=88882), datetime.now(UTC)).trades[0]
        added = await store.append(
            epoch=p.epoch,
            channel="trade",
            payload="{}",
            received_at=datetime.now(UTC),
            trades=(first, first, second),
        )
        assert len(added) == 2 and sum(t.quantity for t in added) == first.quantity * 2
    finally:
        await engine.dispose()


@pytest.mark.parametrize("table", ["market_receipts", "public_trades"])
@pytest.mark.parametrize("operation", ["DELETE FROM", "TRUNCATE"])
async def test_market_history_append_only(
    database_settings: Settings, table: str, operation: str
) -> None:
    engine = create_engine(database_settings, "migration")
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text(f"{operation} aca.{table}"))
    finally:
        await engine.dispose()


class FakeStream:
    def __init__(self, messages: list[str]) -> None:
        self.frames = messages
        self.closed = False

    async def messages(self) -> AsyncIterator[str]:
        try:
            for frame in self.frames:
                yield frame
        finally:
            self.closed = True


async def test_disconnect_invalidates_books_and_duplicate_receipts_post_once(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    epoch = uuid4()
    stream = FakeStream([trade_message(trade_id=999991), trade_message(trade_id=999991)])
    collector = PublicCapture(MarketStore(engine), CaptureStatus(), stream=stream)
    collector.epoch = epoch
    processor = PublicProcessor(rules(), epoch)
    try:
        with pytest.raises(StopAsyncIteration):
            await collector.epoch_loop(processor, asyncio.Event())
        assert collector.new_trades == collector.duplicates == 1
        assert stream.closed and all(book.book is None for book in processor.books.values())
    finally:
        await collector.rest.close()
        await engine.dispose()


async def test_malformed_frame_is_quarantined_and_not_ignored(database_settings: Settings) -> None:
    engine = create_engine(database_settings)
    epoch = uuid4()
    stream = FakeStream(['{"channel":"book","type":"unknown"}'])
    collector = PublicCapture(MarketStore(engine), CaptureStatus(), stream=stream)
    collector.epoch = epoch
    processor = PublicProcessor(rules(), epoch)
    try:
        with pytest.raises(MarketDataError):
            await collector.epoch_loop(processor, asyncio.Event())
        async with collector.store.sessions() as session:
            receipt = await session.scalar(
                select(MarketReceipt).where(MarketReceipt.epoch == epoch)
            )
            assert receipt and not receipt.valid and receipt.channel == "quarantine"
            assert json.loads(receipt.payload)["type"] == "unknown"
        assert stream.closed
    finally:
        await collector.rest.close()
        await engine.dispose()


async def test_failed_storage_cannot_leave_processor_ready(
    database_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_market_processor import initialize

    engine = create_engine(database_settings)
    processor = PublicProcessor(rules(), uuid4())
    initialize(processor)
    stream = FakeStream([trade_message(trade_id=776655)])
    store = MarketStore(engine)

    async def unavailable(**kwargs: object) -> tuple[object, ...]:
        raise OSError("fixture storage unavailable")

    monkeypatch.setattr(store, "append", unavailable)
    collector = PublicCapture(store, CaptureStatus(), stream=stream)
    try:
        with pytest.raises(OSError):
            await collector.epoch_loop(processor, asyncio.Event())
        assert all(book.book is None for book in processor.books.values())
        assert stream.closed
    finally:
        await collector.rest.close()
        await engine.dispose()


async def test_recovery_starts_new_epoch_without_replaying_canonical_trades(
    database_settings: Settings,
) -> None:
    stop = asyncio.Event()

    class ReconnectingStream:
        attempts = 0

        async def messages(self) -> AsyncIterator[str]:
            self.attempts += 1
            if self.attempts == 1:
                yield '{"channel":"unknown"}'
            else:
                yield trade_message(trade_id=11223344)
                yield trade_message(trade_id=11223344)
                stop.set()

    engine = create_engine(database_settings)
    epochs: list[object] = []

    class FixtureCapture(PublicCapture):
        async def bootstrap(self) -> PublicProcessor:
            epochs.append(self.epoch)
            return PublicProcessor(rules(), self.epoch)

    collector = FixtureCapture(MarketStore(engine), CaptureStatus(), stream=ReconnectingStream())
    try:
        await asyncio.wait_for(collector.run(stop), timeout=15)
        assert len(epochs) == len(set(epochs)) == 2
        assert collector.reconnects == 1
        assert collector.new_trades == 1 and collector.duplicates == 1
        assert collector.status.processor is None
    finally:
        await engine.dispose()


async def test_public_cross_protocol_microsecond_alignment_preserves_source_evidence(
    database_settings: Settings,
) -> None:
    from datetime import timedelta

    from aca.adapters.kraken.wire import public_trade_time_compatible

    engine = create_engine(database_settings)
    epoch = uuid4()
    trade = (
        PublicProcessor(rules(), epoch)
        .process(trade_message(trade_id=91827364), datetime.now(UTC))
        .trades[0]
    )
    store = MarketStore(engine, timestamp_compatible=public_trade_time_compatible)
    try:
        await store.append(
            epoch=epoch,
            channel="rest:normalized_trades",
            payload="{}",
            received_at=datetime.now(UTC),
            trades=(trade,),
        )
        aligned = trade.model_copy(update={"event_at": trade.event_at + timedelta(microseconds=1)})
        assert (
            await store.append(
                epoch=epoch,
                channel="trade",
                payload="{}",
                received_at=datetime.now(UTC),
                trades=(aligned,),
            )
            == ()
        )
        async with store.sessions() as session:
            evidence = await session.scalar(
                select(MarketReceipt).where(
                    MarketReceipt.epoch == epoch, MarketReceipt.channel == "timestamp_alignment"
                )
            )
            assert (
                evidence and evidence.normalized["policy"] == "kraken-public-cross-protocol-1us-v1"
            )
        # Larger timestamp differences and changed economics never qualify.
        for invalid in (
            trade.model_copy(update={"event_at": trade.event_at + timedelta(microseconds=2)}),
            aligned.model_copy(update={"quantity": trade.quantity * 2}),
        ):
            with pytest.raises(MarketDataError):
                await store.append(
                    epoch=epoch,
                    channel="trade",
                    payload="{}",
                    received_at=datetime.now(UTC),
                    trades=(invalid,),
                )
        with pytest.raises(MarketDataError):
            await store.append(
                epoch=epoch,
                channel="rest:normalized_trades",
                payload="{}",
                received_at=datetime.now(UTC),
                trades=(aligned,),
            )
    finally:
        await engine.dispose()
