"""Durable UTC scheduling, current-state repeats and financial isolation on PostgreSQL."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from test_notifications import AT, response, webhook
from test_stored_replay import Feed

from aca.domain.trading import Balance
from aca.execution.paper import PaperExecution
from aca.observability.delivery import NotificationOwner, history, read_notices, status
from aca.observability.discord import DiscordSender
from aca.observability.notices import (
    critical,
    daily_summary,
    notice_payload,
    notices,
    receipts,
    summary_day,
)
from aca.operations.config import Settings
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import StoredReplay
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter, events, fills, journals, outbox, venue_events

pytest_plugins = ("test_notifications", "test_stored_replay")


@pytest.mark.parametrize(
    "offset,expected",
    [
        (timedelta(days=1, minutes=9, seconds=59), "2026-10-17"),
        (timedelta(days=1, minutes=10), "2026-10-18"),
        (timedelta(days=1, hours=12), "2026-10-18"),
        (timedelta(days=2, minutes=10), "2026-10-19"),
    ],
)
def test_daily_eligibility_uses_utc_not_local_midnight(offset: timedelta, expected: str) -> None:
    from zoneinfo import ZoneInfo

    assert summary_day((AT + offset).astimezone(ZoneInfo("Europe/London"))).isoformat() == expected


async def count_finance(engine: AsyncEngine, account: UUID) -> list[int]:
    async with engine.connect() as c:
        return [
            int(
                await c.scalar(
                    select(func.count()).select_from(table).where(table.c.account_id == account)
                )
                or 0
            )
            for table in (events, fills, journals, outbox, venue_events)
        ]


@pytest.mark.integration
async def test_daily_prepare_is_unique_after_restart_no_secret_or_financial_mutation(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    before = await count_finance(engine, account)
    at = AT + timedelta(days=1, minutes=10)
    async with NotificationOwner(engine, account) as owner:
        early = await owner.prepare(at - timedelta(microseconds=1))
        assert not early["daily_scheduled"]
        first = await owner.prepare(at)
        assert first["daily_scheduled"] and first["critical_active"]
        assert not (await owner.prepare(at))["daily_scheduled"]
    async with NotificationOwner(engine, account) as owner:
        assert not (await owner.prepare(at + timedelta(minutes=1)))["daily_scheduled"]
    async with engine.connect() as c:
        rows = (
            (await c.execute(select(notices).where(notices.c.account_id == account)))
            .mappings()
            .all()
        )
        daily = next(row for row in rows if row["kind"] == "daily")
        summary = daily["document"]["summary"]
        assert Decimal(summary["contributions_gbp"]) == 250
        assert Decimal(summary["fees_gbp"]) == 0
        assert summary["daily_pnl_gbp"] is None and not summary["daily_return_complete"]
        assert summary["observed"] is None and summary["backup_status"] == "unverified"
        assert summary["tax_reserve_estimate_gbp"] is None
    assert await count_finance(engine, account) == before
    report = await status(engine, account)
    assert not report["configured"] and report["scheduled_backlog"] == 2


@pytest.mark.integration
async def test_critical_precedes_fifo_and_repeats_only_after_five_minutes_across_restart(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return response()

    sender = DiscordSender(
        webhook(),
        transport=httpx.MockTransport(handler),
    )
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            await owner.prepare(AT)
            assert await owner.tick(sender, AT) == "delivered"
            assert "CRITICAL REMINDER" in str(sent[-1]["content"])
            current = (await status(engine, account))["state"]
            assert isinstance(current, dict) and current["cursor"] == 0
        async with NotificationOwner(engine, account) as owner:
            await owner.prepare(AT + timedelta(seconds=299))
            assert (await status(engine, account))["scheduled_backlog"] == 0
            await owner.prepare(AT + timedelta(seconds=301))
            assert await owner.tick(sender, AT + timedelta(seconds=301)) == "delivered"
            assert "CRITICAL REMINDER" in str(sent[-1]["content"])
            await owner.prepare(AT + timedelta(seconds=302))
            assert (await status(engine, account))["scheduled_backlog"] == 0
        assert len(sent) == 2 and sent[0]["allowed_mentions"] == {"parse": []}
    finally:
        await sender.close()


@pytest.mark.integration
async def test_recovery_supersedes_queued_reminder_without_http(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return response()

    sender = DiscordSender(
        webhook(),
        transport=httpx.MockTransport(handler),
    )
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            await owner.prepare(AT)
        store = PaperStore(engine, account)
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            assert (await service.recover(AT)).complete
            await service.review("operator reviewed hard halt fixture", AT)
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, AT) == "superseded"
            assert not sent
            assert not (await owner.prepare(AT + timedelta(minutes=5)))["critical_active"]
    finally:
        await sender.close()


@pytest.mark.integration
async def test_retry_retains_notice_identity_and_rate_deadline_without_advancing_alert_cursor(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return (
            httpx.Response(429, headers={"Retry-After": "7200"}, stream=httpx.ByteStream(b"{}"))
            if len(requests) == 1
            else response()
        )

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(handler))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            await owner.prepare(AT)
            assert await owner.tick(sender, AT) == "retry"
        async with NotificationOwner(engine, account) as owner:
            await owner.prepare(AT + timedelta(minutes=5))
            assert (await status(engine, account))["scheduled_backlog"] == 1
            assert await owner.tick(sender, AT + timedelta(hours=1)) == "waiting"
            assert await owner.tick(sender, AT + timedelta(hours=2, seconds=1)) == "delivered"
            await owner.prepare(AT + timedelta(hours=2, seconds=2))
            assert (await status(engine, account))["scheduled_backlog"] == 0
        assert requests[0] == requests[1]
        report = await status(engine, account)
        current = report["state"]
        assert isinstance(current, dict) and current["cursor"] == 0
        assert report["scheduled_backlog"] == 0
    finally:
        await sender.close()


@pytest.mark.integration
async def test_inflight_transition_is_not_replaced_by_a_new_critical_notice(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    messages = []

    def handler(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content))
        if len(messages) == 1:
            raise httpx.ReadTimeout("fixture")
        return response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(handler))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            assert await owner.tick(sender, AT) == "retry"
            await owner.prepare(AT + timedelta(seconds=10))
            assert await owner.tick(sender, AT + timedelta(seconds=10)) == "delivered"
            assert messages[0] == messages[1] and "cash_flow" in messages[0]["content"]
            assert await owner.tick(sender, AT + timedelta(seconds=12)) == "delivered"
            assert "CRITICAL REMINDER" in messages[2]["content"]
    finally:
        await sender.close()


@pytest.mark.integration
async def test_scheduled_send_crash_keeps_committed_attempt_and_receipt_is_atomic(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        async with engine.connect() as c:
            row = (
                (
                    await c.execute(
                        select(history)
                        .where(history.c.account_id == account)
                        .order_by(history.c.number.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .one()
            )
            assert row["kind"] == "attempt_started" and row["document"]["notice_id"]
            assert not await c.scalar(
                select(receipts.c.notice_id).where(receipts.c.account_id == account)
            )
        if len(requests) == 1:
            raise asyncio.CancelledError
        return response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(handler))
    before = await count_finance(engine, account)
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            await owner.prepare(AT)
            with pytest.raises(asyncio.CancelledError):
                await owner.tick(sender, AT)
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, AT + timedelta(seconds=29)) == "waiting"
            assert await owner.tick(sender, AT + timedelta(seconds=31)) == "delivered"
        assert requests[0] == requests[1]
        async with engine.connect() as c:
            assert (
                await c.scalar(
                    select(func.count())
                    .select_from(receipts)
                    .where(receipts.c.account_id == account)
                )
                == 1
            )
        assert await count_finance(engine, account) == before
    finally:
        await sender.close()


@pytest.mark.integration
async def test_daily_uses_original_recorded_interval_and_missing_midnight_is_explicit(
    replay: tuple[StoredReplay, Feed],
    database_settings: Settings,
) -> None:
    runner, _ = replay
    engine, account = runner.store.engine, runner.store.account_id
    async with engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        summary = await daily_summary(c, runner.store, AT.date())
    observed = summary["observed"]
    assert isinstance(observed, dict)
    assert Decimal(str(observed["nav_gbp"])) == 1000
    assert Decimal(str(observed["pnl_gbp"])) == 0
    assert summary["daily_pnl_gbp"] is None and not summary["daily_return_complete"]
    notification_engine = create_engine(database_settings)
    try:
        async with NotificationOwner(notification_engine, account) as owner:
            assert (await owner.prepare(AT + timedelta(days=1, minutes=10)))["daily_scheduled"]
    finally:
        await notification_engine.dispose()
    async with engine.connect() as c:
        row = (
            (
                await c.execute(
                    select(notices).where(
                        notices.c.account_id == account, notices.c.kind == "daily"
                    )
                )
            )
            .mappings()
            .one()
        )
        payload = notice_payload(row["document"], row["id"], account)
        assert "Recorded interval" in str(
            payload["content"]
        ) and "midnight coverage unqualified" in str(payload["content"])


@pytest.mark.integration
async def test_notice_tables_are_append_only_and_owner_is_independent(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    async with NotificationOwner(engine, account) as owner:
        await owner.prepare(AT)
        async with PaperWriter(PaperStore(engine, account)):
            pass
        with pytest.raises(RuntimeError, match="another notification owner"):
            async with NotificationOwner(engine, account):
                pass
    for table in (notices, receipts):
        async with engine.begin() as c:
            with pytest.raises(DBAPIError):
                await c.execute(table.delete())


@pytest.mark.integration
async def test_daily_accounting_half_open_dates_and_no_cross_account_data(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    store = PaperStore(engine, account)
    async with PaperWriter(store) as writer:
        service = PaperExecution(writer, PaperExchange(store))
        await service.cashflow("one-second-before", Decimal(50), AT + timedelta(days=1, seconds=-1))
        await service.cashflow("at-next-midnight", Decimal(75), AT + timedelta(days=1))
    async with engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        report = await daily_summary(c, store, AT.date())
    assert Decimal(str(report["contributions_gbp"])) == 300
    assert Decimal(str(report["withdrawals_gbp"])) == 0
    assert Decimal(str(report["fees_gbp"])) == 0


@pytest.mark.integration
async def test_critical_classifier_detects_unprotected_inventory_without_exposing_raw_reason(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    async with engine.connect() as c:
        state = await PaperStore(engine, account).load(c)
    assert state is not None
    altered = state.model_copy(
        update={
            "balances": (Balance(asset="BTC", quantity=Decimal(1), cost_gbp=Decimal(1)),),
            "incident": "untrusted @everyone payload",
        }
    )
    active = critical(altered)
    assert active is not None and "inventory_protection_difference" in active[1]
    message = notice_payload(
        {
            "schema_version": 1,
            "account_id": str(account),
            "kind": "critical",
            "live_execution_available": False,
            "reasons": list(active[1]),
        },
        uuid4(),
        account,
    )
    assert "untrusted" not in str(message["content"]) and "@everyone" not in str(message["content"])


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"kind": "unknown"},
        {"live_execution_available": True},
        {"reasons": ["@everyone"]},
    ],
)
def test_unknown_or_unsafe_scheduled_payload_is_rejected(change: dict[str, object]) -> None:
    account = uuid4()
    doc = {
        "schema_version": 1,
        "account_id": str(account),
        "kind": "critical",
        "live_execution_available": False,
        "reasons": ["hard_halt"],
        **change,
    }
    with pytest.raises(ValueError):
        notice_payload(doc, uuid4(), account)


@pytest.mark.integration
async def test_daily_delivery_is_durable_and_does_not_skip_transition_backlog(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    at = AT + timedelta(days=1, minutes=10)
    messages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content)["content"])
        return response()

    before = await count_finance(engine, account)
    sender = DiscordSender(webhook(), transport=httpx.MockTransport(handler))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), at)
            await owner.prepare(at)
            assert await owner.tick(sender, at) == "delivered"
            assert await owner.tick(sender, at + timedelta(seconds=2)) == "delivered"
            assert "CRITICAL REMINDER" in messages[0] and "DAILY SUMMARY" in messages[1]
            current = (await status(engine, account))["state"]
            assert isinstance(current, dict) and current["cursor"] == 0
        async with NotificationOwner(engine, account) as owner:
            await owner.prepare(at + timedelta(seconds=3))
            assert await owner.tick(sender, at + timedelta(seconds=4)) == "delivered"
            assert "cash_flow" in messages[-1]
        previews = await read_notices(engine, account)
        assert isinstance(previews["items"], list) and len(previews["items"]) == 2
        assert all(item["outcome"] == "delivered" for item in previews["items"])
        assert await count_finance(engine, account) == before
    finally:
        await sender.close()


@pytest.mark.integration
async def test_bad_daily_evidence_leaves_critical_notifications_available(
    notification_account: tuple[AsyncEngine, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from aca.observability import delivery

    engine, account = notification_account

    async def invalid(c: AsyncConnection, store: PaperStore, day: object) -> dict[str, object]:
        assert await c.scalar(select(func.current_setting("transaction_read_only"))) == "on"
        raise ValueError("untrusted source text must remain local")

    monkeypatch.setattr(delivery, "daily_summary", invalid)
    at = AT + timedelta(days=1, minutes=10)
    sender = DiscordSender(webhook(), transport=httpx.MockTransport(lambda _: response()))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), at)
            result = await owner.prepare(at)
            assert result["daily_evidence_unavailable"] and not result["daily_scheduled"]
            assert await owner.tick(sender, at) == "delivered"
        previews = await read_notices(engine, account)
        assert isinstance(previews["items"], list) and len(previews["items"]) == 1
    finally:
        await sender.close()


@pytest.mark.integration
async def test_long_downtime_schedules_only_latest_day_without_reminder_flood(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    at = AT + timedelta(days=20, minutes=10)
    async with NotificationOwner(engine, account) as owner:
        await owner.prepare(at)
        await owner.prepare(at + timedelta(hours=2))
    previews = await read_notices(engine, account)
    items = previews["items"]
    assert isinstance(items, list) and len(items) == 2
    summary = next(item for item in items if item["kind"] == "daily")["document"]["summary"]
    assert summary["day"] == "2026-11-06" and summary["observed"] is None
    assert (await status(engine, account))["scheduled_backlog"] == 2


@pytest.mark.integration
async def test_notice_queries_are_read_only_account_scoped_and_bounded(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    async with NotificationOwner(engine, account) as owner:
        await owner.prepare(AT)
    for value in (0, 101):
        with pytest.raises(ValueError, match="budget"):
            await read_notices(engine, account, value)
    with pytest.raises(ValueError, match="does not exist"):
        await read_notices(engine, uuid4())
    other = uuid4()
    store = PaperStore(engine, other)
    async with PaperWriter(store) as writer:
        await PaperExecution(writer, PaperExchange(store)).initialise(AT)
    assert (await read_notices(engine, other))["items"] == []


@pytest.mark.integration
async def test_eight_uncertain_scheduled_attempts_retain_dead_letter_receipt(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("fixture")

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(handler))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            await owner.prepare(AT)
            for n in range(8):
                assert await owner.tick(sender, AT + timedelta(minutes=10 * n)) == "retry"
            assert await owner.tick(sender, AT + timedelta(minutes=80)) == "dead_letter"
        assert len(attempts) == 8
        items = (await read_notices(engine, account))["items"]
        assert isinstance(items, list) and items[0]["outcome"] == "dead_letter"
        report = await status(engine, account)
        assert report["terminal_failures"] == 1 and report["scheduled_backlog"] == 0
    finally:
        await sender.close()
