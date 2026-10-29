"""Measured local workload and bounded extrapolation; never elapsed reliability.

Run only through capacity_trial.py --observation-profile in its disposable DB.
The old suffix, catalogue, memory and database ceilings remain enforced.
"""

import asyncio
import json
import math
import resource
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, process_time
from uuid import UUID, uuid4

from capacity_fixture import seed_events, seed_values
from process_worker import AT
from restore_fixture import create_database, settings_for
from sqlalchemy import func, select, text
from test_stored_replay import Feed, evidence

from aca.app.paper_supervisor import PaperSupervisor, SupervisorStatus
from aca.execution.paper import PaperExecution
from aca.market_data.paper_feed import StoredPaperFeed
from aca.operations.history import compact_history, vacuum_history
from aca.reporting.historical import historical_performance
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.audit_prefix import package_fingerprint
from aca.storage.database import create_engine
from aca.storage.history_blocks import blocks
from aca.storage.migrate import migrate
from aca.storage.paper import PaperStore, PaperWriter, events
from aca.storage.replay import valuations


async def run() -> None:
    probe = sys.argv[2:] == ["--probe"]
    settings = settings_for(sys.argv[1])
    create_database(settings)
    await migrate(settings)
    engine = create_engine(settings)
    store = PaperStore(engine, uuid4())
    started, cpu = monotonic(), process_time()
    result: dict[str, object] = {
        "kind": "paper-observation-profile-measurement-v1",
        "package_source_sha256": package_fingerprint(),
        "synthetic": True,
        "qualifying_days": 0,
        "56_day_reliability_demonstrated": False,
        "limits_changed": {
            "default_database_bytes": 2_000_000_000,
            "opt_in_database_bytes": 8_000_000_000,
            "suffix": False,
            "financial_or_risk_limits": False,
        },
        "cycles": [],
    }

    def progress(phase: str) -> None:
        print(
            "ACA_CAPACITY_PROGRESS "
            + json.dumps({"phase": phase, "elapsed_seconds": monotonic() - started}),
            file=sys.stderr,
            flush=True,
        )

    async def measure_size() -> tuple[int, int]:
        async with engine.connect() as c:
            size = await c.scalar(text("SELECT pg_database_size(current_database())"))
            compressed = await c.scalar(select(func.sum(func.octet_length(blocks.c.payload))))
            return int(size or 0), int(compressed or 0)

    async def compact(previous: UUID | None) -> tuple[PaperStore, float]:
        async with engine.connect() as c:
            version = await c.scalar(select(func.max(events.c.version)))
            source = await c.scalar(select(func.max(valuations.c.number)))
        began = monotonic()
        value = await compact_history(
            settings,
            store.account_id,
            uuid4(),
            "Measured stopped-worker local observation maintenance",
            version,
            source,
            previous_history=previous,
        )
        await vacuum_history(settings, store.account_id)
        return PaperStore(
            engine, store.account_id, history_prefix=UUID(value["certificate"]["id"])
        ), monotonic() - began

    try:
        # Existing 100k boundary. Bulk fixtures are not ingestion-rate evidence.
        async with PaperWriter(store) as writer:
            venue = PaperExchange(store)
            service = PaperExecution(writer, venue)
            await service.initialise(AT)
            state = await service.cashflow("profile-opening", Decimal(1000), AT)
            replay = StoredReplay(service, venue)
            await replay.enrol()
            await seed_events(writer, state, 1000 if probe else 100000)
            await seed_values(writer, replay, 999 if probe else 99999)
        progress("seeded")
        fast, first_seconds = await compact(None)
        progress("compacted")
        result["initial_full_maintenance_seconds"] = first_seconds
        initial_size, initial_payload = await measure_size()
        cycles = []
        for index in range(1 if probe else 3):
            async with PaperWriter(fast) as writer:
                venue = PaperExchange(fast)
                service = PaperExecution(writer, venue)
                state = await service._state()
                await seed_events(writer, state, state.version + 3600)
                await seed_values(writer, StoredReplay(service, venue), 2700)
            old = await historical_performance(fast)
            fast, seconds = await compact(fast.history_prefix)
            began = monotonic()
            async with PaperWriter(fast) as writer:
                replay = StoredReplay(
                    PaperExecution(writer, PaperExchange(fast)), PaperExchange(fast)
                )
                await replay.audit()
            restart_seconds = monotonic() - began
            new = await historical_performance(fast)
            assert new["period"] == old["period"]
            size, payload = await measure_size()
            progress("profile_cycle_" + str(index))
            cycles.append(
                {
                    "cycle": index,
                    "maintenance_seconds": seconds,
                    "restart_seconds": restart_seconds,
                    "database_bytes": size,
                    "compressed_payload_bytes": payload,
                }
            )
        result["cycles"] = cycles
        # Real publication/replay/supervisor calls, fresh two-asset books, varying
        # prices and trades. Burst input is exercised; no historical seed speed
        # becomes a live throughput claim.
        tick_times = []
        async with PaperWriter(fast) as writer:
            venue = PaperExchange(fast)
            service = PaperExecution(writer, venue)
            replay = StoredReplay(service, venue)
            feed = Feed(replay)
            async with writer.transaction() as c:
                head = await replay.history.latest(c)
                assert head is not None
                now = replay.history.restore(head).state.as_of + timedelta(seconds=1)
            await feed.bootstrap(now)
            now += timedelta(microseconds=1)
            await feed.marker(
                "instrument_rules",
                now,
                {
                    "rules": [
                        evidence(now, feed.epoch, symbol)
                        .rules.model_copy(update={"observed_at": now})
                        .model_dump(mode="json")
                        for symbol in ("BTC/GBP", "ETH/GBP")
                    ]
                },
            )
            status = SupervisorStatus()

            async def capacity() -> None:
                async with writer.transaction() as c:
                    from aca.reporting.history_prefix import suffix_budget

                    await suffix_budget(c, fast, writer.accepted_history, new_commitment=True)

            loop = PaperSupervisor(
                service,
                status,
                marks=StoredPaperFeed(fast).marks,
                capacity=capacity,
                clock=lambda: now,
                timer=lambda: now.timestamp(),
                replay=replay,
            )
            await loop.start()
            input_started = monotonic()
            input_count = 0
            for index in range(24):
                now += timedelta(seconds=1)
                if index % 4 == 0:
                    books = [
                        evidence(
                            now,
                            feed.epoch,
                            symbol,
                            bid=str(999 + index),
                            ask=str(1000 + index),
                            depth="10",
                        ).book.model_dump(mode="json")
                        for symbol in ("BTC/GBP", "ETH/GBP")
                    ]
                    await feed.marker("book", now, {"books": books})
                    await feed.marker("capture_health", now, {"market_ready": True})
                    input_count += 2
                if index % 10 == 0:
                    await feed.trade(now, size="0.1")
                    input_count += 1
                began = monotonic()
                await loop.tick()
                tick_times.append(monotonic() - began)
                assert status.market_ready and status.reconciled and status.reason is None, (
                    status.reason
                )
            result["workload"] = {
                "ticks": len(tick_times),
                "input_frames": input_count,
                "modelled_seconds": 24,
                "wall_seconds": monotonic() - input_started,
                "max_tick_seconds": max(tick_times),
                "backlog_at_end": status.reason,
                "tick_deadline_seconds": 3,
                "risk_checks_skipped": 0,
            }
        # Complete original-row reconstruction still runs, independently of all
        # certificates, and must equal the bounded economic report and state.
        progress("workload_measured")
        began = monotonic()
        full = await historical_performance(store)
        indexed = await historical_performance(fast)
        assert full["period"] == indexed["period"]
        async with engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            assert await store.audit(c, full=True) == await fast.audit(c)
        result["independent_rebuild_seconds"] = monotonic() - began
        final_size, final_payload = await measure_size()
        async with engine.connect() as c:
            retained_trade_bytes = await c.scalar(
                text(
                    "SELECT max(pg_column_size(p) + pg_column_size(r)) "
                    "FROM aca.public_trades p JOIN aca.market_receipts r ON r.id = p.receipt_id"
                )
            )
        if type(retained_trade_bytes) is not int or retained_trade_bytes <= 0:
            raise ValueError("representative retained trade sizing unavailable")
        # Fixed 56-day envelope: 1 financial observation/s, <=.75 public frames/s,
        # one maintenance/hour, 2x measured storage/downtime plus 512MB reserve.
        # This is conditional on the declared producer rate, not an assertion
        # that an uncapped Kraken public stream fits that envelope.
        max_pause = max(
            float(c["maintenance_seconds"]) + float(c["restart_seconds"]) for c in cycles
        )
        payload_hour = max(
            (
                cycles[i]["compressed_payload_bytes"]
                - (cycles[i - 1]["compressed_payload_bytes"] if i else initial_payload)
            )
            for i in range(len(cycles))
        )
        projected = (
            initial_size
            + 2 * max(payload_hour, (final_size - initial_size) // len(cycles)) * 56 * 24
            + 512_000_000
            + 2_000_000_000  # separate retained trade/raw evidence allowance
        )
        pause = 2 * max_pause + 2  # explicit process stop/start contingency
        total_seconds = 56 * 86400
        # Fixed maximum encoded source row 8kB, including its original receipt.
        # Hourly final partial blocks count separately; the catalogue limit stays.
        catalogue_blocks = (
            math.ceil(total_seconds / 256)
            + 3 * math.ceil(total_seconds * 0.75 / 256)
            + math.ceil(total_seconds * 0.75 / 125)
            + 5 * 56 * 24
            + math.ceil(100000 / 256) * 5
        )
        model = {
            "days": 56,
            "financial_records_per_second": "1",
            "public_frames_per_second": "0.75",
            "public_trades_per_second": "0.1",
            "maximum_encoded_source_row_bytes": 8000,
            "projected_catalogue_blocks": catalogue_blocks,
            "catalogue_limit": 100000,
            "retained_trade_bytes_measured": retained_trade_bytes,
            "retained_trade_bytes_with_row_and_index_allowance": retained_trade_bytes + 512,
            "retained_trade_evidence_allowance_bytes": 2_000_000_000,
            "retained_trade_evidence_projection_2x": 2
            * (retained_trade_bytes + 512)
            * 0.1
            * total_seconds,
            "capacity_is_conditional_on_declared_rate_and_record_size": True,
            "maintenance_every_seconds": 3600,
            "suffix_financial": 3600,
            "suffix_public": 2700,
            "suffix_limit": 4096,
            "maintenance_unavailability_bound_seconds": pause,
            "availability_lower_model": 1 - pause / 3600,
            "projected_database_bytes": projected,
            "database_limit_bytes": 8_000_000_000,
            "storage_growth_multiplier": 2,
            "reserve_bytes": 512_000_000,
            "backup_and_host_loss_proven": False,
            "production_stream_fit_proven": False,
        }
        result["model"] = model
        result["database_bytes"] = final_size
        result["compressed_payload_bytes"] = final_payload
        result["accepted"] = (
            not probe
            and pause <= 18
            and projected < 8_000_000_000
            and max(tick_times) < 3
            and catalogue_blocks < 100000
            and 2 * (retained_trade_bytes + 512) * 0.1 * total_seconds <= 2_000_000_000
        )
        result["cpu_seconds"] = process_time() - cpu
        result["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for name in ("memory.peak", "memory.max", "cpu.max"):
            path = Path("/sys/fs/cgroup") / name
            result[name] = path.read_text().strip() if path.exists() else None
        result["elapsed_seconds"] = monotonic() - started
        print(json.dumps(result, sort_keys=True))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
