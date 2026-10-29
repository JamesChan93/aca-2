"""Explicit stopped-worker paper maintenance; migration credentials stay offline."""

import hashlib
import json
from contextlib import aclosing
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import Table, func, select, text

import aca
from aca.domain.trading import fingerprint
from aca.operations.config import RiskPolicy, Settings
from aca.reporting.historical import Period, Totals, _advance, _evidence, read_performance
from aca.reporting.history_prefix import HistoryCertificate, ReportBlock
from aca.reporting.period_cache import PeriodRecord, TotalsRecord
from aca.simulation.historical import HistoricalReplay
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import ReplayReceipt, restore_cursor
from aca.storage.audit_prefix import (
    accept_prefix,
    environment_fingerprint,
    package_fingerprint,
    policy_fingerprint,
    prefixes,
)
from aca.storage.database import create_engine, verify_runtime
from aca.storage.history_blocks import (
    BLOCK_ROWS,
    all_rows,
    blocks,
    certificates,
    decode_rows,
    document_digest,
    encode_rows,
    manifest,
    sql_rows,
)
from aca.storage.models import MarketReceipt, PublicTradeRecord
from aca.storage.paper import PaperStore, checkpoints, events, lock_key, outbox
from aca.storage.replay import (
    NORMALIZED_REPLAY_CHANNELS,
    PUBLIC_REPLAY_LOCK,
    cursors,
    public_events,
    valuations,
)


async def vacuum_history(settings: Settings, account: UUID) -> None:
    """Reclaim dead-row space for reuse; no history deletion or VACUUM FULL.

    Run after cutover, once its read snapshots have closed. The same stopped
    producer/writer rule applies; autocommit is PostgreSQL's VACUUM requirement.
    """
    authority = create_engine(settings, "migration")
    try:
        async with authority.connect() as admin:
            try:
                await admin.execution_options(isolation_level="AUTOCOMMIT")
                if await admin.scalar(text("SELECT current_user")) != "aca_migrator":
                    raise ValueError("history vacuum requires the migration role")
                for key in (lock_key(account), -lock_key(account), PUBLIC_REPLAY_LOCK):
                    if not await admin.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                    ):
                        raise ValueError("stop collector, venue and paper writer before vacuum")
                await admin.execute(
                    text(
                        "VACUUM (ANALYZE) aca.paper_events, aca.paper_replay_valuations, "
                        "aca.paper_replay_cursors, aca.public_replay_events, aca.market_receipts, "
                        "aca.paper_history_blocks"
                    )
                )
            finally:
                await admin.invalidate()
    finally:
        await authority.dispose()


