"""Exact terminal-event review cannot replay an alert or release trading controls."""

import json
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine
from test_notification_health import Timer
from test_notifications import AT, TOKEN, URL, response, webhook
from test_scheduled_notifications import count_finance

from aca.observability.delivery import NotificationOwner, history, status
from aca.observability.discord import DiscordSender
from aca.observability.health import NotificationMetrics, NotificationStatus, read_sample
from aca.observability.reviews import (
    ReviewRequest,
    read_failures,
    reviews,
)
from aca.operations import notifications
from aca.operations.cli import _parser
from aca.operations.config import Settings
from aca.storage.database import verify_runtime

pytest_plugins = ("test_notifications",)
ACTOR = UUID("89593ee2-109f-420e-a1c1-838a83763e7c")
EVIDENCE = "a" * 64


def request(item: dict[str, object], **changes: object) -> ReviewRequest:
    return ReviewRequest.model_validate(
        {
            "failure_number": item["failure_number"],
            "failure_hash": item["failure_hash"],
            "actor": ACTOR,
            "resolution": "delivery_loss_accepted",
            "evidence_sha256": EVIDENCE,
            **changes,
        }
    )


async def abandon(engine: AsyncEngine, account: UUID, *, count: int = 1) -> list[dict[str, object]]:
    sender = DiscordSender(webhook(), transport=httpx.MockTransport(lambda _: response(400)))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            for i in range(count):
                assert await owner.tick(sender, AT + timedelta(seconds=2 * i)) == "abandoned"
    finally:
        await sender.close()
    return cast(list[dict[str, object]], (await read_failures(engine, account))["items"])


