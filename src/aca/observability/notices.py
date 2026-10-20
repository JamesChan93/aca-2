"""Scheduled paper notices. Their receipts never modify the financial outbox."""

from datetime import date, datetime, time, timedelta
from decimal import localcontext
from uuid import UUID

from sqlalchemy import Column, DateTime, ForeignKey, MetaData, String, Table, Uuid, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.accounting.ledger import stored
from aca.accounting.operations import investment_amount
from aca.domain.trading import (
    TERMINAL,
    UNCERTAIN,
    ZERO,
    Asset,
    Journal,
    PortfolioSnapshot,
    fingerprint,
)
from aca.domain.values import FINANCIAL_CONTEXT, utc_datetime
from aca.portfolio.state import market_exposure, unit_price
from aca.reporting.historical import read_performance
from aca.storage.paper import PaperStore, events, journals, outbox

metadata = MetaData(schema="aca")
notices = Table(
    "paper_notices",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id")),
    Column("kind", String(16)),
    Column("key", String(160)),
    Column("created_at", DateTime(timezone=True)),
    Column("document", JSONB),
    Column("fingerprint", String(64)),
)
receipts = Table(
    "paper_notice_receipts",
    metadata,
    Column("notice_id", Uuid, ForeignKey("aca.paper_notices.id"), primary_key=True),
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id")),
    Column("at", DateTime(timezone=True)),
    Column("outcome", String(16)),
)


def critical(state: PortfolioSnapshot) -> tuple[str, tuple[str, ...]] | None:
    reasons = []
    uncertain = sorted(str(o.intent.client_id) for o in state.orders if o.state in UNCERTAIN)
    if state.kill.hard:
        reasons.append("hard_halt")
    if uncertain:
        reasons.append("uncertain_order")
    with localcontext(FINANCIAL_CONTEXT):
        assets: tuple[Asset, ...] = ("BTC", "ETH")
        for asset in assets:
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
            if reserved != state.quantity(asset):
                reasons.append("inventory_protection_difference")
    if state.incident == "untradeable_residual":
        reasons.append("residual_quarantined")
    differences = {
        "incomplete_financial_history",
        "unrecognised_order",
        "invalid_protection_economics",
        "unreconciled_fill",
        "unresolved_order",
        "order_or_fill_difference",
        "unresolved_cancellation",
        "ambiguous_venue_order",
        "unprotected_or_overreserved_inventory",
    }
    if state.incident and differences.intersection(state.incident.split(",")):
        reasons.append("reconciliation_failed")
    if not reasons:
        return None
    codes = tuple(sorted(set(reasons)))
    episode = fingerprint(
        {
            "reasons": codes,
            "uncertain": uncertain,
            "epoch": str(state.kill.policy_epoch),
            "latch": state.kill.latched_at.isoformat()
            if state.kill.hard and state.kill.latched_at
            else None,
            "incident": state.incident,
        }
    )
    return episode, codes


def summary_day(at: datetime) -> date:
    """At 00:10 UTC the preceding UTC day becomes eligible; one latest day only."""
    return (utc_datetime(at) - timedelta(minutes=10)).date() - timedelta(days=1)


