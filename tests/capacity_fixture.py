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
from tempfile import TemporaryDirectory
from time import monotonic, process_time
from typing import Any, cast
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
from aca.reporting.replay_archive import export_archive, verify_archive
from aca.simulation.stored_replay import ReplayCursor, ReplayReceipt, StoredReplay
from aca.storage.archive_format import ArchiveReference
from aca.storage.database import create_engine
from aca.storage.migrate import migrate
from aca.storage.models import MarketReceipt
from aca.storage.paper import PaperStore, PaperWriter, checkpoints, events
from aca.storage.paper_projection import CHECKPOINT_INTERVAL, delta
from aca.storage.replay import cursors, public_events, valuations

PAGE = 500
TICKS = 20


async def seed_events(writer: PaperWriter, state: Any, target: int = MAX_REPORT_EVENTS) -> Any:
    store = writer.store
    async with writer.transaction() as c:
        previous = await c.scalar(
            select(events.c.fingerprint).where(
                events.c.account_id == store.account_id, events.c.version == state.version
            )
        )
    # Valid no-economic-change financial events at the report event ceiling.
    while state.version < target:
        batch, anchors = [], []
        for _ in range(min(PAGE, target - state.version)):
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
    return state


async def seed_values(writer: PaperWriter, replay: StoredReplay, additional: int) -> None:
    store = writer.store
    async with writer.transaction() as c:
        cursor_row = await replay._latest(c)
        value_row = await replay.history.latest(c)
        assert cursor_row is not None and value_row is not None
        previous_cursor, previous_value = (
            cursor_row["fingerprint"],
            value_row["fingerprint"],
        )
        historical = replay.history.restore(value_row)
    starting_at = historical.state.as_of
    epoch = uuid4()
    count = 1
    while count <= additional:
        size = min(PAGE, additional - count + 1)
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
                at = starting_at + timedelta(milliseconds=index * 100)
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
                value_hash = fingerprint({"previous": previous_value, "valuation": document})
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


