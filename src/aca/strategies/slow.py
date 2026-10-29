"""The predeclared five variants, evaluated after closed-bar finalisation."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Literal

from pydantic import Field, model_validator

from aca.domain.market import Candle, MarketRecord, Symbol
from aca.domain.timing import APPROVAL_TTL, FINALISATION, SLOW_FRESHNESS
from aca.domain.values import FINANCIAL_CONTEXT, FinancialDecimal, UtcDateTime
from aca.features.indicators import atr, sma

Variant = Literal["sma200", "sma100", "donchian55-20", "momentum90", "momentum60"]
ResearchVariant = Literal[
    "sma200",
    "sma100",
    "donchian55-20",
    "momentum90",
    "momentum60",
    "sma200-band1",
    "sma200-confirm2",
]
S1_VARIANTS: tuple[ResearchVariant, ...] = ("sma200-band1", "sma200-confirm2")
VARIANTS: tuple[Variant, ...] = (
    "sma200",
    "sma100",
    "donchian55-20",
    "momentum90",
    "momentum60",
)


class StrategySignal(MarketRecord):
    strategy: ResearchVariant
    strategy_version: Literal["slow-v1", "slow-v2"] = "slow-v1"
    symbol: Symbol
    decision_at: UtcDateTime
    data_watermark: UtcDateTime
    desired: Literal["long", "flat", "hold"]
    stop_distance: FinancialDecimal = Field(gt=0)
    expires_at: UtcDateTime

    @model_validator(mode="after")
    def chronology(self) -> StrategySignal:
        finalised = self.data_watermark + FINALISATION
        if self.decision_at < finalised:
            raise ValueError("signal precedes finalisation")
        deadline = (
            finalised + SLOW_FRESHNESS
            if self.strategy_version == "slow-v2"
            else self.decision_at + APPROVAL_TTL
        )
        if not self.decision_at < self.expires_at <= deadline:
            raise ValueError("signal has expired")
        return self


def signal(
    bars: Sequence[Candle],
    variant: ResearchVariant,
    at: datetime,
    *,
    version: Literal["slow-v1", "slow-v2"] = "slow-v1",
) -> StrategySignal:
    if variant not in (*VARIANTS, *S1_VARIANTS) or len(bars) < (
        201 if variant == "sma200-confirm2" else 200
    ):
        raise ValueError("unknown strategy or insufficient 200-bar warm-up")
    recent = bars[-201:]
    if variant in S1_VARIANTS and (version != "slow-v2" or recent[-1].interval_minutes != 1440):
        raise ValueError("S1 requires corrected daily research signals")
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
        if variant == "sma200-band1":
            average = sma(recent, 200)
            if average is None:
                raise ValueError("SMA warm-up unavailable")
            desired = (
                "long"
                if last.close > average * Decimal("1.01")
                else "flat"
                if last.close < average * Decimal(".99")
                else "hold"
            )
        elif variant == "sma200-confirm2":
            averages = (sma(recent[:-1], 200), sma(recent, 200))
            if any(value is None for value in averages):
                raise ValueError("confirmation warm-up unavailable")
            differences = [
                bar.close - average
                for bar, average in zip(recent[-2:], averages, strict=True)
                if average is not None
            ]
            desired = (
                "long"
                if all(value > 0 for value in differences)
                else "flat"
                if all(value < 0 for value in differences)
                else "hold"
            )
        elif variant.startswith("sma"):
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
            strategy_version=version,
            symbol=last.symbol,
            decision_at=at,
            data_watermark=last.end,
            desired=desired,
            stop_distance=distance,
            expires_at=(
                last.end + FINALISATION + SLOW_FRESHNESS
                if version == "slow-v2"
                else at + APPROVAL_TTL
            ),
        )
