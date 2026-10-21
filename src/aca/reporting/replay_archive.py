"""Audited, lossless replay exports. These files cannot authorise prefix pruning.

Financial journals, permanent IDs and all operational rows remain authoritative
in PostgreSQL. Offline verification does not re-run the financial reconstruction.
"""

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import Field
from sqlalchemy import and_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from aca import __version__
from aca.domain.trading import Record, Time, fingerprint
from aca.operations.config import RiskPolicy, StrictModel
from aca.reporting.historical import read_performance
from aca.simulation.historical import EMPTY_HASH, HistoricalValuation, restore_valuation
from aca.simulation.stored_replay import ReplayReceipt, restore_cursor
from aca.storage.archive_format import ArchiveReader, ArchiveWriter
from aca.storage.models import MarketReceipt
from aca.storage.paper import PaperStore, events
from aca.storage.replay import NORMALIZED_REPLAY_CHANNELS, cursors, public_events, valuations

EXPORT_PAGE = 8  # Includes original raw payloads, unlike the valuation-only audit page.
Hash = str


class ChainRow(StrictModel):
    account_id: UUID
    number: int = Field(ge=0)
    previous_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    fingerprint: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    document: dict[str, Any]


class SourceRow(StrictModel):
    number: int = Field(ge=1)
    receipt_id: UUID
    fingerprint: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    published_at: Time
    document: dict[str, Any]


class ReceiptRow(Record):
    id: UUID
    epoch: UUID
    received_at: Time
    channel: str = Field(max_length=80)
    valid: bool
    reason: str | None = Field(max_length=80)
    payload_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    payload: str
    normalized: dict[str, Any]


class Observation(Record):
    valuation: ChainRow
    cursor: ChainRow | None
    source: SourceRow | None
    receipt: ReceiptRow | None


class Context(Record):
    kind: Literal["replay-preservation-v1"] = "replay-preservation-v1"
    account_id: UUID
    application_version: str = Field(max_length=32)
    created_at: Time
    policy_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    financial_version: int = Field(ge=1)
    financial_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    financial_state_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_number: int = Field(ge=0)
    head_number: int = Field(ge=0)
    head_valuation_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    head_cursor_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    opening_cursor_previous_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    valuation_count: int = Field(ge=1, le=100000)
    cursor_count: int = Field(ge=1, le=100000)
    database_audit_report_hash: Hash = Field(pattern=r"^[a-f0-9]{64}$")
    database_semantics_checked_at_export: Literal[True] = True
    usable_as_operational_prefix: Literal[False] = False
    pruning_authorised: Literal[False] = False
    promotion_evidence: Literal[False] = False
    live_execution_available: Literal[False] = False


