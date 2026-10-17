"""Connection-local L2 book; every accepted message validates top-10 CRC32."""

import zlib
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from aca.adapters.kraken.wire import BookData, MarketDataError
from aca.domain.market import Level, OrderBook, Symbol


def checksum(bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal]) -> int:
    pieces: list[str] = []
    for levels, reverse in ((asks, False), (bids, True)):
        for price in sorted(levels, reverse=reverse)[:10]:
            for value in (price, levels[price]):
                pieces.append(format(value, "f").replace(".", "").lstrip("0"))
    return zlib.crc32("".join(pieces).encode()) & 0xFFFFFFFF


class KrakenBook:
    def __init__(self, symbol: Symbol, epoch: UUID) -> None:
        self.symbol = symbol
        self.epoch = epoch
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.book: OrderBook | None = None

    def invalidate(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.book = None

    def apply(self, data: BookData, kind: str, received: datetime) -> OrderBook:
        try:
            if data.symbol != self.symbol or kind not in {"snapshot", "update"}:
                raise MarketDataError("unexpected book binding or type")
            if kind == "snapshot":
                self.invalidate()
            elif self.book is None:
                raise MarketDataError("book update before snapshot")
            if (
                self.book
                and data.timestamp
                and self.book.event_at
                and data.timestamp < self.book.event_at
            ):
                raise MarketDataError("book timestamp regressed")
            for levels, changes, reverse in (
                (self.bids, data.bids, True),
                (self.asks, data.asks, False),
            ):
                for item in changes:
                    # Replace both price key and quantity to preserve wire decimal formatting.
                    levels.pop(item.price, None)
                    if item.qty:
                        levels[item.price] = item.qty
                for price in sorted(levels, reverse=reverse)[10:]:
                    del levels[price]
            if checksum(self.bids, self.asks) != data.checksum:
                raise MarketDataError("book checksum mismatch")
            self.book = OrderBook(
                venue="kraken",
                symbol=self.symbol,
                epoch=self.epoch,
                bids=tuple(
                    Level(price=p, quantity=self.bids[p]) for p in sorted(self.bids, reverse=True)
                ),
                asks=tuple(Level(price=p, quantity=self.asks[p]) for p in sorted(self.asks)),
                checksum_valid=True,
                event_at=data.timestamp,
                received_at=received,
            )
            return self.book
        except Exception:
            self.invalidate()
            raise
