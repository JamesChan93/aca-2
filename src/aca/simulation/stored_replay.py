"""Bounded, commit-ordered public replay. No strategy or real exchange writer."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Self
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.domain.market import InstrumentRules, MarketTrade, OrderBook, Symbol
from aca.domain.trading import TERMINAL, Record, Time, VenueOrder, fingerprint
from aca.domain.values import utc_datetime
from aca.execution.paper import PaperExecution
from aca.portfolio.state import unit_price
from aca.simulation.day_boundary import DayBoundary
from aca.simulation.historical import AUDIT_PAGE, HistoricalReplay, HistoricalValuation
from aca.simulation.paper import PaperExchange
from aca.storage.models import MarketReceipt
from aca.storage.paper import fills
from aca.storage.replay import cursors, public_events

EMPTY_HASH = "0" * 64


class ReplayReceipt(Record):
    receipt_id: UUID
    epoch: UUID
    received_at: Time
    channel: str
    valid: bool
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    normalized: dict[str, Any]
    trades: tuple[MarketTrade, ...] = Field(max_length=256)


class ReplayCursor(Record):
    account_id: UUID
    number: int = Field(ge=0)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    epoch: UUID | None = None
    last_received: Time | None = None
    health_at: Time | None = None
    rules: tuple[InstrumentRules, ...] = Field(default=(), max_length=2)
    books: tuple[OrderBook, ...] = Field(default=(), max_length=2)
    book_sources: dict[Symbol, UUID] = Field(default_factory=dict)
    trade_watermarks: dict[Symbol, Time] = Field(default_factory=dict)
    reason: str | None = "awaiting_new_epoch"

    @model_validator(mode="after")
    def unique(self) -> Self:
        if (
            len({r.symbol for r in self.rules}) != len(self.rules)
            or len({b.symbol for b in self.books}) != len(self.books)
            or set(self.book_sources) != {b.symbol for b in self.books}
            or any(b.epoch != self.epoch for b in self.books)
        ):
            raise ValueError("invalid replay context bindings")
        return self


class ReplayBatch(Record):
    cursor: int
    processed: int
    more: bool
    deferred: bool = False
    market_observations: int
    skipped_trades: int
    reason: str | None
    promotion_evidence: bool = False
    live_execution_available: bool = False


class StoredReplay:
    def __init__(
        self,
        service: PaperExecution,
        venue: PaperExchange,
        *,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.service, self.venue, self.store = service, venue, service.store
        self.history = HistoricalReplay(self.store, venue, service.policy)
        self.fault = fault or (lambda point: None)

    def _cursor(self, row: RowMapping, previous: str | None = None) -> ReplayCursor:
        document = row["document"]
        if fingerprint({"previous": row["previous_hash"], "cursor": document}) != row[
            "fingerprint"
        ] or (previous is not None and previous != row["previous_hash"]):
            raise ValueError("corrupt replay cursor chain")
        cursor = ReplayCursor.restore(document)
        if cursor.number != row["number"] or cursor.account_id != self.store.account_id:
            raise ValueError("replay cursor identity mismatch")
        return cursor

    async def _latest(self, c: AsyncConnection) -> RowMapping | None:
        return (
            (
                await c.execute(
                    select(cursors)
                    .where(cursors.c.account_id == self.store.account_id)
                    .order_by(cursors.c.number.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _save(self, c: AsyncConnection, cursor: ReplayCursor, previous: str) -> None:
        document = cursor.document()
        await c.execute(
            cursors.insert().values(
                account_id=self.store.account_id,
                number=cursor.number,
                previous_hash=previous,
                fingerprint=fingerprint({"previous": previous, "cursor": document}),
                document=document,
            )
        )

    async def enrol(self) -> ReplayCursor:
        """Explicit flat-account baseline. Never backfill order-sensitive UUID history."""
        async with self.service.writer.transaction() as c:
            await self.venue._locked(c)
            state = await self.store.load(c)
            if state is not None and state.accounting_invalidated:
                raise ValueError("accounting correction invalidated replay enrolment")
            row = await self._latest(c)
            if row is not None and await self.history.latest(c) is not None:
                return self._cursor(row)
            if state is None:
                raise ValueError("paper account not initialised")
            remote = self.venue._snapshot(await self.venue._rows(c), state.as_of)
            if (
                state.quantity("BTC")
                or state.quantity("ETH")
                or any(o.state not in TERMINAL for o in state.orders)
                or any(o.state not in TERMINAL for o in remote.orders)
            ):
                raise ValueError(
                    "replay enrolment requires a flat account and no outstanding orders"
                )
            posted = dict(
                (
                    await c.execute(
                        select(fills.c.id, fills.c.document).where(
                            fills.c.account_id == self.store.account_id
                        )
                    )
                ).all()
            )
            if posted != {fill.id: fill.document() for fill in remote.fills}:
                raise ValueError("replay enrolment requires reconciled financial history")
            tail = (
                await c.execute(
                    select(public_events.c.number, public_events.c.fingerprint)
                    .order_by(public_events.c.number.desc())
                    .limit(1)
                )
            ).one_or_none()
            if row is not None:
                cursor = self._cursor(row)
                if cursor.number != (tail.number if tail else 0):
                    raise ValueError("drain legacy replay before historical enrolment")
                await self.history.enrol(c, state, cursor.number, cursor.source_hash)
                return cursor
            cursor = ReplayCursor(
                account_id=self.store.account_id,
                number=tail.number if tail else 0,
                source_hash=tail.fingerprint if tail else EMPTY_HASH,
            )
            await self._save(c, cursor, EMPTY_HASH)
            await self.history.enrol(c, state, cursor.number, cursor.source_hash)
            return cursor

    async def audit(self) -> ReplayCursor:
        """Owner-start full cursor chain check in bounded pages, with source anchors."""
        async with self.service.writer.transaction() as c:
            number, previous, last = -1, EMPTY_HASH, None
            while True:
                rows = (
                    (
                        await c.execute(
                            select(cursors)
                            .where(
                                cursors.c.account_id == self.store.account_id,
                                cursors.c.number > number,
                            )
                            .order_by(cursors.c.number)
                            .limit(AUDIT_PAGE)
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
                                    public_events.c.number.in_([row["number"] for row in rows])
                                )
                            )
                        ).all()
                    )
                    if rows
                    else {}
                )
                for row in rows:
                    last = self._cursor(row, previous)
                    if last.number:
                        if anchors.get(last.number) != last.source_hash:
                            raise ValueError("replay cursor source anchor mismatch")
                    number, previous = last.number, row["fingerprint"]
                if len(rows) < AUDIT_PAGE:
                    break
            if last is None:
                raise ValueError("paper account must explicitly enrol before replay")
            await self.history.audit(c, last.number)
            return last

    async def _invalidate(self, c: AsyncConnection, at: datetime, active: bool) -> None:
        # Queue credit never survives a known coverage interruption, even when
        # the collector later recovers inside the same subscription epoch.
        if not active:
            return
        rows = await self.venue._rows(c)
        number = await self.venue._number(c)
        for order in self.venue._snapshot(rows, at).orders:
            if order.state not in TERMINAL and order.queue_initialized:
                number += 1
                reset = order.model_copy(
                    update={"queue_initialized": False, "queue_ahead": Decimal(0)}
                )
                await self.venue._append(
                    c,
                    number=number,
                    source=f"queue-reset:{number}",
                    kind="order",
                    document=reset.document(),
                )

    async def _consume(
        self,
        c: AsyncConnection,
        context: ReplayCursor,
        receipt: ReplayReceipt,
        active: bool,
        cancelled: tuple[VenueOrder, ...],
        historical: list[HistoricalValuation],
    ) -> tuple[ReplayCursor, int, int]:
        at, channel = receipt.received_at, receipt.channel
        if channel == "epoch_started":
            await self._invalidate(c, at, active)
            return (
                context.model_copy(
                    update={
                        "epoch": receipt.epoch,
                        "last_received": at,
                        "books": (),
                        "book_sources": {},
                        "trade_watermarks": {},
                        "rules": (),
                        "health_at": None,
                        "reason": "warming_up",
                    }
                ),
                0,
                0,
            )
        if context.epoch != receipt.epoch:
            await self._invalidate(c, at, active)
            return (
                context.model_copy(
                    update={
                        "books": (),
                        "book_sources": {},
                        "trade_watermarks": {},
                        "health_at": None,
                        "reason": "epoch_mismatch",
                    }
                ),
                0,
                len(receipt.trades),
            )
        if not receipt.valid or channel in {
            "capture_fault",
            "capture_stopped",
            "quarantine",
            "coverage_gap",
        }:
            await self._invalidate(c, at, active)
            return (
                context.model_copy(
                    update={
                        "books": (),
                        "book_sources": {},
                        "trade_watermarks": {},
                        "health_at": None,
                        "reason": "capture_interrupted",
                    }
                ),
                0,
                0,
            )
        if channel == "instrument_rules":
            raw = receipt.normalized.get("rules")
            if not isinstance(raw, list) or len(raw) != 2:
                raise ValueError("invalid replay rules")
            rules = tuple(InstrumentRules.model_validate_json(json.dumps(r)) for r in raw)
            if {r.symbol for r in rules} != {"BTC/GBP", "ETH/GBP"} or any(
                r.venue != "kraken" for r in rules
            ):
                raise ValueError("invalid replay instrument binding")
            return context.model_copy(update={"rules": rules}), 0, 0
        if channel == "capture_health":
            if receipt.normalized.get("market_ready") is not True:
                raise ValueError("inconsistent capture health")
            return context.model_copy(update={"health_at": at, "reason": None}), 0, 0
        if channel not in {"book", "trade"}:
            if channel in {
                "status",
                "instrument",
                "heartbeat",
                "subscription",
                "trade_recovery",
                "local:closed_candle",
            } or channel.startswith("rest:"):
                if channel == "status" and receipt.normalized.get("system") != "online":
                    await self._invalidate(c, at, active)
                    context = context.model_copy(
                        update={
                            "books": (),
                            "book_sources": {},
                            "trade_watermarks": {},
                            "health_at": None,
                            "reason": "system_offline",
                        }
                    )
                return context, 0, 0
            raise ValueError("unclassified public replay channel")
        if context.last_received and at < context.last_received:
            raise ValueError("public replay receipt time regressed")
        books = {b.symbol: b for b in context.books}
        sources = dict(context.book_sources)
        watermarks = dict(context.trade_watermarks)
        observations: list[tuple[OrderBook, MarketTrade | None]] = []
        if channel == "book":
            raw = receipt.normalized.get("books")
            if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
                raise ValueError("invalid replay book collection")
            for document in raw:
                book = OrderBook.model_validate_json(json.dumps(document))
                previous_book = books.get(book.symbol)
                if (
                    book.epoch != receipt.epoch
                    or book.received_at != at
                    or book.venue != "kraken"
                    or (book.symbol in books and book.received_at < books[book.symbol].received_at)
                    or (
                        previous_book is not None
                        and book.event_at is not None
                        and previous_book.event_at is not None
                        and book.event_at < previous_book.event_at
                    )
                ):
                    raise ValueError("invalid replay book identity/time")
                books[book.symbol] = book
                sources[book.symbol] = receipt.receipt_id
                observations.append((book, None))
        else:
            if receipt.normalized.get("snapshot") is not False:
                if receipt.trades:
                    raise ValueError("trade snapshot cannot contain replay liquidity")
            else:
                for trade in receipt.trades:
                    prior_book = books.get(trade.symbol)
                    if trade.received_at != at or trade.venue != "kraken":
                        raise ValueError("invalid replay trade identity/time")
                    if (
                        prior_book is None
                        or not 0 <= (at - trade.event_at).total_seconds() <= 5
                        or trade.event_at < (prior_book.event_at or prior_book.received_at)
                        or (
                            trade.symbol in watermarks and trade.event_at < watermarks[trade.symbol]
                        )
                    ):
                        await self._invalidate(c, at, active)
                        continue  # Late trades cannot consume a queue observed in their future.
                    observations.append((prior_book, trade))
                    watermarks[trade.symbol] = trade.event_at
        context = context.model_copy(
            update={
                "books": tuple(books.values()),
                "book_sources": sources,
                "last_received": at,
                "trade_watermarks": watermarks,
            }
        )
        rules_by_symbol = {r.symbol: r for r in context.rules}
        historical[0] = self.history.observe(
            historical[0],
            at,
            self.history.marks(context.books, context.book_sources),
            self._reason(context, at),
        )
        if active:
            await self.history.cancel_entries(c, historical[0], at)
        applied, used_trades = 0, 0
        for book, aggression in observations:
            rule = rules_by_symbol.get(book.symbol)
            if (
                context.health_at is None
                or not 0 <= (at - context.health_at).total_seconds() <= 10
                or set(books) != {"BTC/GBP", "ETH/GBP"}
                or rule is None
                or rule.status != "online"
                or not 0 <= (at - rule.observed_at).total_seconds() <= 3600
                or not 0 <= (at - book.received_at).total_seconds() <= 5
                or (
                    book.event_at is not None and not 0 <= (at - book.event_at).total_seconds() <= 5
                )
            ):
                await self._invalidate(c, at, active)
                continue
            if active:
                await self.venue.advance(
                    book, rule, at, aggression, connection=c, observation_id=sources[book.symbol]
                )
                historical[0] = await self.history.import_fills(c, historical[0], at)
                await self.history.cancel_entries(c, historical[0], at)
            else:
                effective_at = aggression.event_at if aggression else at
                if any(
                    o.intent.symbol == book.symbol
                    and o.cancelled_at is not None
                    and o.accepted_at + self.venue.latency <= effective_at < o.cancelled_at
                    for o in cancelled
                ):
                    raise ValueError(
                        "paper cancellation overtook late market evidence; review required"
                    )
            applied += 1
            used_trades += int(aggression is not None)
        return context, applied, len(receipt.trades) - used_trades

    async def batch(self, *, limit: int = 32, at: datetime) -> ReplayBatch:
        if not 1 <= limit <= 64:
            raise ValueError("replay limit must be 1..64")
        at = utc_datetime(at)
        if (await self.service._state()).accounting_invalidated:
            raise ValueError(
                "accounting correction invalidated historical replay; rebuild required"
            )
        async with self.service.writer.transaction() as c:
            previous = await self._latest(c)
            if previous is None:
                raise ValueError("paper account must explicitly enrol before replay")
            context = self._cursor(previous)
            history_row = await self.history.latest(c)
            if history_row is None:
                raise ValueError("historical valuation baseline missing; flat enrolment required")
            if history_row["number"] != context.number:
                raise ValueError("historical valuation cursor mismatch")
            if self.history.restore(history_row).source_hash != context.source_hash:
                raise ValueError("historical valuation source mismatch")
            pending = await c.scalar(
                select(public_events.c.number)
                .where(public_events.c.number > context.number)
                .limit(1)
            )
            if pending is None:
                return ReplayBatch(
                    cursor=context.number,
                    processed=0,
                    more=False,
                    market_observations=0,
                    skipped_trades=0,
                    reason=self._reason(context, at),
                )
        old = await self.service._state()
        if old.ready or old.incident != "paper_replay_pending":
            await self.service._save(
                old,
                old.model_copy(update={"ready": False, "incident": "paper_replay_pending"}),
                at=at,
                kind="paper_replay_pending",
            )
        async with self.service.writer.transaction() as c:
            await self.venue._locked(c)
            remote = self.venue._snapshot(await self.venue._rows(c), at)
            active = any(order.state not in TERMINAL for order in remote.orders)
            cancelled = tuple(order for order in remote.orders if order.cancelled_at is not None)
            previous = await self._latest(c)
            if previous is None:
                raise ValueError("paper account must explicitly enrol before replay")
            context = self._cursor(previous)
            history_row = await self.history.latest(c)
            if history_row is None or history_row["number"] != context.number:
                raise ValueError("historical valuation cursor mismatch")
            historical = [self.history.restore(history_row)]
            history_hash = history_row["fingerprint"]
            controls = await self.history.controls(c, historical[0].control_version)
            rows = (
                (
                    await c.execute(
                        select(
                            public_events,
                            MarketReceipt.__table__.c.payload_hash.label("receipt_hash"),
                            MarketReceipt.__table__.c.epoch.label("receipt_epoch"),
                            MarketReceipt.__table__.c.channel.label("receipt_channel"),
                            MarketReceipt.__table__.c.received_at.label("receipt_at"),
                            MarketReceipt.__table__.c.valid.label("receipt_valid"),
                        )
                        .select_from(public_events)
                        .join(
                            MarketReceipt.__table__, public_events.c.receipt_id == MarketReceipt.id
                        )
                        .where(public_events.c.number > context.number)
                        .order_by(public_events.c.number)
                        .limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            applied = skipped = processed = 0
            deferred = False
            for row in rows[:limit]:
                if fingerprint(row["document"]) != row["fingerprint"]:
                    raise ValueError("corrupt public replay envelope")
                receipt = ReplayReceipt.restore(row["document"])
                if (
                    receipt.receipt_id != row["receipt_id"]
                    or receipt.payload_hash != row["receipt_hash"]
                    or receipt.epoch != row["receipt_epoch"]
                    or receipt.channel != row["receipt_channel"]
                    or receipt.received_at != row["receipt_at"]
                    or receipt.valid != row["receipt_valid"]
                    or receipt.received_at > row["published_at"] + timedelta(milliseconds=500)
                ):
                    raise ValueError("public replay source/time mismatch")
                if receipt.received_at > at:
                    # Concurrent capture may commit after this tick's decision
                    # time. Preserve the entire suffix, never skip to later IDs.
                    deferred = True
                    break
                # A timer need not hit 00:00:00.000000. Value that instant from
                # the last complete healthy prefix, before applying the first
                # following receipt. Neither its prices nor later trades enter
                # this boundary. A >5s bridge, reconnect or fault stays missing.
                boundary_at = datetime.combine(receipt.received_at.date(), time(), UTC)
                historical[0] = historical[0].model_copy(update={"day_boundary": None})
                if (
                    historical[0].state.as_of < boundary_at <= receipt.received_at
                    and (receipt.received_at - boundary_at).total_seconds() <= 5
                    and context.last_received is not None
                    and 0 <= (boundary_at - context.last_received).total_seconds() <= 5
                    and receipt.epoch == context.epoch
                    and receipt.valid
                    and receipt.channel
                    not in {
                        "capture_fault",
                        "capture_stopped",
                        "quarantine",
                        "coverage_gap",
                        "epoch_started",
                    }
                    and self._reason(context, boundary_at) is None
                ):
                    boundary_value = await self.history.apply_controls(
                        c, historical[0], controls, boundary_at - timedelta(microseconds=1)
                    )
                    boundary_value = self.history.observe(
                        boundary_value,
                        boundary_at,
                        self.history.marks(context.books, context.book_sources),
                        None,
                    )
                    if boundary_value.nav_gbp is not None:
                        boundary = DayBoundary(
                            at=boundary_at,
                            previous_source=context.number,
                            previous_source_hash=context.source_hash,
                            previous_valuation_hash=history_hash,
                            following_source=row["number"],
                            following_source_hash=row["fingerprint"],
                            context_hash=fingerprint(context.document()),
                            state=boundary_value.state,
                            nav_gbp=boundary_value.nav_gbp,
                            unit_price=unit_price(boundary_value.state, boundary_at),
                            incomplete_since=boundary_value.incomplete_since,
                        )
                        boundary.validate_economics()
                        historical[0] = boundary_value.model_copy(update={"day_boundary": boundary})
                historical[0] = await self.history.apply_controls(
                    c, historical[0], controls, receipt.received_at
                )
                historical[0] = historical[0].model_copy(
                    update={
                        "number": row["number"],
                        "source_hash": row["fingerprint"],
                        "source_received_at": receipt.received_at,
                    }
                )
                context, count, missed = await self._consume(
                    c, context, receipt, active, cancelled, historical
                )
                if receipt.received_at < historical[0].state.as_of:
                    if receipt.channel != "heartbeat":
                        raise ValueError("historical market/control time regressed")
                    # A delayed heartbeat has no economic timestamp or prices.
                    # Retain the last state, rather than reprice it from its future.
                    historical[0] = historical[0].model_copy(
                        update={"nav_gbp": None, "reason": "late_auxiliary_receipt"}
                    )
                else:
                    historical[0] = self.history.observe(
                        historical[0],
                        receipt.received_at,
                        self.history.marks(context.books, context.book_sources),
                        self._reason(context, receipt.received_at),
                    )
                    if active:
                        await self.history.cancel_entries(c, historical[0], receipt.received_at)
                history_hash = await self.history.save(c, historical[0], history_hash)
                processed += 1
                applied += count
                skipped += missed
                context = context.model_copy(
                    update={"number": row["number"], "source_hash": row["fingerprint"]}
                )
            if processed:
                self.fault("before_cursor")
                await self.history.propagate(c, historical[0], at)
                await self._save(c, context, previous["fingerprint"])
            self.fault("before_commit")
        self.fault("after_commit")
        return ReplayBatch(
            cursor=context.number,
            processed=processed,
            more=not deferred and len(rows) > limit and rows[limit]["receipt_at"] <= at,
            deferred=deferred or (len(rows) > limit and rows[limit]["receipt_at"] > at),
            market_observations=applied,
            skipped_trades=skipped,
            reason=self._reason(context, at),
        )

    @staticmethod
    def _reason(context: ReplayCursor, at: datetime) -> str | None:
        if (
            context.health_at is None
            or not 0 <= (at - context.health_at).total_seconds() <= 10
            or {b.symbol for b in context.books} != {"BTC/GBP", "ETH/GBP"}
            or {r.symbol for r in context.rules} != {"BTC/GBP", "ETH/GBP"}
            or any(
                r.status != "online" or not 0 <= (at - r.observed_at).total_seconds() <= 3600
                for r in context.rules
            )
            or any(
                not 0 <= (at - b.received_at).total_seconds() <= 5
                or (b.event_at is not None and not 0 <= (at - b.event_at).total_seconds() <= 5)
                for b in context.books
            )
        ):
            return context.reason or "replay_evidence_unavailable"
        return context.reason
