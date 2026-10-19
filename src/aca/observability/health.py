"""Cached local notification health. Reads never acquire execution/delivery ownership."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from time import monotonic
from uuid import UUID

import structlog
from prometheus_client import Counter, Gauge
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncEngine

from aca.app.health import FoundationMetrics, FoundationStatus, HealthServer
from aca.domain.values import UtcDateTime
from aca.observability.delivery import status
from aca.operations.config import StrictModel
from aca.storage.database import verify_runtime


class QueueSample(StrictModel):
    """Allowlisted monitoring values; no message bodies, raw incidents or destinations."""

    as_of: UtcDateTime
    configured: bool
    enabled: bool
    transitions: int = Field(ge=0)
    daily: int = Field(ge=0)
    critical: int = Field(ge=0)
    oldest_transition_at: UtcDateTime | None
    oldest_daily_at: UtcDateTime | None
    oldest_critical_at: UtcDateTime | None
    delivered: int = Field(ge=0)
    retry_events: int = Field(ge=0)
    terminal_failures: int = Field(ge=0)
    last_delivery_at: UtcDateTime | None
    attempts: int = Field(ge=0, le=8)
    not_before: UtcDateTime | None


async def read_sample(engine: AsyncEngine, account_id: UUID) -> QueueSample:
    await verify_runtime(engine)
    report = await status(engine, account_id)
    state = report["state"]
    queues = report["scheduled_queues"]
    if not isinstance(queues, dict) or not (state is None or isinstance(state, dict)):
        raise ValueError("notification monitoring evidence is invalid")
    daily, critical = queues.get("daily", {}), queues.get("critical", {})
    return QueueSample.model_validate_json(
        json.dumps(
            {
                "as_of": report["as_of"],
                "configured": report["configured"],
                "enabled": state["enabled"] if state else False,
                "transitions": report["backlog"],
                "daily": daily.get("count", 0),
                "critical": critical.get("count", 0),
                "oldest_transition_at": report["oldest_pending_at"],
                "oldest_daily_at": daily.get("oldest_at"),
                "oldest_critical_at": critical.get("oldest_at"),
                "delivered": report["delivered"],
                "retry_events": report["retry_events"],
                "terminal_failures": report["terminal_failures"],
                "last_delivery_at": report["last_delivery_at"],
                "attempts": state["attempts"] if state else 0,
                "not_before": state["not_before"] if state else None,
            }
        )
    )


class NotificationStatus(FoundationStatus):
    def __init__(self, account_id: UUID, timer: Callable[[], float] = monotonic) -> None:
        super().__init__(freshness_seconds=10)
        self.account_id, self.timer = account_id, timer
        self.sample: QueueSample | None = None
        self.worker_last_success: float | None = None
        self.worker_faulted = False
        self.daily_evidence_unavailable = False

    @property
    def ready(self) -> bool:
        return (
            self.database_ready
            and self.schema_ready
            and not self.shutting_down
            and self.last_success is not None
            and 0 <= self.timer() - self.last_success <= self.freshness_seconds
        )

    @property
    def worker_fresh(self) -> bool:
        return (
            not self.shutting_down
            and not self.worker_faulted
            and self.worker_last_success is not None
            and 0 <= self.timer() - self.worker_last_success <= 30
        )

    @property
    def reason(self) -> str | None:
        if self.shutting_down:
            return "stopping"
        if self.worker_faulted:
            return "worker_fault"
        if not self.ready or self.sample is None:
            return "monitoring_evidence_unavailable"
        if not self.worker_fresh:
            return "worker_stale"
        if not self.sample.configured:
            return "destination_unconfigured"
        if not self.sample.enabled:
            return "destination_disabled"
        if self.daily_evidence_unavailable:
            return "daily_evidence_unavailable"
        if self.sample.terminal_failures:
            return "retained_terminal_failures"
        return None

    @property
    def notification_ready(self) -> bool:
        return self.reason is None

    def age(self, at: datetime | None) -> float:
        if at is None or self.sample is None or self.last_success is None:
            return -1
        elapsed = max(0, self.timer() - self.last_success)
        age = (self.sample.as_of - at).total_seconds() + elapsed
        return age if age >= 0 else -1

    def retry_wait(self) -> float:
        if self.sample is None or self.last_success is None:
            return -1
        if self.sample.not_before is None:
            return 0
        elapsed = max(0, self.timer() - self.last_success)
        return max(0, (self.sample.not_before - self.sample.as_of).total_seconds() - elapsed)

    def document(self) -> dict[str, object]:
        doc = super().document()
        doc.update(
            {
                "schema_version": 1,
                "phase": 4,
                "kind": "paper_notification_health_v1",
                "account_id": str(self.account_id),
                "notification_ready": self.notification_ready,
                "worker_fresh": self.worker_fresh,
                "monitoring_fresh": self.ready,
                "reason": self.reason,
                "sample": self.sample.model_dump(mode="json") if self.sample else None,
                "daily_evidence_unavailable": self.daily_evidence_unavailable,
                "retry_wait_seconds": self.retry_wait(),
                "promotion_evidence": False,
            }
        )
        return doc


class NotificationMetrics(FoundationMetrics):
    def __init__(self) -> None:
        super().__init__()
        self.notification_ready = Gauge(
            "aca_notifications_ready", "Local notification readiness only", registry=self.registry
        )
        self.worker_fresh = Gauge(
            "aca_notifications_worker_fresh",
            "Completed worker tick within 30s",
            registry=self.registry,
        )
        self.monitoring_fresh = Gauge(
            "aca_notifications_monitoring_fresh",
            "Successful sample within 10s",
            registry=self.registry,
        )
        self.destination_enabled = Gauge(
            "aca_notifications_destination_enabled",
            "Configured and enabled destination",
            registry=self.registry,
        )
        self.queue = Gauge(
            "aca_notifications_pending",
            "Pending items in last sample",
            ["kind"],
            registry=self.registry,
        )
        self.age = Gauge(
            "aca_notifications_oldest_age_seconds",
            "Oldest timestamp age; transition event time is NOT queue latency; -1 if absent/future",
            ["kind"],
            registry=self.registry,
        )
        self.events = Gauge(
            "aca_notifications_delivery_events",
            "Retained delivery journal counts, not process counters",
            ["kind"],
            registry=self.registry,
        )
        self.wait = Gauge(
            "aca_notifications_retry_wait_seconds", "Known delivery wait", registry=self.registry
        )
        self.attempts = Gauge(
            "aca_notifications_current_attempts", "Current item attempts", registry=self.registry
        )
        self.delivery_age = Gauge(
            "aca_notifications_last_delivery_age_seconds",
            "Age of last confirmed delivery; -1 if absent/future",
            registry=self.registry,
        )
        self.samples = Counter(
            "aca_notifications_monitor_samples",
            "Process monitoring samples",
            ["result"],
            registry=self.registry,
        )

    def render(self, status: FoundationStatus) -> bytes:
        if isinstance(status, NotificationStatus):
            self.notification_ready.set(int(status.notification_ready))
            self.worker_fresh.set(int(status.worker_fresh))
            self.monitoring_fresh.set(int(status.ready))
            sample = status.sample
            self.destination_enabled.set(
                int(sample is not None and sample.configured and sample.enabled)
            )
            for kind, field in (
                ("transition", "transitions"),
                ("daily", "daily"),
                ("critical", "critical"),
            ):
                self.queue.labels(kind).set(getattr(sample, field) if sample else -1)
                self.age.labels(kind).set(
                    status.age(getattr(sample, f"oldest_{kind}_at") if sample else None)
                )
            for kind in ("delivered", "retry_events", "terminal_failures"):
                self.events.labels(kind).set(getattr(sample, kind) if sample else -1)
            self.wait.set(status.retry_wait())
            self.attempts.set(sample.attempts if sample else -1)
            self.delivery_age.set(status.age(sample.last_delivery_at if sample else None))
        return super().render(status)


class NotificationHealthServer(HealthServer):
    def response(self, method: str, path: str) -> tuple[int, str, bytes]:
        if method == "GET" and path in {"/health/ready", "/health/notifications"}:
            doc = self.status.document()
            return (
                200 if doc.get("notification_ready") is True else 503,
                "application/json",
                json.dumps(doc, sort_keys=True).encode(),
            )
        return super().response(method, path)


class NotificationMonitor:
    def __init__(
        self,
        state: NotificationStatus,
        metrics: NotificationMetrics,
        read: Callable[[], Awaitable[QueueSample]],
    ) -> None:
        self.state, self.metrics, self.read = state, metrics, read

    async def poll(self) -> None:
        try:
            async with asyncio.timeout(4):
                sample = await self.read()
            self.state.sample = sample
            self.state.database_ready = self.state.schema_ready = True
            self.state.last_success = self.state.timer()
            self.metrics.samples.labels("success").inc()
        except Exception as exc:
            self.state.database_ready = self.state.schema_ready = False
            self.state.last_success = None
            self.metrics.database_failures.inc()
            self.metrics.samples.labels("failure").inc()
            structlog.get_logger().warning(
                "notification_monitor_unavailable", error_type=type(exc).__name__
            )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll()
            try:
                async with asyncio.timeout(5):
                    await stop.wait()
            except TimeoutError:
                pass
