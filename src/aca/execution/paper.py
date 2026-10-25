"""Single-writer paper execution. Consumed commands are never replayed."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.accounting.ledger import apply_journals, cash_flow, fill_journal, stored
from aca.domain.market import Symbol
from aca.domain.timing import APPROVAL_TTL, CLOCK_VERSION, TRANSMISSION_TTL
from aca.domain.trading import (
    TERMINAL,
    UNCERTAIN,
    ZERO,
    Asset,
    Balance,
    Fill,
    KillSwitchState,
    Order,
    OrderIntent,
    PortfolioSnapshot,
    ReconciliationResult,
    Valuation,
    fingerprint,
    replace_order,
)
from aca.domain.values import FINANCIAL_CONTEXT, utc_datetime
from aca.operations.config import RiskPolicy
from aca.portfolio.reserves import TaxReserveRecord, TaxReserveRequest
from aca.portfolio.state import (
    entries_paused,
    episode_risk,
    flow_units,
    free_cash,
    market_exposure,
    nav,
    observe_risk,
    unit_price,
)
from aca.ports.execution import DefinitiveRejection, ExecutionPort
from aca.risk.execution import DispatchDenied, MarketEvidence, execution_guard
from aca.risk.portfolio import approve
from aca.sizing.long_spot import round_increment, size_long
from aca.storage.paper import PaperWriter, fills, journals, venue_events
from aca.storage.replay import cursors, public_events
from aca.storage.reserves import MAX_RESERVES, for_event, reserves
from aca.strategies.slow import StrategySignal


class SimulatedCrash(BaseException):
    """Fault harness process-stop analogue; deliberately escapes normal handling."""


class PaperExecution:
    def __init__(
        self,
        writer: PaperWriter,
        venue: ExecutionPort,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.writer, self.store, self.venue = writer, writer.store, venue
        self.policy = RiskPolicy()
        self.fault = fault or (lambda point: None)

    async def _state(self) -> PortfolioSnapshot:
        async with self.writer.transaction() as c:
            state = await self.store.load(c)
            if state is None:
                raise ValueError("paper account not initialised")
            return state

    async def _save(
        self,
        old: PortfolioSnapshot,
        new: PortfolioSnapshot,
        *,
        at: datetime,
        kind: str,
        source: str | None = None,
        alert: bool = False,
    ) -> PortfolioSnapshot:
        at = utc_datetime(at)
        if at < old.as_of:
            raise ValueError("paper account clock moved backwards")
        state = new.model_copy(update={"version": old.version + 1, "as_of": at})
        async with self.writer.transaction() as c:
            actual = await self.store.load(c)
            if actual != old:
                raise ValueError("paper state changed before commit")
            await self.store.append(
                c, old, state, source=source or str(uuid4()), kind=kind, alert=alert
            )
        return state

    async def initialise(self, at: datetime) -> PortfolioSnapshot:
        at = utc_datetime(at)
        async with self.writer.transaction() as c:
            state = await self.store.load(c)
            if state is not None:
                return state
            state = PortfolioSnapshot(
                account_id=self.store.account_id,
                version=1,
                as_of=at,
                balances=tuple(
                    Balance(asset=a, quantity=ZERO, cost_gbp=ZERO) for a in ("GBP", "BTC", "ETH")
                ),
                kill=KillSwitchState(policy_epoch=uuid4()),
            )
            await self.store.append(c, None, state, source="initialise", kind="initialised")
            return state

    async def _require_drained_replay(self, c: AsyncConnection) -> None:
        cursor = await c.scalar(
            select(cursors.c.number)
            .where(cursors.c.account_id == self.store.account_id)
            .order_by(cursors.c.number.desc())
            .limit(1)
        )
        if cursor is not None:
            pending = await c.scalar(
                select(public_events.c.number).where(public_events.c.number > cursor).limit(1)
            )
            unposted = await c.scalar(
                select(venue_events.c.number)
                .where(
                    venue_events.c.account_id == self.store.account_id,
                    venue_events.c.kind == "fill",
                    ~select(fills.c.id)
                    .where(
                        fills.c.account_id == self.store.account_id,
                        fills.c.id == venue_events.c.document["id"].astext,
                    )
                    .exists(),
                )
                .limit(1)
            )
            if pending is not None or unposted is not None:
                raise ValueError("operation requires drained replay and reconciled fills")

    async def cashflow(self, source: str, amount: Decimal, at: datetime) -> PortfolioSnapshot:
        at = utc_datetime(at)
        journal = cash_flow(source, amount, at)
        async with self.writer.transaction() as c:
            state = await self.store.load(c)
            if state is None:
                raise ValueError("paper account not initialised")
            prior = await c.scalar(
                select(journals.c.document).where(
                    journals.c.account_id == self.store.account_id, journals.c.source == source
                )
            )
            if prior is not None:
                from aca.domain.trading import Journal

                previous = Journal.restore(prior)
                if (
                    previous.movements != journal.movements
                    or previous.occurred_at != at
                    or previous.kind != journal.kind
                ):
                    raise ValueError("conflicting cash-flow source")
                return state
            if at < state.as_of:
                raise ValueError("cash-flow clock moved backwards")
            if state.accounting_invalidated:
                raise ValueError("accounting correction invalidated investment units")
            await self._require_drained_replay(c)
            if amount < 0 and -amount > free_cash(state):
                raise ValueError("withdrawal spends reserved funds")
            units = flow_units(state, amount, at)
            new = state.model_copy(
                update={
                    "balances": apply_journals(state.balances, (journal,)),
                    "units": units,
                    "version": state.version + 1,
                    "as_of": at,
                }
            )
            if not state.units and state.daily_day != at.date():
                new = new.model_copy(
                    update={"daily_day": at.date(), "daily_unit_price": state.last_unit_price}
                )
            new = observe_risk(new, at, self.policy)
            await self.store.append(
                c,
                state,
                new,
                source=f"flow:{source}",
                kind="cash_flow",
                journal=journal,
                alert=True,
            )
        if entries_paused(new, at):
            await self.cancel_entries(at)
        return await self._state()

    async def mark(self, marks: tuple[Valuation, ...], at: datetime) -> PortfolioSnapshot:
        old = await self._state()
        candidate = old.model_copy(update={"marks": marks})
        try:
            candidate = observe_risk(candidate, at, self.policy)
        except ValueError:
            candidate = candidate.model_copy(
                update={"ready": False, "incident": "invalid_gbp_valuation"}
            )
        if candidate == old:
            if entries_paused(old, at):
                await self.cancel_entries(at)
            return await self._state()
        new = await self._save(
            old,
            candidate,
            at=at,
            kind="valuation",
            alert=candidate.kill != old.kill or candidate.incident != old.incident,
        )
        if entries_paused(new, at):
            await self.cancel_entries(at)
        return await self._state()

    async def reserve_tax_cash(
        self, request: TaxReserveRequest, *, at: datetime | None = None
    ) -> dict[str, object]:
        """Record an exact, idempotent manual earmark without a venue operation.

        CLI time comes from PostgreSQL. The optional clock is for deterministic
        local simulation and tests; no CLI timestamp override is exposed.
        """
        request = TaxReserveRequest.restore(request.document())
        async with self.writer.transaction() as c:
            old = await self.store.load(c)
            if old is None:
                raise ValueError("paper account not initialised")
            existing = await c.scalar(
                select(reserves.c.event_version).where(
                    reserves.c.account_id == self.store.account_id,
                    reserves.c.request_id == request.request_id,
                )
            )
            if existing is not None:
                record = await for_event(c, self.store.account_id, existing)
                if record.request() != request:
                    raise ValueError("reserve request ID already has a different decision")
                inserted = False
            else:
                if request.expected_version != old.version:
                    raise ValueError("reserve request has a stale paper state version")
                if request.amount_gbp == old.tax_reserve_gbp:
                    raise ValueError("tax cash reserve is already this amount")
                await self._require_drained_replay(c)
                # Also apply to offline accounts that have not enrolled in replay.
                unposted = await c.scalar(
                    select(venue_events.c.number)
                    .where(
                        venue_events.c.account_id == self.store.account_id,
                        venue_events.c.kind == "fill",
                        ~select(fills.c.id)
                        .where(
                            fills.c.account_id == self.store.account_id,
                            fills.c.id == venue_events.c.document["id"].astext,
                        )
                        .exists(),
                    )
                    .limit(1)
                )
                if unposted is not None:
                    raise ValueError("tax cash reserve requires reconciled fills")
                count = await c.scalar(
                    select(func.count())
                    .select_from(reserves)
                    .where(reserves.c.account_id == self.store.account_id)
                )
                if count is None or count >= MAX_RESERVES:
                    raise ValueError("tax cash reserve evidence budget exhausted")
                timestamp = utc_datetime(
                    at if at is not None else await c.scalar(select(func.clock_timestamp()))
                )
                record = TaxReserveRecord(
                    **request.model_dump(),
                    account_id=self.store.account_id,
                    event_version=old.version + 1,
                    previous_gbp=old.tax_reserve_gbp,
                    recorded_at=timestamp,
                )
                new = old.model_copy(
                    update={
                        "tax_reserve_gbp": record.amount_gbp,
                        "version": record.event_version,
                        "as_of": timestamp,
                    }
                )
                self.fault("before_tax_reserve")
                await self.store.append(
                    c,
                    old,
                    new,
                    source=f"tax-reserve:{record.request_id}",
                    kind="tax_reserve_set",
                    tax_reserve=record,
                    alert=True,
                )
                self.fault("before_tax_reserve_commit")
                inserted = True
            current = new if inserted else old
        self.fault("after_tax_reserve_commit")
        return {
            "schema_version": 1,
            "account_id": str(self.store.account_id),
            "mode": "paper",
            "decision": record.document(),
            "inserted": inserted,
            "current_state_version": current.version,
            "current_tax_cash_reserve_gbp": str(current.tax_reserve_gbp),
            "financial_posting_created": False,
            "tax_estimate": False,
            "promotion_evidence": False,
            "live_execution_available": False,
        }

    async def queue(
        self,
        signal: StrategySignal,
        market: MarketEvidence,
        at: datetime,
        *,
        time_in_force: Literal["GTD", "IOC", "FOK"] = "GTD",
        client_id: UUID | None = None,
    ) -> Order:
        at = utc_datetime(at)
        old = await self._state()
        if (
            signal.desired != "long"
            or signal.symbol != market.rules.symbol
            or not signal.decision_at <= at < signal.expires_at
            or old.kill.hard
        ):
            raise DispatchDenied("invalid, stale or halted strategy signal")
        if time_in_force not in {"GTD", "IOC", "FOK"}:
            raise ValueError("unsupported time in force")
        with localcontext(FINANCIAL_CONTEXT):
            entry = round_increment(
                market.book.bids[0].price
                if time_in_force == "GTD"
                else market.book.asks[0].price * Decimal("1.001"),
                market.rules.price_increment,
            )
            distance = max(signal.stop_distance, entry * Decimal("0.05"))
            if distance >= entry:
                raise DispatchDenied("protective distance exceeds entry price")
            trigger = round_increment(entry - distance, market.rules.price_increment)
            protective = round_increment(
                trigger * Decimal("0.99"), market.rules.price_increment, up=True
            )
            allowance = stored(entry * Decimal("0.002"))
            result = size_long(
                equity=nav(old, at),
                free_cash=free_cash(old),
                entry=entry,
                protective_limit=protective,
                entry_fee=Decimal("0.008"),
                exit_fee=Decimal("0.008"),
                allowance_per_unit=allowance,
                liquidity_quantity=stored(
                    sum((level.quantity for level in market.book.asks), ZERO) * Decimal("0.01")
                ),
                rules=market.rules,
                policy=self.policy,
                existing_market_value=market_exposure(old, at),
                existing_asset_value=old.quantity("BTC" if signal.symbol == "BTC/GBP" else "ETH")
                * entry,
                existing_planned_risk=episode_risk(old),
            )
        if result.reason:
            await self._save(old, old, at=at, kind=f"skipped_{result.reason}", alert=True)
            raise DispatchDenied(result.reason)
        ident = client_id or uuid4()
        if any(o.intent.client_id == ident for o in old.orders):
            raise DispatchDenied("economic action identity already exists")
        intent = OrderIntent(
            id=ident,
            client_id=ident,
            symbol=signal.symbol,
            purpose="entry",
            side="buy",
            quantity=result.quantity,
            limit_price=entry,
            stop_trigger=trigger,
            protective_limit=protective,
            fee_bound=Decimal("0.008"),
            allowance_per_unit=allowance,
            created_at=at,
            expires_at=min(
                signal.expires_at,
                at + (TRANSMISSION_TTL if signal.strategy_version == "slow-v2" else APPROVAL_TTL),
            ),
            strategy_version=signal.strategy_version,
            timing_version=CLOCK_VERSION if signal.strategy_version == "slow-v2" else None,
            rules_hash=fingerprint(market.rules.model_dump(mode="json")),
            episode_id=uuid4(),
            time_in_force=time_in_force,
        )
        approval = approve(old, intent, market.rules, self.policy, at)
        if not approval.allowed:
            await self._save(old, old, at=at, kind="risk_rejected", alert=True)
            raise DispatchDenied(",".join(approval.reasons))
        order = Order(intent=intent, approval=approval, state="VALIDATED")
        await self._save(
            old, replace_order(old, order), at=at, kind="intent_reserved", source=f"intent:{ident}"
        )
        self.fault("before_submitting")
        return order

    async def dispatch(self, client_id: UUID, market: MarketEvidence, at: datetime) -> Order:
        old = await self._state()
        order = old.order(client_id)
        try:
            if order.intent.purpose == "entry":
                from aca.reporting.history_prefix import suffix_budget

                try:
                    async with self.writer.transaction() as c:
                        await suffix_budget(
                            c, self.store, self.writer.accepted_history, new_commitment=True
                        )
                except ValueError as error:
                    raise DispatchDenied(
                        "history maintenance required before new entries"
                    ) from error
            execution_guard(old, order, market, self.policy, at)
        except DispatchDenied:
            if order.state == "VALIDATED":
                expired = order.model_copy(update={"state": "EXPIRED"})
                await self._save(
                    old, replace_order(old, expired), at=at, kind="dispatch_denied", alert=True
                )
            raise
        return await self._transmit(old, order, at)

    async def _transmit(self, old: PortfolioSnapshot, order: Order, at: datetime) -> Order:
        ident = order.intent.client_id
        submitting = order.model_copy(
            update={"state": "SUBMITTING", "pending_operation": "submit", "submitted_at": at}
        )
        await self._save(
            old, replace_order(old, submitting), at=at, kind="submitting", source=f"consume:{ident}"
        )
        self.fault("after_submitting_before_send")
        try:
            async with asyncio.timeout(5):
                result = await self.venue.submit(order.intent, at)
            self.fault("after_send_before_ack")
            if result.intent != order.intent:
                raise ValueError("venue response economics mismatch")
        except DefinitiveRejection:
            current = await self._state()
            rejected = current.order(ident).model_copy(
                update={"state": "REJECTED", "pending_operation": None}
            )
            await self._save(
                current,
                replace_order(current, rejected),
                at=at,
                kind="definitive_rejection",
                alert=True,
            )
            return rejected
        except Exception:
            current = await self._state()
            unknown = current.order(ident).model_copy(update={"state": "UNKNOWN"})
            await self._save(
                current,
                replace_order(current, unknown).model_copy(
                    update={"ready": False, "incident": "unknown_submission"}
                ),
                at=at,
                kind="unknown_submission",
                alert=True,
            )
            return unknown
        current = await self._state()
        acknowledged = current.order(ident).model_copy(
            update={"state": "ACKNOWLEDGED", "venue_id": result.venue_id, "pending_operation": None}
        )
        await self._save(current, replace_order(current, acknowledged), at=at, kind="acknowledged")
        return acknowledged

    async def _post_fill(self, fill: Fill, at: datetime) -> None:
        async with self.writer.transaction() as c:
            state = await self.store.load(c)
            if state is None:
                raise ValueError("paper account missing")
            source = f"authoritative_fill:{fill.id}"
            old_event = await c.scalar(
                select(fills.c.document).where(
                    fills.c.account_id == self.store.account_id, fills.c.id == fill.id
                )
            )
            if old_event is not None:
                if old_event != fill.document():
                    raise ValueError("conflicting authoritative fill id")
                return
            order = state.order(fill.client_id)
            if order.intent.symbol != fill.symbol or order.intent.side != fill.side:
                raise ValueError("fill/order economics mismatch")
            if (
                fill.occurred_at > at
                or order.submitted_at is None
                or fill.occurred_at < order.submitted_at
            ):
                raise ValueError("fill chronology mismatch")
            if (fill.side == "buy" and fill.price > order.intent.limit_price) or (
                fill.side == "sell" and fill.price < order.intent.limit_price
            ):
                raise ValueError("fill violates order price bound")
            cumulative = stored(order.filled + fill.quantity)
            if cumulative > order.intent.quantity:
                raise ValueError("cumulative fill exceeds order")
            lifecycle = (
                "FILLED"
                if cumulative == order.intent.quantity
                else order.state
                if order.state in TERMINAL or order.pending_operation == "cancel"
                else "PARTIALLY_FILLED"
            )
            updated = order.model_copy(update={"filled": cumulative, "state": lifecycle})
            journal = fill_journal(fill, state.balances)
            new = replace_order(state, updated).model_copy(
                update={
                    "balances": apply_journals(state.balances, (journal,)),
                    "version": state.version + 1,
                    "as_of": at,
                }
            )
            try:
                new = observe_risk(new, at, self.policy)
            except ValueError:
                new = new.model_copy(update={"ready": False, "incident": "invalid_gbp_valuation"})
            await self.store.append(
                c,
                state,
                new,
                source=source,
                kind="confirmed_fill",
                journal=journal,
                alert=True,
            )
            await c.execute(
                fills.insert().values(
                    account_id=self.store.account_id,
                    id=fill.id,
                    journal_id=journal.id,
                    document=fill.document(),
                )
            )

    async def reconcile(self, at: datetime) -> ReconciliationResult:
        at = utc_datetime(at)
        snapshot = await self.venue.snapshot(at)
        if not snapshot.complete or not 0 <= (at - snapshot.observed_at).total_seconds() <= 30:
            old = await self._state()
            await self._save(
                old,
                old.model_copy(update={"ready": False, "incident": "incomplete_reconciliation"}),
                at=at,
                kind="reconciliation_incomplete",
                alert=True,
            )
            return ReconciliationResult(
                complete=False,
                differences=("incomplete_venue_history",),
                observed_at=at,
                orders_checked=0,
                fills_checked=0,
            )
        differences: list[str] = []
        if (await self._state()).accounting_invalidated:
            differences.append("accounting_correction_requires_rebuild")
        async with self.writer.transaction() as c:
            posted_ids = set(
                (
                    await c.execute(
                        select(fills.c.id).where(fills.c.account_id == self.store.account_id)
                    )
                ).scalars()
            )
        if not posted_ids.issubset({fill.id for fill in snapshot.fills}):
            differences.append("incomplete_financial_history")
        venue = {o.intent.client_id: o for o in snapshot.orders}
        # Discover linked protective orders before importing their fills. Arbitrary
        # unknown orders never acquire local authority through reconciliation.
        for discovered in snapshot.orders:
            remote = discovered
            state = await self._state()
            known = {o.intent.client_id for o in state.orders}
            if remote.intent.client_id not in known:
                if remote.intent.purpose != "protection" or remote.intent.parent_id not in known:
                    differences.append("unrecognised_order")
                    continue
                parent = state.order(remote.intent.parent_id)
                if (
                    remote.intent.symbol != parent.intent.symbol
                    or remote.intent.episode_id != parent.intent.episode_id
                    or remote.intent.limit_price != parent.intent.protective_limit
                    or remote.intent.stop_trigger != parent.intent.stop_trigger
                ):
                    differences.append("invalid_protection_economics")
                    continue
                local = Order(
                    intent=remote.intent,
                    approval=None,
                    state="OPEN",
                    venue_id=remote.venue_id,
                    submitted_at=remote.accepted_at,
                )
                await self._save(
                    state, replace_order(state, local), at=at, kind="protection_discovered"
                )
        for fill in snapshot.fills:
            try:
                await self._post_fill(fill, at)
            except ValueError:
                differences.append("unreconciled_fill")
        old = await self._state()
        state = old
        for order in old.orders:
            found = venue.get(order.intent.client_id)
            if found is None:
                if (
                    order.submitted_at is not None
                    or order.state in UNCERTAIN
                    or order.state
                    in {
                        "ACKNOWLEDGED",
                        "OPEN",
                        "PARTIALLY_FILLED",
                    }
                ):
                    differences.append("unresolved_order")
                continue
            if found.intent != order.intent or found.filled != order.filled:
                differences.append("order_or_fill_difference")
                continue
            if order.pending_operation == "cancel" and found.state not in TERMINAL:
                differences.append("unresolved_cancellation")
                continue
            if found.state in UNCERTAIN or found.state in {"VALIDATED", "ACKNOWLEDGED"}:
                differences.append("ambiguous_venue_order")
                continue
            updated = order.model_copy(
                update={
                    "state": found.state,
                    "venue_id": found.venue_id,
                    "triggered": found.triggered,
                    "pending_operation": None,
                }
            )
            state = replace_order(state, updated)
        asset_symbols: tuple[tuple[Asset, Symbol], ...] = (("BTC", "BTC/GBP"), ("ETH", "ETH/GBP"))
        for asset, symbol in asset_symbols:
            protected = sum(
                (
                    o.remaining
                    for o in state.orders
                    if o.intent.symbol == symbol
                    and o.intent.side == "sell"
                    and o.state not in TERMINAL
                ),
                ZERO,
            )
            if protected != state.quantity(asset):
                differences.append("unprotected_or_overreserved_inventory")
        try:
            nav(state, at)
        except ValueError:
            differences.append("invalid_gbp_valuation")
        differences = sorted(set(differences))
        state = state.model_copy(
            update={
                "ready": not differences,
                "incident": ",".join(differences) if differences else None,
                "reconciled_at": at if not differences else old.reconciled_at,
                "account_evidence_at": at,
            }
        )
        state = await self._save(
            old,
            state,
            at=at,
            kind="reconciled" if not differences else "reconciliation_failed",
            alert=bool(differences) and (old.ready or old.incident != state.incident),
        )
        # A fill can breach a risk threshold. Cancel only entries after its
        # financial transaction and complete snapshot have committed.
        if entries_paused(state, at) and any(
            o.intent.purpose == "entry" and o.state not in TERMINAL and o.state not in UNCERTAIN
            for o in state.orders
        ):
            await self.cancel_entries(at)
        return ReconciliationResult(
            complete=not differences,
            differences=tuple(differences),
            observed_at=at,
            orders_checked=len(snapshot.orders),
            fills_checked=len(snapshot.fills),
        )

    async def recover(self, at: datetime) -> ReconciliationResult:
        old = await self._state()
        state = old.model_copy(update={"ready": False})
        for order in old.orders:
            if order.state in UNCERTAIN:
                state = replace_order(state, order.model_copy(update={"state": "UNKNOWN"}))
            elif order.state == "VALIDATED":
                state = replace_order(state, order.model_copy(update={"state": "EXPIRED"}))
        await self._save(old, state, at=at, kind="recovering", alert=True)
        return await self.reconcile(at)

    async def cancel(self, client_id: UUID, at: datetime) -> None:
        old = await self._state()
        order = old.order(client_id)
        if order.state in TERMINAL:
            return
        if order.state == "VALIDATED":
            await self._save(
                old,
                replace_order(old, order.model_copy(update={"state": "EXPIRED"})),
                at=at,
                kind="entry_expired",
            )
            return
        if order.state in UNCERTAIN:
            return  # Resolve original ambiguity before issuing another mutation.
        pending = order.model_copy(
            update={"state": "PENDING_CANCEL", "pending_operation": "cancel"}
        )
        await self._save(old, replace_order(old, pending), at=at, kind="pending_cancel")
        try:
            async with asyncio.timeout(5):
                await self.venue.cancel(client_id, at)
            self.fault("after_cancel_before_ack")
        except Exception:
            current = await self._state()
            await self._save(
                current,
                replace_order(
                    current, current.order(client_id).model_copy(update={"state": "UNKNOWN"})
                ).model_copy(update={"ready": False, "incident": "unknown_cancellation"}),
                at=at,
                kind="unknown_cancellation",
                alert=True,
            )
            return
        await self.reconcile(at)

    async def cancel_entries(self, at: datetime) -> None:
        for order in (await self._state()).orders:
            if (
                order.intent.purpose == "entry"
                and order.state not in TERMINAL
                and order.state not in UNCERTAIN
            ):
                await self.cancel(order.intent.client_id, at)

    async def halt(self, reason: str, at: datetime, *, hard: bool = True) -> None:
        if not reason.strip() or len(reason) > 240:
            raise ValueError("halt requires a bounded reviewable reason")
        old = await self._state()
        kill = old.kill.model_copy(
            update={
                "soft": True,
                "hard": old.kill.hard or hard,
                "reason": reason,
                "latched_at": at,
                "actor": "operator",
            }
        )
        await self._save(
            old,
            old.model_copy(update={"kill": kill}),
            at=at,
            kind="hard_halt" if hard else "soft_halt",
            alert=True,
        )
        await self.cancel_entries(at)

    async def review(self, reason: str, at: datetime) -> None:
        if len(reason.strip()) < 12 or len(reason) > 240:
            raise ValueError("manual epoch review requires a substantive reason")
        result = await self.reconcile(at)
        if not result.complete:
            raise DispatchDenied("reconciliation must complete before review")
        old = await self._state()
        async with self.writer.transaction() as c:
            await self._require_drained_replay(c)
        price = unit_price(old, at)
        # The prior epoch and inception high remain in immutable history. Daily
        # suspensions cannot be cleared through an epoch review.
        kill = KillSwitchState(
            policy_epoch=uuid4(),
            daily_day=old.kill.daily_day,
            reason=reason,
            latched_at=at,
            actor="operator",
        )
        await self._save(
            old,
            old.model_copy(update={"kill": kill, "high_water": price}),
            at=at,
            kind="manual_epoch_review",
            alert=True,
        )

    async def exit_position(
        self,
        symbol: Symbol,
        market: MarketEvidence,
        at: datetime,
        *,
        manual_review: str | None = None,
        client_id: UUID | None = None,
        expires_at: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
        refresh: Callable[[datetime], Awaitable[tuple[MarketEvidence, ...]]] | None = None,
    ) -> Order | None:
        state = await self._state()
        if client_id is not None and any(o.intent.client_id == client_id for o in state.orders):
            raise DispatchDenied("economic action identity already exists")
        if state.kill.hard and not manual_review:
            raise DispatchDenied("hard halt requires an explicit position decision")
        b, r = market.book, market.rules
        if (
            b.symbol != symbol
            or r.symbol != symbol
            or b.venue != r.venue
            or not market.system_online
            or not market.metadata_consistent
            or r.status != "online"
            or abs(market.clock_offset_ms) > 500
            or not 0 <= (at - b.received_at).total_seconds() <= 5
            or (b.event_at is not None and not 0 <= (at - b.event_at).total_seconds() <= 5)
            or not 0 <= (at - r.observed_at).total_seconds() <= 3600
        ):
            raise DispatchDenied("exit requires fresh validated market evidence")
        with localcontext(FINANCIAL_CONTEXT):
            mid = (b.bids[0].price + b.asks[0].price) / 2
            if (b.asks[0].price - b.bids[0].price) / mid > Decimal("0.002"):
                raise DispatchDenied("exit spread bound")
        if manual_review is not None and not 12 <= len(manual_review.strip()) <= 240:
            raise ValueError("manual exit requires a substantive position decision")
        asset: Asset = "BTC" if symbol == "BTC/GBP" else "ETH"
        with localcontext(FINANCIAL_CONTEXT):
            pre_limit = round_increment(
                b.bids[0].price * Decimal("0.998"), r.price_increment, up=True
            )
            pre_quantity = round_increment(state.quantity(asset), r.quantity_increment)
            if pre_quantity and (
                pre_quantity < r.minimum_quantity or pre_quantity * pre_limit < r.minimum_cost
            ):
                await self._save(
                    state,
                    state.model_copy(update={"ready": False, "incident": "untradeable_residual"}),
                    at=at,
                    kind="residual_quarantined",
                    alert=True,
                )
                return None
        for order in state.orders:
            if (
                order.intent.symbol == symbol
                and order.intent.purpose == "protection"
                and order.state not in TERMINAL
            ):
                await self.cancel(order.intent.client_id, at)
        result = await self.reconcile(at)
        # Missing protection during an intentional closing operation is expected;
        # every other discrepancy remains a veto. Own inventory is sold once.
        if any(d != "unprotected_or_overreserved_inventory" for d in result.differences):
            raise DispatchDenied("unresolved exit cancellation or fills")
        state = await self._state()
        if clock is not None:
            at = utc_datetime(clock())
        if refresh is not None:
            markets = await refresh(at)
            market = next(m for m in markets if m.book.symbol == symbol)
            if clock is not None:
                at = utc_datetime(clock())
        if expires_at is not None and at >= expires_at:
            raise DispatchDenied("strategy exit decision expired during cancellation")
        if state.kill.hard and not manual_review:
            raise DispatchDenied("hard halt requires an explicit position decision")
        if (
            market.book.symbol != symbol
            or market.rules.symbol != symbol
            or market.book.venue != market.rules.venue
            or not market.system_online
            or not market.metadata_consistent
            or market.rules.status != "online"
            or abs(market.clock_offset_ms) > 500
            or not 0 <= (at - market.book.received_at).total_seconds() <= 5
            or (
                market.book.event_at is not None
                and not 0 <= (at - market.book.event_at).total_seconds() <= 5
            )
            or not 0 <= (at - market.rules.observed_at).total_seconds() <= 3600
        ):
            raise DispatchDenied("exit evidence changed during cancellation")
        with localcontext(FINANCIAL_CONTEXT):
            mid = (market.book.bids[0].price + market.book.asks[0].price) / 2
            if (market.book.asks[0].price - market.book.bids[0].price) / mid > Decimal("0.002"):
                raise DispatchDenied("exit spread changed during cancellation")
        if any(
            o.intent.symbol == symbol and o.intent.side == "sell" and o.state not in TERMINAL
            for o in state.orders
        ):
            raise DispatchDenied("inventory already reserved by a sell")
        q = round_increment(state.quantity(asset), market.rules.quantity_increment)
        if not q:
            return None
        with localcontext(FINANCIAL_CONTEXT):
            limit = round_increment(
                market.book.bids[0].price * Decimal("0.998"), market.rules.price_increment, up=True
            )
        if q < market.rules.minimum_quantity or q * limit < market.rules.minimum_cost:
            await self._save(
                state,
                state.model_copy(update={"ready": False, "incident": "untradeable_residual"}),
                at=at,
                kind="residual_quarantined",
                alert=True,
            )
            return None
        parent = next(
            o
            for o in reversed(state.orders)
            if o.intent.symbol == symbol and o.intent.purpose == "entry"
        )
        ident = client_id or uuid4()
        transmission = TRANSMISSION_TTL if parent.intent.timing_version else APPROVAL_TTL
        intent = OrderIntent(
            id=ident,
            client_id=ident,
            symbol=symbol,
            purpose="exit",
            side="sell",
            quantity=q,
            limit_price=limit,
            stop_trigger=ZERO,
            protective_limit=ZERO,
            fee_bound=parent.intent.fee_bound,
            allowance_per_unit=ZERO,
            created_at=at,
            expires_at=min(expires_at, at + transmission) if expires_at else at + transmission,
            strategy_version="safety-v1",
            timing_version=parent.intent.timing_version,
            rules_hash=fingerprint(market.rules.model_dump(mode="json")),
            episode_id=parent.intent.episode_id,
            time_in_force="IOC",
            operator_reason=manual_review,
        )
        order = Order(intent=intent, approval=None, state="VALIDATED")
        state = await self._save(
            state, replace_order(state, order), at=at, kind="closing_reserved", alert=True
        )
        return await self._transmit(state, order, at)
