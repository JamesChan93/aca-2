"""Opt-in public capture supervisor. Every epoch starts without usable books."""

import asyncio
import json
import signal
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import func, select, text

from aca.adapters.kraken.rest import PublicRest
from aca.adapters.kraken.stream import PublicStream
from aca.adapters.kraken.wire import BINDINGS, MarketDataError, public_trade_time_compatible
from aca.app.health import FoundationStatus, HealthServer
from aca.domain.market import Symbol
from aca.market_data.candles import CandleAggregator, unexplained_gaps
from aca.market_data.metrics import CaptureMetrics
from aca.market_data.processor import PublicProcessor
from aca.market_data.store import MarketStore
from aca.operations.config import Settings
from aca.ports.market import MarketStreamPort
from aca.storage.database import FoundationStore, create_engine, verify_runtime
from aca.storage.models import PublicTradeRecord


async def _next_message(iterator: AsyncIterator[str]) -> str:
    return await anext(iterator)


class CaptureStatus(FoundationStatus):
    def __init__(self) -> None:
        super().__init__(freshness_seconds=5)
        self.processor: PublicProcessor | None = None
        self.reason = "bootstrapping"
        self.last_rest_check: float | None = None
        self.clock_evidence: dict[str, object] = {}
        self.counters: dict[str, object] = {}

    def document(self) -> dict[str, object]:
        result = super().document()
        market = (
            self.processor.health(datetime.now(UTC))
            if self.processor
            else {"market_ready": False, "blockers": [self.reason]}
        )
        if (
            self.last_rest_check is None
            or monotonic() - self.last_rest_check > 60
            or not self.ready
        ):
            market["market_ready"] = False
            market["rest_or_database_stale"] = True
        result.update(
            {
                "phase": 2,
                "public_data_only": True,
                "market_data": market,
                "clock_evidence": self.clock_evidence,
                "capture_statistics": self.counters,
            }
        )
        return result


class CaptureHealthServer(HealthServer):
    def response(self, method: str, path: str) -> tuple[int, str, bytes]:
        if method == "GET" and path == "/health/market":
            document = self.status.document()
            market = document["market_data"]
            ready = isinstance(market, dict) and market.get("market_ready") is True
            return (200 if ready else 503), "application/json", json.dumps(document).encode()
        return super().response(method, path)