async def compact_history(
    settings: Settings,
    account: UUID,
    actor: UUID,
    reason: str,
    expected_version: int,
    expected_source: int,
    *,
    previous_history: UUID | None = None,
) -> dict[str, Any]:
    """Full re-audit, lossless copy/read-back, then one transactional cutover.

    Failure rolls back every block, certificate and deletion. Journals, fills,
    movements, commands, order identities, controls and checkpoint anchors remain.
    The financial audit record published first is harmless if cutover fails.
    """
    if type(expected_source) is not int or expected_source < 0:
        raise ValueError("history compaction requires an exact expected source")
    financial = await accept_prefix(
        settings, account, actor, reason, expected_version, previous_history=previous_history
    )
    runtime, authority = create_engine(settings), create_engine(settings, "migration")
    store = PaperStore(runtime, account, history_prefix=previous_history)
    certificate_id = uuid4()
    source_hash = package_fingerprint()
    try:
        await verify_runtime(runtime)
        async with authority.connect() as admin:
            try:
                if await admin.scalar(text("SELECT current_user")) != "aca_migrator":
                    raise ValueError("history compaction requires the migration role")
                for key in (lock_key(account), -lock_key(account), PUBLIC_REPLAY_LOCK):
                    if not await admin.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                    ):
                        raise ValueError("stop collector, venue and paper writer before compaction")
                await admin.commit()
                async with runtime.connect() as c:
                    await c.execution_options(
                        isolation_level="REPEATABLE READ", postgresql_readonly=True
                    )
                    # Global public evidence can only be moved for the nominated
                    # single enrolled account. Do not invalidate another reader.
                    enrolled = list(await c.scalars(select(cursors.c.account_id).distinct()))
                    if enrolled != [account]:
                        raise ValueError(
                            "history compaction currently requires one enrolled account"
                        )
                    from aca.reporting.history_prefix import required_history

                    accepted = (await required_history(c, store)) if previous_history else None
                    read = await read_performance(c, store)
                    report = read.report
                    if (
                        report is None
                        or read.aggregate is None
                        or read.financial.accounting_invalidated
                    ):
                        raise ValueError("history compaction requires valid historical accounting")
                    if (
                        read.financial.version != expected_version
                        or report["historical_head_source"] != expected_source
                    ):
                        raise ValueError("history compaction expected head changed")
                    history = HistoricalReplay(store, PaperExchange(store), RiskPolicy())
                    first = (
                        (
                            await c.execute(
                                select(valuations)
                                .where(valuations.c.account_id == account)
                                .order_by(valuations.c.number)
                                .limit(1)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    head = await history.latest(c)
                    cursor_row = (
                        (
                            await c.execute(
                                select(cursors)
                                .where(cursors.c.account_id == account)
                                .order_by(cursors.c.number.desc())
                                .limit(1)
                            )
                        )
                        .mappings()
                        .one()
                    )
                    assert head is not None  # noqa: S101
                    baseline, last = history.restore(first), history.restore(head)
                    # Reconcile the complete cursor chain and original raw receipt
                    # identity, including already compressed source records.
                    sources = all_rows(
                        c, account, "source", accepted.head.number if accepted else -1
                    )
                    source = await anext(sources, None)
                    previous = accepted.cursor_hash if accepted else "0" * 64
                    try:
                        async with aclosing(
                            all_rows(c, account, "cursor", accepted.head.number if accepted else -1)
                        ) as cursor_rows:
                            async for row in cursor_rows:
                                cursor = restore_cursor(row, account, previous)
                                while source is not None and source["number"] < cursor.number:
                                    source = await anext(sources, None)
                                if cursor.number:
                                    if (
                                        source is None
                                        or source["number"] != cursor.number
                                        or source["fingerprint"] != cursor.source_hash
                                    ):
                                        raise ValueError("history cursor/source gap")
                                    receipt = ReplayReceipt.restore(source["document"])
                                    if (
                                        fingerprint(source["document"]) != source["fingerprint"]
                                        or receipt.receipt_id != source["receipt_id"]
                                    ):
                                        raise ValueError("history source identity mismatch")
                                previous = row["fingerprint"]
                    finally:
                        await sources.aclose()
                    if previous != cursor_row["fingerprint"]:
                        raise ValueError("history cursor head mismatch")
                    async with admin.begin():
                        await admin.execute(text("SET LOCAL statement_timeout='30s'"))

                        new_financial_versions: list[list[int]] = []

                        async def put(
                            kind: str, rows: list[dict[str, Any]], document: dict[str, Any]
                        ) -> None:
                            key = "version" if kind == "financial" else "number"
                            payload = encode_rows(rows)
                            record = dict(
                                account_id=account,
                                kind=kind,
                                first=rows[0][key],
                                last=rows[-1][key],
                                count=len(rows),
                                digest=hashlib.sha256(payload).hexdigest(),
                                payload=payload,
                                document=document,
                                document_digest=document_digest(document),
                            )
                            # Before deleting any row, independently decode exactly
                            # what will be stored, under the decompression hard cap.
                            if decode_rows(record) != rows:
                                raise ValueError("lossless history read-back differs")
                            await admin.execute(blocks.insert().values(**record))
                            if kind == "financial":
                                new_financial_versions.append([row["version"] for row in rows])

                        # Compute report blocks with the original audited arithmetic.
                        previous_value = accepted.head if accepted else baseline
                        evidence = await _evidence(c, store, previous_value, last)
                        old_head = await c.scalar(
                            select(func.max(blocks.c.last)).where(
                                blocks.c.account_id == account,
                                blocks.c.kind == "report:" + source_hash,
                            )
                        )
                        old_raw_head = await c.scalar(
                            select(func.max(blocks.c.last)).where(
                                blocks.c.account_id == account, blocks.c.kind == "valuation"
                            )
                        )
                        pending: list[dict[str, Any]] = []
                        period = None
                        before_block, bridge = previous_value, Totals()
                        bridge_flows: tuple[tuple[datetime, Decimal], ...] = ()
                        boundaries = []

                        async def flush() -> None:
                            nonlocal pending, period, boundaries
                            if not pending or period is None:
                                return
                            summary = ReportBlock(
                                period=PeriodRecord.capture(
                                    period, previous_value, pending[-1]["fingerprint"]
                                ),
                                previous=before_block,
                                bridge=TotalsRecord.capture(bridge),
                                bridge_flows=bridge_flows,
                                boundaries=tuple(boundaries),
                            )
                            new_raw = [
                                row
                                for row in pending
                                if old_raw_head is None or row["number"] > old_raw_head
                            ]
                            if new_raw:
                                await put("valuation", new_raw, {})
                            await admin.execute(
                                blocks.insert().values(
                                    account_id=account,
                                    kind="report:" + source_hash,
                                    first=pending[0]["number"],
                                    last=pending[-1]["number"],
                                    count=len(pending),
                                    digest=hashlib.sha256(b"{}").hexdigest(),
                                    payload=b"{}",
                                    document=summary.document(),
                                    document_digest=document_digest(summary.document()),
                                    first_at=summary.period.first.state.as_of,
                                    last_at=summary.period.last.state.as_of,
                                )
                            )
                            pending, period, boundaries = [], None, []

                        async with aclosing(
                            history.audited_values(c, expected_source)
                        ) as economic_rows:
                            async for economic_row, value in economic_rows:
                                totals = period.totals if period is not None else Totals()
                                dated = _advance(previous_value, value, evidence, totals)
                                if old_head is None or value.number > old_head:
                                    if period is None:
                                        period = Period(value, economic_row["fingerprint"])
                                        before_block, bridge, bridge_flows = (
                                            previous_value,
                                            totals,
                                            dated,
                                        )
                                    else:
                                        period.flows.extend(dated)
                                    period.observe(value)
                                    pending.append(dict(economic_row))
                                    if value.day_boundary:
                                        boundaries.append(value.day_boundary)
                                previous_value = value
                                if len(pending) == BLOCK_ROWS:
                                    await flush()
                        await flush()

                        # Cursor/source rows are stored once. Retained endpoint
                        # anchors can coexist with identical compressed originals.
                        for kind in ("cursor", "source"):
                            receipt_cache = {}
                            pending_bytes = 0
                            old = await c.scalar(
                                select(func.max(blocks.c.last)).where(
                                    blocks.c.account_id == account, blocks.c.kind == kind
                                )
                            )
                            pending = []
                            async with aclosing(
                                sql_rows(
                                    c,
                                    account,
                                    kind,
                                    old if old is not None else -1,
                                    expected_source,
                                )
                            ) as retained_rows:
                                async for row in retained_rows:
                                    if kind == "source":
                                        if row["receipt_id"] not in receipt_cache:
                                            raw_rows = (
                                                (
                                                    await c.execute(
                                                        select(cast(Table, MarketReceipt.__table__))
                                                        .join(
                                                            public_events,
                                                            public_events.c.receipt_id
                                                            == MarketReceipt.id,
                                                        )
                                                        .where(
                                                            public_events.c.number >= row["number"],
                                                            public_events.c.number
                                                            <= expected_source,
                                                        )
                                                        .order_by(public_events.c.number)
                                                        .limit(8)
                                                    )
                                                )
                                                .mappings()
                                                .all()
                                            )
                                            receipt_cache = {raw["id"]: raw for raw in raw_rows}
                                        raw = receipt_cache[row["receipt_id"]]
                                        from aca.reporting.replay_archive import ReceiptRow

                                        raw_receipt = ReceiptRow(**dict(raw))
                                        normal = (
                                            raw_receipt.normalized
                                            if raw_receipt.channel in NORMALIZED_REPLAY_CHANNELS
                                            else {}
                                        )
                                        original = ReplayReceipt.restore(row["document"])
                                        if (
                                            hashlib.sha256(raw_receipt.payload.encode()).hexdigest()
                                            != raw_receipt.payload_hash
                                            or original.payload_hash != raw_receipt.payload_hash
                                            or original.epoch != raw_receipt.epoch
                                            or original.received_at != raw_receipt.received_at
                                            or original.valid != raw_receipt.valid
                                            or original.channel != raw_receipt.channel
                                            or original.normalized != normal
                                        ):
                                            raise ValueError("history raw receipt/source mismatch")
                                        row["raw_receipt"] = raw_receipt.document()
                                    row_bytes = len(json.dumps(row, default=str).encode())
                                    if pending and pending_bytes + row_bytes > 1_000_000:
                                        await put(kind, pending, {})
                                        pending, pending_bytes = [], 0
                                    pending.append(row)
                                    pending_bytes += row_bytes
                                    if len(pending) == BLOCK_ROWS:
                                        await put(kind, pending, {})
                                        pending, pending_bytes = [], 0
                            if pending:
                                await put(kind, pending, {})

                        # Only non-journal observation events are moved. Keep every
                        # checkpoint/audit/alert anchor plus the complete load tail.
                        eligible = (
                            select(events)
                            .where(
                                events.c.account_id == account,
                                events.c.kind.in_(("valuation", "reconciled")),
                                events.c.version < expected_version - 128,
                                ~events.c.version.in_(
                                    select(checkpoints.c.version).where(
                                        checkpoints.c.account_id == account
                                    )
                                ),
                                ~events.c.version.in_(
                                    select(prefixes.c.version).where(
                                        prefixes.c.account_id == account
                                    )
                                ),
                                ~events.c.id.in_(
                                    select(outbox.c.event_id).where(outbox.c.account_id == account)
                                ),
                            )
                            .order_by(events.c.version)
                        )
                        # Never buffer the selected financial prefix in memory.
                        number = 0
                        while True:
                            page = [
                                dict(row)
                                for row in (
                                    await c.execute(
                                        eligible.where(events.c.version > number).limit(BLOCK_ROWS)
                                    )
                                ).mappings()
                            ]
                            if not page:
                                break
                            for row in page:
                                if row["document"].get("journal_id") or row["document"].get(
                                    "journal_ids"
                                ):
                                    raise ValueError(
                                        "observation event unexpectedly owns a journal"
                                    )
                            await put(
                                "financial", page, {"versions": [row["version"] for row in page]}
                            )
                            number = page[-1]["version"]

                        catalogue_hash, catalogue_count = await manifest(admin, account)
                        if package_fingerprint() != source_hash:
                            raise ValueError("history code changed during audit")
                        certificate = HistoryCertificate(
                            id=certificate_id,
                            predecessor=previous_history,
                            account_id=account,
                            recorded_at=datetime.now(UTC),
                            actor=actor,
                            reason=reason,
                            application_version=aca.__version__,
                            package_sha256=source_hash,
                            environment_hash=environment_fingerprint(),
                            policy_hash=policy_fingerprint(),
                            financial_prefix=UUID(str(financial["id"])),
                            head=last,
                            head_hash=head["fingerprint"],
                            cursor=restore_cursor(cursor_row, account),
                            cursor_hash=cursor_row["fingerprint"],
                            baseline=baseline,
                            baseline_hash=first["fingerprint"],
                            catalogue_hash=catalogue_hash,
                            catalogue_count=catalogue_count,
                            full_period=PeriodRecord.capture(
                                read.aggregate, last, head["fingerprint"]
                            ),
                        )
                        await admin.execute(
                            certificates.insert().values(
                                id=certificate.id,
                                account_id=account,
                                number=expected_source,
                                fingerprint=fingerprint(certificate.document()),
                                document=certificate.document(),
                            )
                        )

                        # Operator-only DDL is transactional. The runtime still has
                        # neither DDL nor DELETE, and rollback restores all triggers.
                        moved = {}
                        for table in (
                            events,
                            valuations,
                            cursors,
                            public_events,
                            cast(Table, MarketReceipt.__table__),
                        ):
                            await admin.execute(
                                text(
                                    f"ALTER TABLE aca.{table.name} DISABLE TRIGGER reject_mutation"
                                )
                            )
                        moved["financial"] = 0
                        for versions in new_financial_versions:
                            result = await admin.execute(
                                events.delete().where(
                                    events.c.account_id == account,
                                    events.c.version.in_(versions),
                                )
                            )
                            moved["financial"] += result.rowcount
                        for kind, table in (("valuation", valuations), ("cursor", cursors)):
                            result = await admin.execute(
                                table.delete().where(
                                    table.c.account_id == account,
                                    table.c.number > baseline.number,
                                    table.c.number < expected_source,
                                )
                            )
                            moved[kind] = result.rowcount
                        # Keep source endpoints and receipts referenced by the
                        # normalized trade store. Their original bytes also remain
                        # in source blocks for independent reconstruction.
                        removable = select(public_events.c.receipt_id).where(
                            public_events.c.number > baseline.number,
                            public_events.c.number < expected_source,
                        )
                        receipt_ids = list(await admin.scalars(removable))
                        result = await admin.execute(
                            public_events.delete().where(
                                public_events.c.number > baseline.number,
                                public_events.c.number < expected_source,
                            )
                        )
                        moved["source"] = result.rowcount
                        moved["raw_receipt"] = 0
                        for offset in range(0, len(receipt_ids), BLOCK_ROWS):
                            result = await admin.execute(
                                cast(Table, MarketReceipt.__table__)
                                .delete()
                                .where(
                                    MarketReceipt.id.in_(receipt_ids[offset : offset + BLOCK_ROWS]),
                                    ~MarketReceipt.id.in_(select(PublicTradeRecord.receipt_id)),
                                )
                            )
                            moved["raw_receipt"] += result.rowcount
                        await admin.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
                        for table in (
                            events,
                            valuations,
                            cursors,
                            public_events,
                            cast(Table, MarketReceipt.__table__),
                        ):
                            await admin.execute(
                                text(f"ALTER TABLE aca.{table.name} ENABLE TRIGGER reject_mutation")
                            )
                return {
                    "certificate": certificate.document(),
                    "certificate_fingerprint": fingerprint(certificate.document()),
                    "moved_rows": moved,
                    "lossless": True,
                    "journals_or_ids_removed": 0,
                    "live_execution_available": False,
                    "promotion_evidence": False,
                }
            finally:
                await admin.invalidate()
    finally:
        await runtime.dispose()
        await authority.dispose()
