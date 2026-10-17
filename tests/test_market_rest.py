import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from aca.adapters.kraken.rest import PublicRest, retry_after
from aca.adapters.kraken.wire import MarketDataError


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += delay
        await asyncio.sleep(0)

    def clock(self) -> float:
        return self.now


async def test_public_rate_limit_retry_after_and_serialization() -> None:
    time = FakeTime()
    timestamps: list[float] = []
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        timestamps.append(time.now)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "12"}, text="throttled")
        return httpx.Response(200, json={"error": [], "result": {"ok": True}})

    client = PublicRest(transport=httpx.MockTransport(handler), sleep=time.sleep, clock=time.clock)
    try:
        await asyncio.gather(client.read("Time"), client.read("SystemStatus"))
        assert len(requests) == 3 and timestamps[1] >= 12
        assert timestamps[2] >= timestamps[1] + 5
        assert all(r.method == "GET" and r.url.host == "api.kraken.com" for r in requests)
        assert all("api-key" not in r.headers and "api-sign" not in r.headers for r in requests)
        assert client.retries == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "status,body",
    [
        (401, "bad key"),
        (200, '{"error":["EAPI:Invalid key"]}'),
        (200, '{"error":["unknown"]}'),
        (302, "redirect"),
        (200, '{"result":{}}'),
        (200, '{"error":[],"result":[]}'),
    ],
)
async def test_errors_and_redirects_do_not_retry(status: int, body: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(status, text=body, headers={"Location": "https://evil.invalid"})

    client = PublicRest(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(MarketDataError):
            await client.read("Time")
        assert requests == 1
    finally:
        await client.close()


async def test_timeout_bounded_to_three_public_attempts() -> None:
    calls = 0
    time = FakeTime()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture", request=request)

    client = PublicRest(transport=httpx.MockTransport(handler), sleep=time.sleep, clock=time.clock)
    try:
        with pytest.raises(MarketDataError, match="budget"):
            await client.read("Time")
        assert calls == 3
        with pytest.raises(MarketDataError, match="allowlisted"):
            await client.read("AddOrder")  # type: ignore[arg-type]
        assert calls == 3
    finally:
        await client.close()


async def test_oversized_rest_body_fails_without_retry() -> None:
    client = PublicRest(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="x" * 2_000_001))
    )
    try:
        with pytest.raises(MarketDataError, match="oversized"):
            await client.read("Time")
        assert client.requests == 1
    finally:
        await client.close()


def pair_payload() -> dict[str, Any]:
    common = {
        "quote": "ZGBP",
        "lot_decimals": 8,
        "lot_multiplier": 1,
        "costmin": "0.43",
        "status": "online",
    }
    return {
        "XXBTZGBP": common | {"base": "XXBT", "tick_size": "0.1", "ordermin": "0.00006"},
        "XETHZGBP": common | {"base": "XETH", "tick_size": "0.01", "ordermin": "0.002"},
    }


async def test_metadata_bindings_and_minimum_refresh() -> None:
    payload = pair_payload()
    time = FakeTime()
    client = PublicRest(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"error": [], "result": payload})
        ),
        sleep=time.sleep,
        clock=time.clock,
    )
    try:
        rules = await client.instruments()
        assert rules[0].symbol == "BTC/GBP" and rules[0].quantity_increment == Decimal("0.00000001")
        payload["XXBTZGBP"]["ordermin"] = "0.001"
        assert (await client.instruments())[0].minimum_quantity == Decimal("0.001")
        payload["XXBTZGBP"]["base"] = "XETH"
        with pytest.raises(MarketDataError, match="binding"):
            await client.instruments()
    finally:
        await client.close()


async def test_ohlc_excludes_unfinished_last_row_and_checks_boundaries() -> None:
    rows = [
        [1767225600, "10", "12", "9", "11", "10.5", "2", 3],
        [1767240000, "11", "13", "10", "12", "11.5", "2", 3],
    ]
    client = PublicRest(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"error": [], "result": {"XXBTZGBP": rows, "last": 1767240000}}
            )
        )
    )
    try:
        candles = await client.candles("BTC/GBP", 240)
        assert len(candles) == 1 and candles[0].complete
        assert candles[0].start == datetime(2026, 1, 1, tzinfo=UTC)
    finally:
        await client.close()


async def test_rest_trade_identity_timestamp_and_cursor() -> None:
    raw = (
        '{"error":[],"result":{"XXBTZGBP":['
        '["100.10","0.01000000",1767225600.123456,"b","l","",42],'
        '["100.10","0.01000000",1767225600.123456,"b","l","",43]],'
        '"last":"1767225600123456000"}}'
    )
    client = PublicRest(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=raw)))
    try:
        trades, cursor = await client.trades("BTC/GBP", "0")
        assert trades[0].event_at.microsecond == 123456
        assert [t.trade_id for t in trades] == ["42", "43"]
        assert cursor == "1767225600123456000"
        assert trades[0].quantity == Decimal("0.01000000")
    finally:
        await client.close()


def test_retry_after_dates_and_malformed_values() -> None:
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    assert retry_after("Sat, 17 Oct 2026 12:00:20 GMT", now) == 20
    assert retry_after("bad", now) == 60
    assert retry_after("NaN", now) == 60
    assert retry_after(None, now) == 0


async def test_first_capture_omits_since_instead_of_requesting_ancient_history() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"error": [], "result": {"XXBTZGBP": [], "last": "123456789"}}
        )

    client = PublicRest(transport=httpx.MockTransport(handler))
    try:
        _, cursor = await client.trades("BTC/GBP", None)
        assert "since" not in seen[0].url.params and cursor == "123456789"
    finally:
        await client.close()


async def test_long_server_throttle_never_retried_early() -> None:
    time = FakeTime()
    client = PublicRest(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "7200"})
        ),
        sleep=time.sleep,
        clock=time.clock,
    )
    try:
        with pytest.raises(MarketDataError, match="throttle"):
            await client.read("Time")
        assert client.requests == 1
    finally:
        await client.close()