class PublicCapture:
    def __init__(
        self, store: MarketStore, status: CaptureStatus, *, stream: MarketStreamPort | None = None
    ) -> None:
        self.store, self.status = store, status
        self.stream = stream or PublicStream()
        self.epoch = uuid4()
        self.last_raw = ""
        self.rest = PublicRest(sink=self._rest_evidence)
        self.logger = structlog.get_logger().bind(phase=2, public_data_only=True)
        self.receipts = 0
        self.new_trades = 0
        self.duplicates = 0
        self.reconnects = 0
        self.faults = 0
        self.checksum_failures = 0
        self.healthy_observations = 0
        self.observations = 0
        self.rules_refreshed = 0.0

    async def _rest_evidence(self, endpoint: str, payload: str, received: datetime) -> None:
        self.last_raw = payload
        await self.store.append(
            epoch=self.epoch, channel=f"rest:{endpoint}", payload=payload, received_at=received
        )

    async def marker(
        self,
        channel: str,
        *,
        valid: bool = True,
        reason: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self.store.append(
            epoch=self.epoch,
            channel=channel,
            payload="{}",
            received_at=datetime.now(UTC),
            normalized=detail,
            valid=valid,
            reason=reason,
        )

    async def check_exchange(self) -> None:
        status = await self.rest.read("SystemStatus")
        if status.get("status") != "online":
            raise MarketDataError("exchange REST status is not online")
        tick = monotonic()
        server = await self.rest.read("Time")
        before, after = self.rest.last_sent_at, self.rest.last_received_at
        if before is None or after is None:
            raise MarketDataError("missing server clock timestamps")
        unixtime = server.get("unixtime")
        if type(unixtime) is not int:
            raise MarketDataError("invalid server time")
        # REST Time has second precision. Do not claim evidence of a <=500ms live clock bound.
        lower = unixtime - after.timestamp()
        upper = unixtime + 1 - before.timestamp()
        self.status.clock_evidence = {
            "offset_lower_seconds": lower,
            "offset_upper_seconds": upper,
            "round_trip_and_budget_wait_seconds": monotonic() - tick,
            "live_clock_bound_verified": False,
        }
        if lower > 0.5 or upper < -0.5:
            raise MarketDataError("clock offset outside safe interval")
        await self.store.check_capacity()
        self.status.last_rest_check = monotonic()

    async def recover_recent_trades(self, symbol: Symbol) -> None:
        async with self.store.sessions() as session:
            last = await session.scalar(
                select(func.max(PublicTradeRecord.event_at)).where(
                    PublicTradeRecord.venue == "kraken", PublicTradeRecord.symbol == symbol
                )
            )
        cursor: str | None
        if last is None or datetime.now(UTC) - last > timedelta(days=1):
            cursor = None
        else:
            start = last - timedelta(seconds=60)
            delta = start - datetime(1970, 1, 1, tzinfo=UTC)
            cursor = str(
                (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
            )
        for _ in range(20):
            trades, next_cursor = await self.rest.trades(symbol, cursor)
            await self.store.append(
                epoch=self.epoch,
                channel="rest:normalized_trades",
                payload=self.last_raw,
                received_at=datetime.now(UTC),
                trades=trades,
                normalized={"symbol": symbol, "since": cursor, "next_cursor": next_cursor},
            )
            if cursor is None or len(trades) < 1000 or next_cursor == cursor:
                break
            if int(next_cursor) < int(cursor):
                raise MarketDataError("REST trade cursor regressed")
            cursor = next_cursor
        else:
            raise MarketDataError("trade recovery page budget exhausted")
        # No claim that a recent snapshot, replay or reconnect proves a gap-free archive.
        await self.marker(
            "trade_recovery",
            detail={"symbol": symbol, "history_complete": False, "coverage": "bounded_overlap"},
        )

    async def bootstrap(self) -> PublicProcessor:
        await self.store.check_capacity()
        rules = await self.rest.instruments()
        await self.marker(
            "instrument_rules", detail={"rules": [r.model_dump(mode="json") for r in rules]}
        )
        await self.check_exchange()
        for symbol in BINDINGS:
            for interval in (240, 1440):
                candles = await self.rest.candles(symbol, interval)
                await self.store.append(
                    epoch=self.epoch,
                    channel="rest:closed_candles",
                    payload=self.last_raw,
                    received_at=datetime.now(UTC),
                    normalized={
                        "candles": [c.model_dump(mode="json") for c in candles],
                        "history_complete": False,
                        "unexplained_gaps": unexplained_gaps(candles),
                    },
                )
            await self.recover_recent_trades(symbol)
        await self.check_exchange()
        self.rules_refreshed = monotonic()
        return PublicProcessor(rules, self.epoch)

    async def maintain_exchange(self, processor: PublicProcessor) -> None:
        await self.check_exchange()
        if monotonic() - self.rules_refreshed < 3000:
            return
        rules = await self.rest.instruments()
        refreshed = {rule.symbol: rule for rule in rules}
        if set(refreshed) != set(processor.rules) or any(
            refreshed[symbol].model_dump(exclude={"observed_at"})
            != processor.rules[symbol].model_dump(exclude={"observed_at"})
            for symbol in processor.rules
        ):
            raise MarketDataError("changed REST rules require fresh v2 verification")
        await self.marker(
            "instrument_rules", detail={"rules": [r.model_dump(mode="json") for r in rules]}
        )
        processor.rules = refreshed
        self.rules_refreshed = monotonic()

    async def epoch_loop(self, processor: PublicProcessor, stop: asyncio.Event) -> None:
        iterator: AsyncIterator[str] = self.stream.messages()
        pending: asyncio.Task[str] | None = None
        aggregate: CandleAggregator | None = None
        started = monotonic()
        last_message = monotonic()
        next_database = 0.0
        next_observation = 0.0
        next_health_receipt = 0.0
        last_health: bool | None = None
        next_rest = monotonic() + 30
        rest_task: asyncio.Task[None] | None = None
        try:
            while not stop.is_set():
                if pending is None:
                    pending = asyncio.create_task(_next_message(iterator))
                done, _ = await asyncio.wait({pending}, timeout=1)
                if monotonic() >= next_database:
                    await verify_runtime(self.store.engine)
                    self.status.database_ready = self.status.schema_ready = True
                    self.status.last_success = monotonic()
                    next_database = monotonic() + 1
                now = datetime.now(UTC)
                if pending in done:
                    raw = pending.result()
                    pending = None
                    last_message = monotonic()
                    try:
                        frame = processor.process(raw, now)
                        added = await self.store.append(
                            epoch=self.epoch,
                            channel=frame.channel,
                            payload=raw,
                            received_at=now,
                            normalized=frame.normalized,
                            trades=frame.trades,
                        )
                    except Exception as exc:
                        processor.invalidate()
                        await self.store.append(
                            epoch=self.epoch,
                            channel="quarantine",
                            payload=raw,
                            received_at=now,
                            valid=False,
                            reason=type(exc).__name__,
                        )
                        raise
                    self.receipts += 1
                    self.new_trades += len(added)
                    self.duplicates += len(frame.trades) - len(added)
                    if aggregate:
                        for trade in added:
                            aggregate.add(trade)
                if monotonic() - last_message > 10:
                    raise MarketDataError("public WebSocket heartbeat deadline exceeded")
                health = processor.health(now)
                if monotonic() - started > 30 and (
                    processor.instrument_seen != set(BINDINGS)
                    or processor.trade_snapshot_seen != set(BINDINGS)
                    or any(book.book is None for book in processor.books.values())
                ):
                    raise MarketDataError("public snapshot deadline exceeded")
                healthy = bool(
                    health["market_ready"]
                    and self.status.last_rest_check is not None
                    and monotonic() - self.status.last_rest_check <= 60
                )
                if monotonic() >= next_observation:
                    self.observations += 1
                    self.healthy_observations += int(healthy)
                    next_observation = monotonic() + 1
                    self.status.counters = self.report()
                if healthy != last_health:
                    self.logger.info(
                        "public_market_readiness_changed",
                        market_ready=healthy,
                        blockers=health["blockers"],
                        epoch=str(self.epoch),
                    )
                    last_health = healthy
                    next_health_receipt = 0.0
                if monotonic() >= next_health_receipt:
                    await self.marker(
                        "capture_health",
                        valid=healthy,
                        detail={
                            "market_ready": healthy,
                            "blockers": health["blockers"],
                            "clock_evidence": self.status.clock_evidence,
                            "live_execution_available": False,
                        },
                    )
                    next_health_receipt = monotonic() + 5
                if healthy:
                    if aggregate is None:
                        aggregate = CandleAggregator(now)
                elif aggregate is not None:
                    # Start a new coverage segment if evidence becomes unsafe; do not bridge gaps.
                    aggregate = None
                    await self.marker(
                        "coverage_gap",
                        valid=False,
                        reason="market_evidence_unhealthy",
                        detail=health,
                    )
                if aggregate:
                    for candle in aggregate.close(now):
                        await self.marker(
                            "local:closed_candle", detail={"candle": candle.model_dump(mode="json")}
                        )
                if rest_task and rest_task.done():
                    rest_task.result()
                    rest_task = None
                    next_rest = monotonic() + 30
                if rest_task is None and monotonic() >= next_rest:
                    rest_task = asyncio.create_task(self.maintain_exchange(processor))
        finally:
            processor.invalidate()
            for task in (pending, rest_task):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (pending, rest_task) if task), return_exceptions=True
            )
            if hasattr(iterator, "aclose"):
                await iterator.aclose()

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        try:
            while not stop.is_set():
                self.epoch = uuid4()
                self.status.processor = None
                self.status.reason = "recovering"
                try:
                    await self.marker(
                        "epoch_started",
                        detail={"fresh_snapshots_required": True, "trade_history_complete": False},
                    )
                    processor = await self.bootstrap()
                    self.status.processor = processor
                    began = monotonic()
                    await self.epoch_loop(processor, stop)
                    if monotonic() - began >= 60:
                        failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self.status.processor:
                        self.status.processor.invalidate()
                    self.status.reason = type(exc).__name__
                    self.logger.warning(
                        "public_capture_interrupted",
                        epoch=str(self.epoch),
                        error_type=type(exc).__name__,
                        reason=str(exc)
                        if isinstance(exc, MarketDataError)
                        else "validation_or_transport_failure",
                    )
                    try:
                        await self.marker("capture_fault", valid=False, reason=type(exc).__name__)
                    except Exception:
                        # An unauditable capture must recover explicitly.
                        raise
                    failures += 1
                    self.faults += 1
                    if isinstance(exc, MarketDataError) and "checksum" in str(exc):
                        self.checksum_failures += 1
                    self.status.counters = self.report()
                finally:
                    self.status.processor = None
                if not stop.is_set():
                    self.reconnects += 1
                    try:
                        async with asyncio.timeout(min(60, 2 ** min(failures + 1, 6))):
                            await stop.wait()
                    except TimeoutError:
                        pass
        finally:
            self.status.processor = None
            self.status.reason = "stopped"
            await self.rest.close()

    def report(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "received_websocket_frames": self.receipts,
            "new_stream_trades": self.new_trades,
            "duplicate_stream_trades": self.duplicates,
            "reconnects": self.reconnects,
            "faults": self.faults,
            "checksum_failures": self.checksum_failures,
            "healthy_observations": self.healthy_observations,
            "observations": self.observations,
            "rest_requests": self.rest.requests,
            "rest_retries": self.rest.retries,
            "trading_ready": False,
            "live_execution_available": False,
        }


