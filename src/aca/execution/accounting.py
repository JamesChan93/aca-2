"""Single-writer external flows and atomic reversal/replacement administration."""

from datetime import datetime
from decimal import localcontext
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.accounting.ledger import apply_journals, reverse
from aca.accounting.operations import (
    CorrectionRequest,
    TransferRequest,
    flow_from_journal,
    flow_journal,
    transfer_projection,
)
from aca.domain.trading import TERMINAL, UNCERTAIN, ZERO, Journal, PortfolioSnapshot, fingerprint
from aca.domain.values import FINANCIAL_CONTEXT, utc_datetime
from aca.portfolio.state import entries_paused, free_cash
from aca.reporting.paper import performance
from aca.storage.accounting import AccountingRecord, insert_record, operations, record_at
from aca.storage.paper import fills, journals, venue_events

if TYPE_CHECKING:
    from aca.execution.paper import PaperExecution

INVALIDATED = "accounting_correction_requires_rebuild"
MAX_OPERATIONS = 10000


async def safe_mutation(
    service: PaperExecution, c: AsyncConnection, state: PortfolioSnapshot
) -> None:
    await service._require_drained_replay(c)
    if any(o.state in UNCERTAIN for o in state.orders):
        raise ValueError("accounting mutation requires resolved commands")
    unposted = await c.scalar(
        select(venue_events.c.number)
        .where(
            venue_events.c.account_id == state.account_id,
            venue_events.c.kind == "fill",
            ~select(fills.c.id)
            .where(
                fills.c.account_id == state.account_id,
                fills.c.id == venue_events.c.document["id"].astext,
            )
            .exists(),
        )
        .limit(1)
    )
    if unposted is not None:
        raise ValueError("accounting mutation requires reconciled fills")
    count = await c.scalar(
        select(func.count())
        .select_from(operations)
        .where(operations.c.account_id == state.account_id)
    )
    if count is None or count >= MAX_OPERATIONS:
        raise ValueError("accounting operation evidence budget exhausted")


def inventory_available(state: PortfolioSnapshot) -> None:
    if free_cash(state) < 0:
        raise ValueError("accounting mutation spends reserved GBP")
    with localcontext(FINANCIAL_CONTEXT):
        for asset in ("BTC", "ETH"):
            reserved = sum(
                (
                    o.remaining
                    for o in state.orders
                    if o.intent.symbol == f"{asset}/GBP"
                    and o.intent.side == "sell"
                    and o.state not in TERMINAL
                ),
                ZERO,
            )
            if reserved > state.quantity(asset):
                raise ValueError("accounting mutation spends reserved inventory")


async def external_transfer(service: PaperExecution, request: TransferRequest) -> dict[str, object]:
    request = TransferRequest.restore(request.document())
    async with service.writer.transaction() as c:
        old = await service.store.load(c)
        if old is None:
            raise ValueError("paper account not initialised")
        prior = await c.scalar(
            select(operations.c.event_version).where(
                operations.c.account_id == old.account_id,
                (operations.c.request_id == request.request_id)
                | (operations.c.external_reference == request.external_reference),
            )
        )
        if prior is not None:
            record = await record_at(c, old.account_id, prior)
            if record.request != request:
                raise ValueError("conflicting accounting request/reference")
            return {
                "inserted": False,
                "record": record.document(),
                "live_execution_available": False,
            }
        flow = request.flow
        at = flow.occurred_at
        if old.accounting_invalidated or request.expected_version != old.version or at < old.as_of:
            raise ValueError(
                "transfer requires valid accounting, current version and nonregressing event time"
            )
        # Prevent user-supplied future event times from creating a premature flow.
        if at > utc_datetime(await c.scalar(select(func.clock_timestamp()))):
            raise ValueError("transfer event time is in the future")
        await safe_mutation(service, c, old)
        if any(o.intent.purpose == "entry" and o.state not in TERMINAL for o in old.orders):
            raise ValueError("transfer requires no pending or reserved entry orders")
        journal = flow_journal(flow, old.balances, source=f"transfer:{request.external_reference}")
        new = transfer_projection(old, flow, journal, service.policy)
        inventory_available(new)
        record = AccountingRecord(
            account_id=old.account_id,
            request_id=request.request_id,
            event_version=new.version,
            kind="asset_transfer",
            recorded_at=at,
            request=request,
            previous_state_hash=fingerprint(old.document()),
            resulting_state_hash=fingerprint(new.document()),
            postings=(journal,),
        )
        await insert_record(c, record)
        service.fault("before_transfer_commit")
        await service.store.append(
            c,
            old,
            new,
            source=f"accounting:{request.request_id}",
            kind="asset_transfer",
            journal=journal,
            alert=True,
        )
    service.fault("after_transfer_commit")
    if entries_paused(new, at):
        await service.cancel_entries(at)
    return {"inserted": True, "record": record.document(), "live_execution_available": False}


