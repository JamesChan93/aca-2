"""Offline paper administration. No key, URL or live-mode option exists."""

import argparse
import asyncio
import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from aca.app.paper_supervisor import supervise
from aca.execution.paper import PaperExecution
from aca.market_data.paper_feed import FeedUnavailable, StoredPaperFeed
from aca.operations.config import Settings
from aca.reporting.paper import performance
from aca.research.artifacts import read_json
from aca.simulation.paper import PaperExchange
from aca.simulation.replay import ReplayPlan, demo_plan, run_replay
from aca.simulation.stored_replay import StoredReplay
from aca.storage.database import create_engine, verify_runtime
from aca.storage.paper import PaperStore, PaperWriter, read_alerts


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    actions = commands.add_parser("paper").add_subparsers(dest="action", required=True)
    for name in (
        "status",
        "journal",
        "alerts",
        "recover",
        "halt",
        "review",
        "demo",
        "replay",
        "audit",
        "supervise",
        "enrol",
        "consume",
    ):
        parser = actions.add_parser(name)
        parser.add_argument("--account", type=UUID, required=True)
        if name in {"halt", "review"}:
            parser.add_argument("--reason", required=True)
        if name == "replay":
            parser.add_argument("--input", type=Path, required=True)
        if name == "supervise":
            parser.add_argument("--duration", type=int, default=300, metavar="SECONDS")
            parser.add_argument(
                "--replay",
                action="store_true",
                help="consume ordered public evidence for an enrolled paper account",
            )
        if name == "consume":
            parser.add_argument("--limit", type=int, default=32, choices=range(1, 65))


async def execute(args: argparse.Namespace, settings: Settings) -> dict[str, object] | str:
    if args.action == "supervise":
        return await supervise(settings, args.account, args.duration, replay_enabled=args.replay)
    engine = create_engine(settings)
    store = PaperStore(engine, args.account)
    try:
        await verify_runtime(engine)
        if args.action == "alerts":
            return {
                "schema_version": 1,
                "items": await read_alerts(store),
                "live_execution_available": False,
            }
        if args.action in {"status", "journal", "audit"}:
            async with engine.connect() as c:
                await c.execution_options(isolation_level="REPEATABLE READ")
                if args.action == "audit":
                    state = await store.audit(c)
                    return {
                        "schema_version": 1,
                        "account_id": str(args.account),
                        "complete": state is not None,
                        "version": state.version if state else None,
                        "live_execution_available": False,
                    }
                state = await store.load(c)
                history = await store.history(c)
            if args.action == "status":
                at = datetime.now(UTC)
                return {
                    "schema_version": 1,
                    "mode": "paper",
                    "as_of": at.isoformat(),
                    "state": state.document() if state else None,
                    "performance": performance(state, history, at) if state else None,
                    "live_execution_available": False,
                }
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(
                (
                    "journal_id",
                    "source",
                    "kind",
                    "occurred_at_utc",
                    "account",
                    "gbp_debit_signed",
                    "reverses",
                    "reason",
                )
            )
            for journal in history:
                for line in journal.lines:
                    writer.writerow(
                        (
                            journal.id,
                            journal.source,
                            journal.kind,
                            journal.occurred_at.isoformat(),
                            line.account,
                            str(line.gbp),
                            journal.reverses or "",
                            journal.reason,
                        )
                    )
            return output.getvalue()
        async with PaperWriter(store) as owner:
            venue = PaperExchange(store)
            service = PaperExecution(owner, venue)
            if args.action == "enrol":
                cursor = await StoredReplay(service, venue).enrol()
                return {
                    "cursor": cursor.document(),
                    "live_execution_available": False,
                    "requires_new_capture_epoch": True,
                    "promotion_evidence": False,
                }
            if args.action == "consume":
                replay = StoredReplay(service, venue)
                await replay.audit()
                at = datetime.now(UTC)
                result = await replay.batch(limit=args.limit, at=at)
                reason = "paper_replay_backlog" if result.more else None
                if result.reason and reason is None:
                    reason = "paper_replay_unavailable"
                try:
                    marks = await StoredPaperFeed(store).marks(at)
                    await service.mark(marks, at)
                except FeedUnavailable:
                    reason = "paper_feed_unavailable"
                reconciliation = await service.reconcile(at)
                if reason:
                    state = await service._state()
                    await service._save(
                        state,
                        state.model_copy(update={"ready": False, "incident": reason}),
                        at=at,
                        kind=reason,
                    )
                return {
                    "batch": result.document(),
                    "reconciliation": reconciliation.document(),
                    "live_execution_available": False,
                    "promotion_evidence": False,
                }
            if args.action in {"demo", "replay"}:
                plan = (
                    demo_plan(uuid4())
                    if args.action == "demo"
                    else ReplayPlan.restore(read_json(args.input))
                )
                return await run_replay(service, venue, plan)
            at = datetime.now(UTC)
            if args.action == "recover":
                return (await service.recover(at)).document()
            if args.action == "halt":
                await service.halt(args.reason, at)
            else:
                await service.review(args.reason, at)
            return (await service._state()).document()
    finally:
        await engine.dispose()


def command(args: argparse.Namespace, settings: Settings) -> int:
    output = asyncio.run(execute(args, settings))
    print(output if isinstance(output, str) else json.dumps(output, sort_keys=True))
    return 0
