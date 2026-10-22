"""Protected historical economics, mergeable report blocks and bounded suffixes."""

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Literal, Self, cast
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

import aca
from aca.domain.trading import Record, Time, fingerprint
from aca.domain.values import FinancialDecimal
from aca.reporting.historical import CalendarWindow, Period, Totals, _advance, _evidence
from aca.reporting.period_cache import PeriodRecord, TotalsRecord, add_totals, merge
from aca.simulation.day_boundary import DayBoundary
from aca.simulation.historical import HistoricalReplay, HistoricalValuation
from aca.simulation.stored_replay import ReplayCursor
from aca.storage.audit_prefix import (
    environment_fingerprint,
    package_fingerprint,
    policy_fingerprint,
)
from aca.storage.history_blocks import all_rows, blocks, certificates, document_digest, manifest
from aca.storage.paper import PaperStore
from aca.storage.replay import cursors, valuations

MAX_ONLINE_SUFFIX = 4096


class ReportBlock(Record):
    period: PeriodRecord
    previous: HistoricalValuation
    bridge: TotalsRecord
    bridge_flows: tuple[tuple[Time, FinancialDecimal], ...]
    boundaries: tuple[DayBoundary, ...]


class HistoryCertificate(Record):
    format: Literal["paper-history-v1"] = "paper-history-v1"
    id: UUID
    account_id: UUID
    recorded_at: Time
    actor: UUID
    reason: str = Field(min_length=10, max_length=240)
    application_version: str
    package_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    financial_prefix: UUID
    head: HistoricalValuation
    head_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    cursor: ReplayCursor
    cursor_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline: HistoricalValuation
    baseline_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalogue_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalogue_count: int = Field(ge=1, le=100000)
    full_period: PeriodRecord
    storage_compaction_authorised: Literal[True] = True
    original_financial_history_preserved: Literal[True] = True
    permanent_order_ids_preserved: Literal[True] = True
    economic_audit_version: Literal["historical-performance-v3"] = "historical-performance-v3"
    live_execution_available: Literal[False] = False
    promotion_evidence: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.full_period.first != self.baseline
            or self.full_period.last != self.head
            or self.full_period.first_hash != self.baseline_hash
            or self.full_period.last_hash != self.head_hash
        ):
            raise ValueError("historical certificate aggregate endpoints differ")
        return self


