"""T24/T31: outbound delivery faults cannot mutate paper economics or leak secrets."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from aca.execution.paper import PaperExecution
from aca.observability.delivery import NotificationOwner, history, status
from aca.observability.discord import DiscordSender, Webhook, classify
from aca.observability.logging import configure_logging, redact
from aca.operations.config import Settings
from aca.operations.secrets import SecretFileError
from aca.simulation.paper import PaperExchange
from aca.storage.database import create_engine, verify_runtime
from aca.storage.paper import PaperStore, PaperWriter, events, fills, journals, outbox, venue_events

AT = datetime(2026, 10, 18, tzinfo=UTC)
TOKEN = "fixture-not-a-real-discord-token-" + "x" * 40
URL = "https://discord.com/api/webhooks/123456789012345678/" + TOKEN


def webhook() -> Webhook:
    return Webhook.parse(SecretStr(URL))


def response(code: int = 200, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        code,
        headers=headers,
        stream=httpx.ByteStream(b'{"id":"123456789012345678"}'),
    )


@pytest.fixture
async def notification_account(
    database_settings: Settings,
) -> AsyncIterator[tuple[AsyncEngine, UUID]]:
    engine = create_engine(database_settings)
    account_id = uuid4()
    store = PaperStore(engine, account_id)
    try:
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            await service.initialise(AT)
            await service.cashflow("opening", Decimal(250), AT)
            await service.halt("fixture hard halt", AT)
        yield engine, account_id
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("https://", "http://"),
        URL.replace("discord.com", "discord.com.evil.invalid"),
        URL.replace("discord.com", "127.0.0.1"),
        URL + "?wait=false",
        URL + "#fragment",
        URL.replace("discord.com", "operator@discord.com"),
        URL.replace("/api/", "/api/v99/"),
    ],
)
def test_webhook_is_an_exact_https_allowlist(url: str) -> None:
    with pytest.raises(ValueError):
        Webhook.parse(SecretStr(url))


def test_webhook_file_permissions_and_redaction(tmp_path: Path) -> None:
    path = tmp_path / "webhook"
    path.write_text(URL)
    path.chmod(0o600)
    value = Webhook.load(path)
    assert URL not in repr(value) and TOKEN not in repr(value)
    assert redact(f"Request {URL} token {TOKEN}") == "Request [REDACTED] token [REDACTED]"
    path.chmod(0o666)
    with pytest.raises(SecretFileError):
        Webhook.load(path)


@pytest.mark.parametrize(
    "headers,body,minimum",
    [
        ({"Retry-After": "65"}, b'{"retry_after":64.57}', timedelta(seconds=65)),
        ({}, b'{"retry_after":1.2345678}', timedelta(microseconds=1234568)),
        ({"Retry-After": "Sun, 18 Oct 2026 00:02:00 GMT"}, b"{}", timedelta(seconds=120)),
        (
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "7200"},
            b"{}",
            timedelta(seconds=7200),
        ),
        ({}, b"bad-json", timedelta(seconds=60)),
    ],
)
def test_server_rate_deadlines_are_never_shortened(
    headers: dict[str, str], body: bytes, minimum: timedelta
) -> None:
    result = classify(429, httpx.Headers(headers), body, AT)
    assert result.outcome == "retry" and result.delay >= minimum


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "604801", "1e10000"])
def test_unsupported_rate_deadline_disables_instead_of_retrying_early(value: str) -> None:
    assert classify(429, httpx.Headers({"Retry-After": value}), b"{}", AT).outcome == "disabled"


async def test_http_redirect_and_body_budget_are_bounded() -> None:
    requests = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response(302, headers={"Location": "https://evil.invalid"})

    client = DiscordSender(webhook(), transport=httpx.MockTransport(redirect))
    try:
        assert (await client.send({"content": "fixture"}, AT)).outcome == "disabled"
        assert len(requests) == 1
        assert requests[0].url.params["wait"] == "true"
    finally:
        await client.close()
    client = DiscordSender(
        webhook(),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=httpx.ByteStream(b"x" * 20000))
        ),
    )
    try:
        assert (await client.send({}, AT)).reason == "response_budget_exceeded"
    finally:
        await client.close()


@pytest.mark.integration
async def test_delivery_commits_before_send_deduplicates_and_preserves_finance(
    notification_account: tuple[AsyncEngine, UUID], capsys: pytest.CaptureFixture[str]
) -> None:
    engine, account = notification_account
    configure_logging()
    requests: list[httpx.Request] = []

    async def counts() -> list[int]:
        async with engine.connect() as c:
            return [
                int(
                    await c.scalar(
                        select(func.count()).select_from(table).where(table.c.account_id == account)
                    )
                    or 0
                )
                for table in (events, fills, journals, venue_events, outbox)
            ]

    before = await counts()

    async def deliver(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Separate connection sees a committed attempt, without a long-running sender transaction.
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
            assert row["kind"] == "attempt_started"
        message = json.loads(request.content)
        assert message["allowed_mentions"] == {"parse": []}
        assert "Live trading: disabled" in message["content"]
        assert "incident" not in message["content"]
        return response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(deliver))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            assert await owner.tick(sender, AT) == "delivered"
            assert await owner.tick(sender, AT + timedelta(seconds=2)) == "delivered"
            assert await owner.tick(sender, AT + timedelta(seconds=4)) == "idle"
        # Restart cannot replay already acknowledged alerts, even though source rows remain intact.
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, AT + timedelta(seconds=5)) == "idle"
        assert len(requests) == 2 and "CRITICAL" in json.loads(requests[1].content)["content"]
        report = await status(engine, account)
        assert report["backlog"] == 0 and report["terminal_failures"] == 0
        assert await counts() == before
        async with PaperWriter(PaperStore(engine, account)) as writer:
            assert writer.connection is not None
            await writer.store.audit(writer.connection)
    finally:
        await sender.close()
    output = capsys.readouterr().out
    assert URL not in output and TOKEN not in output


@pytest.mark.integration
async def test_404_disable_survives_restart_and_requires_explicit_reconfiguration(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    calls = 0

    def missing(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(404)

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(missing))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            assert await owner.tick(sender, AT) == "disabled"
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, AT + timedelta(days=1)) == "disabled"
            assert calls == 1
            with pytest.raises(ValueError):
                await owner.configure(webhook(), AT, after_version=0)
            await owner.configure(webhook(), AT + timedelta(days=1))
            assert await owner.tick(sender, AT + timedelta(days=1)) == "disabled"
            assert calls == 2
        assert (await status(engine, account))["backlog"] == 2
    finally:
        await sender.close()


@pytest.mark.integration
async def test_throttle_is_durable_destination_wide_and_reconfigure_cannot_shorten_it(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    calls = 0

    def limited(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(429, headers={"Retry-After": "7200"}) if calls == 1 else response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(limited))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            assert await owner.tick(sender, AT) == "retry"
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT + timedelta(seconds=1))
            assert await owner.tick(sender, AT + timedelta(seconds=7199)) == "waiting"
            assert calls == 1
            assert await owner.tick(sender, AT + timedelta(seconds=7201)) == "delivered"
        assert (await status(engine, account))["backlog"] == 1
    finally:
        await sender.close()


@pytest.mark.integration
async def test_unknown_network_delivery_has_bounded_attempts_and_durable_dead_letter(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    calls = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(URL, request=request)

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(timeout))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            for attempt in range(8):
                at = AT + timedelta(seconds=1000 * attempt)
                assert await owner.tick(sender, at) == "retry"
                assert await owner.tick(sender, at) == "waiting"
            assert await owner.tick(sender, AT + timedelta(seconds=8000)) == "dead_letter"
            assert calls == 8
        report = await status(engine, account)
        assert report["terminal_failures"] == 1 and report["backlog"] == 1
        assert URL not in json.dumps(report) and TOKEN not in json.dumps(report)
    finally:
        await sender.close()


@pytest.mark.integration
async def test_crash_after_transmission_retries_notification_without_replaying_finance(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    calls = 0

    async def crash(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(crash))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            with pytest.raises(asyncio.CancelledError):
                await owner.tick(sender, AT)
        report = await status(engine, account)
        assert isinstance(report["state"], dict) and report["state"]["outcome"] == "in_flight"
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, AT + timedelta(seconds=1)) == "waiting"
            assert await owner.tick(sender, AT + timedelta(seconds=31)) == "delivered"
        assert calls == 2
        async with engine.connect() as c:
            assert (
                await c.scalar(
                    select(func.count())
                    .select_from(journals)
                    .where(journals.c.account_id == account)
                )
                == 1
            )
    finally:
        await sender.close()


@pytest.mark.integration
async def test_single_notification_owner_does_not_block_execution_and_db_loss_stops_send(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    calls = 0

    def deliver(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(deliver))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            with pytest.raises(RuntimeError, match="another"):
                async with NotificationOwner(engine, account):
                    pass
            async with PaperWriter(PaperStore(engine, account)):
                pass
            assert owner.connection is not None
            await owner.connection.invalidate()
            with pytest.raises(RuntimeError, match="connection unavailable"):
                await owner.tick(sender, AT)
            assert calls == 0
        await verify_runtime(engine)
        async with engine.connect() as c:
            with pytest.raises(DBAPIError):
                await c.execute(text("DELETE FROM aca.paper_notification_events"))
    finally:
        await sender.close()


@pytest.mark.integration
async def test_initial_cutoff_and_destination_binding_are_explicit(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    other = Webhook.parse(SecretStr(URL[:-1] + "y"))
    sender = DiscordSender(other, transport=httpx.MockTransport(lambda _: response()))
    try:
        async with NotificationOwner(engine, account) as owner:
            with pytest.raises(ValueError):
                await owner.configure(webhook(), AT, after_version=-1)
            with pytest.raises(ValueError):
                await owner.configure(webhook(), AT, after_version=999999)
            await owner.configure(webhook(), AT, after_version=2)
            with pytest.raises(ValueError, match="secret"):
                await owner.tick(sender, AT)
            await owner.configure(other, AT)
            assert await owner.tick(sender, AT) == "delivered"
        assert (await status(engine, account))["backlog"] == 0
    finally:
        await sender.close()


@pytest.mark.integration
async def test_successful_last_bucket_slot_delays_next_alert_across_restart(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    calls = 0

    def deliver(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "30"})

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(deliver))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            assert await owner.tick(sender, AT) == "delivered"
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, AT + timedelta(seconds=29)) == "waiting"
            assert calls == 1
            assert await owner.tick(sender, AT + timedelta(seconds=31)) == "delivered"
        report = await status(engine, account)
        assert report["delivered"] == 2 and report["retry_events"] == 0
    finally:
        await sender.close()


@pytest.mark.integration
async def test_permanent_message_failure_does_not_block_later_alerts(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    calls = 0

    def deliver(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(400) if calls == 1 else response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(deliver))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            assert await owner.tick(sender, AT) == "abandoned"
            assert await owner.tick(sender, AT + timedelta(seconds=2)) == "delivered"
        report = await status(engine, account)
        assert report["terminal_failures"] == 1 and report["backlog"] == 0
    finally:
        await sender.close()


@pytest.mark.integration
async def test_database_insert_failure_cannot_dispatch_an_unaudited_message(
    notification_account: tuple[AsyncEngine, UUID], database_settings: Settings
) -> None:
    engine, account = notification_account
    migration_engine = create_engine(database_settings, "migration")
    calls = 0

    def deliver(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(deliver))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            async with migration_engine.begin() as c:
                await c.execute(
                    text("REVOKE INSERT ON aca.paper_notification_events FROM aca_runtime")
                )
            with pytest.raises(DBAPIError):
                await owner.tick(sender, AT)
            assert calls == 0
    finally:
        async with migration_engine.begin() as c:
            await c.execute(text("GRANT INSERT ON aca.paper_notification_events TO aca_runtime"))
        await sender.close()
        await migration_engine.dispose()


def test_alert_payload_excludes_raw_incident_and_unknown_category() -> None:
    from aca.observability.delivery import payload

    account, alert, event = uuid4(), uuid4(), uuid4()
    category = "@everyone " + TOKEN
    document = {
        "schema_version": 1,
        "account_id": str(account),
        "category": category,
        "mode": "paper",
        "version": 2,
        "hard_halt": False,
        "incident": URL,
        "live_execution_available": False,
    }
    row: dict[str, object] = {
        "account_id": account,
        "id": alert,
        "event_id": event,
        "category": category,
        "document": document,
    }
    message = json.dumps(payload(row, 2))
    assert TOKEN not in message and URL not in message and "@everyone" not in message
    assert "WARNING" in message and "paper_state_changed" in message
    document["schema_version"] = 2
    with pytest.raises(ValueError):
        payload(row, 2)
