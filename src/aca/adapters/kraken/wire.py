"""Lossless JSON and explicit Kraken bindings. Never guess asset prefixes."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from aca.domain.market import Symbol
from aca.domain.values import parse_decimal, utc_datetime

BINDINGS: dict[Symbol, tuple[str, str, str]] = {
    "BTC/GBP": ("XXBTZGBP", "XXBT", "ZGBP"),
    "ETH/GBP": ("XETHZGBP", "XETH", "ZGBP"),
}
STATUSES = {
    "online",
    "cancel_only",
    "post_only",
    "limit_only",
    "reduce_only",
    "maintenance",
    "delisted",
    "work_in_progress",
}


class MarketDataError(ValueError):
    """Safe-to-display market data failure; raw responses never enter log messages."""


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise MarketDataError("duplicate JSON key")
        result[key] = value
    return result


def _constant(_: str) -> object:
    raise MarketDataError("non-finite JSON number")


def decode(payload: str | bytes) -> dict[str, object]:
    if len(payload) > 2_000_000:
        raise MarketDataError("oversized market message")
    try:
        result = json.loads(
            payload, parse_float=Decimal, parse_constant=_constant, object_pairs_hook=_pairs
        )
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise MarketDataError("invalid market JSON") from exc
    if not isinstance(result, dict):
        raise MarketDataError("market message must be an object")
    return result


def number(value: object) -> Decimal:
    # JSON integers are exact. Financial Python floats remain forbidden.
    return parse_decimal(str(value) if type(value) is int else value)


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("wire timestamp must be RFC3339 text")
    return utc_datetime(datetime.fromisoformat(value))


WireDecimal = Annotated[Decimal, BeforeValidator(number)]
WireTime = Annotated[datetime, BeforeValidator(timestamp)]


class WireModel(BaseModel):
    # Additive non-critical fields are allowed on vendor messages, never on configuration.
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore", hide_input_in_errors=True)


class Pair(WireModel):
    base: str
    quote: str
    lot_decimals: int = Field(ge=0, le=18)
    lot_multiplier: int = Field(default=1, ge=1, le=1)
    tick_size: WireDecimal = Field(gt=0)
    ordermin: WireDecimal = Field(gt=0)
    costmin: WireDecimal = Field(gt=0)
    status: str


class WireLevel(WireModel):
    price: WireDecimal = Field(gt=0)
    qty: WireDecimal = Field(ge=0)


class BookData(WireModel):
    symbol: Symbol
    bids: list[WireLevel] = Field(default_factory=list, max_length=1000)
    asks: list[WireLevel] = Field(default_factory=list, max_length=1000)
    checksum: int = Field(ge=0, le=2**32 - 1)
    timestamp: WireTime | None = None


class TradeData(WireModel):
    symbol: Symbol
    side: Literal["buy", "sell"]
    price: WireDecimal = Field(gt=0)
    qty: WireDecimal = Field(gt=0)
    trade_id: int = Field(ge=0)
    timestamp: WireTime
    ord_type: Literal["limit", "market"]


class InstrumentData(WireModel):
    symbol: Symbol
    base: str
    quote: str
    price_increment: WireDecimal = Field(gt=0)
    qty_increment: WireDecimal = Field(gt=0)
    qty_min: WireDecimal = Field(gt=0)
    cost_min: WireDecimal = Field(gt=0)
    status: str


def now() -> datetime:
    return datetime.now(UTC)


def public_trade_time_compatible(
    prior: datetime,
    current: datetime,
    prior_channel: str,
    current_channel: str,
    venue: str,
) -> bool:
    """Observed public REST epoch-decimal/RFC3339 rounding difference, bounded to 1us.

    This rule does not establish private fill identity or authorise financial postings.
    Same-protocol timestamp changes, other venues and larger discrepancies fail.
    """
    return (
        venue == "kraken"
        and {prior_channel, current_channel} == {"trade", "rest:normalized_trades"}
        and abs(prior - current) <= timedelta(microseconds=1)
    )
