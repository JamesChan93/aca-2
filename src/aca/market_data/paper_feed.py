"""Read recent, same-epoch public evidence; never infer health from quote recency."""

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from aca.domain.market import InstrumentRules, OrderBook
from aca.domain.trading import Valuation
from aca.domain.values import utc_datetime
from aca.storage.models import MarketReceipt
from aca.storage.paper import PaperStore


class FeedUnavailable(ValueError):
    """Current complete public market evidence is unavailable."""


class StoredPaperFeed:
    def __init__(self, store: PaperStore) -> None:
        self.store = store

    async def marks(self, at: datetime) -> tuple[Valuation, ...]:
        at = utc_datetime(at)
        async with self.store.engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ")

            async def latest(channel: str) -> MarketReceipt:
                # Bounded result, supported by channel/time index from 0005.
                row = (
                    (
                        await c.execute(
                            select(MarketReceipt.__table__)
                            .where(
                                MarketReceipt.channel == channel, MarketReceipt.received_at <= at
                            )
                            .order_by(MarketReceipt.received_at.desc(), MarketReceipt.id.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise FeedUnavailable("public capture evidence missing")
                return MarketReceipt(**row)

            epoch = await latest("epoch_started")
            health = await latest("capture_health")
            rules = await latest("instrument_rules")
            if (
                not health.valid
                or health.normalized.get("market_ready") is not True
                or health.epoch != epoch.epoch
                or rules.epoch != epoch.epoch
                or not rules.valid
                or not 0 <= (at - health.received_at).total_seconds() <= 10
                or health.received_at < epoch.received_at
                or not 0 <= (at - rules.received_at).total_seconds() <= 3600
            ):
                raise FeedUnavailable("public capture health or rules stale/incomplete")
            for channel in ("capture_fault", "capture_stopped", "quarantine", "coverage_gap"):
                fault = (
                    await c.execute(
                        select(MarketReceipt.received_at)
                        .where(
                            MarketReceipt.channel == channel,
                            MarketReceipt.epoch == epoch.epoch,
                            MarketReceipt.received_at <= at,
                        )
                        .order_by(MarketReceipt.received_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if fault is not None and fault >= health.received_at:
                    raise FeedUnavailable("public capture interrupted")
            rows = (
                (
                    await c.execute(
                        select(MarketReceipt.__table__)
                        .where(
                            MarketReceipt.channel == "book",
                            MarketReceipt.epoch == epoch.epoch,
                            MarketReceipt.received_at >= at - timedelta(seconds=5),
                            MarketReceipt.received_at <= at,
                        )
                        .order_by(MarketReceipt.received_at.desc(), MarketReceipt.id.desc())
                        .limit(20)
                    )
                )
                .mappings()
                .all()
            )
        try:
            raw_rules = rules.normalized["rules"]
            if not isinstance(raw_rules, list) or len(raw_rules) != 2:
                raise ValueError("incomplete rules")
            by_symbol = {
                r.symbol: r
                for r in (InstrumentRules.model_validate_json(json.dumps(doc)) for doc in raw_rules)
            }
            books: dict[str, OrderBook] = {}
            for row in rows:
                if not row["valid"]:
                    raise ValueError("invalid book evidence")
                raw_books = row["normalized"].get("books")
                if not isinstance(raw_books, list) or not 1 <= len(raw_books) <= 2:
                    raise ValueError("invalid book collection")
                for doc in raw_books:
                    book = OrderBook.model_validate_json(json.dumps(doc))
                    rule = by_symbol[book.symbol]
                    if (
                        book.venue != "kraken"
                        or rule.venue != "kraken"
                        or book.epoch != epoch.epoch
                        or book.received_at != row["received_at"]
                        or rule.status != "online"
                        or not 0 <= (at - rule.observed_at).total_seconds() <= 3600
                        or not 0 <= (at - book.received_at).total_seconds() <= 5
                        or (
                            book.event_at is not None
                            and not 0 <= (at - book.event_at).total_seconds() <= 5
                        )
                    ):
                        raise ValueError("invalid current quote evidence")
                    books.setdefault(book.symbol, book)
            if set(books) != {"BTC/GBP", "ETH/GBP"} or set(by_symbol) != set(books):
                raise ValueError("incomplete current books")
            return tuple(
                Valuation(
                    asset="BTC" if symbol == "BTC/GBP" else "ETH",
                    bid_gbp=books[symbol].bids[0].price,
                    observed_at=books[symbol].received_at,
                    source=f"kraken-public-book:{epoch.epoch}",
                )
                for symbol in ("BTC/GBP", "ETH/GBP")
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise FeedUnavailable("invalid public market projection") from exc
