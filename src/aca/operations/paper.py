"""Offline paper administration. No key, URL or live-mode option exists."""

import argparse
import asyncio
import csv
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4

from aca.accounting.operations import CorrectionRequest, TransferRequest
from aca.app.paper_supervisor import supervise
from aca.domain.trading import fingerprint
from aca.domain.values import parse_decimal, utc_datetime
from aca.execution.accounting import correct_accounting, external_transfer
from aca.execution.paper import PaperExecution
from aca.market_data.paper_feed import FeedUnavailable, StoredPaperFeed
from aca.observability.notices import daily_summary
from aca.operations.config import RiskPolicy, Settings
from aca.portfolio.reserves import TaxReserveRequest
from aca.reporting.historical import historical_performance
from aca.reporting.paper import performance
from aca.reporting.replay_archive import export_archive, verify_archive
from aca.research.artifacts import read_json
from aca.simulation.historical import HistoricalReplay
from aca.simulation.paper import PaperExchange
from aca.simulation.replay import ReplayPlan, demo_plan, run_replay
from aca.simulation.stored_replay import StoredReplay
from aca.storage.accounting import accounting_status
from aca.storage.archive_format import ArchiveReference
from aca.storage.database import create_engine, verify_runtime
from aca.storage.paper import PaperStore, PaperWriter, read_alerts
from aca.storage.reserves import read_reserves
from aca.storage.strategy import PaperSelection, select_strategy, strategy_status
from aca.strategies.slow import VARIANTS


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
        "valuations",
        "performance",
        "tax-reserve",
        "tax-reserves",
        "create",
        "fund",
        "select",
        "decisions",
        "run",
        "daily",
        "transfer",
        "correct",
        "accounting",
        "archive-export",
        "archive-verify",
        "audit-prefix-accept",
        "history-compact",
    ):
        parser = actions.add_parser(name)
        parser.add_argument("--account", type=UUID, required=True)
        if name in {
            "status",
            "journal",
            "recover",
            "halt",
            "review",
            "supervise",
            "enrol",
            "consume",
            "performance",
            "tax-reserve",
            "fund",
            "select",
            "run",
            "daily",
            "transfer",
            "correct",
        }:
            parser.add_argument(
                "--audit-prefix",
                type=UUID,
                help="explicit operator-sealed financial audit; requires retained SQL evidence",
            )
            parser.add_argument(
                "--history-prefix", type=UUID, help="explicit operator-verified compacted history"
            )
        if name in {"audit-prefix-accept", "history-compact"}:
            parser.add_argument("--actor", type=UUID, required=True)
            parser.add_argument("--reason", required=True)
            parser.add_argument("--expected-version", type=int, required=True)
        if name == "history-compact":
            parser.add_argument("--expected-source", type=int, required=True)
        if name in {"performance", "daily", "consume", "run", "supervise"}:
            parser.add_argument("--archive", type=Path, help="explicit retained replay input")
            parser.add_argument("--archive-sha256", help="independently retained file SHA256")
        if name in {"halt", "review"}:
            parser.add_argument("--reason", required=True)
        if name == "replay":
            parser.add_argument("--input", type=Path, required=True)
        if name in {"transfer", "correct"}:
            parser.add_argument(
                "--input", type=Path, required=True, help="strict versioned JSON operator request"
            )
        if name == "accounting":
            parser.add_argument("--after-version", type=int, default=0)
            parser.add_argument("--limit", type=int, default=20, choices=range(1, 101))
        if name == "archive-export":
            parser.add_argument("--output", type=Path, required=True)
        if name == "archive-verify":
            parser.add_argument("--input", type=Path, required=True)
            parser.add_argument("--sha256", required=True)
        if name in {"supervise", "run"}:
            parser.add_argument("--duration", type=int, default=300, metavar="SECONDS")
        if name == "supervise":
            parser.add_argument(
                "--replay",
                action="store_true",
                help="consume ordered public evidence for an enrolled paper account",
            )
        if name == "select":
            parser.add_argument("--strategy", choices=VARIANTS, required=True)
            parser.add_argument("--symbol", choices=("BTC/GBP", "ETH/GBP"), required=True)
            parser.add_argument("--interval", type=int, choices=(240, 1440), required=True)
            parser.add_argument("--entry-tif", choices=("GTD", "FOK"), default="GTD")
            parser.add_argument("--operational-experiment", action="store_true", required=True)
        if name == "fund":
            parser.add_argument("--reference", required=True)
            parser.add_argument("--amount-gbp", required=True)
            parser.add_argument("--occurred-at", required=True, help="aware ISO UTC timestamp")
        if name == "daily":
            parser.add_argument("--day", type=date.fromisoformat, required=True)
        if name == "consume":
            parser.add_argument("--limit", type=int, default=32, choices=range(1, 65))
        if name == "valuations":
            parser.add_argument("--limit", type=int, default=20, choices=range(1, 101))
            parser.add_argument("--before", type=int)
        if name == "performance":
            parser.add_argument("--start-source", type=int)
            parser.add_argument("--end-source", type=int)
        if name == "tax-reserve":
            parser.add_argument("--request-id", type=UUID, required=True)
            parser.add_argument("--actor", type=UUID, required=True)
            parser.add_argument("--expected-version", type=int, required=True)
            parser.add_argument(
                "--amount-gbp", required=True, help="absolute Decimal-string earmark"
            )
            parser.add_argument("--evidence-sha256", required=True)
            parser.add_argument("--reason", required=True)
        if name == "tax-reserves":
            parser.add_argument("--after-version", type=int, default=0)
            parser.add_argument("--limit", type=int, default=20, choices=range(1, 101))


