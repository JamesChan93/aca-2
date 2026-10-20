"""Real ENOSPC drill worker, restricted to the generated restore namespace."""

import asyncio
import json
import sys
from time import monotonic
from typing import Any
from uuid import UUID

import httpx
import structlog
from process_worker import AT, market, proposal
from restore_fixture import NoCommands, create_database, settings_for
from sqlalchemy.exc import DBAPIError
from test_notifications import response, webhook

from aca.app.paper_supervisor import PaperSupervisor, SupervisorMetrics, SupervisorStatus
from aca.domain.trading import fingerprint
from aca.execution.paper import PaperExecution
from aca.observability.delivery import NotificationOwner
from aca.observability.discord import DiscordSender
from aca.portfolio.state import entries_paused, reserved_cash
from aca.storage.database import create_engine
from aca.storage.migrate import migrate
from aca.storage.paper import PaperStore, PaperWriter, WriterUnavailable

ACCOUNT = UUID("85749d3e-d0bb-42f8-bbe7-c2bb09af73c5")


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


async def run() -> None:
    action, name = sys.argv[1:]
    settings = settings_for(name)
    if action == "watch":
        create_database(settings)
        await migrate(settings)
    engine = create_engine(settings)
    store = PaperStore(engine, ACCOUNT)
    try:
        if action == "notify":
            attempts = 0

            def transport(_: httpx.Request) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return response()

            sender = DiscordSender(webhook(), transport=httpx.MockTransport(transport))
            try:
                async with NotificationOwner(engine, ACCOUNT) as owner:
                    await owner.configure(webhook(), AT)
                    prepared = await owner.prepare(AT, include_daily=False)
                    assert prepared["critical_active"]
                    assert await owner.tick(sender, AT) == "delivered"
                assert attempts == 1
                emit(
                    {
                        "phase": "notified",
                        "controlled_http_attempts": 1,
                        "live_execution_available": False,
                    }
                )
                return
            finally:
                await sender.close()
        async with PaperWriter(store) as writer:
            venue = NoCommands(store)
            service = PaperExecution(writer, venue)
            if action == "watch":
                from decimal import Decimal

                await service.initialise(AT)
                await service.cashflow("storage-opening", Decimal(1000), AT)
                await service.recover(AT)
                order = await service.queue(proposal(), market(), AT, time_in_force="FOK")
                state = await service._state()
                assert reserved_cash(state) > 0
                emit(
                    {
                        "phase": "ready",
                        "state_hash": fingerprint(state.document()),
                        "reserved_gbp": str(reserved_cash(state)),
                        "balances": [b.document() for b in state.balances],
                    }
                )
                # Only the local orchestrator writes this pipe. No sockets/credentials.
                command = await asyncio.to_thread(sys.stdin.readline)
                if command != "faulted\n":
                    raise ValueError("storage drill requires its explicit fault gate")
                began = monotonic()
                try:
                    await service.dispatch(order.intent.client_id, market(), AT)
                except Exception as error:
                    assert isinstance(error, (DBAPIError, WriterUnavailable, TimeoutError))
                else:
                    raise AssertionError("storage fault did not prevent dispatch")

                async def marks(at: Any) -> tuple[()]:
                    return ()

                async def capacity() -> None:
                    pass

                status = SupervisorStatus()
                supervisor = PaperSupervisor(
                    service, status, marks=marks, capacity=capacity, clock=lambda: AT
                )
                try:
                    await supervisor.run(asyncio.Event(), SupervisorMetrics())
                except Exception as error:
                    assert isinstance(error, (DBAPIError, WriterUnavailable, TimeoutError))
                else:
                    raise AssertionError("unavailable storage did not stop supervisor")
                assert not status.supervision_ready and status.reason == "paper_supervisor_fault"
                detected = monotonic() - began
                assert detected < 10 and venue.attempts == 0
                emit(
                    {
                        "phase": "blocked",
                        "detected_seconds": detected,
                        "venue_commands": venue.attempts,
                        "unhealthy": True,
                    }
                )
            elif action == "recover":
                state = await service._state()
                before = fingerprint(state.document())
                reserved = reserved_cash(state)
                # No durable SUBMITTING survived: no request may have been sent.
                assert state.orders[0].state == "VALIDATED" and reserved > 0
                async with writer.transaction() as c:
                    assert state.balances == await store.ledger_balances(c)
                    assert len(await store.history(c)) == 1
                await service.recover(AT)
                await service.halt("Storage recovery requires explicit reconciliation review", AT)
                state = await service._state()
                assert state.kill.hard and entries_paused(state, AT)
                assert state.orders[0].state == "EXPIRED"
                assert reserved_cash(state) == 0 and venue.attempts == 0
                emit(
                    {
                        "phase": "recovered",
                        "before_state_hash": before,
                        "before_reserved_gbp": str(reserved),
                        "venue_commands": 0,
                        "disarmed": True,
                        "journals": 1,
                        "fills": 0,
                    }
                )
            else:
                raise ValueError("unknown storage drill action")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    asyncio.run(run())
