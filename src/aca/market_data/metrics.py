"""Read-only capture metrics; no metric can confer trading permission."""

from prometheus_client import Gauge, generate_latest

from aca.app.health import FoundationMetrics, FoundationStatus


class CaptureMetrics(FoundationMetrics):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.market_ready = Gauge(
            "aca_public_market_ready", "Validated public feed readiness", registry=self.registry
        )
        self.book_age = Gauge(
            "aca_public_book_age_seconds",
            "Book receipt age; -1 if absent",
            ["symbol"],
            registry=self.registry,
        )

        self.statistics = Gauge(
            "aca_public_capture_statistics",
            "Process capture counters",
            ["kind"],
            registry=self.registry,
        )

    def render(self, status: FoundationStatus) -> bytes:
        super().render(status)
        document = status.document()
        statistics = document.get("capture_statistics", {})
        if isinstance(statistics, dict):
            for key in (
                "received_websocket_frames",
                "new_stream_trades",
                "duplicate_stream_trades",
                "reconnects",
                "faults",
                "checksum_failures",
                "rest_requests",
                "rest_retries",
            ):
                value = statistics.get(key, 0)
                self.statistics.labels(key).set(value if isinstance(value, int) else 0)
        market = document.get("market_data", {})
        if isinstance(market, dict):
            self.market_ready.set(int(market.get("market_ready") is True))
            ages = market.get("book_age_seconds", {})
            for symbol in ("BTC/GBP", "ETH/GBP"):
                age = ages.get(symbol) if isinstance(ages, dict) else None
                self.book_age.labels(symbol).set(age if isinstance(age, int | float) else -1)
        return generate_latest(self.registry)