async def required_history(c: AsyncConnection, store: PaperStore) -> HistoryCertificate:
    transaction = c.get_transaction()
    cache = c.info.get("aca_history_certificate")
    key = (store.account_id, store.history_prefix, transaction)
    if transaction is not None and cache is not None and cache[0] == key:
        record = cast(HistoryCertificate, cache[1])
        if store.audit_prefix is not None and store.audit_prefix != record.financial_prefix:
            raise ValueError("historical and financial certificates differ")
        store.audit_prefix = record.financial_prefix
        return record
    if store.history_prefix is None:
        raise ValueError("explicit historical certificate required after compaction")
    if not await c.scalar(
        text(
            "SELECT current_user='aca_runtime' AND NOT "
            "has_any_column_privilege('aca.paper_history_certificates','INSERT') AND NOT "
            "has_any_column_privilege('aca.paper_history_blocks','INSERT')"
        )
    ):
        raise ValueError("history reader must have no acceptance authority")
    row = (
        (await c.execute(select(certificates).where(certificates.c.id == store.history_prefix)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("required historical certificate missing")
    record = HistoryCertificate.restore(row["document"])
    if (
        record.id != row["id"]
        or record.account_id != row["account_id"]
        or record.account_id != store.account_id
        or record.head.number != row["number"]
        or fingerprint(record.document()) != row["fingerprint"]
        or record.application_version != aca.__version__
        or record.package_sha256 != package_fingerprint()
        or record.policy_hash != policy_fingerprint()
        or record.environment_hash != environment_fingerprint()
        or record.cursor.number != record.head.number
        or record.cursor.account_id != store.account_id
        or record.head.state.account_id != store.account_id
        or record.baseline.state.account_id != store.account_id
        or record.cursor.source_hash != record.head.source_hash
    ):
        raise ValueError("historical certificate identity/code/policy mismatch")
    root, count = await manifest(c, store.account_id)
    if root != record.catalogue_hash or count != record.catalogue_count:
        raise ValueError("historical catalogue missing or changed; select its current certificate")
    # Verify compressed bytes in PostgreSQL without expanding the historical prefix.
    if (
        await c.scalar(
            select(blocks.c.first)
            .where(
                blocks.c.account_id == store.account_id,
                (func.encode(func.sha256(blocks.c.payload), "hex") != blocks.c.digest)
                | (document_digest(blocks.c.document) != blocks.c.document_digest),
            )
            .limit(1)
        )
        is not None
    ):
        raise ValueError("historical block payload digest mismatch")
    for table, hashed in ((valuations, record.head_hash), (cursors, record.cursor_hash)):
        anchor = (
            (
                await c.execute(
                    select(table).where(
                        table.c.account_id == store.account_id,
                        table.c.number == record.head.number,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if anchor is None or anchor["fingerprint"] != hashed:
            raise ValueError("historical certificate retained head missing")
        name = "valuation" if table is valuations else "cursor"
        if fingerprint({"previous": anchor["previous_hash"], name: anchor["document"]}) != hashed:
            raise ValueError("historical certificate retained head corrupt")
        expected = record.head.document() if table is valuations else record.cursor.document()
        restored = (
            HistoricalValuation.restore(anchor["document"])
            if table is valuations
            else ReplayCursor.restore(anchor["document"])
        )
        if restored.document() != expected:
            raise ValueError("historical certificate retained state mismatch")
    if store.audit_prefix is not None and store.audit_prefix != record.financial_prefix:
        raise ValueError("historical and financial certificates differ")
    store.audit_prefix = record.financial_prefix
    c.info["aca_history_certificate"] = (
        (store.account_id, store.history_prefix, c.get_transaction()),
        record,
    )
    return record


async def suffix_budget(
    c: AsyncConnection,
    store: PaperStore,
    record: HistoryCertificate | None,
    *,
    new_commitment: bool = False,
) -> None:
    from aca.storage.audit_prefix import prefixes
    from aca.storage.paper import events

    version = (
        await c.scalar(select(prefixes.c.version).where(prefixes.c.id == record.financial_prefix))
        if record is not None
        else 0
    )
    head = await c.scalar(
        select(func.max(events.c.version)).where(events.c.account_id == store.account_id)
    )
    count = await c.scalar(
        select(func.count())
        .select_from(valuations)
        .where(
            valuations.c.account_id == store.account_id,
            valuations.c.number > (record.head.number if record is not None else -1),
        )
    )
    financial_limit = MAX_ONLINE_SUFFIX - 32 if new_commitment else MAX_ONLINE_SUFFIX
    if (
        version is None
        or head is None
        or head < version
        or head - version > financial_limit
        or (count or 0) > MAX_ONLINE_SUFFIX
    ):
        raise ValueError("historical suffix budget exceeded; stop and compact before continuing")


async def prefix_period(
    c: AsyncConnection,
    store: PaperStore,
    reader: HistoricalReplay,
    record: HistoryCertificate,
    lower: int,
    upper: int,
    calendar: CalendarWindow | None,
) -> tuple[Period | None, Mapping[str, Any] | None]:
    if calendar is None and lower == record.baseline.number and upper >= record.head.number:
        return record.full_period.period(), {
            "number": record.head.number,
            "fingerprint": record.head_hash,
            "document": record.head.document(),
        }
    query = select(blocks).where(
        blocks.c.account_id == store.account_id, blocks.c.kind == "report:" + record.package_sha256
    )
    if calendar:
        query = query.where(
            blocks.c.last_at >= calendar.start,
            blocks.c.first_at <= calendar.end + timedelta(seconds=5),
        )
    else:
        query = query.where(blocks.c.last >= lower, blocks.c.first <= upper)
    rows = (await c.execute(query.order_by(blocks.c.first))).mappings().all()
    period: Period | None = None
    end: Mapping[str, Any] | None = None
    for row in rows:
        summary = ReportBlock.restore(row["document"])
        p = summary.period
        if calendar:
            for boundary in summary.boundaries:
                if boundary.at == calendar.start:
                    calendar.opening = boundary
                if boundary.at == calendar.end:
                    calendar.closing = boundary
        whole = (
            (calendar.start <= p.first.state.as_of and p.last.state.as_of < calendar.end)
            if calendar
            else lower <= row["first"] and row["last"] <= upper
        )
        if whole:
            if period is None:
                period = p.period()
            else:
                add_totals(period.totals, summary.bridge.totals())
                period.flows.extend(summary.bridge_flows)
                merge(period, p)
            if calendar:
                calendar.first = calendar.first or p.first
                calendar.last = p.last
            end = {
                "fingerprint": p.last_hash,
                "number": p.last.number,
                "document": p.last.document(),
            }
            continue
        previous = summary.previous
        evidence = await _evidence(c, store, previous, p.last)
        async for item in all_rows(c, store.account_id, "valuation", row["first"] - 1, row["last"]):
            value = reader.restore(item)
            selected = calendar.observe(value) if calendar else lower <= value.number <= upper
            totals = period.totals if selected and period is not None else Totals()
            dated = _advance(previous, value, evidence, totals)
            if selected:
                if period is None:
                    period = Period(value, item["fingerprint"])
                else:
                    period.flows.extend(dated)
                period.observe(value)
                end = item
            previous = value
    if calendar is None and period is not None:
        if period.start.number != lower or (
            upper <= record.head.number and (end is None or end["number"] != upper)
        ):
            raise ValueError("performance endpoint must name an exact recorded source")
    return period, end
