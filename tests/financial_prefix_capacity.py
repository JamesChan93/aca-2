"""Same financial ceiling/resource limits; no replay or 56-day capacity claim."""

import asyncio
import importlib.metadata
import json
import resource
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, process_time
from uuid import UUID, uuid4

from capacity_fixture import seed_events
from process_worker import AT
from restore_fixture import NoCommands, create_database, settings_for
from sqlalchemy import func, select, text

import aca
from aca.execution.paper import PaperExecution
from aca.reporting.historical import MAX_REPORT_EVENTS
from aca.storage.audit_prefix import FinancialAuditPrefix, package_fingerprint
from aca.storage.database import create_engine
from aca.storage.migrate import migrate
from aca.storage.paper import PaperStore, PaperWriter, events


async def run() -> None:
    settings = settings_for(sys.argv[1])
    create_database(settings)
    await migrate(settings)
    engine = create_engine(settings)
    store = PaperStore(engine, uuid4())
    result: dict[str, object] = {
        "application_version": aca.__version__,
        "trial_mode": "financial_prefix",
        "operational_budgets_exercised": False,
        "operations": {},
        "package_source_sha256": package_fingerprint(),
        "certifi_version": importlib.metadata.version("certifi"),
        "record_limits": {"events": MAX_REPORT_EVENTS},
        "56_day_headroom_demonstrated": False,
        "live_execution_available": False,
        "promotion_evidence": False,
    }
    began, cpu = monotonic(), process_time()
    try:
        async with PaperWriter(store) as owner:
            service = PaperExecution(owner, NoCommands(store))
            await service.initialise(AT)
            state = await service.cashflow("prefix-trial-opening", Decimal(1000), AT)
            state = await seed_events(owner, state, MAX_REPORT_EVENTS - 16)
        full_start = monotonic()
        async with PaperWriter(store):
            pass
        full_seconds = monotonic() - full_start
        # Exercise the installed CLI in a separate process. Only this disposable
        # worker has the migration secret; ordinary app processes do not need it.
        with TemporaryDirectory(prefix="aca-financial-prefix-") as directory:
            config = Path(directory) / "settings.toml"
            db = settings.database
            config.write_text(
                'mode = "paper"\nenvironment = "development"\n[database]\n'
                + "\n".join(
                    f"{name} = {json.dumps(value)}"
                    for name, value in {
                        "host": db.host,
                        "port": db.port,
                        "name": db.name,
                        "runtime_password_file": db.runtime_password_file,
                        "migration_password_file": db.migration_password_file,
                    }.items()
                )
                + "\n"
            )
            config.chmod(0o600)
            accepting = monotonic()
            process = await asyncio.create_subprocess_exec(
                "aca",
                "--config",
                str(config),
                "paper",
                "audit-prefix-accept",
                "--account",
                str(store.account_id),
                "--actor",
                str(uuid4()),
                "--reason",
                "Isolated financial capacity full-prefix verification",
                "--expected-version",
                str(state.version),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                async with asyncio.timeout(120):
                    stdout, _ = await process.communicate()
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            if process.returncode != 0:
                raise ValueError("packaged financial audit acceptance failed")
            record = json.loads(stdout)
            FinancialAuditPrefix.restore({k: v for k, v in record.items() if k != "fingerprint"})
            acceptance_seconds = monotonic() - accepting
        protected = PaperStore(engine, store.account_id, audit_prefix=UUID(record["id"]))
        prefix_start = monotonic()
        async with PaperWriter(protected) as owner:
            service = PaperExecution(owner, NoCommands(protected))
            assert await service._state() == state
        prefix_seconds = monotonic() - prefix_start
        # Sixteen real, audited suffix writes, including cancelling investor
        # flows. Final record count stays at the original financial ceiling.
        async with PaperWriter(protected) as owner:
            service = PaperExecution(owner, NoCommands(protected))
            state = await service.cashflow("prefix-tail-deposit", Decimal(10), AT)
            state = await service.cashflow("prefix-tail-withdrawal", Decimal(-10), AT)
            for i in range(14):
                state = await service._save(state, state, at=AT, kind=f"prefix-tail-{i}")
            if state.quantity("GBP") != Decimal(1000) or state.units != Decimal(1000):
                raise ValueError("cancelling investment flows changed cash or flow-neutral units")
        suffix_start = monotonic()
        async with PaperWriter(protected):
            pass
        suffix_seconds = monotonic() - suffix_start
        async with engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            full = await store.audit(c, full=True)
            bounded = await protected.audit(c)
            if full != state or bounded != full:
                raise ValueError("protected financial audit differs from full reconstruction")
            count = await c.scalar(
                select(func.count())
                .select_from(events)
                .where(events.c.account_id == store.account_id)
            )
            assert count == MAX_REPORT_EVENTS
            result["database_bytes"] = await c.scalar(
                text("SELECT pg_database_size(current_database())")
            )
        deadline = 30
        measurements = {
            name: {
                "seconds": seconds,
                "deadline_seconds": deadline,
                "operation_finished": True,
                "outcome": "completed" if seconds <= deadline else "deadline_exceeded",
            }
            for name, seconds in {
                "full_financial_writer_startup": full_seconds,
                "protected_financial_writer_startup": prefix_seconds,
                "protected_writer_with_sixteen_suffix_events": suffix_seconds,
            }.items()
        }
        result["financial_prefix"] = {
            "measurements": measurements,
            "operator_full_audit_acceptance_seconds": acceptance_seconds,
            "accepted_event_version": record["version"],
            "retained_financial_events": count,
            "full_financial_economics_equal": True,
            "flow_neutral_units_and_cash_verified": True,
            "packaged_acceptance_cli_verified": True,
            "pruned_records": 0,
            "venue_commands": 0,
            "replay_and_report_capacity_exercised": False,
        }
        result["measured_boundary_failures"] = [
            key for key, value in measurements.items() if value["outcome"] != "completed"
        ]
        result["cpu_seconds"] = process_time() - cpu
        result["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for filename in ("memory.peak", "memory.max", "cpu.max"):
            path = Path("/sys/fs/cgroup") / filename
            result[filename] = path.read_text().strip() if path.exists() else None
        result["elapsed_seconds"] = monotonic() - began
        print(json.dumps(result, sort_keys=True))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
