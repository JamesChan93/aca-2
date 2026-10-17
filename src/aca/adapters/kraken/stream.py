"""Bounded public WebSocket subscriptions; recovery belongs to the collector."""

import json
from collections.abc import AsyncIterator

from websockets.asyncio.client import connect

PUBLIC_WS = "wss://ws.kraken.com/v2"


class FixedOriginConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # No cross-origin or downgrade redirects, even on the public-only transport.
        return exc


class PublicStream:
    async def messages(self) -> AsyncIterator[str]:
        async with FixedOriginConnect(
            PUBLIC_WS,
            proxy=None,
            compression=None,
            open_timeout=10,
            close_timeout=3,
            ping_interval=15,
            ping_timeout=10,
            max_size=2_000_000,
            max_queue=16,
        ) as socket:
            for channel in ("instrument", "book", "trade"):
                params: dict[str, object] = {"channel": channel, "snapshot": True}
                if channel != "instrument":
                    params["symbol"] = ["BTC/GBP", "ETH/GBP"]
                if channel == "book":
                    params["depth"] = 10
                await socket.send(json.dumps({"method": "subscribe", "params": params}))
            async for message in socket:
                if not isinstance(message, str):
                    raise ValueError("binary public frames are not supported")
                yield message