async def capture(settings: Settings, duration: int) -> dict[str, object]:
    if set(settings.instruments) != set(BINDINGS):
        raise MarketDataError("public capture requires both approved instruments")
    engine = create_engine(settings)
    status = CaptureStatus()
    server = CaptureHealthServer(status, CaptureMetrics())
    collector = PublicCapture(
        MarketStore(
            engine,
            database_budget=settings.paper_database_budget,
            timestamp_compatible=public_trade_time_compatible,
        ),
        status,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    worker: asyncio.Task[None] | None = None
    timer: asyncio.TimerHandle | None = None
    try:
        await verify_runtime(engine)
        await FoundationStore(engine).record_startup(settings, uuid4())
        async with engine.connect() as owner:
            acquired = await owner.scalar(text("SELECT pg_try_advisory_lock(741602)"))
            await owner.commit()
            if not acquired:
                raise MarketDataError("another public collector owns this database")
            try:
                await server.start(settings.health.host, settings.health.port)
                if duration:
                    timer = loop.call_later(duration, stop.set)
                worker = asyncio.create_task(collector.run(stop))
                while not stop.is_set():
                    if worker.done():
                        worker.result()
                        break
                    locked = await owner.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                            "AND pid = pg_backend_pid() AND objid = 741602 AND granted)"
                        )
                    )
                    await owner.commit()
                    if not locked:
                        raise MarketDataError("public capture owner lock lost")
                    try:
                        async with asyncio.timeout(1):
                            await stop.wait()
                    except TimeoutError:
                        pass
            finally:
                stop.set()
                if worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                await owner.execute(text("SELECT pg_advisory_unlock(741602)"))
                await owner.commit()
        await collector.marker("capture_stopped", detail=collector.report())
        return collector.report()
    finally:
        if timer:
            timer.cancel()
        status.shutting_down = True
        await server.close()
        await collector.rest.close()
        await engine.dispose()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
