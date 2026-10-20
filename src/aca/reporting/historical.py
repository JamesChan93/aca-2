"""Read-only, source-bounded GBP performance from sealed paper evidence."""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from aca import __version__
from aca.accounting.ledger import apply_journals, cash_flow, fill_journal, stored
from aca.accounting.operations import flow_from_journal, flow_journal, flow_marks, investment_amount
from aca.domain.trading import ZERO, Asset, Fill, Journal, fingerprint
from aca.domain.values import FINANCIAL_CONTEXT
from aca.operations.config import RiskPolicy
from aca.portfolio.reserves import TaxReserveRecord
from aca.portfolio.state import flow_units, unit_price
from aca.reporting.returns import MAX_RETURN_FLOWS, investor_return
from aca.simulation.historical import EMPTY_HASH, HistoricalReplay, HistoricalValuation
from aca.simulation.paper import PaperExchange
from aca.simulation.stored_replay import ReplayCursor
from aca.storage.paper import PaperStore, checkpoints, events, fills, journals, venue_events
from aca.storage.replay import cursors, public_events, valuations
from aca.storage.reserves import for_event

MAX_REPORT_VALUES = 100000
MAX_REPORT_EVENTS = 100000
MAX_REPORT_JOURNALS = 10000
MAX_REPORT_FILLS = 4096
PAGE = 64


@dataclass
class Totals:
    contributions: Decimal = ZERO
    withdrawals: Decimal = ZERO
    fees: Decimal = ZERO
    realised: Decimal = ZERO
    trades: int = 0
    flows: int = 0
    transferred_unrealised: Decimal = ZERO
    fee_assets: dict[Asset, Decimal] = field(default_factory=dict)

    def add(self, journal: Journal, fill: Fill | None = None) -> None:
        with localcontext(FINANCIAL_CONTEXT):
            if journal.kind in {"contribution", "withdrawal", "transfer"}:
                amount = investment_amount(journal)
                self.contributions += max(ZERO, amount)
                self.withdrawals += max(ZERO, -amount)
                self.flows += 1
            else:
                self.trades += 1
            self.fees += sum(
                (line.gbp for line in journal.lines if line.account == "expense:fees"), ZERO
            )
            self.realised -= sum(
                (line.gbp for line in journal.lines if line.account == "pnl:realised"), ZERO
            )
            self.transferred_unrealised -= sum(
                (line.gbp for line in journal.lines if line.account == "equity:transfer_basis"),
                ZERO,
            )
            for fee in fill.fees if fill else ():
                self.fee_assets[fee.asset] = self.fee_assets.get(fee.asset, ZERO) + fee.quantity


@dataclass
class Evidence:
    flows: deque[tuple[int, Journal]]
    trades: deque[tuple[int, Fill, Journal]]
    reserves: deque[TaxReserveRecord]


async def _budget(c: AsyncConnection, store: PaperStore) -> None:
    for table, limit in (
        (valuations, MAX_REPORT_VALUES),
        (events, MAX_REPORT_EVENTS),
        (journals, MAX_REPORT_JOURNALS),
        (venue_events, MAX_REPORT_EVENTS),
        (fills, MAX_REPORT_FILLS),
    ):
        count = await c.scalar(
            select(func.count()).select_from(table).where(table.c.account_id == store.account_id)
        )
        if count is None or count > limit:
            raise ValueError("performance report evidence budget exceeded")


