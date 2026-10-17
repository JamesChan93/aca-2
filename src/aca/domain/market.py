"""Immutable venue-neutral market data. No execution authority is represented."""

from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aca.domain.values import FinancialDecimal, UtcDateTime

Symbol = Literal["BTC/GBP", "ETH/GBP"]


class MarketRecord(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema version requires an integer")
        return value


class InstrumentRules(MarketRecord):
    venue: str
    symbol: Symbol
    venue_symbol: str
    rest_pair: str
    product: Literal["spot"] = "spot"
    price_increment: FinancialDecimal = Field(gt=0)
    quantity_increment: FinancialDecimal = Field(gt=0)
    minimum_quantity: FinancialDecimal = Field(gt=0)
    minimum_cost: FinancialDecimal = Field(gt=0)
    status: str
    observed_at: UtcDateTime


class Level(MarketRecord):
    price: FinancialDecimal = Field(gt=0)
    quantity: FinancialDecimal = Field(gt=0)


class OrderBook(MarketRecord):
    venue: str
    symbol: Symbol
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    checksum_valid: Literal[True]
    event_at: UtcDateTime | None
    received_at: UtcDateTime
    epoch: UUID

    @model_validator(mode="after")
    def ordered(self) -> Self:
        for levels, reverse in ((self.bids, True), (self.asks, False)):
            prices = [item.price for item in levels]
            if not prices or prices != sorted(set(prices), reverse=reverse):
                raise ValueError("book must contain unique sorted levels on both sides")
        if self.bids[0].price >= self.asks[0].price:
            raise ValueError("crossed or locked book")
        return self


class MarketTrade(MarketRecord):
    venue: str
    symbol: Symbol
    trade_id: str = Field(min_length=1, max_length=80)
    side: Literal["buy", "sell"]
    price: FinancialDecimal = Field(gt=0)
    quantity: FinancialDecimal = Field(gt=0)
    event_at: UtcDateTime
    received_at: UtcDateTime


class Candle(MarketRecord):
    venue: str
    symbol: Symbol
    interval_minutes: Literal[240, 1440]
    start: UtcDateTime
    end: UtcDateTime
    open: FinancialDecimal = Field(gt=0)
    high: FinancialDecimal = Field(gt=0)
    low: FinancialDecimal = Field(gt=0)
    close: FinancialDecimal = Field(gt=0)
    volume: FinancialDecimal = Field(ge=0)
    trade_count: int = Field(ge=0)
    complete: bool
    quality: tuple[str, ...]
    source: str
    observed_at: UtcDateTime

    @model_validator(mode="after")
    def valid_bar(self) -> Self:
        if (
            self.end <= self.start
            or (self.end - self.start).total_seconds() != 60 * self.interval_minutes
        ):
            raise ValueError("invalid half-open candle interval")
        if int(self.start.timestamp()) % (60 * self.interval_minutes) or self.start.microsecond:
            raise ValueError("candle is not aligned to UTC interval")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("invalid OHLC ordering")
        if self.complete and self.observed_at.timestamp() < self.end.timestamp() + 10:
            raise ValueError("candle has not passed finalisation delay")
        if self.complete and self.quality:
            raise ValueError("incomplete-quality candle cannot qualify")
        return self


def economic_trade_key(trade: MarketTrade) -> tuple[str, str, str]:
    return trade.venue, trade.symbol, trade.trade_id


def decimal_text(value: Decimal) -> str:
    return format(value, "f")
