"""Bounded local HTTP health/metrics surface, with no command endpoints."""

import asyncio
import json
from dataclasses import dataclass, field
from time import monotonic

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

BLOCKERS = (
    "exchange_connectivity_not_implemented",
    "portfolio_accounting_not_implemented",
    "risk_enforcement_not_implemented",
    "paper_execution_not_implemented",
    "reconciliation_not_implemented",
    "live_execution_not_implemented",
)


@dataclass(slots=True)
class FoundationStatus:
    database_ready: bool = False
    schema_ready: bool = False
    shutting_down: bool = False
    last_success: float | None = None
    freshness_seconds: float = 10.0

    @property
    def ready(self) -> bool:
        return (
            self.database_ready
            and self.schema_ready
            and not self.shutting_down
            and self.last_success is not None
            and monotonic() - self.last_success <= self.freshness_seconds
        )

    def document(self) -> dict[str, object]:
        return {
            "phase": 1,
            "mode": "paper",
            "foundation_ready": self.ready,
            "database_ready": self.database_ready,
            "schema_ready": self.schema_ready,
            "reconciled": False,
            "trading_ready": False,
            "live_execution_available": False,
            "incomplete_safety_requirements": list(BLOCKERS),
        }


@dataclass
class FoundationMetrics:
    registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    ready: Gauge = field(init=False)
    database: Gauge = field(init=False)
    trading: Gauge = field(init=False)
    database_failures: Counter = field(init=False)

    def __post_init__(self) -> None:
        self.ready = Gauge(
            "aca_foundation_ready", "Foundation readiness only", registry=self.registry
        )
        self.database = Gauge("aca_database_ready", "Database safety check", registry=self.registry)
        self.trading = Gauge(
            "aca_trading_ready", "Trading readiness; always zero in Phase 1", registry=self.registry
        )
        self.database_failures = Counter(
            "aca_database_check_failures", "Failed database checks", registry=self.registry
        )
        self.trading.set(0)

    def render(self, status: FoundationStatus) -> bytes:
        self.ready.set(int(status.ready))
        self.database.set(int(status.database_ready))
        self.trading.set(0)
        return generate_latest(self.registry)


class HealthServer:
    def __init__(self, status: FoundationStatus, metrics: FoundationMetrics) -> None:
        self.status = status
        self.metrics = metrics
        self.server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self, host: str, port: int) -> None:
        self.server = await asyncio.start_server(self._handle, host, port, limit=8192)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        tasks = tuple(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        self._connections.add(task)
        try:
            if len(self._connections) > 32:
                return
            try:
                async with asyncio.timeout(2):
                    headers = await reader.readuntil(b"\r\n\r\n")
                request = headers.split(b"\r\n", 1)[0].decode("ascii").split(" ")
                if len(request) != 3 or request[2] not in {"HTTP/1.0", "HTTP/1.1"}:
                    raise ValueError("invalid request")
                method, path, _ = request
            except (
                TimeoutError,
                ValueError,
                UnicodeError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ):
                code, content_type, body = 400, "application/json", b'{"error":"bad_request"}'
            else:
                code, content_type, body = self.response(method, path)
            reason = {
                200: "OK",
                400: "Bad Request",
                404: "Not Found",
                405: "Method Not Allowed",
                503: "Service Unavailable",
            }[code]
            writer.write(
                f"HTTP/1.1 {code} {reason}\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n"
                "Cache-Control: no-store\r\n\r\n".encode()
                + body,
            )
            async with asyncio.timeout(2):
                await writer.drain()
        except ConnectionError, TimeoutError:
            pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            except ConnectionError, TimeoutError:
                pass
            self._connections.discard(task)

    def response(self, method: str, path: str) -> tuple[int, str, bytes]:
        if method != "GET":
            return 405, "application/json", b'{"error":"read_only_surface"}'
        if path == "/metrics":
            return 200, CONTENT_TYPE_LATEST, self.metrics.render(self.status)
        if path == "/health/live":
            return 200, "application/json", b'{"alive":true,"phase":1}'
        if path in {"/health/ready", "/health/trading"}:
            ready = self.status.ready if path == "/health/ready" else False
            return (
                (200 if ready else 503),
                "application/json",
                json.dumps(
                    self.status.document(),
                    sort_keys=True,
                ).encode(),
            )
        return 404, "application/json", b'{"error":"not_found"}'