async def daily_summary(c: AsyncConnection, store: PaperStore, day: date) -> dict[str, object]:
    """Audited calendar accounting plus explicitly bounded observed performance.

    Missing midnight evidence must not become a fabricated full-day return.
    The caller supplies a database-enforced read-only REPEATABLE READ snapshot.
    """
    from datetime import UTC

    start = datetime.combine(day, time(), UTC)
    end = start + timedelta(days=1)
    history = await read_performance(c, store, day=day)
    state = history.financial
    if state.accounting_invalidated:
        result: dict[str, object] = {
            "day": day.isoformat(),
            "contributions_gbp": None,
            "withdrawals_gbp": None,
            "fees_gbp": None,
            "accounting_totals_available": False,
            "fault_events": None,
            "observed": None,
            "daily_pnl_gbp": None,
            "daily_twr": None,
            "day_boundaries": None,
            "daily_return_complete": False,
            "daily_return_reason": "accounting_correction_requires_rebuild",
            "units_valid": False,
            "tax_reserve_estimate_gbp": None,
            "backup_status": "unverified",
            "financial_head_version": state.version,
        }
        return {**result, "report_hash": fingerprint(result)}
    rows = (
        (
            await c.execute(
                select(journals.c.document).where(
                    journals.c.account_id == store.account_id,
                    journals.c.occurred_at >= start,
                    journals.c.occurred_at < end,
                )
            )
        )
        .scalars()
        .all()
    )
    contributions = withdrawals = fees = ZERO
    with localcontext(FINANCIAL_CONTEXT):
        for raw in rows:
            journal = Journal.restore(raw)
            if journal.kind not in {"contribution", "withdrawal", "trade", "transfer"}:
                raise ValueError("daily summary does not support transfers/corrections")
            if journal.kind in {"contribution", "withdrawal", "transfer"}:
                if len(journal.movements) != 1:
                    raise ValueError("unsupported daily investment flow")
                amount = investment_amount(journal)
                contributions += max(ZERO, amount)
                withdrawals += max(ZERO, amount.copy_negate())
            fees += sum(
                (line.gbp for line in journal.lines if line.account == "expense:fees"), ZERO
            )
    faults = await c.scalar(
        select(func.count())
        .select_from(outbox)
        .where(
            outbox.c.account_id == store.account_id,
            outbox.c.at >= start,
            outbox.c.at < end,
            (outbox.c.document["hard_halt"].as_boolean().is_(True))
            | (outbox.c.document["incident"].as_string().is_not(None)),
        )
    )
    window = history.calendar
    if window is None:
        raise ValueError("daily summary requires a calendar window")
    first, closing = window.first, window.last
    opening, closing_boundary = window.opening, window.closing
    observed = None
    if first is not None and closing is not None:
        report = history.report
        if report is None or not isinstance(report["period"], dict):
            raise ValueError("invalid observed performance report")
        period = report["period"]
        with localcontext(FINANCIAL_CONTEXT):
            observed = {
                "start": first.state.as_of.isoformat(),
                "end": closing.state.as_of.isoformat(),
                "nav_gbp": str(closing.nav_gbp) if closing.nav_gbp is not None else None,
                "pnl_gbp": period["pnl_gbp"],
                "twr": period["twr"],
                "drawdown": str(
                    stored(
                        1
                        - unit_price(closing.state, closing.state.as_of) / closing.state.high_water
                    )
                )
                if closing.nav_gbp is not None
                else None,
                "exposure_gbp": str(market_exposure(closing.state, closing.state.as_of))
                if closing.nav_gbp is not None
                else None,
                "valuation_complete": period["valuation_complete"],
                "source_start": first.number,
                "source_end": closing.number,
                "report_hash": report["report_hash"],
            }
    daily_pnl = daily_twr = None
    boundary_evidence = None
    if opening is not None and closing_boundary is not None:
        # The shared read already checked every source, financial movement and
        # embedded boundary against its prefix, including after this UTC day.
        if opening.incomplete_since is None and closing_boundary.incomplete_since is None:
            with localcontext(FINANCIAL_CONTEXT):
                daily_pnl = str(
                    stored(closing_boundary.nav_gbp - opening.nav_gbp - contributions + withdrawals)
                )
                daily_twr = str(stored(closing_boundary.unit_price / opening.unit_price - 1))
            boundary_evidence = {
                "opening": opening.document(),
                "closing": closing_boundary.document(),
            }
    result = {
        "day": day.isoformat(),
        "contributions_gbp": str(contributions),
        "withdrawals_gbp": str(withdrawals),
        "fees_gbp": str(fees),
        "fault_events": faults,
        "observed": observed,
        "daily_pnl_gbp": daily_pnl,
        "daily_twr": daily_twr,
        "day_boundaries": boundary_evidence,
        "daily_return_complete": daily_pnl is not None,
        "daily_return_reason": None
        if daily_pnl is not None
        else "midnight_boundary_coverage_not_qualified",
        "tax_reserve_estimate_gbp": None,
        "backup_status": "unverified",
        "financial_head_version": state.version,
        "financial_head_hash": await c.scalar(
            select(events.c.fingerprint).where(
                events.c.account_id == store.account_id,
                events.c.version == state.version,
            )
        ),
    }
    return {**result, "report_hash": fingerprint(result)}


