"""C3 bounded capacity measurement, not a market-throughput or reliability claim.

Bulk construction uses the production record/hash contracts. It deliberately
does not pretend bulk INSERT speed measures the actual execution writer.
"""

import asyncio
import hashlib
import importlib.metadata
import json
import resource
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, process_time
from typing import Any
from uuid import uuid4

from process_worker import AT
from restore_fixture import NoCommands, create_database, settings_for
from sqlalchemy import func, insert, select, text

import aca
from aca.app.paper_supervisor import PaperSupervisor, SupervisorMetrics, SupervisorStatus
from aca.domain.trading import fingerprint
from aca.execution.paper import PaperExecution
from aca.observability.notices import daily_summary
from aca.reporting.historical import MAX_REPORT_EVENTS, MAX_REPORT_VALUES, historical_performance
from aca.simulation.stored_replay import ReplayCursor, ReplayReceipt, StoredReplay
from aca.storage.database import create_engine
from aca.storage.migrate import migrate
from aca.storage.models import MarketReceipt
from aca.storage.paper import PaperStore, PaperWriter, checkpoints, events
from aca.storage.paper_projection import CHECKPOINT_INTERVAL, delta
from aca.storage.replay import cursors, public_events, valuations

PAGE = 500
TICKS = 20


async def seed_events(writer: PaperWriter, state: Any) -> None:
    store = writer.store
    async with writer.transaction() as c:
        previous = await c.scalar(
            select(events.c.fingerprint).where(
                events.c.account_id == store.account_id, events.c.version == state.version
            )
        )
    # Valid no-economic-change financial events at the report event ceiling.
    while state.version < MAX_REPORT_EVENTS:
        batch, anchors = [], []
        for _ in range(min(PAGE, MAX_REPORT_EVENTS - state.version)):
            after = state.model_copy(update={"version": state.version + 1})
            document = delta(state, after, None)
            source = "capacity:" + str(after.version)
            hashed = fingerprint(
                {
                    "previous": previous,
                    "version": after.version,
                    "kind": "valuation",
                    "source": source,
                    "state": document,
                }
            )
            batch.append(
                dict(
                    account_id=store.account_id,
                    version=after.version,
                    id=uuid4(),
                    source=source,
                    kind="valuation",
                    at=AT,
                    previous_hash=previous,
                    fingerprint=hashed,
                    document=document,
                )
            )
            if after.version % CHECKPOINT_INTERVAL == 0:
                anchors.append(
                    dict(
                        account_id=store.account_id,
                        version=after.version,
                        event_hash=hashed,
                        state_hash=fingerprint(after.document()),
                        document=after.document(),
                    )
                )
            state, previous = after, hashed
        async with writer.transaction() as c:
            await c.execute(events.insert(), batch)
            if anchors:
                await c.execute(checkpoints.insert(), anchors)


