"""Notification observations cannot deliver messages or confer trading permission."""

import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine
from test_notifications import AT, TOKEN, URL, response, webhook
from test_scheduled_notifications import count_finance
from test_service import wait_health

from aca.observability.delivery import NotificationOwner, status
from aca.observability.discord import DiscordSender
from aca.observability.health import (
    NotificationHealthServer,
    NotificationMetrics,
    NotificationMonitor,
    NotificationStatus,
    QueueSample,
    read_sample,
)
from aca.operations import notifications
from aca.operations.cli import _health, _parser
from aca.operations.config import HealthSettings, Settings
from aca.storage.database import create_engine
from aca.storage.paper import PaperStore, PaperWriter

pytest_plugins = ("test_notifications",)


class Timer:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def sample(**changes: object) -> QueueSample:
    return QueueSample.model_validate(
        {
            "as_of": AT,
            "configured": True,
            "enabled": True,
            "transitions": 2,
            "daily": 1,
            "critical": 1,
            "oldest_transition_at": AT - timedelta(hours=24),
            "oldest_daily_at": AT - timedelta(seconds=10),
            "oldest_critical_at": AT,
            "delivered": 3,
            "retry_events": 1,
            "terminal_failures": 0,
            "reviewed_terminal_failures": 0,
            "unreviewed_terminal_failures": 0,
            "last_delivery_at": AT - timedelta(seconds=1),
            "attempts": 2,
            "not_before": AT + timedelta(hours=2),
            **changes,
        }
    )


def healthy() -> tuple[NotificationStatus, Timer]:
    timer = Timer()
    state = NotificationStatus(uuid4(), timer)
    state.database_ready = state.schema_ready = True
    state.last_success = state.worker_last_success = timer()
    state.sample = sample()
    return state, timer


def test_monotonic_cache_and_worker_freshness_are_independent() -> None:
    state, timer = healthy()
    assert state.notification_ready and state.retry_wait() == 7200
    timer.now += 10
    assert state.ready and state.age(state.sample.oldest_daily_at if state.sample else None) == 20
    assert state.retry_wait() == 7190
    timer.now += 0.001
    assert state.worker_fresh and not state.notification_ready
    assert state.reason == "monitoring_evidence_unavailable"
    state.last_success = timer()
    timer.now = 130
    state.last_success = timer()
    assert state.worker_fresh
    timer.now += 0.001
    assert state.ready and not state.worker_fresh and state.reason == "worker_stale"
    # A timer discontinuity cannot create a fresh observation.
    timer.now = 99
    assert not state.ready and not state.worker_fresh


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"configured": False, "enabled": False}, "destination_unconfigured"),
        ({"enabled": False}, "destination_disabled"),
        (
            {"terminal_failures": 1, "unreviewed_terminal_failures": 1},
            "unreviewed_terminal_failures",
        ),
    ],
)
def test_destination_failures_degrade_health_without_changing_delivery(
    change: dict[str, object], reason: str
) -> None:
    state, _ = healthy()
    state.sample = sample(**change)
    assert state.reason == reason and not state.notification_ready
    assert state.sample.attempts == 2 and state.retry_wait() == 7200
    assert state.document()["trading_ready"] is False


def test_daily_failure_and_shutdown_cannot_be_hidden_by_healthy_database() -> None:
    state, _ = healthy()
    state.daily_evidence_unavailable = True
    assert state.reason == "daily_evidence_unavailable"
    state.worker_faulted = True
    assert state.reason == "worker_fault" and not state.worker_fresh
    state.shutting_down = True
    assert state.reason == "stopping" and not state.ready


