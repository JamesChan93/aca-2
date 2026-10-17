"""Bounded, event-time OHLC aggregation. Unexplained empty intervals stay absent."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from itertools import pairwise
from typing import Literal

from aca.domain.market import Candle, MarketTrade, Symbol
from aca.domain.values import FINANCIAL_CONTEXT, utc_datetime


class LateTradeError(ValueError):
    pass


@dataclass
class _Bar:
    first_key: tuple[datetime, datetime, str]
    last_key: tuple[datetime, datetime, str]
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    count: int


class CandleAggregator:
    def __init__(self, coverage_start: datetime, *, venue: str = "kraken") -> None:
        self.coverage_start = utc_datetime(coverage_start)
        self.venue = venue
        self.bars: dict[tuple[Symbol, int, datetime], _Bar] = {}
        self.finalized_until: dict[tuple[Symbol, int], datetime] = {}

    def add(self, trade: MarketTrade) -> None:
        if trade.venue != self.venue:
            raise ValueError("trade belongs to a different venue")
        for interval in (240, 1440):
            stamp = int(trade.event_at.timestamp())
            start = datetime.fromtimestamp(stamp - stamp % (interval * 60), UTC)
            if start < self.finalized_until.get(
                (trade.symbol, interval), datetime(1970, 1, 1, tzinfo=UTC)
            ):
                raise LateTradeError("trade arrived after candle finalisation")
            if trade.event_at < self.coverage_start:
                # Snapshot/recovery trades remain durable but cannot establish stream coverage.
                continue
            key = trade.symbol, interval, start
            order = trade.event_at, trade.received_at, trade.trade_id
            if key not in self.bars:
                self.bars[key] = _Bar(
                    order,
                    order,
                    trade.price,
                    trade.price,
                    trade.price,
                    trade.price,
                    trade.quantity,
                    1,
                )
                continue
            bar = self.bars[key]
            if order < bar.first_key:
                bar.first_key, bar.open = order, trade.price
            if order > bar.last_key:
                bar.last_key, bar.close = order, trade.price
            bar.high, bar.low = max(bar.high, trade.price), min(bar.low, trade.price)
            with localcontext(FINANCIAL_CONTEXT):
                bar.volume += trade.quantity
            bar.count += 1

    def close(self, now: datetime) -> tuple[Candle, ...]:
        result: list[Candle] = []
        for (symbol, interval, start), bar in tuple(self.bars.items()):
            end = start + timedelta(minutes=interval)
            if now < end + timedelta(seconds=10):
                continue
            complete = self.coverage_start <= start
            typed_interval: Literal[240, 1440] = 240 if interval == 240 else 1440
            result.append(
                Candle(
                    venue=self.venue,
                    symbol=symbol,
                    interval_minutes=typed_interval,
                    start=start,
                    end=end,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    trade_count=bar.count,
                    complete=complete,
                    quality=() if complete else ("partial_capture_interval",),
                    source="local_public_trades",
                    observed_at=now,
                )
            )
            self.finalized_until[symbol, interval] = end
            del self.bars[symbol, interval, start]
        return tuple(result)


def unexplained_gaps(candles: tuple[Candle, ...]) -> tuple[dict[str, str], ...]:
    """Report missing intervals without asserting they were periods without trades."""
    gaps: list[dict[str, str]] = []
    for previous, current in pairwise(candles):
        if (previous.venue, previous.symbol, previous.interval_minutes) != (
            current.venue,
            current.symbol,
            current.interval_minutes,
        ):
            raise ValueError("gap audit requires one venue, instrument and interval")
        if previous.end < current.start:
            gaps.append(
                {
                    "start": previous.end.isoformat(),
                    "end": current.start.isoformat(),
                    "classification": "unexplained",
                }
            )
    return tuple(gaps)