async def run() -> None:
    if not (1 <= MAX_REPORT_EVENTS <= 100000 and 1 <= MAX_REPORT_VALUES <= 100000):
        raise ValueError("capacity fixture requires review above 100,000 records")
    settings = settings_for(sys.argv[1])
    create_database(settings)
    await migrate(settings)
    engine = create_engine(settings)
    store = PaperStore(engine, uuid4())
    package = Path(aca.__file__).parent
    source_hash = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        source_hash.update(
            path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes()
        )
    result: dict[str, Any] = {
        "application_version": aca.__version__,
        "package_source_sha256": source_hash.hexdigest(),
        "certifi_version": importlib.metadata.version("certifi"),
        "live_execution_available": False,
        "promotion_evidence": False,
        "bulk_seed_is_ingestion_measurement": False,
    }
    cpu, began = process_time(), monotonic()
    try:
        async with PaperWriter(store) as writer:
            venue = NoCommands(store)
            service = PaperExecution(writer, venue)
            await service.initialise(AT)
            state = await service.cashflow("capacity-opening", Decimal(1000), AT)
            replay = StoredReplay(service, venue)
            await replay.enrol()
            async with writer.transaction() as c:
                cursor_row = await replay._latest(c)
                value_row = await replay.history.latest(c)
                assert cursor_row is not None and value_row is not None
                previous_cursor, previous_value = (
                    cursor_row["fingerprint"],
                    value_row["fingerprint"],
                )
                historical = replay.history.restore(value_row)
            await seed_events(writer, state)
            # Subsecond cash-only observations inside one UTC day, at the value ceiling.
            epoch = uuid4()
            count = 1
            while count < MAX_REPORT_VALUES:
                size = min(PAGE, MAX_REPORT_VALUES - count)
                async with writer.transaction() as c:
                    numbers = list(
                        await c.scalars(
                            text(
                                "SELECT nextval('aca.public_replay_events_number_seq') "
                                "FROM generate_series(1,:count)"
                            ),
                            {"count": size},
                        )
                    )
                    source_rows, receipt_rows, value_rows, cursor_rows = [], [], [], []
                    for index, number in enumerate(numbers, count):
                        at = AT + timedelta(milliseconds=index * 100)
                        receipt = ReplayReceipt(
                            receipt_id=uuid4(),
                            epoch=epoch,
                            received_at=at,
                            channel="capture_health",
                            valid=True,
                            payload_hash=hashlib.sha256(b"{}").hexdigest(),
                            normalized={},
                            trades=(),
                        )
                        raw = receipt.document()
                        hashed = fingerprint(raw)
                        receipt_rows.append(
                            dict(
                                id=receipt.receipt_id,
                                schema_version=1,
                                epoch=epoch,
                                received_at=at,
                                channel=receipt.channel,
                                valid=True,
                                reason=None,
                                payload_hash=receipt.payload_hash,
                                payload="{}",
                                normalized={},
                            )
                        )
                        source_rows.append(
                            dict(
                                number=number,
                                receipt_id=receipt.receipt_id,
                                document=raw,
                                fingerprint=hashed,
                                published_at=at,
                            )
                        )
                        value = replay.history.observe(historical, at, (), "awaiting_new_epoch")
                        value = value.model_copy(
                            update={
                                "number": number,
                                "source_hash": hashed,
                                "source_received_at": at,
                            }
                        )
                        document = value.document()
                        value_hash = fingerprint(
                            {"previous": previous_value, "valuation": document}
                        )
                        value_rows.append(
                            dict(
                                account_id=store.account_id,
                                number=number,
                                previous_hash=previous_value,
                                fingerprint=value_hash,
                                document=document,
                            )
                        )
                        context = ReplayCursor(
                            account_id=store.account_id,
                            number=number,
                            source_hash=hashed,
                            last_received=at,
                        )
                        document = context.document()
                        cursor_hash = fingerprint({"previous": previous_cursor, "cursor": document})
                        cursor_rows.append(
                            dict(
                                account_id=store.account_id,
                                number=number,
                                previous_hash=previous_cursor,
                                fingerprint=cursor_hash,
                                document=document,
                            )
                        )
                        previous_value, previous_cursor, historical = value_hash, cursor_hash, value
                    await c.execute(insert(MarketReceipt), receipt_rows)
                    await c.execute(public_events.insert(), source_rows)
                    await c.execute(valuations.insert(), value_rows)
                    await c.execute(cursors.insert(), cursor_rows)
                count += size
        result["seed_seconds"] = monotonic() - began
        result["record_limits"] = {
            "events": MAX_REPORT_EVENTS,
            "valuations": MAX_REPORT_VALUES,
            "public_receipts": MAX_REPORT_VALUES - 1,
        }
        result["operations"] = {}

        async def measure(name: str, operation: Any, deadline: float) -> None:
            started = monotonic()
            completed = False
            try:
                # Keep the acceptance deadline unchanged, but allow a bounded
                # diagnostic tail so an overrun records its actual duration.
                # An operation finishing after its budget still fails it.
                async with asyncio.timeout(60):
                    await operation()
                completed = True
            except TimeoutError:
                pass
            seconds = monotonic() - started
            outcome = "completed" if completed and seconds <= deadline else "deadline_exceeded"
            result["operations"][name] = {
                "seconds": seconds,
                "deadline_seconds": deadline,
                "outcome": outcome,
                "operation_finished": completed,
                "measurement_timeout_seconds": 60,
            }

        async def startup() -> None:
            async with PaperWriter(store):
                pass

        async def history_audit() -> None:
            async with PaperWriter(store) as owner:
                await StoredReplay(
                    PaperExecution(owner, NoCommands(store)), NoCommands(store)
                ).audit()

        await measure("financial_writer_startup", startup, 30)
        await measure("replay_startup_audit", history_audit, 20)
        await measure("historical_report", lambda: historical_performance(store), 20)
        async with PaperWriter(store) as owner:
            replay_reader = StoredReplay(
                PaperExecution(owner, NoCommands(store)), NoCommands(store)
            )
            # The supervisor's existing ten-second startup timeout begins after
            # financial writer acquisition, unlike the combined measurement.
            await measure("replay_audit_after_writer_acquisition", replay_reader.audit, 10)

        async def summary() -> None:
            async with engine.connect() as c:
                await c.execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                )
                await daily_summary(c, store, AT.date())

        # The notification owner's existing summary budget is 15 seconds.
        await measure("scheduled_daily_summary", summary, 15)
        # Finite real one-second loop, including actual maintenance writes, with
        # no invented feed health/market fills. Use the non-replay loop separately.
        tick_store = PaperStore(engine, uuid4())
        async with PaperWriter(tick_store) as seed_writer:
            seed_service = PaperExecution(seed_writer, NoCommands(tick_store))
            await seed_service.initialise(AT)
            seed_state = await seed_service.cashflow("loop-opening", Decimal(1000), AT)
            await seed_events(seed_writer, seed_state)
        stop, health, metrics = asyncio.Event(), SupervisorStatus(), SupervisorMetrics()
        async with PaperWriter(tick_store) as owner:
            venue = NoCommands(tick_store)
            service = PaperExecution(owner, venue)

            async def marks(at: Any) -> tuple[()]:
                return ()

            async def capacity() -> None:
                pass

            durations = []

            class MeasuredSupervisor(PaperSupervisor):
                async def tick(self) -> None:
                    tick = monotonic()
                    await super().tick()
                    durations.append(monotonic() - tick)
                    if len(durations) >= TICKS:
                        stop.set()

            start = monotonic()
            supervisor = MeasuredSupervisor(
                service,
                health,
                marks=marks,
                capacity=capacity,
                clock=lambda: AT + timedelta(seconds=monotonic() - start),
            )
            await supervisor.run(stop, metrics)
            result["one_second_loop"] = {
                "financial_events_before_startup": MAX_REPORT_EVENTS,
                "ticks": len(durations),
                "elapsed_seconds": monotonic() - start,
                "max_tick_seconds": max(durations),
                "mean_tick_seconds": sum(durations) / len(durations),
                "healthy_ticks": health.healthy_ticks,
                "venue_commands": venue.attempts,
            }
        async with engine.connect() as c:
            result["database_bytes"] = await c.scalar(
                text("SELECT pg_database_size(current_database())")
            )
            result["relations"] = [
                dict(r)
                for r in (
                    await c.execute(
                        text(
                            "SELECT relname,n_live_tup,pg_total_relation_size(relid) AS bytes "
                            "FROM pg_stat_user_tables WHERE schemaname='aca' ORDER BY relname"
                        )
                    )
                ).mappings()
            ]
            result["global_source_head"] = await c.scalar(select(func.max(public_events.c.number)))
        result["cpu_seconds"] = process_time() - cpu
        result["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for filename in ("memory.peak", "memory.max", "cpu.max"):
            path = Path("/sys/fs/cgroup") / filename
            result[filename] = path.read_text().strip() if path.exists() else None
        result["elapsed_seconds"] = monotonic() - began
        result["observations_per_56_days_at_1hz"] = 56 * 86400
        result["value_limit_days_at_1hz"] = MAX_REPORT_VALUES / 86400
        result["56_day_headroom_demonstrated"] = False
        result["configured_budget_bytes"] = 2_000_000_000
        result["measured_boundary_failures"] = [
            key for key, value in result["operations"].items() if value["outcome"] != "completed"
        ]
        print(json.dumps(result, sort_keys=True))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
