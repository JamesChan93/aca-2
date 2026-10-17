"""Bounded public GET transport. No private path, authentication or mutation exists."""

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Literal

import httpx

from aca.adapters.kraken.wire import BINDINGS, STATUSES, MarketDataError, Pair, decode, number
from aca.domain.market import Candle, InstrumentRules, MarketTrade, Symbol

Endpoint = Literal[
    "Time", "SystemStatus", "Assets", "AssetPairs", "Ticker", "Depth", "Trades", "OHLC"
]
ENDPOINTS = {"Time", "SystemStatus", "Assets", "AssetPairs", "Ticker", "Depth", "Trades", "OHLC"}
EvidenceSink = Callable[[str, str, datetime], Awaitable[None]]


def retry_after(value: str | None, now: datetime) -> float:
    if value is None:
        return 0
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value).astimezone(UTC) - now).total_seconds()
        except ValueError, TypeError, OverflowError:
            return 60
    if not math.isfinite(seconds) or seconds < 0:
        return 60
    if seconds > 3600:
        raise MarketDataError("server throttle exceeds bounded retry window")
    return seconds


class PublicRest:
    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sink: EvidenceSink | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url="https://api.kraken.com",
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={
                "User-Agent": "aca-platform/0.1 public-read-only",
                "Accept-Encoding": "identity",
            },
        )
        self.sink = sink
        self.sleep = sleep
        self.clock = clock
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.requests = 0
        self.retries = 0
        self.last_sent_at: datetime | None = None
        self.last_received_at: datetime | None = None

    async def close(self) -> None:
        await self.client.aclose()

    async def read(
        self, endpoint: Endpoint, params: dict[str, str] | None = None
    ) -> dict[str, object]:
        if endpoint not in ENDPOINTS:
            raise MarketDataError("public endpoint not allowlisted")
        async with self.lock:
            for attempt in range(3):
                await self.sleep(max(0, self.next_request - self.clock()))
                self.next_request = self.clock() + 5
                self.requests += 1
                delay = 0.0
                try:
                    async with asyncio.timeout(15):
                        self.last_sent_at = datetime.now(UTC)
                        async with self.client.stream(
                            "GET", f"/0/public/{endpoint}", params=params
                        ) as response:
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes(65536):
                                chunks.extend(chunk)
                                if len(chunks) > 2_000_000:
                                    raise MarketDataError("oversized REST response")
                            raw = bytes(chunks).decode("utf-8")
                            received = datetime.now(UTC)
                            self.last_received_at = received
                            if self.sink:
                                await self.sink(endpoint, raw, received)
                            if response.status_code in {429, 500, 502, 503, 504}:
                                delay = retry_after(response.headers.get("Retry-After"), received)
                            elif response.status_code != 200:
                                raise MarketDataError("public REST HTTP failure")
                            else:
                                payload = decode(raw)
                                errors = payload.get("error")
                                if not isinstance(errors, list):
                                    raise MarketDataError("missing REST error envelope")
                                if not errors:
                                    result = payload.get("result")
                                    if not isinstance(result, dict):
                                        raise MarketDataError("missing REST result")
                                    return result
                                if all(
                                    isinstance(e, str)
                                    and (
                                        e.startswith("EAPI:Rate limit")
                                        or e.startswith("EService:Throttled")
                                    )
                                    for e in errors
                                ):
                                    delay = 60
                                else:
                                    # Unknown errors and authentication failures are never retried.
                                    raise MarketDataError("public REST error blocks collection")
                except httpx.TransportError, TimeoutError:
                    pass
                if attempt == 2:
                    break
                self.retries += 1
                # No credentials or mutable commands can reach this retry loop.
                self.next_request = (
                    self.clock() + max(delay, 2 ** (attempt + 1)) + random.uniform(0, 1)  # noqa: S311 - backoff jitter only
                )
            raise MarketDataError("public REST retry budget exhausted")

    async def instruments(self) -> tuple[InstrumentRules, ...]:
        result = await self.read("AssetPairs", {"pair": "XBTGBP,ETHGBP", "country_code": "GB"})
        observed = datetime.now(UTC)
        rules: list[InstrumentRules] = []
        for symbol, (pair_id, base, quote) in BINDINGS.items():
            pair = Pair.model_validate(result.get(pair_id))
            if (pair.base, pair.quote) != (base, quote) or pair.status not in STATUSES:
                raise MarketDataError("unknown instrument binding or status")
            rules.append(
                InstrumentRules(
                    venue="kraken",
                    symbol=symbol,
                    venue_symbol=symbol,
                    rest_pair=pair_id,
                    price_increment=pair.tick_size,
                    quantity_increment=Decimal(1).scaleb(-pair.lot_decimals),
                    minimum_quantity=pair.ordermin,
                    minimum_cost=pair.costmin,
                    status=pair.status,
                    observed_at=observed,
                )
            )
        return tuple(rules)

    async def candles(self, symbol: Symbol, interval: int) -> tuple[Candle, ...]:
        if symbol not in BINDINGS or interval not in {240, 1440}:
            raise MarketDataError("unapproved candle instrument or interval")
        result = await self.read("OHLC", {"pair": BINDINGS[symbol][0], "interval": str(interval)})
        observed = datetime.now(UTC)
        rows = result.get(BINDINGS[symbol][0])
        if not isinstance(rows, list) or not rows:
            raise MarketDataError("missing candle rows")
        candles: list[Candle] = []
        previous: datetime | None = None
        # Kraken always includes the unfinished last candle, even with a historical since.
        for row in rows[:-1]:
            if (
                not isinstance(row, list)
                or len(row) != 8
                or type(row[0]) is not int
                or type(row[7]) is not int
            ):
                raise MarketDataError("invalid candle row")
            start = datetime.fromtimestamp(row[0], UTC)
            end = start + timedelta(minutes=interval)
            if previous is not None and start <= previous:
                raise MarketDataError("candle order regressed")
            previous = start
            if end + timedelta(seconds=10) > observed:
                continue
            candles.append(
                Candle(
                    venue="kraken",
                    symbol=symbol,
                    interval_minutes=interval,  # type: ignore[arg-type]
                    start=start,
                    end=end,
                    open=number(row[1]),
                    high=number(row[2]),
                    low=number(row[3]),
                    close=number(row[4]),
                    volume=number(row[6]),
                    trade_count=row[7],
                    complete=True,
                    quality=(),
                    source="kraken_rest_ohlc",
                    observed_at=observed,
                )
            )
        return tuple(candles)

    async def trades(
        self, symbol: Symbol, since: str | None
    ) -> tuple[tuple[MarketTrade, ...], str]:
        if symbol not in BINDINGS or (
            since is not None and (not since.isdigit() or len(since) > 24)
        ):
            raise MarketDataError("invalid trade cursor")
        params = {"pair": BINDINGS[symbol][0], "count": "1000"}
        if since is not None:
            params["since"] = since
        result = await self.read("Trades", params)
        rows, cursor = result.get(BINDINGS[symbol][0]), result.get("last")
        if not isinstance(rows, list) or not isinstance(cursor, str) or not cursor.isdigit():
            raise MarketDataError("invalid trades page")
        received = datetime.now(UTC)
        trades: list[MarketTrade] = []
        for row in rows:
            if (
                not isinstance(row, list)
                or len(row) != 7
                or type(row[6]) is not int
                or row[3] not in {"b", "s"}
                or row[4] not in {"m", "l"}
            ):
                raise MarketDataError("invalid REST trade row")
            seconds = number(row[2])
            event = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
                microseconds=int(seconds * 1_000_000)
            )
            trades.append(
                MarketTrade(
                    venue="kraken",
                    symbol=symbol,
                    trade_id=str(row[6]),
                    side="buy" if row[3] == "b" else "sell",
                    price=number(row[0]),
                    quantity=number(row[1]),
                    event_at=event,
                    received_at=received,
                )
            )
        return tuple(trades), cursor
