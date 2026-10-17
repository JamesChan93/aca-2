"""Indicators use only the supplied closed prefix; ATR uses a simple 14-bar mean."""

from collections.abc import Sequence
from decimal import Decimal, localcontext

from aca.domain.market import Candle
from aca.domain.values import FINANCIAL_CONTEXT


def sma(bars: Sequence[Candle], period: int) -> Decimal | None:
    if period < 1:
        raise ValueError("period must be positive")
    if len(bars) < period:
        return None
    with localcontext(FINANCIAL_CONTEXT):
        return sum((bar.close for bar in bars[-period:]), Decimal(0)) / period


def atr(bars: Sequence[Candle], period: int = 14) -> Decimal | None:
    if period < 1:
        raise ValueError("period must be positive")
    if len(bars) <= period:
        return None
    with localcontext(FINANCIAL_CONTEXT):
        ranges = [
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
            for previous, current in zip(bars[-period - 1 : -1], bars[-period:], strict=True)
        ]
        return sum(ranges, Decimal(0)) / period