async def execute(args: argparse.Namespace, settings: Settings) -> dict[str, object] | str:
    if args.action == "history-compact":
        from aca.operations.history import compact_history

        return await compact_history(
            settings,
            args.account,
            args.actor,
            args.reason,
            args.expected_version,
            args.expected_source,
        )
    if args.action == "audit-prefix-accept":
        from aca.storage.audit_prefix import accept_prefix

        return await accept_prefix(
            settings, args.account, args.actor, args.reason, args.expected_version
        )
    audit_prefix = getattr(args, "audit_prefix", None)
    history_prefix = getattr(args, "history_prefix", None)
    archive_path, archive_hash = (
        getattr(args, "archive", None),
        getattr(args, "archive_sha256", None),
    )
    if (archive_path is None) != (archive_hash is None):
        raise ValueError("--archive and --archive-sha256 must be supplied together")
    archive = (
        ArchiveReference(archive_path, archive_hash)
        if archive_path is not None and archive_hash is not None
        else None
    )
    if args.action == "archive-verify":
        verified = verify_archive(args.input, args.sha256)
        context = verified["context"]
        if not isinstance(context, dict) or context["account_id"] != str(args.account):
            raise ValueError("archive does not belong to the requested account")
        return verified
    if args.action in {"supervise", "run"}:
        return await supervise(
            settings,
            args.account,
            args.duration,
            replay_enabled=args.action == "run" or args.replay,
            strategy_enabled=args.action == "run",
            replay_archive=archive,
            audit_prefix=audit_prefix,
            history_prefix=history_prefix,
        )
    engine = create_engine(settings)
    store = PaperStore(
        engine,
        args.account,
        replay_archive=archive,
        audit_prefix=audit_prefix,
        history_prefix=history_prefix,
    )
    try:
        await verify_runtime(engine)
        if args.action == "archive-export":
            return await export_archive(store, args.output)
        if args.action in {"decisions", "daily", "accounting"}:
            from sqlalchemy import text

            async with engine.connect() as c:
                await c.execution_options(isolation_level="REPEATABLE READ")
                await c.execute(text("SET TRANSACTION READ ONLY"))
                if args.action == "decisions":
                    return await strategy_status(c, args.account)
                if args.action == "accounting":
                    return await accounting_status(
                        c, args.account, after_version=args.after_version, limit=args.limit
                    )
                return await daily_summary(c, store, args.day)
        if args.action == "tax-reserves":
            return await read_reserves(
                engine, args.account, after_version=args.after_version, limit=args.limit
            )
        if args.action == "performance":
            return await historical_performance(
                store, start_source=args.start_source, end_source=args.end_source
            )
        if args.action == "valuations":
            from sqlalchemy import select

            from aca.storage.replay import valuations

            query = select(valuations).where(valuations.c.account_id == args.account)
            if args.before is not None:
                if args.before < 0:
                    raise ValueError("valuation cursor must be nonnegative")
                query = query.where(valuations.c.number < args.before)
            history_reader = HistoricalReplay(store, PaperExchange(store), RiskPolicy())
            async with engine.connect() as c:
                await c.execution_options(isolation_level="REPEATABLE READ")
                state = await store.load(c)
                rows = (
                    (await c.execute(query.order_by(valuations.c.number.desc()).limit(args.limit)))
                    .mappings()
                    .all()
                )
            return {
                "schema_version": 1,
                "account_id": str(args.account),
                "items": [history_reader.restore(row).document() for row in rows],
                "next_before": rows[-1]["number"] if len(rows) == args.limit else None,
                "provisional": True,
                "accounting_valid": state is not None and not state.accounting_invalidated,
                "units_valid": state is not None and not state.accounting_invalidated,
                "promotion_evidence": False,
                "live_execution_available": False,
            }
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
                    state = await store.audit(c, full=True)
                    return {
                        "schema_version": 1,
                        "account_id": str(args.account),
                        "complete": state is not None,
                        "accounting_valid": state is not None and not state.accounting_invalidated,
                        "units_valid": state is not None and not state.accounting_invalidated,
                        "version": state.version if state else None,
                        "live_execution_available": False,
                    }
                state = (
                    await store.audit(c) if audit_prefix or history_prefix else await store.load(c)
                )
                history = await store.history(c)
                strategy = await strategy_status(c, args.account)
            if args.action == "status":
                at = datetime.now(UTC)
                return {
                    "schema_version": 1,
                    "mode": "paper",
                    "as_of": at.isoformat(),
                    "state": state.document() if state else None,
                    "performance": performance(state, history, at) if state else None,
                    "strategy": strategy,
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
            if args.action == "create":
                return (await service.initialise(datetime.now(UTC))).document()
            if args.action == "transfer":
                return await external_transfer(
                    service, TransferRequest.restore(read_json(args.input))
                )
            if args.action == "correct":
                return await correct_accounting(
                    service, CorrectionRequest.restore(read_json(args.input))
                )
            if args.action == "fund":
                return (
                    await service.cashflow(
                        args.reference,
                        parse_decimal(args.amount_gbp),
                        utc_datetime(datetime.fromisoformat(args.occurred_at)),
                    )
                ).document()
            if args.action == "select":
                chosen = PaperSelection(
                    account_id=args.account,
                    strategy=args.strategy,
                    symbol=args.symbol,
                    interval_minutes=args.interval,
                    entry_time_in_force=args.entry_tif,
                    selected_at=datetime.now(UTC),
                    policy_hash=fingerprint(RiskPolicy().model_dump(mode="json")),
                )
                return (await select_strategy(owner, chosen)).document()
            if args.action == "tax-reserve":
                return await service.reserve_tax_cash(
                    TaxReserveRequest(
                        request_id=args.request_id,
                        actor=args.actor,
                        expected_version=args.expected_version,
                        amount_gbp=args.amount_gbp,
                        evidence_sha256=args.evidence_sha256,
                        reason=args.reason,
                    )
                )
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