async def correct_accounting(
    service: PaperExecution, request: CorrectionRequest, *, at: datetime | None = None
) -> dict[str, object]:
    request = CorrectionRequest.restore(request.document())
    async with service.writer.transaction() as c:
        old = await service.store.load(c)
        if old is None:
            raise ValueError("paper account not initialised")
        prior = await c.scalar(
            select(operations.c.event_version).where(
                operations.c.account_id == old.account_id,
                operations.c.request_id == request.request_id,
            )
        )
        if prior is not None:
            record = await record_at(c, old.account_id, prior)
            if record.request != request:
                raise ValueError("correction request ID has a different decision")
            return {
                "inserted": False,
                "record": record.document(),
                "live_execution_available": False,
            }
        if request.expected_version != old.version:
            raise ValueError("correction has a stale paper state version")
        await safe_mutation(service, c, old)
        if any(o.state not in TERMINAL for o in old.orders):
            raise ValueError("correction requires no pending or reserved orders")
        timestamp = utc_datetime(
            at if at is not None else await c.scalar(select(func.clock_timestamp()))
        )
        if timestamp < old.as_of or request.replacement.occurred_at > timestamp:
            raise ValueError("correction record time regressed or replacement occurs in future")
        opening = await c.scalar(
            select(func.min(journals.c.occurred_at)).where(journals.c.account_id == old.account_id)
        )
        if opening is None or request.replacement.occurred_at < opening:
            raise ValueError("replacement predates supported account history")
        raw = await c.scalar(
            select(journals.c.document).where(
                journals.c.account_id == old.account_id,
                journals.c.id == request.target_journal,
            )
        )
        if raw is None:
            raise ValueError("correction target journal not found in this account")
        original = Journal.restore(raw)
        original_flow = flow_from_journal(original)
        if original_flow == request.replacement:
            raise ValueError("correction does not change the external flow")
        if (
            await c.scalar(
                select(operations.c.request_id).where(
                    operations.c.account_id == old.account_id,
                    operations.c.target_journal == original.id,
                )
            )
            is not None
        ):
            raise ValueError("journal already reversed; correct its replacement instead")
        undo = reverse(
            original,
            source=f"correction:{request.request_id}:reverse",
            reason=request.reason,
            at=timestamp,
        )
        inventory = old.balances
        if request.replacement.kind == "transfer" and request.replacement.quantity < 0:
            inventory = apply_journals(inventory, (undo,))
        replacement = flow_journal(
            request.replacement, inventory, source=f"correction:{request.request_id}:replace"
        ).model_copy(update={"replaces": original.id})
        new = old.model_copy(
            update={
                "balances": apply_journals(old.balances, (undo, replacement)),
                "version": old.version + 1,
                "as_of": timestamp,
                "accounting_invalidated": True,
                "ready": False,
                "incident": INVALIDATED,
                "kill": old.kill.model_copy(
                    update={
                        "hard": True,
                        "reason": INVALIDATED,
                        "latched_at": old.kill.latched_at or timestamp,
                        "actor": "operator",
                    }
                ),
            }
        )
        inventory_available(new)
        history = await service.store.history(c)
        record = AccountingRecord(
            account_id=old.account_id,
            request_id=request.request_id,
            event_version=new.version,
            kind="accounting_correction",
            recorded_at=timestamp,
            request=request,
            previous_state_hash=fingerprint(old.document()),
            resulting_state_hash=fingerprint(new.document()),
            postings=(undo, replacement),
            original=original,
            prior_report=performance(old, history, old.as_of),
        )
        await insert_record(c, record)
        service.fault("before_correction_commit")
        await service.store.append(
            c,
            old,
            new,
            source=f"accounting:{request.request_id}",
            kind="accounting_correction",
            journal=undo,
            additional_journals=(replacement,),
            alert=True,
        )
    service.fault("after_correction_commit")
    return {"inserted": True, "record": record.document(), "live_execution_available": False}