@pytest.mark.parametrize(
    "change",
    [
        {"transitions": True},
        {"attempts": 9},
        {"daily": 1.0},
        {"as_of": datetime(2026, 10, 19)},
        {"webhook": URL},
    ],
)
def test_monitor_boundary_rejects_unknown_or_inexact_values(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        sample(**change)


def test_metrics_have_fixed_labels_and_unknown_ages_are_explicit() -> None:
    state, timer = healthy()
    metrics = NotificationMetrics()
    rendered = metrics.render(state).decode()
    assert 'aca_notifications_pending{kind="transition"} 2.0' in rendered
    assert 'aca_notifications_oldest_age_seconds{kind="transition"} 86400.0' in rendered
    assert "aca_notifications_retry_wait_seconds 7200.0" in rendered
    assert "aca_trading_ready 0.0" in rendered
    for forbidden in (str(state.account_id), URL, TOKEN, "destination_hash", "alert_id"):
        assert forbidden not in rendered
    state.sample = sample(oldest_daily_at=AT + timedelta(minutes=1), last_delivery_at=None)
    timer.now += 2
    rendered = metrics.render(state).decode()
    assert 'aca_notifications_oldest_age_seconds{kind="daily"} -1.0' in rendered
    assert "aca_notifications_last_delivery_age_seconds -1.0" in rendered
    timer.now += 9
    assert "aca_notifications_ready 0.0" in metrics.render(state).decode()


async def test_failed_observation_keeps_cached_counts_but_revokes_freshness(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state, _ = healthy()
    metrics = NotificationMetrics()

    async def fail() -> QueueSample:
        raise ValueError(URL)

    monitor = NotificationMonitor(state, metrics, fail)
    await monitor.poll()
    assert state.reason == "monitoring_evidence_unavailable"
    assert state.sample is not None and state.sample.transitions == 2
    assert state.last_success is None
    assert state.retry_wait() == -1
    rendered = metrics.render(state).decode()
    assert "aca_notifications_monitoring_fresh 0.0" in rendered
    assert 'aca_notifications_monitor_samples_total{result="failure"} 1.0' in rendered
    assert URL not in capsys.readouterr().out


async def test_hung_observation_has_deadline_and_cancellation_is_prompt() -> None:
    state, _ = healthy()
    entered = asyncio.Event()

    async def hang() -> QueueSample:
        entered.set()
        await asyncio.Event().wait()
        return sample()

    monitor = NotificationMonitor(state, NotificationMetrics(), hang)
    async with asyncio.timeout(5):
        await monitor.poll()
    assert not state.ready
    entered.clear()
    task = asyncio.create_task(monitor.run(asyncio.Event()))
    try:
        await entered.wait()
        task.cancel()
        async with asyncio.timeout(1):
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_http_scrapes_only_read_cache_and_cli_accepts_local_route() -> None:
    state, _ = healthy()
    reads = 0

    async def read() -> QueueSample:
        nonlocal reads
        reads += 1
        return sample()

    metrics = NotificationMetrics()
    monitor = NotificationMonitor(state, metrics, read)
    await monitor.poll()
    server = NotificationHealthServer(state, metrics)
    await server.start("127.0.0.1", 0)
    assert server.server is not None
    port = server.server.sockets[0].getsockname()[1]
    try:
        for _ in range(4):
            doc = await wait_health(port, 200)
            assert doc["notification_ready"] and doc["trading_ready"] is False
            assert server.response("GET", "/health/notifications")[0] == 200
            assert server.response("GET", "/health/trading")[0] == 503
            assert server.response("POST", "/health/notifications")[0] == 405
            assert server.response("GET", "/orders")[0] == 404
            assert b"aca_notifications_ready 1.0" in server.response("GET", "/metrics")[2]
        assert reads == 1
        assert (
            await asyncio.to_thread(_health, f"http://127.0.0.1:{port}/health/notifications") == 0
        )
        state.database_ready = False
        assert (await wait_health(port, 503))["notification_ready"] is False
    finally:
        await server.close()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.integration
async def test_read_only_snapshot_does_not_compete_for_delivery_or_execution_ownership(
    notification_account: tuple[AsyncEngine, UUID],
    database_settings: Settings,
) -> None:
    engine, account = notification_account
    observer = create_engine(database_settings)
    before = await count_finance(engine, account)
    checked = []

    def check_read_only(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "paper_notification_events" in statement:
            # SQLAlchemy adapts the driver cursor to this synchronous event hook.
            from typing import Any, cast

            adapted = cast(Any, cursor)
            adapted.execute("SHOW transaction_read_only")
            assert adapted.fetchone()[0] == "on"
            adapted.execute("SHOW transaction_isolation")
            assert adapted.fetchone()[0] == "repeatable read"
            checked.append(True)

    event.listen(observer.sync_engine, "before_cursor_execute", check_read_only)
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), AT)
            await owner.prepare(AT + timedelta(days=1, minutes=10))
            # Both slots in the owner's pool are occupied. Observation has its own pool.
            async with PaperWriter(PaperStore(engine, account)):
                async with asyncio.timeout(4):
                    report = await read_sample(observer, account)
                assert report.configured and report.enabled
                assert report.transitions == 2 and report.critical == 1 and report.daily == 1
        assert checked and await count_finance(engine, account) == before
        assert report.oldest_transition_at == AT
        assert report.oldest_daily_at == AT + timedelta(days=1, minutes=10)
        assert TOKEN not in report.model_dump_json() and URL not in report.model_dump_json()
        with pytest.raises(ValueError, match="does not exist"):
            await read_sample(observer, uuid4())
    finally:
        event.remove(observer.sync_engine, "before_cursor_execute", check_read_only)
        await observer.dispose()


@pytest.mark.integration
async def test_throttle_disable_and_terminal_events_remain_visible_after_restart(
    notification_account: tuple[AsyncEngine, UUID],
) -> None:
    engine, account = notification_account
    at = datetime.now(UTC)
    codes = [429, 400, 404]

    def send(_: httpx.Request) -> httpx.Response:
        return response(codes.pop(0), headers={"Retry-After": "7200"} if len(codes) == 2 else {})

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(send))
    before = await count_finance(engine, account)
    try:
        async with NotificationOwner(engine, account) as owner:
            await owner.configure(webhook(), at)
            assert await owner.tick(sender, at) == "retry"
        report = await read_sample(engine, account)
        assert report.attempts == 1 and report.retry_events == 1
        assert (
            report.not_before is not None
            and (report.not_before - report.as_of).total_seconds() > 7100
        )
        async with NotificationOwner(engine, account) as owner:
            assert await owner.tick(sender, at + timedelta(seconds=7201)) == "abandoned"
            assert await owner.tick(sender, at + timedelta(seconds=7203)) == "disabled"
        report = await read_sample(engine, account)
        assert not report.enabled and report.terminal_failures == 1 and report.transitions == 1
        assert await count_finance(engine, account) == before
    finally:
        await sender.close()


