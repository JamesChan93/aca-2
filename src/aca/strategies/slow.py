"""The predeclared five variants, evaluated after closed-bar finalisation."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal

from pydantic import Field, model_validator

from aca.domain.market import Candle, MarketRecord, Symbol
from aca.domain.values import FINANCIAL_CONTEXT, FinancialDecimal, UtcDateTime
from aca.features.indicators import atr, sma

Variant = Literal["sma200", "sma100", "donchian55-20", "momentum90", "momentum60"]
VARIANTS: tuple[Variant, ...] = (
    "sma200",
    "sma100",
    "donchian55-20",
    "momentum90",
    "momentum60",
)


class StrategySignal(MarketRecord):
    strategy: Variant
    strategy_version: Literal["slow-v1"] = "slow-v1"
    symbol: Symbol
    decision_at: UtcDateTime
    data_watermark: UtcDateTime
    desired: Literal["long", "flat", "hold"]
    stop_distance: FinancialDecimal = Field(gt=0)
    expires_at: UtcDateTime

    @model_validator(mode="after")
    def chronology(self) -> StrategySignal:
        if self.decision_at < self.data_watermark + timedelta(seconds=10):
            raise ValueError("signal precedes finalisation")
        if self.expires_at <= self.decision_at:
            raise ValueError("signal has expired")
        return self


def signal(bars: Sequence[Candle], variant: Variant, at: datetime) -> StrategySignal:
    if variant not in VARIANTS or len(bars) < 200:
        raise ValueError("unknown strategy or insufficient 200-bar warm-up")
    recent = bars[-201:]
    last = recent[-1]
    for index, bar in enumerate(recent):
        if (
            bar.symbol != last.symbol
            or bar.interval_minutes != last.interval_minutes
            or bar.end + timedelta(seconds=10) > at
            or not (bar.complete or bar.quality == ("confirmed_no_trade",))
            or (index and recent[index - 1].end != bar.start)
        ):
            raise ValueError("unsafe or discontinuous strategy prefix")
    with localcontext(FINANCIAL_CONTEXT):
        volatility = atr(recent)
        if volatility is None:
            raise ValueError("ATR warm-up unavailable")
        distance = max(2 * volatility, last.close * Decimal("0.05")).quantize(
            Decimal("1e-18"), rounding=ROUND_CEILING
        )
        desired: Literal["long", "flat", "hold"]
        if variant.startswith("sma"):
            average = sma(recent, int(variant[3:]))
            if average is None:
                raise ValueError("SMA warm-up unavailable")
            desired = "long" if last.close > average else "flat"
        elif variant.startswith("momentum"):
            period = int(variant[8:])
            desired = "long" if last.close > recent[-period - 1].close else "flat"
        else:
            # The current bar is excluded from both channels.
            upper = max(bar.high for bar in recent[-56:-1])
            lower = min(bar.low for bar in recent[-21:-1])
            desired = "long" if last.close > upper else "flat" if last.close < lower else "hold"
        return StrategySignal(
            strategy=variant,
            symbol=last.symbol,
            decision_at=at,
            data_watermark=last.end,
            desired=desired,
            stop_distance=distance,
            expires_at=at + timedelta(seconds=2),
        )
