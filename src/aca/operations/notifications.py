"""Opt-in delivery of existing paper alerts. No exchange or inbound Discord access."""

import argparse
import asyncio
import json
import signal
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from aca.observability.delivery import NotificationOwner, read_notices, status
from aca.observability.discord import DiscordSender, Webhook
from aca.observability.health import (
    NotificationHealthServer,
    NotificationMetrics,
    NotificationMonitor,
    NotificationStatus,
    read_sample,
)
from aca.observability.reviews import ReviewRequest, read_failures
from aca.operations.config import ConfigurationError, Settings
from aca.storage.database import create_engine, verify_runtime


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    actions = commands.add_parser("notifications").add_subparsers(dest="action", required=True)
    for name in ("configure", "run", "status", "prepare", "notices", "failures", "review"):
        parser = actions.add_parser(name)
        parser.add_argument("--account", required=True, type=UUID)
        if name in {"configure", "run"}:
            parser.add_argument("--webhook-file", required=True, type=Path)
        if name == "configure":
            parser.add_argument("--after-version", type=int)
        if name == "run":
            parser.add_argument("--duration", type=int, default=300, metavar="SECONDS")
        if name == "review":
            parser.add_argument("--failure-number", required=True, type=int)
            parser.add_argument("--failure-hash", required=True)
            parser.add_argument("--actor", required=True, type=UUID)
            parser.add_argument(
                "--resolution",
                required=True,
                choices=(
                    "manual_notification_confirmed",
                    "delivery_loss_accepted",
                ),
            )
            parser.add_argument("--evidence-sha256", required=True)
        if name == "failures":
            parser.add_argument("--after-number", type=int, default=0)
        if name in {"notices", "failures"}:
            parser.add_argument("--limit", type=int, choices=range(1, 101), default=20)


async def execute(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    if args.action == "run" and not 0 <= args.duration <= 86400:
        raise ConfigurationError("duration must be 0..86400 seconds")
    # Register/redact the URL before constructing any HTTP client or database connection.
    webhook = Webhook.load(args.webhook_file) if args.action in {"configure", "run"} else None
    engine = create_engine(settings)
    sender: DiscordSender | None = None
    health: NotificationStatus | None = None
    server: NotificationHealthServer | None = None
    monitor_task: asyncio.Task[None] | None = None
    monitor_engine: AsyncEngine | None = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals: list[signal.Signals] = []
    try:
        await verify_runtime(engine)
        if args.action == "status":
            return await status(engine, args.account)
        if args.action == "notices":
            return await read_notices(engine, args.account, args.limit)
        if args.action == "failures":
            return await read_failures(
                engine, args.account, after_number=args.after_number, limit=args.limit
            )
        if webhook is None and args.action not in {"prepare", "review"}:
            raise ConfigurationError("a notification secret file is required")
        request = (
            ReviewRequest(
                failure_number=args.failure_number,
                failure_hash=args.failure_hash,
                actor=args.actor,
                resolution=args.resolution,
                evidence_sha256=args.evidence_sha256,
            )
            if args.action == "review"
            else None
        )
        async with NotificationOwner(engine, args.account) as owner:
            if request is not None:
                return await owner.review(request)
            if args.action == "prepare":
                return await owner.prepare(datetime.now(UTC))
            if args.action == "configure" and webhook is not None:
                await owner.configure(webhook, datetime.now(UTC), after_version=args.after_version)
            else:
                if webhook is None:
                    raise ConfigurationError("a notification secret file is required")
                sender = DiscordSender(webhook)
                health, metrics = NotificationStatus(args.account), NotificationMetrics()
                server = NotificationHealthServer(health, metrics)
                await server.start(settings.health.host, settings.health.port)
                # Separate pool: observation cannot consume the delivery owner's spare
                # connection while it builds a daily report. HTTP handlers only read cache.
                monitor_engine = create_engine(settings)
                observer_engine = monitor_engine
                monitor = NotificationMonitor(
                    health, metrics, lambda: read_sample(observer_engine, args.account)
                )
                monitor_task = asyncio.create_task(monitor.run(stop))
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, stop.set)
                    signals.append(sig)
                deadline = monotonic() + args.duration if args.duration else None
                next_daily = 0.0
                while not stop.is_set() and (deadline is None or monotonic() < deadline):
                    include_daily = monotonic() >= next_daily
                    prepared = await owner.prepare(datetime.now(UTC), include_daily=include_daily)
                    if include_daily:
                        next_daily = monotonic() + 60
                        health.daily_evidence_unavailable = bool(
                            prepared["daily_evidence_unavailable"]
                        )
                    if prepared["daily_evidence_unavailable"]:
                        structlog.get_logger().error(
                            "notification_summary_evidence_unavailable",
                            account_id=str(args.account),
                        )
                    outcome = await owner.tick(sender, datetime.now(UTC))
                    health.worker_last_success = monotonic()
                    if outcome in {"disabled", "dead_letter", "abandoned"}:
                        structlog.get_logger().error(
                            "notification_delivery_incident",
                            account_id=str(args.account),
                            outcome=outcome,
                        )
                    if outcome == "disabled":
                        break
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=1)
                    except TimeoutError:
                        pass
        result = await status(engine, args.account)
        if health is not None:
            health.shutting_down = True
            result["worker_health"] = health.document()
        return result
    except BaseException:
        if health is not None:
            health.worker_faulted = True
        raise
    finally:
        if health is not None:
            health.shutting_down = True
        stop.set()
        if monitor_task is not None:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
        if server is not None:
            await server.close()
        if monitor_engine is not None:
            await monitor_engine.dispose()
        for sig in signals:
            loop.remove_signal_handler(sig)
        if sender is not None:
            await sender.close()
        await engine.dispose()


def command(args: argparse.Namespace, settings: Settings) -> int:
    result = asyncio.run(execute(args, settings))
    print(json.dumps(result, sort_keys=True))
    if args.action == "prepare":
        return 1 if result["daily_evidence_unavailable"] else 0
    state = result.get("state")
    failed = (isinstance(state, dict) and not state["enabled"]) or result.get(
        "unreviewed_terminal_failures", 0
    ) != 0
    return 1 if args.action == "run" and failed else 0