async def run() -> None:
    if sys.argv[2:] not in ([], ["--preservation"], ["--compaction"]):
        raise ValueError("unsupported capacity fixture mode")
    compaction = sys.argv[2:] == ["--compaction"]
    preservation_only = sys.argv[2:] == ["--preservation"]
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
        "trial_mode": "compaction"
        if compaction
        else "preservation"
        if preservation_only
        else "operations",
        "operational_budgets_exercised": not preservation_only,
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
            await seed_events(writer, state)
            await seed_values(writer, replay, MAX_REPORT_VALUES - 1)
        result["seed_seconds"] = monotonic() - began
        result["record_limits"] = {
            "events": MAX_REPORT_EVENTS,
            "valuations": MAX_REPORT_VALUES,
            "public_receipts": MAX_REPORT_VALUES - 1,
        }
        result["operations"] = {}
        if compaction:
            from uuid import UUID

            from aca.operations.history import compact_history

            async with engine.connect() as c:
                expected_version = await c.scalar(
                    select(func.max(events.c.version)).where(
                        events.c.account_id == store.account_id
                    )
                )
                expected_source = await c.scalar(
                    select(func.max(valuations.c.number)).where(
                        valuations.c.account_id == store.account_id
                    )
                )
            compacting = monotonic()
            compacted = await compact_history(
                settings,
                store.account_id,
                uuid4(),
                "Isolated capacity complete-history compaction",
                expected_version,
                expected_source,
            )
            result["compaction"] = {"seconds": monotonic() - compacting, **compacted}
            store = PaperStore(
                engine, store.account_id, history_prefix=UUID(compacted["certificate"]["id"])
            )
            from aca.reporting.history_prefix import MAX_ONLINE_SUFFIX

            async with PaperWriter(store) as owner:
                venue = NoCommands(store)
                service = PaperExecution(owner, venue)
                current = await service._state()
                await seed_events(owner, current, current.version + MAX_ONLINE_SUFFIX)
                await seed_values(owner, StoredReplay(service, venue), MAX_ONLINE_SUFFIX)
            indexed_report = await historical_performance(store)
            full_report = await historical_performance(PaperStore(engine, store.account_id))
            assert indexed_report["period"] == full_report["period"]
            assert (
                cast(dict[str, Any], indexed_report["period"])["observations"]
                == MAX_REPORT_VALUES + MAX_ONLINE_SUFFIX
            )
            assert Decimal(cast(dict[str, Any], indexed_report["period"])["pnl_gbp"]) == 0
            result["compaction"]["bounded_suffix_records"] = MAX_ONLINE_SUFFIX
            result["compaction"]["full_report_economics_match"] = True

        async def measure(
            name: str,
            operation: Any,
            deadline: float,
            target: dict[str, Any] | None = None,
        ) -> Any:
            started, completed, returned = monotonic(), False, None
            try:
                # Diagnostic headroom never changes an acceptance deadline.
                async with asyncio.timeout(60):
                    returned = await operation()
                completed = True
            except TimeoutError:
                pass
            seconds = monotonic() - started
            outcome = "completed" if completed and seconds <= deadline else "deadline_exceeded"
            (result["operations"] if target is None else target)[name] = {
                "seconds": seconds,
                "deadline_seconds": deadline,
                "outcome": outcome,
                "operation_finished": completed,
                "measurement_timeout_seconds": 60,
            }
            return returned

        if not preservation_only:

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
        # The original operational trial still uses PostgreSQL. The separately
        # exported prefix also exercises the explicit online input path; these
        # subset measurements cannot stand in for the full C3 workload.
        if preservation_only:
            with TemporaryDirectory(prefix="aca-replay-preservation-") as temporary:
                path = Path(temporary) / "replay.aca"
                archive_start = monotonic()
                async with asyncio.timeout(600):
                    exported = await export_archive(store, path)
                export_seconds = monotonic() - archive_start
                verify_start = monotonic()
                verified = verify_archive(path, str(exported["archive_sha256"]))
                verify_seconds = monotonic() - verify_start
                if not verified["integrity_verified"] or verified["usable_as_operational_prefix"]:
                    raise ValueError("archive verification/capability mismatch")
                result["replay_preservation"] = {
                    "export_seconds": export_seconds,
                    "independent_verify_seconds": verify_seconds,
                    "archive": verified,
                    "pruned_records": 0,
                    "operational_read_path_uses_archive": True,
                    "cold_storage_capacity_demonstrated": False,
                }
                reference = ArchiveReference(path, str(exported["archive_sha256"]))
                online_store = PaperStore(engine, store.account_id, replay_archive=reference)
                measurements: dict[str, Any] = {}
                online_report = await measure(
                    "historical_report",
                    lambda: historical_performance(online_store),
                    20,
                    measurements,
                )
                same = None
                if online_report is not None:
                    sql_document = {
                        **{k: v for k, v in online_report.items() if k != "report_hash"},
                        "replay_archive_sha256": None,
                    }
                    same = (
                        fingerprint(sql_document)
                        == cast(dict[str, Any], exported["context"])["database_audit_report_hash"]
                    )
                    if not same:
                        raise ValueError("archived report differs from the audited export snapshot")
                archive_owner = PaperWriter(online_store)
                acquired = False

                async def archive_startup() -> None:
                    nonlocal acquired
                    await archive_owner.__aenter__()
                    acquired = True

                try:
                    await measure("combined_writer_startup", archive_startup, 20, measurements)
                    if acquired:
                        archive_venue = NoCommands(online_store)
                        archive_replay = StoredReplay(
                            PaperExecution(archive_owner, archive_venue), archive_venue
                        )
                        await measure(
                            "replay_audit_after_writer_acquisition",
                            archive_replay.audit,
                            10,
                            measurements,
                        )
                finally:
                    if acquired:
                        await archive_owner.__aexit__()

                async def archived_summary() -> None:
                    async with engine.connect() as c:
                        await c.execution_options(
                            isolation_level="REPEATABLE READ", postgresql_readonly=True
                        )
                        await daily_summary(c, online_store, AT.date())

                await measure("scheduled_daily_summary", archived_summary, 15, measurements)
                result["archive_input"] = {
                    "measurements": measurements,
                    "economic_equivalence_verified": same,
                    "financial_history_still_audited_in_full": online_report is not None,
                    "prefix_pruning_available": False,
                    "retained_rows_removed": 0,
                    "measured_boundary_failures": [
                        key
                        for key, value in measurements.items()
                        if value["outcome"] != "completed"
                    ],
                }
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
