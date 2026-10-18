"""One-second maintenance of existing virtual accounts; no strategy/fill dispatch."""

import asyncio
import json
import shutil
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

import structlog
from prometheus_client import Counter, Gauge
from sqlalchemy import text

from aca.app.health import FoundationMetrics, FoundationStatus, HealthServer
from aca.domain.trading import Valuation
from aca.execution.paper import PaperExecution
from aca.market_data.paper_feed import FeedUnavailable, StoredPaperFeed
from aca.operations.config import Settings
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.database import create_engine, verify_runtime
from aca.storage.paper import PaperStore, PaperWriter
from aca.storage.replay import cursors


class SupervisorStatus(FoundationStatus):
    def __init__(self) -> None:
        super().__init__(freshness_seconds=2)
        self.account_id: UUID | None = None
        self.market_ready = False
        self.reconciled = False
        self.reason: str | None = "starting"
        self.ticks = 0
        self.healthy_ticks = 0
        self.tick_seconds = 0.0
        self.fill_replay_enabled = False
        self.replay_cursor: int | None = None
        self.replay_processed = 0
        self.replay_skipped_trades = 0

    @property
    def supervision_ready(self) -> bool:
        return self.ready and self.market_ready and self.reconciled and self.reason is None

    def document(self) -> dict[str, object]:
        doc = super().document()
        doc.update(
            {
                "phase": 4,
                "kind": "paper_maintenance_v1",
                "account_id": str(self.account_id),
                "supervision_ready": self.supervision_ready,
                "market_ready": self.market_ready,
                "reconciled": self.reconciled,
                "reason": self.reason,
                "ticks": self.ticks,
                "healthy_ticks": self.healthy_ticks,
                "last_tick_seconds": self.tick_seconds,
                "strategy_dispatch_enabled": False,
                "fill_replay_enabled": self.fill_replay_enabled,
                "replay_cursor": self.replay_cursor,
                "replay_processed": self.replay_processed,
                "replay_skipped_trades": self.replay_skipped_trades,
                "promotion_evidence": False,
            }
        )
        return doc


class SupervisorMetrics(FoundationMetrics):
    def __init__(self) -> None:
        super().__init__()
        self.supervision = Gauge(
            "aca_paper_supervision_ready", "Maintenance readiness only", registry=self.registry
        )
        self.duration = Gauge(
            "aca_paper_tick_seconds", "Last maintenance tick duration", registry=self.registry
        )
        self.observations = Counter(
            "aca_paper_observations",
            "Observed maintenance ticks",
            ["healthy"],
            registry=self.registry,
        )
        self.replay_cursor = Gauge(
            "aca_paper_replay_cursor", "Last committed public replay number", registry=self.registry
        )
        self.replay_processed = Gauge(
            "aca_paper_replay_processed", "Frames consumed this process", registry=self.registry
        )
        self.replay_skipped = Gauge(
            "aca_paper_replay_skipped_trades", "Unusable update trades", registry=self.registry
        )

    def render(self, status: FoundationStatus) -> bytes:
        if isinstance(status, SupervisorStatus):
            self.supervision.set(int(status.supervision_ready))
            self.duration.set(status.tick_seconds)
            self.replay_cursor.set(status.replay_cursor or 0)
            self.replay_processed.set(status.replay_processed)
            self.replay_skipped.set(status.replay_skipped_trades)
        return super().render(status)


class SupervisorHealthServer(HealthServer):
    def response(self, method: str, path: str) -> tuple[int, str, bytes]:
        if method == "GET" and path == "/health/paper":
            doc = self.status.document()
            return (
                200 if doc.get("supervision_ready") is True else 503,
                "application/json",
                json.dumps(doc, sort_keys=True).encode(),
            )
        return super().response(method, path)


