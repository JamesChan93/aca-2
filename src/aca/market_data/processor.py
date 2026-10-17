"""Ordered per-connection validation. Snapshot completion never implies trade history."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from aca.adapters.kraken.book import KrakenBook
from aca.adapters.kraken.wire import (
    BINDINGS,
    STATUSES,
    BookData,
    InstrumentData,
    MarketDataError,
    TradeData,
    decode,
)
from aca.domain.market import InstrumentRules, MarketTrade, Symbol


@dataclass(frozen=True)
class Frame:
    channel: str
    normalized: dict[str, Any] = field(default_factory=dict)
    trades: tuple[MarketTrade, ...] = ()


class PublicProcessor:
    def __init__(self, rules: tuple[InstrumentRules, ...], epoch: UUID) -> None:
        self.rules = {r.symbol: r for r in rules}
        if set(self.rules) != set(BINDINGS):
            raise MarketDataError("incomplete instrument rules")
        self.epoch = epoch
        self.books = {symbol: KrakenBook(symbol, epoch) for symbol in BINDINGS}
        self.instrument_seen: set[Symbol] = set()
        self.trade_snapshot_seen: set[Symbol] = set()
        self.last_received: datetime | None = None
        self.system_online = False

    def invalidate(self) -> None:
        for book in self.books.values():
            book.invalidate()
        self.instrument_seen.clear()
        self.trade_snapshot_seen.clear()
        self.system_online = False

    def process(self, raw: str, received: datetime) -> Frame:
        try:
            return self._process(raw, received)
        except Exception:
            self.invalidate()
            raise

    def _process(self, raw: str, received: datetime) -> Frame:
        message = decode(raw)
        if "method" in message:
            if message.get("method") != "subscribe" or message.get("success") is not True:
                raise MarketDataError("public subscription rejected or unknown response")
            return Frame("subscription")
        channel = message.get("channel")
        if channel == "heartbeat":
            self.last_received = received
            return Frame("heartbeat")
        if channel == "status":
            data = message.get("data")
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise MarketDataError("invalid system status")
            status = data[0].get("system")
            if status not in {"online", "maintenance", "cancel_only", "post_only"}:
                raise MarketDataError("unknown system status")
            self.system_online = status == "online"
            self.last_received = received
            return Frame("status", {"system": status})
        if channel not in {"instrument", "book", "trade"} or message.get("type") not in {
            "snapshot",
            "update",
        }:
            raise MarketDataError("unknown safety-critical channel or message type")
        kind = message["type"]
        if channel == "instrument":
            data = message.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("pairs"), list):
                raise MarketDataError("missing instrument pairs")
            normalized: list[dict[str, object]] = []
            for raw_pair in data["pairs"]:
                if not isinstance(raw_pair, dict):
                    raise MarketDataError("invalid instrument row")
                if raw_pair.get("symbol") not in BINDINGS:
                    continue
                pair = InstrumentData.model_validate(raw_pair)
                rule = self.rules[pair.symbol]
                if (pair.base, pair.quote) != (
                    pair.symbol.split("/")[0],
                    "GBP",
                ) or pair.status not in STATUSES:
                    raise MarketDataError("unknown v2 instrument binding or status")
                if (
                    pair.price_increment,
                    pair.qty_increment,
                    pair.qty_min,
                    pair.cost_min,
                    pair.status,
                ) != (
                    rule.price_increment,
                    rule.quantity_increment,
                    rule.minimum_quantity,
                    rule.minimum_cost,
                    rule.status,
                ):
                    raise MarketDataError("REST and v2 instrument rules conflict")
                self.instrument_seen.add(pair.symbol)
                normalized.append(pair.model_dump(mode="json"))
            self.last_received = received
            return Frame("instrument", {"pairs": normalized})
        data = message.get("data")
        if not isinstance(data, list) or not data or len(data) > 10000:
            raise MarketDataError("invalid market event list")
        if channel == "book":
            books = []
            for item in data:
                entry = BookData.model_validate(item)
                book = self.books[entry.symbol].apply(entry, str(kind), received)
                if book.event_at and book.event_at > received + timedelta(seconds=5):
                    raise MarketDataError("book event lies in the future")
                books.append(book.model_dump(mode="json"))
            self.last_received = received
            return Frame("book", {"books": books})
        trades: list[MarketTrade] = []
        for item in data:
            entry_trade = TradeData.model_validate(item)
            if entry_trade.timestamp > received + timedelta(seconds=5):
                raise MarketDataError("trade event lies in the future")
            if kind == "snapshot":
                self.trade_snapshot_seen.add(entry_trade.symbol)
            elif entry_trade.symbol not in self.trade_snapshot_seen:
                raise MarketDataError("trade update before snapshot")
            trades.append(
                MarketTrade(
                    venue="kraken",
                    symbol=entry_trade.symbol,
                    trade_id=str(entry_trade.trade_id),
                    side=entry_trade.side,
                    price=entry_trade.price,
                    quantity=entry_trade.qty,
                    event_at=entry_trade.timestamp,
                    received_at=received,
                )
            )
        self.last_received = received
        return Frame("trade", {"snapshot": kind == "snapshot"}, tuple(trades))

    def health(self, now: datetime) -> dict[str, object]:
        reasons: list[str] = []
        if not self.system_online:
            reasons.append("system_not_online")
        if self.last_received is None or not 0 <= (now - self.last_received).total_seconds() <= 10:
            reasons.append("connection_stale")
        if self.instrument_seen != set(BINDINGS):
            reasons.append("instrument_snapshot_incomplete")
        if self.trade_snapshot_seen != set(BINDINGS):
            reasons.append("trade_snapshot_incomplete")
        ages: dict[str, float | None] = {}
        for symbol, state in self.books.items():
            book = state.book
            age = (now - book.received_at).total_seconds() if book else None
            ages[symbol] = age
            if (
                age is None
                or not 0 <= age <= 5
                or (
                    book
                    and book.event_at
                    and not -0.5 <= (now - book.event_at).total_seconds() <= 5
                )
            ):
                reasons.append(f"book_stale_or_missing:{symbol}")
            rule = self.rules[symbol]
            if rule.status != "online" or not 0 <= (now - rule.observed_at).total_seconds() <= 3600:
                reasons.append(f"rules_stale_or_offline:{symbol}")
        return {
            "schema_version": 1,
            "as_of": now.isoformat(),
            "epoch": str(self.epoch),
            "market_ready": not reasons,
            "blockers": reasons,
            "book_age_seconds": ages,
            "trading_ready": False,
            "live_execution_available": False,
        }
