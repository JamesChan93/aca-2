"""Durable local venue: pessimistic depth/queue fills, no Kraken mutation path."""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.accounting.ledger import stored
from aca.domain.market import InstrumentRules, MarketTrade, OrderBook
from aca.domain.trading import TERMINAL, ZERO, Fee, Fill, OrderIntent, VenueOrder, VenueSnapshot
from aca.domain.values import FINANCIAL_CONTEXT, utc_datetime
from aca.ports.execution import DefinitiveRejection, UnknownOutcome
from aca.sizing.long_spot import round_increment
from aca.storage.paper import PaperStore, lock_key, venue_events


class PaperExchange:
    """Separate authoritative venue stream. Snapshot is complete, never paginated.

    Latency is a minimum 250ms. A 1% participation cap applies to depth and
    aggressive trades. Bar data cannot manufacture passive liquidity.
    """

    def __init__(self, store: PaperStore, *, latency_ms: int = 250) -> None:
        if not 250 <= latency_ms <= 60_000:
            raise ValueError("invalid paper latency")
        self.store, self.latency = store, timedelta(milliseconds=latency_ms)

    async def _rows(self, connection: AsyncConnection) -> list[dict[str, object]]:
        rows = (
            (
                await connection.execute(
                    select(venue_events.c.kind, venue_events.c.document)
                    .where(venue_events.c.account_id == self.store.account_id)
                    .order_by(venue_events.c.number)
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _snapshot(rows: list[dict[str, object]], at: datetime) -> VenueSnapshot:
        orders: dict[UUID, VenueOrder] = {}
        fills: list[Fill] = []
        for row in rows:
            doc = row["document"]
            if not isinstance(doc, dict):
                raise ValueError("invalid paper venue record")
            if row["kind"] == "order":
                order = VenueOrder.restore(doc)
                orders[order.intent.client_id] = order
            elif row["kind"] == "fill":
                fills.append(Fill.restore(doc))
        return VenueSnapshot(
            orders=tuple(orders.values()), fills=tuple(fills), complete=True, observed_at=at
        )

    async def _append(
        self,
        connection: AsyncConnection,
        *,
        number: int,
        source: str,
        kind: str,
        document: dict[str, object],
    ) -> None:
        await connection.execute(
            venue_events.insert().values(
                account_id=self.store.account_id,
                number=number,
                source=source,
                kind=kind,
                document=document,
            )
        )

    async def _locked(self, connection: AsyncConnection) -> None:
        # Separate namespace from the account execution session lock. Serialises
        # simulator evidence transactions, never held across an external request.
        from sqlalchemy import text

        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": -lock_key(self.store.account_id)}
        )

    async def snapshot(self, at: datetime) -> VenueSnapshot:
        at = utc_datetime(at)
        async with self.store.engine.connect() as connection:
            return self._snapshot(await self._rows(connection), at)

    async def submit(self, intent: OrderIntent, at: datetime) -> VenueOrder:
        at = utc_datetime(at)
        async with self.store.engine.begin() as connection:
            await self._locked(connection)
            rows = await self._rows(connection)
            snapshot = self._snapshot(rows, at)
            if any(o.intent.client_id == intent.client_id for o in snapshot.orders):
                raise UnknownOutcome("permanent paper client id already consumed")
            if at >= intent.expires_at or at < intent.created_at:
                raise DefinitiveRejection("expired")
            order = VenueOrder(
                intent=intent, venue_id=f"paper-{intent.client_id}", state="OPEN", accepted_at=at
            )
            await self._append(
                connection,
                number=len(rows) + 1,
                source=f"submit:{intent.client_id}",
                kind="order",
                document=order.document(),
            )
            return order

    async def cancel(self, client_id: UUID, at: datetime) -> VenueOrder:
        at = utc_datetime(at)
        async with self.store.engine.begin() as connection:
            await self._locked(connection)
            rows = await self._rows(connection)
            order = next(
                o for o in self._snapshot(rows, at).orders if o.intent.client_id == client_id
            )
            if order.state in TERMINAL:
                return order
            order = order.model_copy(update={"state": "CANCELLED"})
            await self._append(
                connection,
                number=len(rows) + 1,
                source=f"cancel:{client_id}",
                kind="order",
                document=order.document(),
            )
            return order

    async def advance(
        self,
        book: OrderBook,
        rules: InstrumentRules,
        at: datetime,
        trade: MarketTrade | None = None,
    ) -> VenueSnapshot:
        """Consume one validated chronological replay observation, at most once."""
        at = utc_datetime(at)
        if (
            book.symbol != rules.symbol
            or book.venue != rules.venue
            or rules.status != "online"
            or not 0 <= (at - book.received_at).total_seconds() <= 5
            or (book.event_at is not None and not 0 <= (at - book.event_at).total_seconds() <= 5)
            or not 0 <= (at - rules.observed_at).total_seconds() <= 3600
        ):
            raise ValueError("invalid simulation market evidence")
        if trade is not None and (
            trade.symbol != book.symbol
            or trade.venue != book.venue
            or not 0 <= (at - trade.event_at).total_seconds() <= 5
        ):
            raise ValueError("invalid simulation trade evidence")
        async with self.store.engine.begin() as connection:
            await self._locked(connection)
            rows = await self._rows(connection)
            public_key = f"trade:{trade.venue}:{trade.symbol}:{trade.trade_id}" if trade else None
            if public_key and await connection.scalar(
                select(venue_events.c.number).where(
                    venue_events.c.account_id == self.store.account_id,
                    venue_events.c.source == public_key,
                )
            ):
                return self._snapshot(rows, at)
            snapshot = self._snapshot(rows, at)
            number = len(rows)
            book_key = f"book:{book.epoch}:{book.symbol}:{book.received_at.isoformat()}"
            seen_book = await connection.scalar(
                select(venue_events.c.number).where(
                    venue_events.c.account_id == self.store.account_id,
                    venue_events.c.source == book_key,
                )
            )
            for row in reversed(rows):
                doc = row["document"]
                if (
                    row["kind"] == "market_book"
                    and isinstance(doc, dict)
                    and doc.get("symbol") == book.symbol
                ):
                    if book.received_at < datetime.fromisoformat(str(doc["received_at"])):
                        raise ValueError("paper market observation regressed")
                    break
            if seen_book and trade is None:
                return snapshot

            async def append(source: str, kind: str, doc: dict[str, object]) -> None:
                nonlocal number
                number += 1
                await self._append(
                    connection, number=number, source=source, kind=kind, document=doc
                )

            if public_key:
                await append(public_key, "market_trade", {"at": at.isoformat()})
            if not seen_book:
                await append(
                    book_key,
                    "market_book",
                    {"symbol": book.symbol, "received_at": book.received_at.isoformat()},
                )
            with localcontext(FINANCIAL_CONTEXT):
                budgets = {
                    "buy": [level.quantity * Decimal("0.01") for level in book.asks],
                    "sell": [level.quantity * Decimal("0.01") for level in book.bids],
                }
                for initial in snapshot.orders:
                    if initial.intent.symbol != book.symbol or initial.state in TERMINAL:
                        continue
                    order, i = initial, initial.intent
                    if at < order.accepted_at + self.latency:
                        continue
                    if seen_book and not (i.purpose == "entry" and i.time_in_force == "GTD"):
                        continue
                    if order.triggered_at and at < order.triggered_at + self.latency:
                        continue
                    if i.time_in_force in {"IOC", "FOK"} and at >= i.expires_at:
                        order = order.model_copy(update={"state": "CANCELLED"})
                    elif i.purpose == "entry" and at >= i.created_at + timedelta(seconds=60):
                        order = order.model_copy(update={"state": "EXPIRED"})
                    elif i.purpose == "protection" and not order.triggered:
                        if book.bids[0].price <= i.stop_trigger:
                            order = order.model_copy(update={"triggered": True, "triggered_at": at})
                        # A trigger observation never receives a same-tick fill.
                    else:
                        quantity, cost = ZERO, ZERO
                        remaining = i.quantity - order.filled
                        passive = i.purpose == "entry" and i.time_in_force == "GTD"
                        if passive:
                            if not order.queue_initialized or order.queue_epoch != book.epoch:
                                order = order.model_copy(
                                    update={
                                        "queue_initialized": True,
                                        "queue_epoch": book.epoch,
                                        "queue_ahead": sum(
                                            (
                                                level.quantity
                                                for level in book.bids
                                                if level.price >= i.limit_price
                                            ),
                                            ZERO,
                                        ),
                                    }
                                )
                            elif (
                                trade
                                and trade.side == "sell"
                                and trade.price <= i.limit_price
                                and trade.event_at >= order.accepted_at + self.latency
                            ):
                                consumed = min(order.queue_ahead, trade.quantity)
                                order = order.model_copy(
                                    update={"queue_ahead": order.queue_ahead - consumed}
                                )
                                quantity = round_increment(
                                    min(remaining, (trade.quantity - consumed) * Decimal("0.01")),
                                    rules.quantity_increment,
                                )
                                cost = quantity * i.limit_price
                        else:
                            levels = book.asks if i.side == "buy" else book.bids
                            consumed_depth: list[Decimal] = []
                            for index, level in enumerate(levels):
                                if (i.side == "buy" and level.price > i.limit_price) or (
                                    i.side == "sell" and level.price < i.limit_price
                                ):
                                    break
                                q = round_increment(
                                    min(remaining - quantity, budgets[i.side][index]),
                                    rules.quantity_increment,
                                )
                                cost += q * level.price
                                quantity += q
                                consumed_depth.append(q)
                            if i.time_in_force == "FOK" and quantity != remaining:
                                quantity, cost = ZERO, ZERO
                            else:
                                for index, used in enumerate(consumed_depth):
                                    budgets[i.side][index] -= used
                        if quantity:
                            price = stored(cost / quantity)
                            fill_id = str(uuid5(i.client_id, f"fill:{number + 1}"))
                            fee = Fee(
                                asset="GBP",
                                quantity=stored(cost * i.fee_bound),
                                rate_gbp=Decimal(1),
                                valued_at=at,
                                valuation_source="paper-cost-policy-v1",
                            )
                            fill = Fill(
                                id=fill_id,
                                client_id=i.client_id,
                                symbol=i.symbol,
                                side=i.side,
                                quantity=quantity,
                                price=price,
                                fees=(fee,),
                                occurred_at=at,
                                liquidity="maker" if passive else "taker",
                            )
                            await append(f"fill:{fill_id}", "fill", fill.document())
                            cumulative = order.filled + quantity
                            order = order.model_copy(
                                update={
                                    "filled": cumulative,
                                    "state": "FILLED"
                                    if cumulative == i.quantity
                                    else "PARTIALLY_FILLED",
                                }
                            )
                            if (
                                i.purpose == "entry"
                                and quantity >= rules.minimum_quantity
                                and quantity * i.protective_limit >= rules.minimum_cost
                            ):
                                child_id = uuid5(i.client_id, fill_id)
                                child = OrderIntent(
                                    id=child_id,
                                    client_id=child_id,
                                    symbol=i.symbol,
                                    purpose="protection",
                                    side="sell",
                                    quantity=quantity,
                                    limit_price=i.protective_limit,
                                    stop_trigger=i.stop_trigger,
                                    protective_limit=ZERO,
                                    fee_bound=i.fee_bound,
                                    allowance_per_unit=ZERO,
                                    created_at=at,
                                    expires_at=at + timedelta(days=3650),
                                    strategy_version="safety-v1",
                                    rules_hash=i.rules_hash,
                                    episode_id=i.episode_id,
                                    parent_id=i.client_id,
                                    time_in_force="GTC",
                                )
                                protective = VenueOrder(
                                    intent=child,
                                    venue_id=f"paper-{child_id}",
                                    state="OPEN",
                                    accepted_at=at,
                                )
                                await append(f"child:{child_id}", "order", protective.document())
                        if i.time_in_force in {"IOC", "FOK"} and order.state != "FILLED":
                            order = order.model_copy(update={"state": "CANCELLED"})
                    if order != initial:
                        await append(f"order:{i.client_id}:{number + 1}", "order", order.document())
            return self._snapshot(await self._rows(connection), at)
