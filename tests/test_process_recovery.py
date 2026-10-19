"""Kill real interpreter processes against disposable PostgreSQL, then recover separately."""

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import select, text

from aca.domain.trading import Fill, Journal, Order, PortfolioSnapshot, VenueSnapshot
from aca.operations.config import Settings
from aca.operations.secrets import read_secret
from aca.portfolio.state import reserved_cash
from aca.simulation.paper import PaperExchange
from aca.storage.database import create_engine
from aca.storage.paper import (
    PaperStore,
    PaperWriter,
    WriterUnavailable,
    events,
    fills,
    lock_key,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix", reason="POSIX SIGKILL"),
]
WORKER = Path(__file__).with_name("process_worker.py")
AT = datetime(2026, 10, 19, tzinfo=UTC)
D = Decimal


@dataclass
class Child:
    process: asyncio.subprocess.Process
    trace: Path
    messages: list[dict[str, object]]

    def record(self, message: dict[str, object]) -> None:
        self.messages.append(message)
        # Pytest retains this bounded virtual-account transcript in failed-test
        # output even when CI removes its disposable container and /tmp files.
        print("process_recovery_trace " + json.dumps(message, sort_keys=True))
        with self.trace.open("a") as out:
            out.write(json.dumps(message, sort_keys=True) + "\n")

    async def until(self, event: str) -> dict[str, object]:
        assert self.process.stdout is not None
        async with asyncio.timeout(15):
            while True:
                line = await self.process.stdout.readline()
                assert line, self.messages
                message = json.loads(line)
                assert isinstance(message, dict), message
                self.record(message)
                assert message["event"] != "failed", self.messages
                if message["event"] == event:
                    return message

    async def kill(self) -> None:
        self.process.kill()
        async with asyncio.timeout(5):
            assert await self.process.wait() == -signal.SIGKILL
        self.record({"event": "sigkill_confirmed", "returncode": -signal.SIGKILL})

    async def finish(self) -> None:
        async with asyncio.timeout(5):
            assert await self.process.wait() == 0, self.messages
        assert self.process.stdout is not None and self.process.stderr is not None
        assert await self.process.stdout.read(4096) == b"", self.messages
        assert await self.process.stderr.read(4096) == b"", "unexpected child diagnostics"


@asynccontextmanager
async def child(
    settings: Settings, account: UUID, scenario: str, directory: Path
) -> AsyncIterator[Child]:
    # exec starts a fresh interpreter with its own engine; no forked pools or
    # inherited admin connection environment. Only a runtime secret *path* is sent.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        str(WORKER),
        scenario,
        str(account),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={"PATH": os.defpath},
        limit=65536,
    )
    trace = directory / f"{scenario}-{uuid4().hex}.jsonl"
    trace.touch(mode=0o600)
    worker = Child(process, trace, [])
    try:
        assert process.stdin is not None
        process.stdin.write((settings.database.model_dump_json() + "\n").encode())
        await process.stdin.drain()
        yield worker
    finally:
        if process.returncode is None:
            process.kill()
        async with asyncio.timeout(5):
            await process.wait()
        if process.stdin is not None:
            process.stdin.close()


@dataclass(frozen=True)
class Evidence:
    state: PortfolioSnapshot
    journals: tuple[Journal, ...]
    fills: tuple[Fill, ...]
    venue: VenueSnapshot
    prefix: tuple[tuple[int, str], ...]

    @property
    def entry(self) -> Order:
        return next(o for o in self.state.orders if o.intent.purpose == "entry")


async def inspect(settings: Settings, account: UUID) -> Evidence:
    engine = create_engine(settings)
    store = PaperStore(engine, account)
    try:
        async with engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ")
            await c.execute(text("SET TRANSACTION READ ONLY"))
            state = await store.audit(c)
            assert state is not None
            history = await store.history(c)
            posted = tuple(
                Fill.restore(document)
                for document in await c.scalars(
                    select(fills.c.document).where(fills.c.account_id == account)
                )
            )
            prefix = tuple(
                (int(row[0]), str(row[1]))
                for row in await c.execute(
                    select(events.c.version, events.c.fingerprint)
                    .where(events.c.account_id == account)
                    .order_by(events.c.version)
                )
            )
        venue = await PaperExchange(store).snapshot(AT + timedelta(seconds=1))
        return Evidence(state, history, posted, venue, prefix)
    finally:
        await engine.dispose()


async def wait_for_released_writer(settings: Settings, account: UUID) -> None:
    engine = create_engine(settings)
    try:
        async with asyncio.timeout(5), engine.connect() as c:
            while not await c.scalar(  # noqa: ASYNC110 - wait for a killed backend's socket cleanup
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key(account)}
            ):
                await asyncio.sleep(0.01)
            assert await c.scalar(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key(account)}
            )
    finally:
        await engine.dispose()