class EvidenceValidator:
    """Bounded structural/source validation, distinct from the database audit."""

    def __init__(self, context: Context) -> None:
        self.context = context
        self.previous: HistoricalValuation | None = None
        self.value_hash = EMPTY_HASH
        self.cursor_hash = context.opening_cursor_previous_hash
        self.cursor_number: int | None = None
        self.count = self.cursor_count = 0

    def observe(self, observation: Observation) -> HistoricalValuation:
        context, row = self.context, observation.valuation
        if row.account_id != context.account_id:
            raise ValueError("archive valuation account mismatch")
        value = restore_valuation(
            row.model_dump(), context.account_id, context.policy_hash, self.value_hash
        )
        previous = self.previous
        if previous is None:
            if (
                value.number != context.baseline_number
                or value.reason != "baseline"
                or value.source_received_at is not None
                or observation.cursor is None
            ):
                raise ValueError("archive baseline mismatch")
        elif (
            value.number <= previous.number
            or value.number > context.head_number
            or value.state.as_of < previous.state.as_of
            or value.control_version < previous.control_version
            or value.fill_number < previous.fill_number
            or (previous.incomplete_since and value.incomplete_since != previous.incomplete_since)
        ):
            raise ValueError("archive observation order/chronology mismatch")
        if value.day_boundary is not None:
            boundary = value.day_boundary
            if (
                previous is None
                or boundary.previous_source != previous.number
                or boundary.previous_source_hash != previous.source_hash
                or boundary.previous_valuation_hash != self.value_hash
                or not previous.state.as_of < boundary.at
            ):
                raise ValueError("archive midnight previous anchor mismatch")
        source, receipt = observation.source, observation.receipt
        if value.number == 0:
            if source is not None or receipt is not None or value.source_hash != EMPTY_HASH:
                raise ValueError("archive zero baseline has a source")
        else:
            if source is None or receipt is None:
                raise ValueError("archive source/receipt missing")
            replay_receipt = ReplayReceipt.restore(source.document)
            normalized = receipt.normalized if receipt.channel in NORMALIZED_REPLAY_CHANNELS else {}
            if (
                source.number != value.number
                or fingerprint(source.document) != source.fingerprint
                or source.fingerprint != value.source_hash
                or source.receipt_id != replay_receipt.receipt_id
                or source.receipt_id != receipt.id
                or len(receipt.payload.encode()) > 2_000_000
                or hashlib.sha256(receipt.payload.encode()).hexdigest() != receipt.payload_hash
                or replay_receipt.payload_hash != receipt.payload_hash
                or replay_receipt.epoch != receipt.epoch
                or replay_receipt.received_at != receipt.received_at
                or replay_receipt.channel != receipt.channel
                or replay_receipt.valid != receipt.valid
                or replay_receipt.normalized != normalized
                or (previous is not None and value.source_received_at != receipt.received_at)
            ):
                raise ValueError("archive public source/raw receipt binding mismatch")
        cursor = observation.cursor
        if cursor is not None:
            restored = restore_cursor(cursor.model_dump(), context.account_id, self.cursor_hash)
            if (
                cursor.account_id != context.account_id
                or restored.number != value.number
                or restored.source_hash != value.source_hash
                or (self.cursor_number is not None and restored.number <= self.cursor_number)
            ):
                raise ValueError("archive cursor/source mismatch")
            self.cursor_count += 1
            self.cursor_hash, self.cursor_number = cursor.fingerprint, cursor.number
        self.previous, self.value_hash = value, row.fingerprint
        self.count += 1
        return value

    def finish(self) -> None:
        c = self.context
        if (
            self.previous is None
            or self.previous.number != c.head_number
            or self.value_hash != c.head_valuation_hash
            or self.cursor_number != c.head_number
            or self.cursor_hash != c.head_cursor_hash
            or self.count != c.valuation_count
            or self.cursor_count != c.cursor_count
        ):
            raise ValueError("archive terminal anchors/counts mismatch")


def verify_archive(path: Path, expected_sha256: str) -> dict[str, object]:
    reader = ArchiveReader(path, expected_sha256)
    iterator = reader.rows()
    try:
        header = next(iterator, None)
        if header is None or set(header) != {"context"} or not isinstance(header["context"], dict):
            raise ValueError("archive context header missing")
        context = Context.restore(header["context"])
        if context.policy_hash != fingerprint(RiskPolicy().model_dump(mode="json")):
            raise ValueError("archive policy is unsupported")
        validator = EvidenceValidator(context)
        for document in iterator:
            validator.observe(Observation.restore(document))
        validator.finish()
        if (
            not reader.verified
            or reader.manifest is None
            or reader.manifest["context"] != {"header_hash": fingerprint(header)}
        ):
            raise ValueError("archive manifest/header mismatch")
    finally:
        iterator.close()
    return {
        "schema_version": 1,
        "archive_sha256": expected_sha256,
        "integrity_verified": True,
        "source_bindings_verified": True,
        "database_semantics_rechecked": False,
        "financial_records_included": False,
        "usable_as_operational_prefix": False,
        "pruning_authorised": False,
        "context": context.document(),
        "segments": reader.manifest["segments"],
        "archive_bytes": reader.bytes_read,
        "decoded_segment_bytes": reader.decoded_bytes,
        "promotion_evidence": False,
        "live_execution_available": False,
    }


