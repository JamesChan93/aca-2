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

from aca.observability.delivery import NotificationOwner, status
from aca.observability.discord import DiscordSender, Webhook
from aca.operations.config import ConfigurationError, Settings
from aca.storage.database import create_engine, verify_runtime


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    actions = commands.add_parser("notifications").add_subparsers(dest="action", required=True)
    for name in ("configure", "run", "status"):
        parser = actions.add_parser(name)
        parser.add_argument("--account", required=True, type=UUID)
        if name != "status":
            parser.add_argument("--webhook-file", required=True, type=Path)
        if name == "configure":
            parser.add_argument("--after-version", type=int)
        if name == "run":
            parser.add_argument("--duration", type=int, default=300, metavar="SECONDS")


async def execute(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    if args.action == "run" and not 0 <= args.duration <= 86400:
        raise ConfigurationError("duration must be 0..86400 seconds")
    # Register/redact the URL before constructing any HTTP client or database connection.
    webhook = Webhook.load(args.webhook_file) if args.action != "status" else None
    engine = create_engine(settings)
    sender: DiscordSender | None = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals: list[signal.Signals] = []
    try:
        await verify_runtime(engine)
        if args.action == "status":
            return await status(engine, args.account)
        if webhook is None:
            raise ConfigurationError("a notification secret file is required")
        async with NotificationOwner(engine, args.account) as owner:
            if args.action == "configure":
                await owner.configure(webhook, datetime.now(UTC), after_version=args.after_version)
            else:
                sender = DiscordSender(webhook)
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, stop.set)
                    signals.append(sig)
                deadline = monotonic() + args.duration if args.duration else None
                while not stop.is_set() and (deadline is None or monotonic() < deadline):
                    outcome = await owner.tick(sender, datetime.now(UTC))
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
        return await status(engine, args.account)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        if sender is not None:
            await sender.close()
        await engine.dispose()


def command(args: argparse.Namespace, settings: Settings) -> int:
    result = asyncio.run(execute(args, settings))
    print(json.dumps(result, sort_keys=True))
    state = result.get("state")
    failed = (isinstance(state, dict) and not state["enabled"]) or result["terminal_failures"] != 0
    return 1 if args.action == "run" and failed else 0