async def recovery(settings: Settings, account: UUID, directory: Path) -> dict[str, object]:
    async with child(settings, account, "recover", directory) as worker:
        report = await worker.until("recovered")
        await worker.finish()
        assert not any(m["event"] in {"submit_attempt", "cancel_attempt"} for m in worker.messages)
        return report


def check_financial_identity(evidence: Evidence, expected_fills: int) -> None:
    assert len(evidence.fills) == expected_fills
    assert len(evidence.journals) == 1 + expected_fills
    assert len({f.id for f in evidence.fills}) == expected_fills
    fees = sum(
        (
            line.gbp
            for journal in evidence.journals
            for line in journal.lines
            if line.account == "expense:fees"
        ),
        D(0),
    )
    assert fees == sum((fee.quantity for fill in evidence.fills for fee in fill.fees), D(0))
    assert evidence.state.quantity("BTC") == sum((f.quantity for f in evidence.fills), D(0))
    assert evidence.state.quantity("GBP") == D(1000) - sum(
        (
            f.quantity * f.price + sum((fee.quantity for fee in f.fees), D(0))
            for f in evidence.fills
        ),
        D(0),
    )
    assert sum(
        (o.remaining for o in evidence.state.orders if o.intent.purpose == "protection"), D(0)
    ) == evidence.state.quantity("BTC")


@pytest.mark.parametrize(
    "scenario,paused_state,remote_orders,recovered_state,complete",
    [
        ("uncommitted_intent", None, 0, None, True),
        ("before_submitting", "VALIDATED", 0, "EXPIRED", True),
        ("after_submitting_before_send", "SUBMITTING", 0, "UNKNOWN", False),
        ("after_send_before_ack", "SUBMITTING", 1, "OPEN", True),
    ],
)
async def test_sigkill_submission_boundaries_preserve_uncertainty_and_never_replay(
    database_settings: Settings,
    tmp_path: Path,
    scenario: str,
    paused_state: str | None,
    remote_orders: int,
    recovered_state: str | None,
    complete: bool,
) -> None:
    account = uuid4()
    async with child(database_settings, account, scenario, tmp_path) as worker:
        await worker.until("paused")
        before = await inspect(database_settings, account)
        assert (before.state.orders[0].state if before.state.orders else None) == paused_state
        assert len(before.venue.orders) == remote_orders
        assert reserved_cash(before.state) > 0 if paused_state else reserved_cash(before.state) == 0
        # The parent sees only committed state while the child owns the session.
        engine = create_engine(database_settings)
        try:
            with pytest.raises(WriterUnavailable):
                async with PaperWriter(PaperStore(engine, account)):
                    pass
        finally:
            await engine.dispose()
        await worker.kill()
        assert sum(m["event"] == "submit_attempt" for m in worker.messages) == remote_orders
    await wait_for_released_writer(database_settings, account)
    report = await recovery(database_settings, account, tmp_path)
    assert isinstance(report["result"], dict) and report["result"]["complete"] is complete
    after = await inspect(database_settings, account)
    assert (after.state.orders[0].state if after.state.orders else None) == recovered_state
    assert after.prefix[: len(before.prefix)] == before.prefix
    assert after.state.balances == before.state.balances
    assert after.journals == before.journals
    assert after.fills == before.fills == ()
    assert after.venue == before.venue
    assert (reserved_cash(after.state) > 0) is (recovered_state in {"UNKNOWN", "OPEN"})
    await recovery(database_settings, account, tmp_path)
    repeated = await inspect(database_settings, account)
    assert repeated.state.orders == after.state.orders
    assert repeated.journals == after.journals
    assert repeated.venue == after.venue


