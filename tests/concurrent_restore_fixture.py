"""Controlled writes during an exported-snapshot dump, on disposable clusters only."""

import asyncio
import json
import select
import sys
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from process_worker import AT
from psycopg import Connection
from restore_fixture import (
    CASES,
    NoCommands,
    admin,
    database_snapshot,
    economics,
    settings_for,
)

from aca.domain.market import MarketTrade
from aca.execution.paper import PaperExecution
from aca.market_data.store import MarketStore
from aca.operations.config import Settings
from aca.portfolio.reserves import TaxReserveRequest
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter

PREFIX_CYCLES = 64
OVERLAP_CYCLES = 32


def command(expected: str) -> None:
    if not select.select([sys.stdin], [], [], 45)[0]:
        raise TimeoutError("snapshot controller stopped responding")
    if sys.stdin.buffer.readline(128) != (expected + "\n").encode():
        raise ValueError("unexpected snapshot controller command")


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, default=str), flush=True)


async def view(settings: Settings, snapshot: str | None = None) -> dict[str, Any]:
    return {
        "live_execution_available": False,
        **database_snapshot(settings, snapshot),
        "economics": await economics(settings, snapshot),
    }


async def writes(settings: Settings, start: int, count: int) -> int:
    """Three real commits per cycle, with no network or exchange commands."""
    engine = create_engine(settings)
    store = PaperStore(engine, CASES["validated"])
    try:
        async with PaperWriter(store) as owner:
            venue = NoCommands(store)
            service, market = PaperExecution(owner, venue), MarketStore(engine)
            epoch = uuid4()
            for index in range(start, start + count):
                at = AT + timedelta(milliseconds=1100 + index)
                await market.append(
                    epoch=epoch,
                    channel="trade",
                    payload="{}",
                    received_at=at,
                    normalized={"snapshot": False},
                    trades=(
                        MarketTrade(
                            venue="kraken",
                            symbol="BTC/GBP",
                            trade_id=f"restore-load-{index}",
                            side="sell",
                            price=Decimal(999),
                            quantity=Decimal("0.01"),
                            received_at=at,
                            event_at=at,
                        ),
                    ),
                )
                state = await service.cashflow(f"restore-load-{index}", Decimal(1), at)
                await service.reserve_tax_cash(
                    TaxReserveRequest(
                        request_id=uuid4(),
                        actor=CASES["validated"],
                        expected_version=state.version,
                        amount_gbp=Decimal(101 if index % 2 == 0 else 100),
                        evidence_sha256="2" * 64,
                        reason="Synthetic concurrent restore fixture reserve review",
                    ),
                    at=at,
                )
            assert venue.attempts == 0
    finally:
        await engine.dispose()
    return count * 3


def blocked_dump(c: Connection[Any], barrier_pid: int) -> int | None:
    row = c.execute(
        "SELECT pid FROM pg_stat_activity WHERE datname=current_database() "
        "AND application_name='pg_dump' AND wait_event_type='Lock' "
        "AND %s = ANY(pg_blocking_pids(pid))",
        (barrier_pid,),
    ).fetchone()
    return row[0] if row else None


async def run(settings: Settings) -> None:
    await writes(settings, 0, PREFIX_CYCLES)
    with admin(settings) as exporter, exporter.transaction():
        exporter.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        exporter.execute("SET LOCAL idle_in_transaction_session_timeout = '120s'")
        row = exporter.execute("SELECT pg_export_snapshot()").fetchone()
        assert row is not None
        snapshot = row[0]
        before = await view(settings, snapshot)
        with admin(settings) as barrier, barrier.transaction(), admin(settings) as observer:
            barrier.execute("SET LOCAL idle_in_transaction_session_timeout = '120s'")
            barrier.execute("LOCK TABLE aca.configuration_versions IN ACCESS EXCLUSIVE MODE")
            pid_row = barrier.execute("SELECT pg_backend_pid()").fetchone()
            assert pid_row is not None
            barrier_pid = pid_row[0]
            emit({"phase": "ready", "snapshot": snapshot, "before": before})
            command("exercise")
            deadline, dump_pid = time.monotonic() + 30, None
            while time.monotonic() < deadline:
                dump_pid = blocked_dump(observer, barrier_pid)
                if dump_pid is not None:
                    break
                await asyncio.sleep(0.02)
            if dump_pid is None:
                raise TimeoutError("pg_dump never reached the controlled overlap barrier")
            commits = await writes(settings, PREFIX_CYCLES, OVERLAP_CYCLES)
            if blocked_dump(observer, barrier_pid) != dump_pid:
                raise AssertionError("dump left the overlap barrier before writes committed")
        # All writers have closed before measuring the upper sequence bound.
        # Exporter stays alive until pg_dump has finished importing/using its view.
        emit(
            {
                "phase": "exercised",
                "commits_while_dump_waited": commits,
                "dump_wait_observed": True,
                "same_snapshot_after_writes": await view(settings, snapshot),
                "source_after": await view(settings),
                "venue_commands": 0,
            }
        )
        command("finish")
    emit({"phase": "closed", "live_execution_available": False})


if __name__ == "__main__":
    try:
        asyncio.run(run(settings_for(sys.argv[1])))
    except Exception as exc:
        print("concurrent restore fixture failed: " + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1) from None
