"""One shared execution owner, deterministic member priority and durable health.

At most one minute of health is buffered. A crash loses that credit; recovery
never invents observations for the lost interval or the stopped process.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from aca.app.paper_strategy import PaperStrategy
from aca.domain.trading import UNCERTAIN, fingerprint
from aca.execution.paper import PaperExecution
from aca.research.artifacts import canonical, digest
from aca.storage.models import MarketReceipt, ResearchArtifact
from aca.storage.studies import (
    HealthBlock,
    PaperStudy,
    append,
    check_release,
    completed_episodes,
    members,
    record_health,
)

if TYPE_CHECKING:
    from aca.app.paper_supervisor import SupervisorStatus


class PaperStudyStrategy:
    def __init__(
        self, service: PaperExecution, study: PaperStudy, clock: Callable[[], datetime]
    ) -> None:
        self.service, self.study = service, study
        self.workers = tuple(
            PaperStrategy(service, study=study, member=member, clock=clock)
            for member in members(study)
        )
        self.last: datetime | None = None
        self.begin: datetime | None = None
        self.healthy = self.unhealthy = Decimal(0)
        self.dependency = Decimal(0)
        self.was_healthy = False
        self.was_dependency = False
        self.dependency_sources: dict[str, dict[str, object]] = {}
        self.episode_version: str | None = None
        self.faults: set[str] = set()
        self.discrepancies: set[str] = set()

    @property
    def last_watermark(self) -> datetime | None:
        return self.workers[0].last_watermark

    @property
    def decisions(self) -> int:
        return sum(w.decisions for w in self.workers)

    @property
    def reason(self) -> str | None:
        return next((w.reason for w in self.workers if w.reason), None)

    async def start(self) -> None:
        check_release(self.study)
        await append(
            self.service.writer,
            self.study,
            "session",
            self.workers[0].clock(),
            {"recovered_disarmed": True, "past_activity_credit": 0},
        )
        for worker in self.workers:
            await worker.start()

    async def tick(self, at: datetime) -> None:
        for worker in self.workers:  # BTC then ETH, one ledger and one risk state.
            await worker.tick(at)

    async def observe(self, status: SupervisorStatus, at: datetime) -> None:
        state = await self.service._state()
        healthy = (
            status.market_ready
            and status.reconciled
            and not state.incident
            and not state.accounting_invalidated
            and not any(o.state in UNCERTAIN for o in state.orders)
            and status.reason in {None, "risk_suspended"}
        )
        dependency_source = None
        if (
            not healthy
            and status.reason in {"paper_feed_unavailable", "paper_replay_unavailable"}
            and not state.accounting_invalidated
            and not any(o.state in UNCERTAIN for o in state.orders)
            and state.incident in {None, "paper_feed_unavailable", "paper_replay_unavailable"}
        ):
            # Missing feeds alone cannot establish an external outage. Exclude
            # only a recent, valid, classified status in the current epoch.
            async with self.service.writer.transaction() as c:
                epoch = await c.scalar(
                    select(MarketReceipt.epoch)
                    .where(
                        MarketReceipt.channel == "epoch_started", MarketReceipt.received_at <= at
                    )
                    .order_by(MarketReceipt.received_at.desc())
                    .limit(1)
                )
                row = (
                    (
                        await c.execute(
                            select(MarketReceipt.__table__)
                            .where(
                                MarketReceipt.channel == "status",
                                MarketReceipt.epoch == epoch,
                                MarketReceipt.valid.is_(True),
                                MarketReceipt.received_at <= at,
                                MarketReceipt.received_at >= at - timedelta(seconds=5),
                            )
                            .order_by(MarketReceipt.received_at.desc())
                            .limit(1)
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if row and row["normalized"].get("system") in {
                "maintenance",
                "cancel_only",
                "post_only",
            }:
                dependency_source = {
                    "receipt_id": str(row["id"]),
                    "epoch": str(row["epoch"]),
                    "received_at": row["received_at"].isoformat(),
                    "payload_hash": row["payload_hash"],
                    "normalized": row["normalized"],
                }
        if self.last is not None:
            seconds = Decimal(str((at - self.last).total_seconds()))
            if not 0 < seconds <= 2 or (
                self.begin is not None and (at - self.begin).total_seconds() > 60
            ):
                await self.flush()
                self.begin = at
            else:
                # Both endpoints must be healthy; a failure cannot credit the
                # interval preceding its first detection.
                self.unhealthy += seconds
                if healthy and self.was_healthy:
                    self.unhealthy -= seconds
                    self.healthy += seconds
                elif dependency_source is not None and self.was_dependency:
                    self.unhealthy -= seconds
                    self.dependency += seconds
        else:
            self.begin = at
        self.last = at
        self.was_healthy = healthy
        self.was_dependency = dependency_source is not None
        if dependency_source is not None:
            self.dependency_sources[str(dependency_source["receipt_id"])] = dependency_source
        if not healthy:
            self.faults.add(status.reason or state.incident or "unhealthy_observation")
        if state.accounting_invalidated or (
            state.incident
            and state.incident
            not in {
                "paper_feed_unavailable",
                "paper_replay_pending",
                "paper_replay_backlog",
                "paper_replay_unavailable",
                "paper_supervisor_starting",
            }
        ):
            self.discrepancies.add(state.incident or "accounting_invalidated")
        if self.begin is not None and (at - self.begin).total_seconds() >= 59:
            await self.flush()
        # Completion is rare; scan only after a changed cumulative financial state.
        orders_hash = fingerprint(
            [(str(o.intent.client_id), str(o.filled), o.state) for o in state.orders]
        )
        if state.orders and self.episode_version != orders_hash:
            await completed_episodes(self.service.writer, self.study)
            self.episode_version = orders_hash

    async def flush(self) -> None:
        if self.begin is not None and self.last is not None and self.last > self.begin:
            reference = None
            if self.dependency:
                proof = {
                    "kind": "paper-dependency-outage-review-v1",
                    "purpose": self.study.approval.purpose,
                    "start": self.begin.isoformat(),
                    "end": self.last.isoformat(),
                    "approved": True,
                    "classification": "verified_recent_public_exchange_status",
                    "excluded_seconds": str(self.dependency),
                    "evidence": list(self.dependency_sources.values()),
                }
                reference = digest(canonical(proof))
                async with self.service.writer.transaction() as c:
                    await c.execute(
                        insert(ResearchArtifact)
                        .values(
                            fingerprint=reference,
                            schema_version=1,
                            kind=proof["kind"],
                            document=proof,
                            registered_at=datetime.now(UTC),
                        )
                        .on_conflict_do_nothing()
                    )
            await record_health(
                self.service.writer,
                self.study,
                HealthBlock(
                    start=self.begin,
                    end=self.last,
                    healthy_seconds=self.healthy,
                    unhealthy_seconds=self.unhealthy,
                    dependency_unavailable_seconds=self.dependency,
                    dependency_evidence=reference,
                    faults=tuple(sorted(self.faults)),
                    financial_discrepancies=tuple(sorted(self.discrepancies)),
                ),
            )
        self.begin = self.last
        self.healthy = self.unhealthy = Decimal(0)
        self.dependency = Decimal(0)
        self.dependency_sources.clear()
        self.faults.clear()
        self.discrepancies.clear()
