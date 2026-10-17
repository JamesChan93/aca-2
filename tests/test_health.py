import asyncio
import json
from time import monotonic

import pytest

from aca.app.health import FoundationMetrics, FoundationStatus, HealthServer


def test_foundation_readiness_never_implies_trading_readiness() -> None:
    state = FoundationStatus(database_ready=True, schema_ready=True, last_success=monotonic())
    server = HealthServer(state, FoundationMetrics())
    assert server.response("GET", "/health/ready")[0] == 200
    code, _, body = server.response("GET", "/health/trading")
    assert code == 503
    assert json.loads(body)["trading_ready"] is False
    assert json.loads(body)["live_execution_available"] is False
    assert b"aca_trading_ready 0.0" in server.response("GET", "/metrics")[2]
    assert server.response("POST", "/health/ready")[0] == 405
    state.shutting_down = True
    assert server.response("GET", "/health/ready")[0] == 503


def test_stale_database_check_fails_readiness() -> None:
    state = FoundationStatus(
        database_ready=True, schema_ready=True, last_success=monotonic() - 20, freshness_seconds=10
    )
    assert not state.ready


@pytest.mark.asyncio
async def test_http_server_routes_and_shutdown() -> None:
    server = HealthServer(FoundationStatus(), FoundationMetrics())
    await server.start("127.0.0.1", 0)
    assert server.server is not None
    port = server.server.sockets[0].getsockname()[1]
    try:
        for request, expected in [
            (b"GET /health/live HTTP/1.1\r\nHost: localhost\r\n\r\n", b"200 OK"),
            (b"GET /health/ready HTTP/1.1\r\nHost: localhost\r\n\r\n", b"503 Service"),
            (b"POST /orders HTTP/1.1\r\nHost: localhost\r\n\r\n", b"405 Method"),
            (b"malformed\r\n\r\n", b"400 Bad"),
        ]:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request)
            await writer.drain()
            response = await reader.read()
            assert expected in response
            writer.close()
            await writer.wait_closed()
    finally:
        await server.close()