@pytest.mark.parametrize(
    "changes",
    [
        {"failure_number": True},
        {"failure_number": 1.0},
        {"failure_number": 0},
        {"failure_hash": "A" * 64},
        {"evidence_sha256": "missing"},
        {"actor": "human"},
        {"resolution": "retry"},
        {"webhook": URL},
    ],
)
def test_review_is_strict_and_has_no_raw_note_or_destination(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request({"failure_number": 1, "failure_hash": "b" * 64}, **changes)


@pytest.mark.integration
async def test_exact_review_is_idempotent_and_new_failure_degrades_health(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    before = await count_finance(engine, account)
    item = (await abandon(engine, account))[0]
    state_before = (await status(engine, account))["state"]
    async with engine.connect() as c:
        history_before = await c.scalar(
            select(func.count()).select_from(history).where(history.c.account_id == account)
        )
    timer = Timer()
    health = NotificationStatus(account, timer)
    health.database_ready = health.schema_ready = True
    health.last_success = health.worker_last_success = timer()
    health.sample = await read_sample(engine, account)
    assert health.reason == "unreviewed_terminal_failures"
    async with NotificationOwner(engine, account) as owner:
        first = await owner.review(request(item))
        assert first["inserted"] and not first["message_resent"]
        assert not first["delivery_state_changed"] and not first["promotion_evidence"]
        with pytest.raises(ValueError, match="different review"):
            await owner.review(request(item, evidence_sha256="c" * 64))
    # Reacquire ownership to model an operator process restart.
    async with NotificationOwner(engine, account) as owner:
        again = await owner.review(request(item))
        assert not again["inserted"] and again["review"] == first["review"]
    report = await status(engine, account)
    assert report["terminal_failures"] == report["reviewed_terminal_failures"] == 1
    assert report["unreviewed_terminal_failures"] == 0 and report["state"] == state_before
    health.sample = await read_sample(engine, account)
    assert health.notification_ready and health.document()["trading_ready"] is False
    metrics = NotificationMetrics().render(health).decode()
    assert 'kind="terminal_failures"} 1.0' in metrics
    assert 'kind="reviewed_terminal_failures"} 1.0' in metrics
    assert 'kind="unreviewed_terminal_failures"} 0.0' in metrics
    assert TOKEN not in metrics and str(ACTOR) not in metrics
    async with engine.connect() as c:
        assert (
            await c.scalar(
                select(func.count()).select_from(history).where(history.c.account_id == account)
            )
            == history_before
        )
    next_sender = DiscordSender(webhook(), transport=httpx.MockTransport(lambda _: response(400)))
    try:
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(next_sender, AT + timedelta(seconds=3)) == "abandoned"
    finally:
        await next_sender.close()
    health.sample = await read_sample(engine, account)
    assert health.sample.terminal_failures == 2 and health.sample.reviewed_terminal_failures == 1
    assert health.reason == "unreviewed_terminal_failures"
    assert await count_finance(engine, account) == before


@pytest.mark.integration
async def test_wrong_target_nonterminal_and_competing_owner_are_rejected(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    item = (await abandon(engine, account))[0]
    async with NotificationOwner(engine, account) as owner:
        with pytest.raises(ValueError, match="hash does not match"):
            await owner.review(request(item, failure_hash="0" * 64))
        with pytest.raises(ValueError, match="does not exist"):
            await owner.review(request(item, failure_number=9999))
        with pytest.raises(ValueError, match="evidence is invalid"):
            await owner.review(request(item, failure_number=1))  # operator_configured
        with pytest.raises(RuntimeError, match="another notification owner"):
            async with NotificationOwner(engine, account):
                pass
    async with NotificationOwner(engine, uuid4()) as owner:
        with pytest.raises(ValueError, match="account does not exist"):
            await owner.review(request(item))
    assert (await status(engine, account))["reviewed_terminal_failures"] == 0


@pytest.mark.integration
async def test_review_preserves_disabled_destination_and_shared_retry_deadline(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    item = (await abandon(engine, account))[0]
    sender = DiscordSender(
        webhook(),
        transport=httpx.MockTransport(lambda _: response(429, headers={"Retry-After": "7200"})),
    )
    try:
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, AT + timedelta(seconds=3)) == "retry"
            state_before = (await status(engine, account))["state"]
            await owner.review(request(item))
            assert (await status(engine, account))["state"] == state_before
            assert await owner.tick(sender, AT + timedelta(seconds=4)) == "waiting"
    finally:
        await sender.close()
    disabled = DiscordSender(webhook(), transport=httpx.MockTransport(lambda _: response(404)))
    try:
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(disabled, AT + timedelta(seconds=7205)) == "disabled"
            await owner.review(request(item))
            assert await owner.tick(disabled, AT + timedelta(seconds=7210)) == "disabled"
    finally:
        await disabled.close()
    health = await read_sample(engine, account)
    assert not health.enabled and health.unreviewed_terminal_failures == 0
    assert (await status(engine, account))["terminal_failures"] == 1


@pytest.mark.integration
async def test_cursor_listing_and_cli_review_need_no_webhook_or_sender(
    notification_account: tuple[AsyncEngine, UUID],
    database_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, account = notification_account
    items = await abandon(engine, account, count=2)

    def forbidden(*args: object) -> None:
        raise AssertionError("review must never construct a sender")

    monkeypatch.setattr(notifications, "DiscordSender", forbidden)
    parser = _parser()
    page = await notifications.execute(
        parser.parse_args(
            [
                "notifications",
                "failures",
                "--account",
                str(account),
                "--limit",
                "1",
            ]
        ),
        database_settings,
    )
    assert page["items"] == [items[0]] and page["next_after_number"] == items[0]["failure_number"]
    cursor = page["next_after_number"]
    assert isinstance(cursor, int)
    rest = await read_failures(engine, account, after_number=cursor)
    assert rest["items"] == items[1:] and rest["next_after_number"] is None
    args = parser.parse_args(
        [
            "notifications",
            "review",
            "--account",
            str(account),
            "--failure-number",
            str(items[1]["failure_number"]),
            "--failure-hash",
            str(items[1]["failure_hash"]),
            "--actor",
            str(ACTOR),
            "--resolution",
            "manual_notification_confirmed",
            "--evidence-sha256",
            EVIDENCE,
        ]
    )
    reviewed = await notifications.execute(args, database_settings)
    listed = cast(list[dict[str, object]], (await read_failures(engine, account))["items"])
    assert listed[0]["review"] is None and listed[1]["review"] == reviewed["review"]
    assert URL not in json.dumps(listed) and webhook().fingerprint not in json.dumps(listed)
    assert (await status(engine, account))["unreviewed_terminal_failures"] == 1
    assert (await read_failures(engine, account, after_number=9999))["items"] == []
    for changes in ({"limit": 0}, {"limit": 101}, {"after_number": -1}, {"after_number": True}):
        with pytest.raises(ValueError):
            await read_failures(engine, account, **changes)


@pytest.mark.integration
async def test_database_references_exact_terminal_event_and_reviews_are_append_only(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    items = await abandon(engine, account, count=2)
    item = items[0]
    async with NotificationOwner(engine, account) as owner:
        await owner.review(request(item))
    async with engine.connect() as c:
        row = dict(
            (await c.execute(select(reviews).where(reviews.c.account_id == account)))
            .mappings()
            .one()
        )
    for statement in (
        reviews.update().where(reviews.c.account_id == account).values(fingerprint="0" * 64),
        reviews.delete().where(reviews.c.account_id == account),
        text("TRUNCATE aca.paper_notification_reviews"),
        reviews.insert().values(**{**row, "failure_number": 1}),
        reviews.insert().values(**{**row, "account_id": uuid4()}),
        reviews.insert().values(
            **{**row, "failure_number": items[1]["failure_number"], "failure_hash": "0" * 64}
        ),
    ):
        async with engine.begin() as c:
            with pytest.raises(DBAPIError):
                await c.execute(statement)
    assert (await status(engine, account))["reviewed_terminal_failures"] == 1
    await verify_runtime(engine)


@pytest.mark.integration
async def test_malformed_retained_review_blocks_status_instead_of_clearing_health(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    item = (await abandon(engine, account))[0]
    async with engine.begin() as c:
        await c.execute(
            reviews.insert().values(
                account_id=account,
                failure_number=item["failure_number"],
                failure_kind="abandoned",
                failure_hash=item["failure_hash"],
                reviewed_at=datetime.now(UTC),
                document={"schema_version": 99},
                fingerprint="0" * 64,
            )
        )
    with pytest.raises(ValidationError):
        await status(engine, account)
    with pytest.raises(ValidationError):
        await read_sample(engine, account)


def test_monitoring_failure_counts_must_balance() -> None:
    from test_notification_health import sample

    with pytest.raises(ValidationError, match="counts do not balance"):
        sample(terminal_failures=1, reviewed_terminal_failures=2)


@pytest.mark.integration
async def test_dead_letter_review_keeps_receipt_and_active_critical_repeat(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    from aca.observability.notices import receipts

    engine, account = notification_account
    before = await count_finance(engine, account)
    calls = []

    def timeout(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        raise httpx.ReadTimeout("fixture timeout", request=req)

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(timeout))
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            await owner.prepare(AT, include_daily=False)
            for attempt in range(8):
                assert await owner.tick(sender, AT + timedelta(seconds=1000 * attempt)) == "retry"
            assert await owner.tick(sender, AT + timedelta(seconds=8000)) == "dead_letter"
            item = cast(list[dict[str, object]], (await read_failures(engine, account))["items"])[0]
            assert item["kind"] == "dead_letter" and item["notice_id"] is not None
            await owner.review(request(item))
            assert len(calls) == 8
            # Review doesn't resolve the hard halt or reset the five-minute repeat.
            await owner.prepare(AT + timedelta(seconds=8001), include_daily=False)
            assert (await status(engine, account))["scheduled_backlog"] == 0
            await owner.prepare(AT + timedelta(seconds=8300), include_daily=False)
            assert (await status(engine, account))["scheduled_backlog"] == 1
    finally:
        await sender.close()
    async with engine.connect() as c:
        assert (
            await c.scalar(
                select(receipts.c.outcome).where(
                    receipts.c.notice_id == UUID(str(item["notice_id"]))
                )
            )
            == "dead_letter"
        )
    assert await count_finance(engine, account) == before


@pytest.mark.integration
async def test_review_rollback_corruption_and_audit_budget_fail_closed(
    notification_account: tuple[AsyncEngine, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aca.observability import reviews as review_module

    engine, account = notification_account
    item = (await abandon(engine, account))[0]
    async with NotificationOwner(engine, account) as owner:
        with pytest.raises(RuntimeError, match="fixture crash"):
            async with owner.transaction() as c:
                await review_module.review_failure(c, account, request(item))
                raise RuntimeError("fixture crash")
        assert (await status(engine, account))["unreviewed_terminal_failures"] == 1
        monkeypatch.setattr(review_module, "MAX_REVIEWS", 0)
        with pytest.raises(ValueError, match="exceeds budget"):
            await owner.review(request(item))
        monkeypatch.setattr(review_module, "MAX_REVIEWS", 10_000)
        await owner.review(request(item))
        monkeypatch.setattr(review_module, "MAX_REVIEWS", 0)
        with pytest.raises(ValueError, match="exceeds budget"):
            await status(engine, account)
