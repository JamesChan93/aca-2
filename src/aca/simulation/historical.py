"""Original-observation paper economics, independent of financial import time."""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.accounting.ledger import apply_journals, fill_journal
from aca.accounting.operations import flow_from_journal, flow_marks, investment_amount
from aca.domain.market import OrderBook, Symbol
from aca.domain.trading import (
    TERMINAL,
    Journal,
    KillSwitchState,
    PortfolioSnapshot,
    Record,
    Time,
    Valuation,
    fingerprint,
)
from aca.domain.values import FinancialDecimal
from aca.operations.config import RiskPolicy
from aca.portfolio.state import entries_paused, flow_units, nav, observe_risk
from aca.simulation.day_boundary import DayBoundary
from aca.simulation.paper import PaperExchange
from aca.storage.paper import PaperStore, events, journals, venue_events
from aca.storage.replay import public_events, valuations
from aca.storage.reserves import for_event

EMPTY_HASH = "0" * 64
CONTROL_KINDS = (
    "cash_flow",
    "asset_transfer",
    "manual_epoch_review",
    "hard_halt",
    "soft_halt",
    "tax_reserve_set",
)


class HistoricalValuation(Record):
    number: int = Field(ge=0)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_received_at: Time | None = None
    state: PortfolioSnapshot
    control_version: int = Field(ge=1)
    fill_number: int = Field(ge=0)
    nav_gbp: FinancialDecimal | None = None
    reason: str | None = "baseline"
    incomplete_since: Time | None = None
    policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    day_boundary: DayBoundary | None = None
    promotion_evidence: bool = False
    live_execution_available: bool = False


