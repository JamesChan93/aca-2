"""C3: measured critical fault -> durable incident -> controlled HTTP attempt.

Elapsed times use real monotonic time. HTTPX transports are controlled; there is
no Discord request, live credential or qualifying observation in these fixtures.
"""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from test_notifications import response, webhook
from test_paper_execution import AT, LostResponse, market, signal

from aca.app.paper_supervisor import PaperSupervisor, SupervisorStatus
from aca.domain.trading import VenueSnapshot
from aca.execution.paper import PaperExecution
from aca.observability.delivery import NotificationOwner
from aca.observability.delivery import status as delivery_status
from aca.observability.discord import DiscordSender
from aca.observability.notices import critical
from aca.operations.config import Settings
from aca.portfolio.state import entries_paused, reserved_cash
from aca.risk.execution import DispatchDenied
from aca.simulation.paper import PaperExchange
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter, fills, journals


class IncompleteResponse(LostResponse):
    async def snapshot(self, at: datetime) -> VenueSnapshot:
        snapshot = await self.venue.snapshot(AT)
        return snapshot.model_copy(update={"orders": (), "fills": (), "complete": False})


@pytest.mark.integration
@pytest.mark.parametrize("delivery", ["delivered", "unreachable", "throttled"])
async def test_critical_detection_and_attempt_timing_are_independent_of_finance(
    database_settings: Settings,
    tmp_path: Path,
    delivery: str,
) -> None:
    engine, notifier_engine = create_engine(database_settings), create_engine(database_settings)
    store = PaperStore(engine, uuid4())
    attempted, release = asyncio.Event(), asyncio.Event()
    attempts: list[float] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        attempts.append(monotonic())
        assert "CRITICAL REMINDER" in json.loads(request.content)["content"]
        attempted.set()
        await release.wait()
        if delivery == "unreachable":
            raise httpx.ConnectError("controlled unreachable destination")
        return (
            response(429, headers={"Retry-After": "65"}) if delivery == "throttled" else response()
        )

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(transport))
    try:
        async with PaperWriter(store) as writer:
            venue = IncompleteResponse(PaperExchange(store))
            service = PaperExecution(writer, venue)
            await service.initialise(AT)
            await service.cashflow("opening", Decimal(1000), AT)
            # Configure healthy evidence before introducing the lost acknowledgement.
            service.venue = venue.venue
            await service.recover(AT)
            service.venue = venue
            order = await service.queue(signal(), market(), AT)
            began = monotonic()
            assert (await service.dispatch(order.intent.client_id, market(), AT)).state == "UNKNOWN"
            state = await service._state()
            assert critical(state) is not None and entries_paused(state, AT)
            detected = monotonic() - began
            assert detected < 10 and reserved_cash(state) > 0

            async def marks(at: datetime) -> tuple[()]:
                return ()

            async def capacity() -> None:
                pass

            health = SupervisorStatus()
            supervisor = PaperSupervisor(
                service, health, marks=marks, capacity=capacity, clock=lambda: AT
            )
            await supervisor.tick()
            assert not health.supervision_ready
            async with NotificationOwner(notifier_engine, store.account_id) as owner:
                await owner.configure(webhook(), AT)
                await owner.prepare(AT, include_daily=False)
                sending = asyncio.create_task(owner.tick(sender, AT))
                try:
                    async with asyncio.timeout(60):
                        await attempted.wait()
                    attempt_seconds = attempts[0] - began
                    assert attempt_seconds < 60
                    # While HTTP is blocked, the independent writer still runs a
                    # risk observation, preserves uncertainty and refuses entries.
                    before = await service._state()
                    await service.mark((), AT + timedelta(milliseconds=1))
                    after = await service._state()
                    assert after.balances == before.balances and after.units == before.units
                    assert reserved_cash(after) == reserved_cash(before)
                    with pytest.raises(DispatchDenied):
                        when = AT + timedelta(milliseconds=1)
                        await service.queue(signal(when), market(when), when)
                    assert venue.calls == 1
                finally:
                    release.set()
                    outcome = await sending
                assert outcome == ("delivered" if delivery == "delivered" else "retry")
                assert await owner.tick(sender, AT) == "waiting"
            report = await delivery_status(notifier_engine, store.account_id)
            current = report["state"]
            assert isinstance(current, dict)
            if delivery == "throttled":
                assert (datetime.fromisoformat(current["not_before"]) - AT).total_seconds() >= 65
            async with writer.transaction() as c:
                assert not (
                    await c.scalars(
                        select(fills.c.id).where(fills.c.account_id == store.account_id)
                    )
                ).all()
                assert (
                    len(
                        (
                            await c.scalars(
                                select(journals.c.id).where(
                                    journals.c.account_id == store.account_id
                                )
                            )
                        ).all()
                    )
                    == 1
                )
            assert len(attempts) == 1 and venue.calls == 1
            (tmp_path / f"critical-timing-{delivery}.json").write_text(
                json.dumps(
                    {
                        "fault": "accepted_submission_lost_ack_incomplete_reconciliation",
                        "transport": delivery,
                        "detected_seconds": detected,
                        "attempt_seconds": attempt_seconds,
                        "http_attempts": 1,
                        "exchange_submissions": 1,
                        "automatic_retransmissions": 0,
                        "fills": 0,
                        "journals": 1,
                        "financial_writer_progressed_during_http": True,
                        "promotion_evidence": False,
                        "live_execution_available": False,
                    },
                    indent=2,
                )
            )
    finally:
        release.set()
        await sender.close()
        await engine.dispose()
        await notifier_engine.dispose()
