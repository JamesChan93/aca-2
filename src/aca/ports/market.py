"""Public read protocols; account and command capabilities are deliberately absent."""

from collections.abc import AsyncIterator
from typing import Protocol

from aca.domain.market import Candle, InstrumentRules, Symbol


class DiscoveryPort(Protocol):
    async def instruments(self) -> tuple[InstrumentRules, ...]: ...


class HistoricalMarketPort(Protocol):
    async def candles(self, symbol: Symbol, interval: int) -> tuple[Candle, ...]: ...


class MarketStreamPort(Protocol):
    def messages(self) -> AsyncIterator[str]: ...