async def _baseline(c: AsyncConnection, store: PaperStore, value: HistoricalValuation) -> None:
    """Bind the historical opening inventory/units to the audited financial prefix."""
    checkpoint = (
        (
            await c.execute(
                select(checkpoints)
                .where(
                    checkpoints.c.account_id == store.account_id,
                    checkpoints.c.version <= value.state.version,
                )
                .order_by(checkpoints.c.version.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    state = await store._checkpoint(c, checkpoint) if checkpoint else None
    number = state.version if state else 0
    previous = checkpoint["event_hash"] if checkpoint else EMPTY_HASH
    while number < value.state.version:
        rows = (
            (
                await c.execute(
                    select(events)
                    .where(
                        events.c.account_id == store.account_id,
                        events.c.version > number,
                        events.c.version <= value.state.version,
                    )
                    .order_by(events.c.version)
                    .limit(PAGE)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            raise ValueError("historical opening financial prefix missing")
        for row in rows:
            state, previous = store._restore_row(row, previous, number + 1, state)
            number = state.version
    if (
        state is None
        or value.reason != "baseline"
        or value.source_received_at is not None
        or value.control_version != state.version
        or value.state.balances != state.balances
        or value.state.units != state.units
        or value.state.last_unit_price != state.last_unit_price
        or value.state.tax_reserve_gbp != state.tax_reserve_gbp
        or value.state.as_of != state.as_of
        or state.quantity("BTC")
        or state.quantity("ETH")
    ):
        raise ValueError("historical opening inventory/units differ from financial evidence")


async def _evidence(
    c: AsyncConnection,
    store: PaperStore,
    baseline: HistoricalValuation,
    end: HistoricalValuation,
) -> Evidence:
    documents = (
        await c.execute(
            select(journals.c.id, journals.c.document).where(
                journals.c.account_id == store.account_id
            )
        )
    ).all()
    sealed = {ident: Journal.restore(doc) for ident, doc in documents}
    controls: list[tuple[int, Journal]] = []
    reserve_records: list[TaxReserveRecord] = []
    number = baseline.control_version
    while True:
        rows = (
            (
                await c.execute(
                    select(events)
                    .where(events.c.account_id == store.account_id, events.c.version > number)
                    .order_by(events.c.version)
                    .limit(PAGE)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            number = row["version"]
            if row["kind"] == "tax_reserve_set" and number <= end.control_version:
                reserve_records.append(await for_event(c, store.account_id, number))
            ident = row["document"].get("journal_id")
            journal = sealed.get(UUID(ident)) if ident else None
            if (
                journal
                and journal.occurred_at <= end.state.as_of
                and journal.kind not in {"contribution", "withdrawal", "trade", "transfer"}
            ):
                raise ValueError("performance does not support transfers/corrections")
            if row["kind"] in {"cash_flow", "asset_transfer"} and number <= end.control_version:
                if journal is not None and journal.kind == "transfer":
                    flow_from_journal(journal)
                    if journal.occurred_at != row["at"]:
                        raise ValueError("transfer chronology differs from event")
                    controls.append((number, journal))
                    continue
                if (
                    journal is None
                    or journal.kind not in {"contribution", "withdrawal"}
                    or journal.occurred_at != row["at"]
                    or len(journal.movements) != 1
                    or journal.movements[0].asset != "GBP"
                    or not journal.movements[0].quantity
                    or (journal.movements[0].quantity > 0) != (journal.kind == "contribution")
                ):
                    raise ValueError("unsupported performance cash flow")
                expected = cash_flow(
                    journal.source, journal.movements[0].quantity, journal.occurred_at
                ).model_copy(update={"id": journal.id})
                if expected != journal:
                    raise ValueError("performance cash-flow economics differ from sealed journal")
                controls.append((number, journal))
        if len(rows) < PAGE:
            break
    rows = (
        (
            await c.execute(
                select(venue_events)
                .where(
                    venue_events.c.account_id == store.account_id,
                    venue_events.c.kind == "fill",
                    venue_events.c.number > baseline.fill_number,
                    venue_events.c.number <= end.fill_number,
                )
                .order_by(venue_events.c.number)
            )
        )
        .mappings()
        .all()
    )
    posted = {
        row["id"]: row
        for row in (
            (await c.execute(select(fills).where(fills.c.account_id == store.account_id)))
            .mappings()
            .all()
        )
    }
    trades: list[tuple[int, Fill, Journal]] = []
    seen: set[str] = set()
    for row in rows:
        fill = Fill.restore(row["document"])
        match = posted.get(fill.id)
        if fill.id in seen or match is None or match["document"] != fill.document():
            raise ValueError("performance requires unique reconciled historical fills")
        seen.add(fill.id)
        journal = sealed.get(match["journal_id"])
        if journal is None:
            raise ValueError("performance fill journal missing")
        trades.append((row["number"], fill, journal))
    return Evidence(deque(controls), deque(trades), deque(reserve_records))


def _advance(
    before: HistoricalValuation,
    after: HistoricalValuation,
    evidence: Evidence,
    totals: Totals,
) -> tuple[tuple[datetime, Decimal], ...]:
    state, investor_flows = before.state, []
    while evidence.reserves and evidence.reserves[0].event_version <= after.control_version:
        record = evidence.reserves.popleft()
        if (
            record.event_version <= before.control_version
            or not before.state.as_of <= record.recorded_at <= after.state.as_of
            or record.previous_gbp != state.tax_reserve_gbp
        ):
            raise ValueError("performance tax cash reserve chronology mismatch")
        state = state.model_copy(update={"tax_reserve_gbp": record.amount_gbp})
    if state.tax_reserve_gbp != after.state.tax_reserve_gbp:
        raise ValueError("historical tax cash reserve does not reconstruct from decisions")
    while evidence.flows and evidence.flows[0][0] <= after.control_version:
        version, journal = evidence.flows.popleft()
        if version <= before.control_version:
            raise ValueError("performance cash flow was already consumed")
        if not before.state.as_of <= journal.occurred_at <= after.state.as_of:
            raise ValueError("performance cash-flow chronology mismatch")
        flow = flow_from_journal(journal)
        if journal.kind == "transfer":
            expected = flow_journal(flow, state.balances, source=journal.source).model_copy(
                update={"id": journal.id}
            )
            if expected != journal:
                raise ValueError("performance transfer economics differ from sealed journal")
        state = flow_marks(state, flow)
        amount = investment_amount(journal)
        price = unit_price(state, journal.occurred_at)
        units = flow_units(state, amount, journal.occurred_at)
        state = state.model_copy(
            update={
                "units": units,
                "last_unit_price": price,
                "balances": apply_journals(state.balances, (journal,)),
            }
        )
        totals.add(journal)
        investor_flows.append((journal.occurred_at, amount.copy_negate()))
    while evidence.trades and evidence.trades[0][0] <= after.fill_number:
        number, fill, journal = evidence.trades.popleft()
        if number <= before.fill_number:
            raise ValueError("performance fill was already consumed")
        if fill.occurred_at != after.state.as_of:
            raise ValueError("performance fill chronology mismatch")
        expected = fill_journal(fill, state.balances).model_copy(update={"id": journal.id})
        if expected != journal:
            raise ValueError("performance fill economics differ from sealed journal")
        state = state.model_copy(update={"balances": apply_journals(state.balances, (journal,))})
        totals.add(journal, fill)
    if state.balances != after.state.balances or state.units != after.state.units:
        raise ValueError("historical balances/units do not reconstruct from financial evidence")
    return tuple(investor_flows)


def _endpoint(value: HistoricalValuation, hashed: str) -> dict[str, object]:
    return {
        "source_number": value.number,
        "valuation_hash": hashed,
        "source_hash": value.source_hash,
        "source_received_at": value.source_received_at.isoformat()
        if value.source_received_at
        else None,
        "economic_as_of": value.state.as_of.isoformat(),
        "control_version": value.control_version,
        "fill_number": value.fill_number,
        "nav_gbp": str(value.nav_gbp) if value.nav_gbp is not None else None,
        "units": str(value.state.units),
        "manual_tax_cash_reserve_gbp": str(value.state.tax_reserve_gbp),
        "unit_price": str(unit_price(value.state, value.state.as_of))
        if value.nav_gbp is not None
        else None,
        "balances": [balance.document() for balance in value.state.balances],
        "marks": [mark.document() for mark in value.state.marks],
    }


class Period:
    def __init__(self, start: HistoricalValuation, hashed: str) -> None:
        self.start = start
        self.opening = _endpoint(start, hashed)
        self.totals = Totals()
        self.flows: list[tuple[datetime, Decimal]] = []
        self.issues: set[str] = set()
        self.count = self.missing = 0
        self.high = ZERO
        self.drawdown = ZERO

    def observe(self, value: HistoricalValuation) -> None:
        self.count += 1
        if value.incomplete_since:
            self.issues.add("incomplete_historical_prefix")
        if value.nav_gbp is None:
            self.missing += 1
            self.issues.add("missing_gbp_valuation")
            return
        with localcontext(FINANCIAL_CONTEXT):
            price = unit_price(value.state, value.state.as_of)
            self.high = max(self.high, price)
            self.drawdown = max(self.drawdown, 1 - price / self.high)

    def finish(self, end: HistoricalValuation, hashed: str) -> dict[str, object]:
        start, t = self.start, self.totals
        with localcontext(FINANCIAL_CONTEXT):
            net = t.contributions - t.withdrawals
            pnl = unrealised = twr = mwr = None
            reason: str | None = "incomplete_valuation_evidence"
            if start.nav_gbp is not None and end.nav_gbp is not None:
                pnl = end.nav_gbp - start.nav_gbp - net
                unrealised = (
                    end.nav_gbp
                    - sum((b.cost_gbp for b in end.state.balances), ZERO)
                    - start.nav_gbp
                    + sum((b.cost_gbp for b in start.state.balances), ZERO)
                )
                if pnl != t.realised + unrealised + t.transferred_unrealised - t.fees:
                    raise ValueError("performance attribution does not balance")
            if not self.issues:
                if start.state.units:
                    opening_price = unit_price(start.state, start.state.as_of)
                else:
                    opening_price = start.state.last_unit_price
                if start.state.units or t.contributions:
                    twr = stored(unit_price(end.state, end.state.as_of) / opening_price - 1)
                opening_nav = start.nav_gbp or ZERO
                mwr, reason = investor_return(
                    ((start.state.as_of, -opening_nav), *self.flows),
                    end.nav_gbp or ZERO,
                    start.state.as_of,
                    end.state.as_of,
                )
                if t.flows + 1 > MAX_RETURN_FLOWS:
                    mwr, reason = None, "flow_budget_exceeded"
            return {
                "opening": self.opening,
                "closing": _endpoint(end, hashed),
                "observations": self.count,
                "missing_nav_observations": self.missing,
                "valuation_complete": not self.issues,
                "issues": sorted(self.issues),
                "contributions_gbp": str(t.contributions),
                "withdrawals_gbp": str(t.withdrawals),
                "net_investment_flows_gbp": str(net),
                "investment_flow_count": t.flows,
                "trade_fill_count": t.trades,
                "fees_gbp": str(t.fees),
                "fee_quantities": {a: str(q) for a, q in sorted(t.fee_assets.items())},
                "realised_pnl_gbp": str(t.realised),
                "transferred_unrealised_gbp": str(t.transferred_unrealised),
                "unrealised_change_gbp": str(unrealised) if unrealised is not None else None,
                "pnl_gbp": str(pnl) if pnl is not None else None,
                "twr": str(twr) if twr is not None else None,
                "max_observed_drawdown": str(stored(self.drawdown)) if not self.issues else None,
                "mwr_annualised": str(mwr) if mwr is not None else None,
                "mwr_unavailable_reason": reason,
                "mwr_convention": "actual/365.25; UTC; annual rate in [-0.9999,1000]",
            }


async def _report(
    c: AsyncConnection, store: PaperStore, start_source: int | None, end_source: int | None
) -> dict[str, object]:
    await _budget(c, store)
    financial = await store.audit(c)
    if financial is None:
        raise ValueError("paper account not initialised")
    if financial.accounting_invalidated:
        from aca.storage.accounting import accounting_status

        invalid: dict[str, object] = {
            "schema_version": 1,
            "report_type": "historical_paper_performance",
            "application_version": __version__,
            "calculation_version": "historical-performance-v2",
            "account_id": str(store.account_id),
            "financial_head_version": financial.version,
            "requested_start_source": start_source,
            "requested_end_source": end_source,
            "accounting_valid": False,
            "units_valid": False,
            "period": None,
            "reason": "accounting_correction_requires_rebuild",
            "corrections": await accounting_status(c, store.account_id),
            "provisional": True,
            "promotion_evidence": False,
            "tax_report_final": False,
            "live_execution_available": False,
        }
        return {**invalid, "report_hash": fingerprint(invalid)}
    reader = HistoricalReplay(store, PaperExchange(store), RiskPolicy())
    head = await reader.latest(c)
    cursor = (
        (
            await c.execute(
                select(cursors)
                .where(cursors.c.account_id == store.account_id)
                .order_by(cursors.c.number.desc())
                .limit(1)
            )
        )
        .mappings()
        .one_or_none()
    )
    if head is None or cursor is None:
        raise ValueError("performance requires explicit historical enrolment")
    if (
        fingerprint({"previous": cursor["previous_hash"], "cursor": cursor["document"]})
        != cursor["fingerprint"]
    ):
        raise ValueError("corrupt performance cursor anchor")
    current_cursor = ReplayCursor.restore(cursor["document"])
    if (
        current_cursor.account_id != store.account_id
        or current_cursor.number != cursor["number"]
        or current_cursor.source_hash != reader.restore(head).source_hash
    ):
        raise ValueError("performance cursor identity mismatch")
    first = (
        (
            await c.execute(
                select(valuations)
                .where(valuations.c.account_id == store.account_id)
                .order_by(valuations.c.number)
                .limit(1)
            )
        )
        .mappings()
        .one()
    )
    baseline = reader.restore(first)
    await _baseline(c, store, baseline)
    lower = baseline.number if start_source is None else start_source
    upper = head["number"] if end_source is None else end_source
    if lower < baseline.number or upper < lower or upper > head["number"]:
        raise ValueError("performance source range outside recorded history")
    end_row = (
        (
            await c.execute(
                select(valuations).where(
                    valuations.c.account_id == store.account_id, valuations.c.number == upper
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if end_row is None:
        raise ValueError("performance endpoint must name an exact recorded source")
    end = reader.restore(end_row)
    evidence = await _evidence(c, store, baseline, end)
    previous, period = baseline, None
    # Exhaust the full audited prefix even for a report ending in the past.
    # Arithmetic uses already validated values; an invalid later record or a
    # missing suffix still prevents returning any accepted report.
    async for row, value in reader.audited_values(c, current_cursor.number):
        if value.number > upper:
            continue
        totals = period.totals if period else Totals()
        dated = _advance(previous, value, evidence, totals)
        if value.number == lower:
            period = Period(value, row["fingerprint"])
        elif period is not None and len(period.flows) <= MAX_RETURN_FLOWS:
            period.flows.extend(dated)
        if period:
            period.observe(value)
        previous = value
    if period is None:
        raise ValueError("performance start must name an exact recorded source")
    financial_hash = await c.scalar(
        select(events.c.fingerprint).where(
            events.c.account_id == store.account_id, events.c.version == financial.version
        )
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "report_type": "historical_paper_performance",
        "application_version": __version__,
        "calculation_version": "historical-performance-v2",
        "account_id": str(store.account_id),
        "mode": "paper",
        "scope": "recorded_historical_baseline_forward",
        "baseline": _endpoint(baseline, first["fingerprint"]),
        "policy_hash": reader.policy_hash,
        "financial_head_version": financial.version,
        "financial_snapshot_as_of": financial.as_of.isoformat(),
        "financial_head_hash": financial_hash,
        "historical_head_source": head["number"],
        "historical_head_hash": head["fingerprint"],
        "cursor_hash": cursor["fingerprint"],
        "unread_public_sources": await c.scalar(
            select(func.count())
            .select_from(public_events)
            .where(public_events.c.number > head["number"])
        ),
        "unapplied_cash_flows": await c.scalar(
            select(func.count())
            .select_from(events)
            .where(
                events.c.account_id == store.account_id,
                events.c.kind.in_(("cash_flow", "asset_transfer")),
                events.c.version > reader.restore(head).control_version,
            )
        ),
        "period": period.finish(end, end_row["fingerprint"]),
        "financial_evidence_reconciled": True,
        "financial_scope": "selected_historical_prefix",
        "provisional": True,
        "promotion_evidence": False,
        "tax_report_final": False,
        "live_execution_available": False,
    }
    return {**result, "report_hash": fingerprint(result)}


async def historical_performance(
    store: PaperStore, *, start_source: int | None = None, end_source: int | None = None
) -> dict[str, object]:
    """One PostgreSQL read-only snapshot; no writer lock or financial side effect."""
    if any(value is not None and value < 0 for value in (start_source, end_source)):
        raise ValueError("performance source numbers must be nonnegative")
    async with store.engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        await c.execute(text("SET LOCAL statement_timeout = '30s'"))
        return await _report(c, store, start_source, end_source)