async def _observations(
    c: AsyncConnection, store: PaperStore, baseline: int, head: int
) -> AsyncIterator[Observation]:
    tables = {"v": valuations, "c": cursors, "s": public_events, "r": MarketReceipt.__table__}
    query = (
        select(
            *(column.label(f"{key}_{column.name}") for key, t in tables.items() for column in t.c)
        )
        .select_from(valuations)
        .outerjoin(
            cursors,
            and_(
                cursors.c.account_id == valuations.c.account_id,
                cursors.c.number == valuations.c.number,
            ),
        )
        .outerjoin(public_events, public_events.c.number == valuations.c.number)
        .outerjoin(MarketReceipt.__table__, MarketReceipt.id == public_events.c.receipt_id)
        .where(valuations.c.account_id == store.account_id, valuations.c.number <= head)
        .order_by(valuations.c.number)
        .limit(EXPORT_PAGE)
    )
    number = baseline - 1
    while True:
        rows = (await c.execute(query.where(valuations.c.number > number))).mappings().all()
        for row in rows:

            def fields(key: str, row: RowMapping = row) -> dict[str, Any]:
                return {col.name: row[f"{key}_{col.name}"] for col in tables[key].c}

            yield Observation(
                valuation=ChainRow(**fields("v")),
                cursor=ChainRow(**fields("c")) if row["c_number"] is not None else None,
                source=SourceRow(**fields("s")) if row["s_number"] is not None else None,
                receipt=ReceiptRow(**fields("r")) if row["r_id"] is not None else None,
            )
            number = row["v_number"]
        if len(rows) < EXPORT_PAGE:
            return


async def _cursor_prefix(
    c: AsyncConnection, store: PaperStore, baseline: int, head: int
) -> tuple[str, int, str]:
    number, previous, opening, count, total = -1, EMPTY_HASH, None, 0, 0
    while True:
        rows = (
            (
                await c.execute(
                    select(cursors)
                    .where(
                        cursors.c.account_id == store.account_id,
                        cursors.c.number > number,
                    )
                    .order_by(cursors.c.number)
                    .limit(256)
                )
            )
            .mappings()
            .all()
        )
        anchors = (
            dict(
                (
                    await c.execute(
                        select(public_events.c.number, public_events.c.fingerprint).where(
                            public_events.c.number.in_([r["number"] for r in rows])
                        )
                    )
                ).all()
            )
            if rows
            else {}
        )
        for row in rows:
            total += 1
            if total > 100000:
                raise ValueError("archive cursor prefix exceeds evidence budget")
            cursor = restore_cursor(row, store.account_id, previous)
            if cursor.number and anchors.get(cursor.number) != cursor.source_hash:
                raise ValueError("archive cursor source anchor mismatch")
            if cursor.number == baseline:
                opening = previous
            if cursor.number >= baseline:
                count += 1
            number, previous = cursor.number, row["fingerprint"]
        if len(rows) < 256:
            break
    if opening is None or number != head:
        raise ValueError("archive cursor baseline/head missing")
    return opening, count, previous


async def export_archive(store: PaperStore, destination: Path) -> dict[str, object]:
    """Pin, audit and export a baseline-forward snapshot using the runtime reader.

    Nothing is written to PostgreSQL and no exchange/simulation command is sent.
    A separate verifier checks the completed temporary file before publication.
    """
    async with store.engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        read = await read_performance(c, store)
        report = read.report
        if report is None or read.financial.accounting_invalidated:
            raise ValueError("archive requires valid enrolled historical accounting")
        baseline = cast(dict[str, Any], report["baseline"])["source_number"]
        head = cast(int, report["historical_head_source"])
        opening, cursor_count, cursor_hash = await _cursor_prefix(c, store, baseline, head)
        financial_hash = await c.scalar(
            select(events.c.fingerprint).where(
                events.c.account_id == store.account_id, events.c.version == read.financial.version
            )
        )
        context = Context(
            account_id=store.account_id,
            application_version=__version__,
            created_at=datetime.now(UTC),
            policy_hash=cast(str, report["policy_hash"]),
            financial_version=read.financial.version,
            financial_hash=cast(str, financial_hash),
            financial_state_hash=fingerprint(read.financial.document()),
            baseline_number=baseline,
            head_number=head,
            head_valuation_hash=cast(str, report["historical_head_hash"]),
            head_cursor_hash=cursor_hash,
            opening_cursor_previous_hash=opening,
            valuation_count=cast(dict[str, Any], report["period"])["observations"],
            cursor_count=cursor_count,
            database_audit_report_hash=cast(str, report["report_hash"]),
        )
        header: dict[str, object] = {"context": context.document()}
        validator = EvidenceValidator(context)
        with ArchiveWriter(destination) as archive:
            archive.append(header)
            async for observation in _observations(c, store, baseline, head):
                validator.observe(observation)
                archive.append(observation.document())
            validator.finish()
            hashed = archive.finish({"header_hash": fingerprint(header)})
            if archive.temporary is None:
                raise ValueError("archive temporary file missing")
            result = verify_archive(archive.temporary, hashed)
            archive.publish()
            return {**result, "database_semantics_checked_at_export": True}