@pytest.mark.parametrize(
    "scenario,posted_before,partial,cancelled,halted,complete",
    [
        ("filled_before_ack", False, False, False, False, True),
        ("partial_before_ack", False, True, False, False, True),
        ("uncommitted_fill", False, False, False, False, True),
        ("committed_fill", True, False, False, False, True),
        ("cancel_partial", True, True, True, False, True),
        ("cancel_before_send", True, True, False, False, False),
        ("cancel_racing_fill", False, False, False, False, True),
        ("committed_halt", True, False, False, True, True),
    ],
)
async def test_sigkill_fills_cancellations_and_halt_recover_exact_economics(
    database_settings: Settings,
    tmp_path: Path,
    scenario: str,
    posted_before: bool,
    partial: bool,
    cancelled: bool,
    halted: bool,
    complete: bool,
) -> None:
    account = uuid4()
    async with child(database_settings, account, scenario, tmp_path) as worker:
        await worker.until("paused")
        before = await inspect(database_settings, account)
        assert len(before.venue.fills) == 1
        assert len(before.fills) == int(posted_before)
        assert len(before.journals) == 1 + int(posted_before)
        if scenario in {"cancel_partial", "cancel_racing_fill", "cancel_before_send"}:
            assert before.entry.state == "PENDING_CANCEL"
            assert reserved_cash(before.state) > 0
        if scenario == "uncommitted_fill":
            assert before.state.quantity("BTC") == 0
            assert len(before.state.orders) == 2  # protection committed before posting transaction
        assert before.state.kill.hard is halted
        await worker.kill()
        assert sum(m["event"] == "submit_attempt" for m in worker.messages) == 1
        assert sum(m["event"] == "cancel_attempt" for m in worker.messages) == int(
            scenario in {"cancel_partial", "cancel_racing_fill", "cancel_before_send"}
        )
    await wait_for_released_writer(database_settings, account)
    report = await recovery(database_settings, account, tmp_path)
    assert isinstance(report["result"], dict) and report["result"]["complete"] is complete
    after = await inspect(database_settings, account)
    assert after.prefix[: len(before.prefix)] == before.prefix
    assert after.venue == before.venue  # recovery never mutates the authoritative local venue
    assert after.entry.state == (
        "UNKNOWN"
        if not complete
        else "CANCELLED"
        if cancelled
        else "PARTIALLY_FILLED"
        if partial
        else "FILLED"
    )
    assert after.entry.pending_operation == ("cancel" if not complete else None)
    assert (reserved_cash(after.state) > 0) is (partial and not cancelled)
    assert after.state.kill == before.state.kill
    assert len(after.state.orders) == 2
    protective = next(o for o in after.state.orders if o.intent.purpose == "protection")
    assert protective.state == "OPEN"
    check_financial_identity(after, 1)
    await recovery(database_settings, account, tmp_path)
    repeated = await inspect(database_settings, account)
    check_financial_identity(repeated, 1)
    assert repeated.journals == after.journals
    assert repeated.fills == after.fills
    assert repeated.state.balances == after.state.balances
    assert repeated.state.kill == after.state.kill
    assert repeated.state.orders == after.state.orders
    assert repeated.venue == after.venue


async def test_dead_database_session_cannot_rearm_old_process_before_consumption(
    database_settings: Settings, tmp_path: Path
) -> None:
    account = uuid4()
    async with child(database_settings, account, "before_submitting", tmp_path) as worker:
        await worker.until("paused")
        backend = next(m["backend_pid"] for m in worker.messages if m["event"] == "writer")
        db = database_settings.database
        admin_file = Path(db.runtime_password_file).with_name("admin")
        connection = await psycopg.AsyncConnection.connect(
            host=db.host,
            port=db.port,
            dbname=db.name,
            user=db.admin_user,
            password=read_secret(admin_file).get_secret_value(),
            connect_timeout=3,
        )
        async with connection:
            cursor = await connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE pid = %s AND datname = %s AND usename = 'aca_runtime' "
                "AND application_name = 'aca-foundation'",
                (backend, db.name),
            )
            assert await cursor.fetchone() == (True,)
        assert worker.process.stdin is not None
        worker.process.stdin.write(b"resume\n")
        await worker.process.stdin.drain()
        result = await worker.until("dispatch_failed")
        assert result["error_type"] == "OperationalError"
        await worker.finish()
        assert not any(m["event"] == "submit_attempt" for m in worker.messages)
    # Fence the old process by observing its exit before acquiring a replacement.
    await wait_for_released_writer(database_settings, account)
    await recovery(database_settings, account, tmp_path)
    after = await inspect(database_settings, account)
    assert after.state.orders[0].state == "EXPIRED"
    assert reserved_cash(after.state) == 0
    assert not after.venue.orders and not after.fills


@pytest.mark.parametrize("host,name", [("db", "aca_test_invalid"), ("127.0.0.1", "aca")])
async def test_worker_rejects_non_disposable_targets_before_connecting(
    tmp_path: Path, host: str, name: str
) -> None:
    from aca.operations.config import DatabaseSettings

    settings = Settings(database=DatabaseSettings(host=host, name=name))
    async with child(settings, uuid4(), "recover", tmp_path) as worker:
        assert worker.process.stdout is not None
        message = json.loads(await asyncio.wait_for(worker.process.stdout.readline(), 5))
        assert message == {"event": "failed", "error_type": "ValueError"}
        assert await asyncio.wait_for(worker.process.wait(), 5) == 1
