"""Immutable operator evidence sealed to a transfer or correction event."""

from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import Column, Integer, MetaData, String, Table, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.accounting.ledger import reverse
from aca.accounting.operations import (
    CorrectionRequest,
    TransferRequest,
    flow_journal,
    transfer_projection,
)
from aca.domain.trading import Journal, PortfolioSnapshot, Record, Time, fingerprint

operations = Table(
    "paper_accounting_operations",
    MetaData(schema="aca"),
    Column("account_id", Uuid, primary_key=True),
    Column("request_id", Uuid, primary_key=True),
    Column("event_version", Integer, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("external_reference", String(120)),
    Column("target_journal", Uuid),
    Column("document", JSONB, nullable=False),
    Column("fingerprint", String(64), nullable=False),
)


class AccountingRecord(Record):
    account_id: UUID
    request_id: UUID
    event_version: int = Field(ge=2)
    kind: Literal["asset_transfer", "accounting_correction"]
    recorded_at: Time
    request: TransferRequest | CorrectionRequest
    previous_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    resulting_state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    postings: tuple[Journal, ...] = Field(min_length=1, max_length=2)
    original: Journal | None = None
    prior_report: dict[str, object] | None = None
    investment_flow: bool = True
    tax_classification: Literal["not_determined"] = "not_determined"
    promotion_evidence: Literal[False] = False
    live_execution_available: Literal[False] = False


async def record_at(c: AsyncConnection, account: UUID, version: int) -> AccountingRecord:
    row = (
        (
            await c.execute(
                select(operations).where(
                    operations.c.account_id == account, operations.c.event_version == version
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("accounting operation evidence missing")
    record = AccountingRecord.restore(row["document"])
    request = record.request
    if (
        record.account_id != account
        or record.event_version != version
        or record.request_id != row["request_id"]
        or record.request_id != request.request_id
        or record.kind != row["kind"]
        or request.expected_version != version - 1
        or fingerprint(row["document"]) != row["fingerprint"]
        or row["external_reference"]
        != (request.external_reference if isinstance(request, TransferRequest) else None)
        or row["target_journal"]
        != (request.target_journal if isinstance(request, CorrectionRequest) else None)
    ):
        raise ValueError("accounting operation identity/hash mismatch")
    return record


async def insert_record(c: AsyncConnection, record: AccountingRecord) -> None:
    r = record.request
    await c.execute(
        operations.insert().values(
            account_id=record.account_id,
            request_id=record.request_id,
            event_version=record.event_version,
            kind=record.kind,
            external_reference=r.external_reference if isinstance(r, TransferRequest) else None,
            target_journal=r.target_journal if isinstance(r, CorrectionRequest) else None,
            document=record.document(),
            fingerprint=fingerprint(record.document()),
        )
    )


async def verify_operation(
    c: AsyncConnection,
    account: UUID,
    old: PortfolioSnapshot | None,
    new: PortfolioSnapshot,
    kind: str,
    batch: tuple[Journal, ...],
) -> None:
    if old and old.accounting_invalidated and not new.accounting_invalidated:
        raise ValueError("accounting invalidation cannot be cleared")
    if new.accounting_invalidated and (not new.kill.hard or new.ready):
        raise ValueError("invalid accounting must remain halted and disarmed")
    if kind not in {"asset_transfer", "accounting_correction"}:
        if old and new.accounting_invalidated != old.accounting_invalidated:
            raise ValueError("accounting invalidation requires correction evidence")
        if any(j.kind in {"transfer", "correction"} or j.replaces for j in batch):
            raise ValueError("transfer/correction requires operator evidence")
        return
    r = await record_at(c, account, new.version)
    if old is None or (
        r.kind != kind
        or r.recorded_at != new.as_of
        or r.postings != batch
        or r.previous_state_hash != fingerprint(old.document())
        or r.resulting_state_hash != fingerprint(new.document())
    ):
        raise ValueError("accounting operation differs from its state transition")
    if isinstance(r.request, TransferRequest):
        if kind != "asset_transfer" or old.accounting_invalidated or len(batch) != 1:
            raise ValueError("invalid transfer operation")
        expected = flow_journal(
            r.request.flow, old.balances, source=f"transfer:{r.request.external_reference}"
        )
        if batch[0] != expected.model_copy(update={"id": batch[0].id}):
            raise ValueError("transfer economics differ from sealed evidence")
        from aca.operations.config import RiskPolicy

        if transfer_projection(old, r.request.flow, batch[0], RiskPolicy()) != new:
            raise ValueError("transfer investment units/risk projection differ from evidence")
    else:
        if kind != "accounting_correction" or len(batch) != 2 or r.original is None:
            raise ValueError("invalid correction operation")
        from aca.accounting.ledger import apply_journals
        from aca.accounting.operations import flow_from_journal
        from aca.storage.paper import journals

        raw = await c.scalar(
            select(journals.c.document).where(
                journals.c.account_id == account, journals.c.id == r.request.target_journal
            )
        )
        if raw != r.original.document():
            raise ValueError("correction original evidence mismatch")
        flow_from_journal(r.original)
        undo = reverse(
            r.original,
            source=f"correction:{r.request_id}:reverse",
            reason=r.request.reason,
            at=r.recorded_at,
        )
        # Apply both postings atomically; an intermediate reversal need not be funded.
        # The replacement cost is based on original acquisition evidence for an
        # outgoing transfer, not on a fabricated historical inventory.
        if r.request.replacement.quantity < 0 and r.request.replacement.kind == "transfer":
            replacement_inventory = apply_journals(old.balances, (batch[0],))
        else:
            replacement_inventory = old.balances
        replacement = flow_journal(
            r.request.replacement,
            replacement_inventory,
            source=f"correction:{r.request_id}:replace",
        )
        replacement = replacement.model_copy(update={"id": batch[1].id, "replaces": r.original.id})
        if batch != (undo.model_copy(update={"id": batch[0].id}), replacement):
            raise ValueError("correction postings differ from immutable decision")
        if not new.accounting_invalidated or new.units != old.units:
            raise ValueError("correction must explicitly invalidate the original units")
        expected_state = old.model_copy(
            update={
                "balances": apply_journals(old.balances, batch),
                "version": old.version + 1,
                "as_of": r.recorded_at,
                "accounting_invalidated": True,
                "ready": False,
                "incident": "accounting_correction_requires_rebuild",
                "kill": old.kill.model_copy(
                    update={
                        "hard": True,
                        "reason": "accounting_correction_requires_rebuild",
                        "latched_at": old.kill.latched_at or r.recorded_at,
                        "actor": "operator",
                    }
                ),
            }
        )
        if new != expected_state:
            raise ValueError("correction changed unrelated risk history or portfolio state")


async def accounting_status(
    c: AsyncConnection, account: UUID, *, after_version: int = 0, limit: int = 20
) -> dict[str, object]:
    if after_version < 0 or not 1 <= limit <= 100:
        raise ValueError("accounting query exceeds budget")
    rows = (
        (
            await c.execute(
                select(operations.c.event_version)
                .where(
                    operations.c.account_id == account, operations.c.event_version > after_version
                )
                .order_by(operations.c.event_version)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    return {
        "schema_version": 1,
        "account_id": str(account),
        "items": [(await record_at(c, account, v)).document() for v in rows[:limit]],
        "next_after_version": rows[limit - 1] if len(rows) > limit else None,
        "promotion_evidence": False,
        "live_execution_available": False,
    }