@pytest.mark.integration
async def test_optional_run_wires_health_and_cancellation_cleans_up_resources(
    notification_account: tuple[AsyncEngine, UUID],
    database_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, account = notification_account
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    secret = tmp_path / "webhook"
    secret.write_text(URL)
    secret.chmod(0o600)
    settings = database_settings.model_copy(update={"health": HealthSettings(port=port)})
    async with NotificationOwner(engine, account) as owner:
        await owner.configure(webhook(), datetime.now(UTC))
    before = await count_finance(engine, account)
    sends = []

    def send(request: httpx.Request) -> httpx.Response:
        sends.append(request)
        return response()

    sender = DiscordSender(webhook(), transport=httpx.MockTransport(send))
    monkeypatch.setattr(notifications, "DiscordSender", lambda _: sender)
    args = _parser().parse_args(
        [
            "notifications",
            "run",
            "--account",
            str(account),
            "--webhook-file",
            str(secret),
            "--duration",
            "0",
        ]
    )
    task = asyncio.create_task(notifications.execute(args, settings))
    try:
        doc = await wait_health(port, 200)
        assert doc["worker_fresh"] and doc["monitoring_fresh"] and sends
        assert doc["trading_ready"] is False and doc["live_execution_available"] is False
        assert TOKEN not in json.dumps(doc) and URL not in json.dumps(doc)
    finally:
        task.cancel()
        async with asyncio.timeout(5):
            with pytest.raises(asyncio.CancelledError):
                await task
    assert sender.client.is_closed
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    async with NotificationOwner(engine, account):
        pass
    assert await count_finance(engine, account) == before
    report = await status(engine, account)
    assert isinstance(report["delivered"], int) and report["delivered"] >= 1