class HistoricalReplay:
    def __init__(self, store: PaperStore, venue: PaperExchange, policy: RiskPolicy) -> None:
        self.store, self.venue, self.policy = store, venue, policy
        self.policy_hash = fingerprint(policy.model_dump(mode="json"))

    def restore(self, row: RowMapping, previous: str | None = None) -> HistoricalValuation:
        document = row["document"]
        if fingerprint({"previous": row["previous_hash"], "valuation": document}) != row[
            "fingerprint"
        ] or (previous is not None and previous != row["previous_hash"]):
            raise ValueError("corrupt historical valuation chain")
        value = HistoricalValuation.restore(document)
        if (
            value.number != row["number"]
            or value.state.account_id != self.store.account_id
            or value.state.orders
            or value.policy_hash != self.policy_hash
            or value.promotion_evidence
            or value.live_execution_available
        ):
            raise ValueError("historical valuation identity/policy mismatch")
        if value.nav_gbp is not None and nav(value.state, value.state.as_of) != value.nav_gbp:
            raise ValueError("historical NAV does not match its evidence")
        if value.day_boundary is not None:
            boundary = value.day_boundary
            boundary.validate_economics()
            if (
                boundary.state.account_id != self.store.account_id
                or boundary.following_source != value.number
                or boundary.following_source_hash != value.source_hash
                or value.source_received_at is None
                or not 0 <= (value.source_received_at - boundary.at).total_seconds() <= 5
            ):
                raise ValueError("UTC boundary source mismatch")
        return value

    async def latest(self, c: AsyncConnection) -> RowMapping | None:
        return (
            (
                await c.execute(
                    select(valuations)
                    .where(valuations.c.account_id == self.store.account_id)
                    .order_by(valuations.c.number.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )

    async def save(self, c: AsyncConnection, value: HistoricalValuation, previous: str) -> str:
        document = value.document()
        hashed = fingerprint({"previous": previous, "valuation": document})
        await c.execute(
            valuations.insert().values(
                account_id=self.store.account_id,
                number=value.number,
                previous_hash=previous,
                fingerprint=hashed,
                document=document,
            )
        )
        return hashed

    async def enrol(
        self, c: AsyncConnection, state: PortfolioSnapshot, number: int, source_hash: str
    ) -> None:
        if await self.latest(c) is not None:
            return
        # Called only after flat/no-outstanding-order checks by StoredReplay.
        value = HistoricalValuation(
            number=number,
            source_hash=source_hash,
            state=state.model_copy(
                update={"orders": (), "marks": (), "incident": None, "ready": False}
            ),
            control_version=state.version,
            fill_number=await self.venue._number(c),
            nav_gbp=nav(state, state.as_of),
            policy_hash=self.policy_hash,
        )
        await self.save(c, value, EMPTY_HASH)

    async def audit(self, c: AsyncConnection, cursor: int) -> HistoricalValuation:
        number, previous, last = -1, EMPTY_HASH, None
        while True:
            rows = (
                (
                    await c.execute(
                        select(valuations)
                        .where(
                            valuations.c.account_id == self.store.account_id,
                            valuations.c.number > number,
                        )
                        .order_by(valuations.c.number)
                        .limit(64)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                value = self.restore(row, previous)
                if value.day_boundary is not None:
                    boundary = value.day_boundary
                    if (
                        last is None
                        or boundary.previous_source != last.number
                        or boundary.previous_source_hash != last.source_hash
                        or boundary.previous_valuation_hash != previous
                        or not last.state.as_of < boundary.at
                    ):
                        raise ValueError("UTC boundary previous anchor mismatch")
                    expected = await self.apply_controls(
                        c,
                        last,
                        await self.controls(c, last.control_version),
                        boundary.at - timedelta(microseconds=1),
                    )
                    expected = self.observe(expected, boundary.at, last.state.marks, None)
                    if (
                        expected.state != boundary.state
                        or expected.incomplete_since != boundary.incomplete_since
                    ):
                        raise ValueError("UTC boundary does not reconstruct from its prefix")
                if last is not None and (
                    value.state.as_of < last.state.as_of
                    or value.control_version < last.control_version
                    or value.fill_number < last.fill_number
                    or (last.incomplete_since and value.incomplete_since != last.incomplete_since)
                ):
                    raise ValueError("historical valuation regressed")
                if value.number:
                    if last is not None:
                        next_source = await c.scalar(
                            select(public_events.c.number)
                            .where(public_events.c.number > last.number)
                            .order_by(public_events.c.number)
                            .limit(1)
                        )
                        if next_source != value.number:
                            raise ValueError("historical valuation source coverage gap")
                    anchor = await c.scalar(
                        select(public_events.c.fingerprint).where(
                            public_events.c.number == value.number
                        )
                    )
                    if anchor != value.source_hash:
                        raise ValueError("historical valuation source anchor mismatch")
                last, number, previous = value, value.number, row["fingerprint"]
            if len(rows) < 64:
                break
        if last is None or last.number != cursor:
            raise ValueError(
                "historical valuation baseline/cursor missing; flat enrolment required"
            )
        return last

    async def controls(self, c: AsyncConnection, version: int) -> list[RowMapping]:
        rows = (
            (
                await c.execute(
                    select(events)
                    .where(
                        events.c.account_id == self.store.account_id,
                        events.c.version > version,
                        events.c.kind.in_(CONTROL_KINDS),
                    )
                    .order_by(events.c.version)
                    .limit(257)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 256:
            raise ValueError("historical control backlog exceeds 256-event budget")
        return list(rows)

    @staticmethod
    def marks(books: tuple[OrderBook, ...], sources: dict[Symbol, UUID]) -> tuple[Valuation, ...]:
        return tuple(
            Valuation(
                asset="BTC" if book.symbol == "BTC/GBP" else "ETH",
                bid_gbp=book.bids[0].price,
                observed_at=book.received_at,
                source=f"replay-book:{sources[book.symbol]}",
            )
            for book in books
        )

    def observe(
        self,
        value: HistoricalValuation,
        at: datetime,
        marks: tuple[Valuation, ...],
        reason: str | None,
    ) -> HistoricalValuation:
        if at < value.state.as_of:
            raise ValueError("historical observation precedes baseline or regresses")
        state = value.state.model_copy(update={"as_of": at, "marks": marks})
        incomplete, amount = value.incomplete_since, None
        try:
            # Cash can be valued during warm-up; held crypto requires the full
            # validated public context, not simply a recent price.
            held = state.quantity("BTC") or state.quantity("ETH")
            if held and reason:
                raise ValueError("incomplete historical market evidence")
            state = observe_risk(state, at, self.policy)
            amount = nav(state, at)
            if state.kill.reason == "missing_utc_midnight_mark":
                incomplete = incomplete or at
        except ValueError:
            reason = reason or "invalid_historical_gbp_valuation"
            incomplete = incomplete or at
        return value.model_copy(
            update={
                "state": state,
                "nav_gbp": amount,
                "reason": reason,
                "incomplete_since": incomplete,
            }
        )

    async def apply_controls(
        self,
        c: AsyncConnection,
        value: HistoricalValuation,
        controls: list[RowMapping],
        at: datetime,
    ) -> HistoricalValuation:
        for row in controls:
            if row["version"] <= value.control_version or row["at"] > at:
                continue
            if row["at"] < value.state.as_of:
                raise ValueError("late historical control requires review")
            state = value.state
            document = row["document"]
            changes: dict[str, Any] = document.get("changes", document)
            if row["kind"] in {"cash_flow", "asset_transfer"}:
                ident = document.get("journal_id")
                raw = await c.scalar(
                    select(journals.c.document).where(
                        journals.c.account_id == self.store.account_id,
                        journals.c.id == UUID(str(ident)),
                    )
                )
                if raw is None:
                    raise ValueError("historical cash-flow journal missing")
                journal = Journal.restore(raw)
                if (
                    journal.occurred_at != row["at"]
                    or journal.kind not in {"contribution", "withdrawal", "transfer"}
                    or len(journal.movements) != 1
                    or (
                        row["kind"] == "cash_flow"
                        and any(m.asset != "GBP" for m in journal.movements)
                    )
                ):
                    raise ValueError("unsupported historical cash flow")
                state = flow_marks(state, flow_from_journal(journal))
                if journal.kind == "transfer" and state.units:
                    state = observe_risk(state, row["at"], self.policy)
                amount = investment_amount(journal)
                units = flow_units(state, amount, row["at"])
                opening = not state.units and state.daily_day != row["at"].date()
                state = state.model_copy(
                    update={
                        "units": units,
                        "balances": apply_journals(state.balances, (journal,)),
                    }
                )
                if opening:
                    state = state.model_copy(
                        update={
                            "daily_day": row["at"].date(),
                            "daily_unit_price": state.last_unit_price,
                        }
                    )
                # Refuse differing pre-flow valuation conventions instead of
                # silently replacing the operational unit ledger.
                if state.units != Decimal(str(changes.get("units", state.units))):
                    raise ValueError("historical and operational investment units differ")
            elif row["kind"] == "tax_reserve_set":
                record = await for_event(c, self.store.account_id, row["version"])
                if record.previous_gbp != state.tax_reserve_gbp:
                    raise ValueError("historical tax cash reserve differs from decision")
                state = state.model_copy(update={"tax_reserve_gbp": record.amount_gbp})
            else:
                raw_kill = changes.get("kill")
                if not isinstance(raw_kill, dict):
                    raise ValueError("historical operator control missing kill state")
                kill = KillSwitchState.restore(raw_kill)
                if row["kind"] == "manual_epoch_review":
                    from aca.portfolio.state import unit_price

                    state = state.model_copy(
                        update={"high_water": unit_price(state, row["at"]), "kill": kill}
                    )
                else:
                    # Operator halts add restrictions; they cannot erase a
                    # loss already detected in this historical epoch.
                    kill = kill.model_copy(
                        update={
                            "hard": kill.hard or state.kill.hard,
                            "soft": kill.soft or state.kill.soft,
                            "daily_day": state.kill.daily_day or kill.daily_day,
                        }
                    )
                    state = state.model_copy(update={"kill": kill})
            value = self.observe(
                value.model_copy(update={"state": state}), row["at"], state.marks, value.reason
            )
            value = value.model_copy(update={"control_version": row["version"]})
        return value

    async def import_fills(
        self, c: AsyncConnection, value: HistoricalValuation, at: datetime
    ) -> HistoricalValuation:
        rows = (
            (
                await c.execute(
                    select(venue_events.c.number, venue_events.c.document)
                    .where(
                        venue_events.c.account_id == self.store.account_id,
                        venue_events.c.kind == "fill",
                        venue_events.c.number > value.fill_number,
                    )
                    .order_by(venue_events.c.number)
                    .limit(4097)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) > 4096:
            raise ValueError("historical fill budget exhausted")
        from aca.domain.trading import Fill

        for row in rows:
            fill = Fill.restore(row["document"])
            if fill.occurred_at != at:
                raise ValueError("historical fill is outside its source observation")
            journal = fill_journal(fill, value.state.balances)
            state = value.state.model_copy(
                update={"balances": apply_journals(value.state.balances, (journal,))}
            )
            value = self.observe(
                value.model_copy(update={"state": state, "fill_number": row["number"]}),
                at,
                state.marks,
                value.reason,
            )
        return value

    async def cancel_entries(
        self, c: AsyncConnection, value: HistoricalValuation, at: datetime
    ) -> None:
        if not entries_paused(value.state, at) and value.nav_gbp is not None:
            return
        snapshot = self.venue._snapshot(await self.venue._rows(c), at)
        number = await self.venue._number(c)
        for order in snapshot.orders:
            if order.intent.purpose != "entry" or order.state in TERMINAL:
                continue
            if order.accepted_at > at:
                raise ValueError("historical risk crossed an already accepted future entry")
            number += 1
            cancelled = order.model_copy(update={"state": "CANCELLED", "cancelled_at": at})
            await self.venue._append(
                c,
                number=number,
                source=f"historical-risk:{value.number}:{order.intent.client_id}",
                kind="order",
                document=cancelled.document(),
            )

    async def propagate(self, c: AsyncConnection, value: HistoricalValuation, at: datetime) -> None:
        old = await self.store.load(c)
        if old is None:
            raise ValueError("paper account missing")
        historical, current = value.state.kill, old.kill
        if current.policy_epoch != historical.policy_epoch:
            # A future operator review is still queued in the control timeline.
            # It cannot silently hide a newly recovered historical breach.
            if historical.hard or historical.soft:
                raise ValueError("historical risk requires review of a later policy epoch")
            return
        kill = current.model_copy(
            update={
                "hard": current.hard or historical.hard,
                "soft": current.soft or historical.soft,
                "daily_day": max(
                    (d for d in (current.daily_day, historical.daily_day) if d is not None),
                    default=None,
                ),
            }
        )
        daily_updates: dict[str, object] = {}
        if value.state.daily_day and (
            old.daily_day is None or value.state.daily_day >= old.daily_day
        ):
            daily_updates = {
                "daily_day": value.state.daily_day,
                "daily_unit_price": value.state.daily_unit_price,
            }
            if kill.daily_day and kill.daily_day < value.state.daily_day:
                kill = kill.model_copy(update={"daily_day": None})
        if (
            (kill.hard and not current.hard)
            or (kill.soft and not current.soft)
            or (
                kill.daily_day != current.daily_day
                and kill.daily_day is not None
                and not (current.hard or current.soft)
            )
        ):
            kill = kill.model_copy(
                update={
                    "reason": historical.reason,
                    "latched_at": historical.latched_at,
                    "actor": "historical_paper_risk",
                }
            )
        candidate = old.model_copy(
            update={
                **daily_updates,
                "kill": kill,
                "high_water": max(old.high_water, value.state.high_water),
                "inception_high_water": max(
                    old.inception_high_water, value.state.inception_high_water
                ),
            }
        )
        if candidate != old:
            await self.store.append(
                c,
                old,
                candidate.model_copy(update={"as_of": at, "version": old.version + 1}),
                source=f"historical-risk:{value.number}",
                kind="historical_risk",
                alert=kill != current,
            )