class PaperSupervisor:
    def __init__(
        self,
        service: PaperExecution,
        status: SupervisorStatus,
        *,
        marks: Callable[[datetime], Awaitable[tuple[Valuation, ...]]],
        capacity: Callable[[], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timer: Callable[[], float] = monotonic,
        replay: StoredReplay | None = None,
    ) -> None:
        self.service, self.status = service, status
        self.marks, self.capacity, self.clock, self.timer = marks, capacity, clock, timer
        self.last_clock: datetime | None = None
        self.replay = replay
        status.fill_replay_enabled = replay is not None
        self.last_timer: float | None = None
        self.next_reconcile = 0.0
        status.account_id = service.store.account_id

    async def suspend(self, reason: str, at: datetime) -> None:
        old = await self.service._state()
        # Persist at the last accepted time if the wall clock has moved backwards.
        at = max(at, old.as_of)
        if old.ready or old.incident != reason:
            await self.service._save(
                old,
                old.model_copy(update={"ready": False, "incident": reason}),
                at=at,
                kind=reason,
                alert=True,
            )
        await self.service.cancel_entries(at)
        # A successful cancellation reconciles the venue and can set ready.
        # Restore the maintenance block after those financial/safety effects.
        current = await self.service._state()
        if current.ready or current.incident != reason:
            await self.service._save(
                current,
                current.model_copy(update={"ready": False, "incident": reason}),
                at=at,
                kind=reason,
            )

    async def start(self) -> None:
        at = self.clock()
        if self.replay is None:
            from sqlalchemy import select

            async with self.service.writer.transaction() as c:
                enrolled = await c.scalar(
                    select(cursors.c.number)
                    .where(cursors.c.account_id == self.service.store.account_id)
                    .limit(1)
                )
            if enrolled is not None:
                raise ValueError("enrolled paper accounts require the replay supervisor")
        if at < (await self.service._state()).as_of:
            raise ValueError("paper supervisor cannot move account clock backwards")
        if self.replay:
            await self.replay.audit()
            for _ in range(8):
                result = await self.replay.batch(limit=64, at=self.clock())
                self.status.replay_cursor = result.cursor
                self.status.replay_processed += result.processed
                self.status.replay_skipped_trades += result.skipped_trades
                if not result.more:
                    break
            else:
                raise ValueError("paper replay startup backlog budget exhausted")
            at = self.clock()
        # The owner already audited the prefix. Recovery never resends consumed
        # work; maintenance cancels outstanding entries and keeps protection.
        await self.service.recover(at)
        await self.service.cancel_entries(at)
        await self.suspend("paper_supervisor_starting", at)

    async def tick(self) -> None:
        at, tick = self.clock(), self.timer()
        self.status.market_ready = self.status.reconciled = False
        self.status.reason = "checking"
        if self.last_clock is not None and self.last_timer is not None:
            if abs((at - self.last_clock).total_seconds() - (tick - self.last_timer)) > 0.5:
                raise ValueError("wall clock discontinuity")
        self.last_clock, self.last_timer = at, tick
        if at < (await self.service._state()).as_of:
            raise ValueError("paper account time ahead of wall clock")
        await self.capacity()
        if self.replay:
            replay_result = await self.replay.batch(limit=64, at=at)
            self.status.replay_cursor = replay_result.cursor
            self.status.replay_processed += replay_result.processed
            self.status.replay_skipped_trades += replay_result.skipped_trades
            if replay_result.more:
                await self.service.reconcile(at)
                self.status.reason = "paper_replay_backlog"
                await self.suspend(self.status.reason, at)
                return
            if replay_result.reason is not None:
                await self.service.reconcile(at)
                self.status.reason = "paper_replay_unavailable"
                await self.suspend(self.status.reason, at)
                return
        try:
            marks = await self.marks(at)
        except FeedUnavailable:
            self.status.reason = "paper_feed_unavailable"
            await self.suspend(self.status.reason, at)
            return
        self.status.market_ready = True
        await self.service.mark(marks, at)
        state = await self.service._state()
        if tick >= self.next_reconcile or (
            not state.ready
            and state.incident
            in {
                "paper_feed_unavailable",
                "paper_supervisor_starting",
                "invalid_gbp_valuation",
                "paper_replay_pending",
                "paper_replay_backlog",
            }
        ):
            result = await self.service.reconcile(at)
            self.next_reconcile = tick + 15
            self.status.reconciled = result.complete
        else:
            self.status.reconciled = (
                state.reconciled_at is not None
                and 0 <= (at - state.reconciled_at).total_seconds() <= 30
            )
        state = await self.service._state()
        # A risk latch is healthy detection, but it cannot grant readiness.
        from aca.portfolio.state import entries_paused

        self.status.reason = (
            state.incident or "risk_suspended" if entries_paused(state, at) else None
        )
        self.status.database_ready = self.status.schema_ready = True
        self.status.last_success = self.timer()

    async def run(self, stop: asyncio.Event, metrics: SupervisorMetrics) -> None:
        try:
            async with asyncio.timeout(10):
                await self.start()
            while not stop.is_set():
                began = self.timer()
                async with asyncio.timeout(3):
                    await self.tick()
                self.status.tick_seconds = self.timer() - began
                self.status.ticks += 1
                healthy = self.status.supervision_ready
                self.status.healthy_ticks += int(healthy)
                metrics.observations.labels(healthy=str(healthy).lower()).inc()
                # No catch-up burst after a slow tick. Cancellation and SIGTERM
                # wake this wait immediately; all deadlines are monotonic.
                try:
                    async with asyncio.timeout(max(0.001, 1 - self.status.tick_seconds)):
                        await stop.wait()
                except TimeoutError:
                    pass
        except BaseException:
            self.status.reason = "paper_supervisor_fault"
            self.status.last_success = None
            raise
        finally:
            self.status.shutting_down = True
            self.status.market_ready = self.status.reconciled = False
            if self.status.reason != "paper_supervisor_fault":
                self.status.reason = "paper_supervisor_stopped"
            try:
                async with asyncio.timeout(5):
                    await self.suspend(self.status.reason, self.clock())
            except Exception:
                # The database may be unavailable. No port transmission is
                # permitted without the writer's durable command transaction.
                structlog.get_logger().error("paper_shutdown_state_unavailable")


async def supervise(
    settings: Settings,
    account_id: UUID,
    duration: int,
    *,
    replay_enabled: bool = False,
) -> dict[str, object]:
    if not 0 <= duration <= 86400:
        raise ValueError("duration must be 0..86400 seconds")
    engine = create_engine(settings)
    store = PaperStore(engine, account_id)
    status, metrics = SupervisorStatus(), SupervisorMetrics()
    server = SupervisorHealthServer(status, metrics)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, stop.set)
    timer = loop.call_later(duration, stop.set) if duration else None

    async def capacity() -> None:
        await verify_runtime(engine)
        async with engine.connect() as c:
            size = await c.scalar(text("SELECT pg_database_size(current_database())"))
        disk = shutil.disk_usage("/")
        if size is None or size >= 2_000_000_000 or disk.free * 100 <= disk.total * 15:
            raise ValueError("paper database/local disk capacity limit")

    try:
        await verify_runtime(engine)
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            supervisor = PaperSupervisor(
                service,
                status,
                marks=StoredPaperFeed(store).marks,
                capacity=capacity,
                replay=StoredReplay(service, venue) if replay_enabled else None,
            )
            await server.start(settings.health.host, settings.health.port)
            await supervisor.run(stop, metrics)
        return status.document()
    finally:
        if timer:
            timer.cancel()
        await server.close()
        for sig in signals:
            loop.remove_signal_handler(sig)
        await engine.dispose()