def notice_payload(document: dict[str, object], ident: UUID, account: UUID) -> dict[str, object]:
    if document.get("schema_version") != 1 or type(document.get("schema_version")) is not int:
        raise ValueError("unknown scheduled notice schema")
    if (
        document.get("account_id") != str(account)
        or document.get("live_execution_available") is not False
    ):
        raise ValueError("scheduled notice identity mismatch")
    kind = document.get("kind")
    if kind == "critical":
        codes = document.get("reasons")
        allowed = {
            "hard_halt",
            "uncertain_order",
            "inventory_protection_difference",
            "residual_quarantined",
            "reconciliation_failed",
        }
        if not isinstance(codes, list) or not codes or any(code not in allowed for code in codes):
            raise ValueError("invalid critical reason codes")
        body = "CRITICAL REMINDER | " + ", ".join(codes)
    elif kind == "daily":
        summary = document.get("summary")
        if not isinstance(summary, dict):
            raise ValueError("missing daily summary")
        day = date.fromisoformat(str(summary["day"]))
        from aca.domain.values import parse_decimal

        if summary.get("accounting_totals_available") is False:
            return {
                "content": f"DAILY SUMMARY | {day.isoformat()} UTC\n"
                "Accounting corrected; units and daily totals require rebuild. "
                f"Entries halted.\nAccount: {account}\nNotice: {ident}\nLive trading: disabled",
                "allowed_mentions": {"parse": []},
            }
        amounts = [
            str(parse_decimal(summary[key]))
            for key in ("contributions_gbp", "withdrawals_gbp", "fees_gbp")
        ]
        pnl = "unavailable; midnight coverage unqualified"
        if summary.get("daily_return_complete") is True:
            pnl = str(parse_decimal(summary["daily_pnl_gbp"])) + " GBP"
        body = (
            f"DAILY SUMMARY | {day.isoformat()} UTC\n"
            f"Contributions GBP: {amounts[0]} | Withdrawals GBP: {amounts[1]}\n"
            f"Fees GBP: {amounts[2]}\n"
            f"Daily P&L: {pnl}\n"
            "Tax reserve estimate: unavailable | Backup: unverified"
        )
        observed = summary.get("observed")
        if isinstance(observed, dict):
            # Decimal and UTC validation prevents arbitrary stored strings becoming message text.
            begin = utc_datetime(datetime.fromisoformat(str(observed["start"])))
            end = utc_datetime(datetime.fromisoformat(str(observed["end"])))
            body += f"\nRecorded interval: {begin.isoformat()} to {end.isoformat()}"
            for label, key in (
                ("Observed NAV GBP", "nav_gbp"),
                ("Observed P&L GBP", "pnl_gbp"),
                ("Observed exposure GBP", "exposure_gbp"),
                ("Observed drawdown", "drawdown"),
            ):
                value = observed.get(key)
                body += f"\n{label}: {parse_decimal(value) if value is not None else 'unavailable'}"
        else:
            body += "\nObserved NAV/P&L/exposure/drawdown: unavailable"
        faults = summary.get("fault_events")
        if type(faults) is not int or faults < 0:
            raise ValueError("invalid daily fault count")
        body += f"\nFault transition events: {faults}\nProvisional paper evidence"
    else:
        raise ValueError("unknown scheduled notice kind")
    content = f"ACA PAPER | {body}\nAccount: {account}\nNotice: {ident}\nLive trading: disabled"
    if len(content) > 2000:
        raise ValueError("scheduled notice exceeds message bound")
    return {"content": content, "allowed_mentions": {"parse": []}}
