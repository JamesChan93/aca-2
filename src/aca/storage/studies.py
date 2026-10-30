"""Append-only, evidence-bound paper studies; operational records never earn credit.

An operator's start review accepts the retained holdout outcome explicitly. A
holdout-use receipt alone is not a passing holdout or an observation approval.
"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID, uuid4, uuid5

from pydantic import Field, model_validator
from sqlalchemy import BigInteger, Column, MetaData, String, Table, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from aca import __version__
from aca.domain.market import Symbol
from aca.domain.trading import TERMINAL, UNCERTAIN, Record, fingerprint
from aca.domain.values import FinancialDecimal, UtcDateTime
from aca.operations.config import RiskPolicy
from aca.operations.observation_profile import OperatingReview
from aca.research.artifacts import canonical, digest
from aca.research.bundles import Hash
from aca.research.calendar import ACTUAL_SCOPE
from aca.research.family import PortfolioName, portfolio_variant
from aca.storage.audit_prefix import package_fingerprint
from aca.storage.models import ResearchArtifact, ResearchHoldoutUse
from aca.storage.paper import PaperWriter
from aca.storage.strategy import PaperDecision, selections
from aca.strategies.slow import ResearchVariant

if TYPE_CHECKING:
    from aca.research.gates import Evaluation, Freeze, HoldoutReview

metadata = MetaData(schema="aca")
studies = Table(
    "paper_studies",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("approval_id", Uuid, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
)
events = Table(
    "paper_study_events",
    metadata,
    Column("study_id", Uuid, primary_key=True),
    Column("number", BigInteger, primary_key=True),
    Column("event_id", Uuid, nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
)
MAX_EVENTS = 200000


class StartApproval(Record):
    kind: Literal["paper-study-start-v1"] = "paper-study-start-v1"
    id: UUID
    actor: UUID
    account_id: UUID
    purpose: Literal["actual", "synthetic"]
    evaluation: Hash
    freeze: Hash
    holdout_review: Hash
    release: str
    package: Hash
    policy: Hash
    costs: Hash
    operating_profile: Hash
    approved_at: UtcDateTime
    expires_at: UtcDateTime
    holdout_accepted: Literal[True]
    reason: str = Field(min_length=20, max_length=2000)

    @model_validator(mode="after")
    def window(self) -> StartApproval:
        if not self.approved_at < self.expires_at <= self.approved_at + timedelta(hours=24):
            raise ValueError("paper start approval requires a window of at most 24 hours")
        return self


class PaperStudy(Record):
    kind: Literal["paper-study-v1"] = "paper-study-v1"
    id: UUID
    approval: StartApproval
    candidate: Hash
    trial: str
    started_at: UtcDateTime
    operating_review: OperatingReview
    initial_state_hash: Hash
    live_execution_available: Literal[False] = False


class StudyMember(Record):
    study_id: UUID
    account_id: UUID
    strategy: ResearchVariant
    strategy_version: Literal["slow-v2"] = "slow-v2"
    symbol: Symbol
    interval_minutes: Literal[240, 1440]
    entry_time_in_force: Literal["GTD"] = "GTD"
    selected_at: UtcDateTime
    policy_hash: Hash

    def identity(self, watermark: datetime) -> UUID:
        return uuid5(self.study_id, f"{self.symbol}:{watermark.isoformat()}")


def members(study: PaperStudy) -> tuple[StudyMember, ...]:
    symbol, interval, variant = study.trial.split(":")
    pairs = ("BTC/GBP", "ETH/GBP") if symbol == "BTC+ETH/GBP" else (symbol,)
    strategy = portfolio_variant(cast(PortfolioName, variant)) if len(pairs) == 2 else variant
    return tuple(
        StudyMember.restore(
            {
                "schema_version": 1,
                "study_id": str(study.id),
                "account_id": str(study.approval.account_id),
                "strategy": strategy,
                "symbol": pair,
                "interval_minutes": int(interval),
                "selected_at": study.started_at.isoformat(),
                "policy_hash": study.approval.policy,
            }
        )
        for pair in pairs
    )


async def read_study(c: AsyncConnection, account: UUID) -> PaperStudy | None:
    row = (
        (await c.execute(select(studies).where(studies.c.account_id == account)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    value = PaperStudy.restore(row["document"])
    if (
        value.id != row["id"]
        or value.approval.id != row["approval_id"]
        or value.approval.account_id != account
        or fingerprint(value.document()) != row["fingerprint"]
    ):
        raise ValueError("paper study identity changed")
    return value


def check_release(study: PaperStudy) -> None:
    if (
        study.approval.release != __version__
        or study.approval.package != package_fingerprint()
        or study.approval.policy != digest(canonical(RiskPolicy().model_dump(mode="json")))
        or study.approval.operating_profile != study.operating_review.identity()
        or study.operating_review.purpose != study.approval.purpose
        or study.operating_review.package != study.approval.package
    ):
        raise ValueError("paper study release/policy changed; no implicit resume")


async def start_study(
    writer: PaperWriter,
    approval: StartApproval,
    evaluation: Evaluation,
    frozen: Freeze,
    holdout: HoldoutReview,
    at: datetime,
    *,
    operating_review: OperatingReview,
    database_budget: int = 2_000_000_000,
) -> PaperStudy:
    from aca.research.gates import assess

    model = operating_review.measurement["model"]
    if not isinstance(model, dict) or model.get("database_limit_bytes") != database_budget:
        raise ValueError("start storage budget differs from reviewed profile")
    decision = assess(
        evaluation.report,
        evaluation.bindings,
        evaluation.evidence,
        calibration=evaluation.calibration,
        reviews=evaluation.reviews,
    )
    binding = evaluation.bindings
    if (
        decision.status != "freeze_eligible"
        or frozen.bindings != binding
        or frozen.decision != digest(canonical(decision.model_dump(mode="json")))
        or approval.evaluation != digest(canonical(evaluation.model_dump(mode="json")))
        or approval.freeze != digest(canonical(frozen.model_dump(mode="json")))
        or approval.holdout_review != digest(canonical(holdout.model_dump(mode="json")))
        or approval.purpose != binding.purpose
        or approval.costs != binding.costs
        or approval.policy != binding.policy
        or approval.account_id != writer.store.account_id
        or holdout.freeze != approval.freeze
        or holdout.decision.bindings != binding
        or holdout.decision.status != "holdout_reviewed"
        or holdout.decision.report != digest(canonical(holdout.holdout_result))
        or not approval.approved_at <= at < approval.expires_at
    ):
        raise ValueError("missing, changed, expired or ineligible paper start evidence")
    async with writer.transaction() as c:
        if await read_study(c, approval.account_id) is not None:
            raise ValueError("paper account already has a study; approval cannot be replayed")
        if await c.scalar(
            select(selections.c.account_id).where(selections.c.account_id == approval.account_id)
        ):
            raise ValueError("operational history cannot be relabelled as a study")
        use = (
            (
                await c.execute(
                    select(ResearchHoldoutUse).where(ResearchHoldoutUse.scope == holdout.scope)
                )
            )
            .mappings()
            .one_or_none()
        )
        saved = await c.scalar(
            select(ResearchArtifact.document).where(
                ResearchArtifact.fingerprint == approval.holdout_review,
                ResearchArtifact.kind == "research-holdout-review-v1",
            )
        )
        if (
            use is None
            or use["freeze"] != approval.freeze
            or saved != holdout.model_dump(mode="json")
            or (approval.purpose == "actual" and holdout.scope != ACTUAL_SCOPE)
            or (approval.purpose == "synthetic" and not holdout.scope.startswith("synthetic:"))
        ):
            raise ValueError("holdout review must have a committed matching use and outcome")
        if approval.purpose == "actual":
            if approval.approved_at < max(frozen.at, use["claimed_at"]):
                raise ValueError("actual start review must follow freeze and holdout use")
            proof = await c.scalar(
                select(ResearchArtifact.document).where(
                    ResearchArtifact.fingerprint == operating_review.production_input_evidence,
                    ResearchArtifact.kind == "paper-input-profile-review-v1",
                )
            )
            if (
                not isinstance(proof, dict)
                or digest(canonical(proof)) != operating_review.production_input_evidence
                or proof.get("purpose") != "actual"
                or proof.get("package") != approval.package
                or proof.get("measurement") != digest(canonical(operating_review.measurement))
                or proof.get("approved") is not True
                or not proof.get("evidence")
            ):
                raise ValueError("actual producer fit requires retained reviewed input evidence")
        state = await writer.store.audit(c)
        if (
            state is None
            or state.orders
            or state.quantity("BTC")
            or state.quantity("ETH")
            or state.quantity("GBP") <= 0
            or state.units <= 0
            or state.accounting_invalidated
            or state.kill.soft
            or state.kill.hard
            or state.kill.daily_day is not None
            or at < state.as_of
        ):
            raise ValueError(
                "study requires a clean funded flat account without prior orders/latches"
            )
        value = PaperStudy(
            id=uuid4(),
            approval=approval,
            candidate=binding.identity(),
            trial=binding.trial,
            started_at=at,
            operating_review=operating_review,
            initial_state_hash=fingerprint(state.document()),
        )
        check_release(value)
        members(value)  # Validate the exact admitted signal definitions before recording start.
        for artifact in (approval, evaluation, frozen, operating_review):
            doc = artifact.model_dump(mode="json")
            ident = digest(canonical(doc))
            await c.execute(
                insert(ResearchArtifact)
                .values(
                    fingerprint=ident,
                    schema_version=1,
                    kind=doc["kind"],
                    document=doc,
                    registered_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=[ResearchArtifact.fingerprint])
            )
            saved_doc = await c.scalar(
                select(ResearchArtifact.document).where(ResearchArtifact.fingerprint == ident)
            )
            if saved_doc != doc:
                raise ValueError("start evidence registry collision")
        await c.execute(
            studies.insert().values(
                id=value.id,
                account_id=approval.account_id,
                approval_id=approval.id,
                fingerprint=fingerprint(value.document()),
                document=value.document(),
            )
        )
        return value


async def history(c: AsyncConnection, study: PaperStudy) -> AsyncGenerator[dict[str, object]]:
    previous, number = "0" * 64, 0
    while True:
        rows = (
            (
                await c.execute(
                    select(events)
                    .where(events.c.study_id == study.id, events.c.number > number)
                    .order_by(events.c.number)
                    .limit(256)
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            number += 1
            doc = row["document"]
            if (
                number > MAX_EVENTS
                or row["number"] != number
                or row["previous_hash"] != previous
                or fingerprint({"previous": previous, "event": doc}) != row["fingerprint"]
            ):
                raise ValueError("paper study event chain corrupt or budget exceeded")
            previous = row["fingerprint"]
            yield doc
        if len(rows) < 256:
            return


async def append(
    writer: PaperWriter,
    study: PaperStudy,
    kind: str,
    at: datetime,
    payload: dict[str, object],
    *,
    identity: UUID | None = None,
) -> None:
    check_release(study)
    if at < study.started_at or writer.store.account_id != study.approval.account_id:
        raise ValueError("paper study event precedes its start/account")
    async with writer.transaction() as c:
        head = (
            (
                await c.execute(
                    select(events)
                    .where(events.c.study_id == study.id)
                    .order_by(events.c.number.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        number, previous = (head["number"] + 1, head["fingerprint"]) if head else (1, "0" * 64)
        if number > MAX_EVENTS:
            raise ValueError("paper study event capacity exhausted")
        doc = {"kind": kind, "at": at.isoformat(), "payload": payload}
        await c.execute(
            events.insert().values(
                study_id=study.id,
                number=number,
                event_id=identity or uuid4(),
                previous_hash=previous,
                fingerprint=fingerprint({"previous": previous, "event": doc}),
                document=doc,
            )
        )


async def claim_signal(
    writer: PaperWriter, study: PaperStudy, member: StudyMember, value: PaperDecision
) -> bool:
    if (
        value.client_id != member.identity(value.watermark)
        or value.selection_hash != fingerprint(member.document())
        or value.account_id != member.account_id
        or value.decision_at < study.started_at
        or (
            value.signal is not None
            and (
                value.signal.symbol != member.symbol
                or value.signal.strategy != member.strategy
                or value.signal.strategy_version != "slow-v2"
            )
        )
    ):
        raise ValueError("study signal binding mismatch")
    if member not in members(study):
        raise ValueError("unregistered study member")
    async with writer.transaction() as c:
        exists = await c.scalar(
            select(events.c.event_id).where(events.c.event_id == value.client_id)
        )
    if exists:
        return False
    await append(
        writer, study, "signal", value.recorded_at, value.document(), identity=value.client_id
    )
    return True


class HealthBlock(Record):
    start: UtcDateTime
    end: UtcDateTime
    healthy_seconds: FinancialDecimal = Field(ge=0)
    unhealthy_seconds: FinancialDecimal = Field(ge=0)
    dependency_unavailable_seconds: FinancialDecimal = Field(ge=0)
    dependency_evidence: Hash | None = None
    faults: tuple[str, ...] = ()
    financial_discrepancies: tuple[str, ...] = ()

    @model_validator(mode="after")
    def duration(self) -> HealthBlock:
        seconds = Decimal(str((self.end - self.start).total_seconds()))
        if not 0 < seconds <= 60 or seconds != (
            self.healthy_seconds + self.unhealthy_seconds + self.dependency_unavailable_seconds
        ):
            raise ValueError("health block must partition at most 60 observed seconds")
        if self.dependency_unavailable_seconds and self.dependency_evidence is None:
            raise ValueError("dependency exclusions require retained reviewed evidence")
        return self


async def record_health(writer: PaperWriter, study: PaperStudy, block: HealthBlock) -> None:
    async with writer.transaction() as c:
        head = await c.scalar(
            select(events.c.document)
            .where(events.c.study_id == study.id, events.c.document["kind"].as_string() == "health")
            .order_by(events.c.number.desc())
            .limit(1)
        )
    if block.start < study.started_at or (
        head and block.start < HealthBlock.restore(head["payload"]).end
    ):
        raise ValueError("paper health blocks overlap or precede approval")
    if block.dependency_unavailable_seconds:
        async with writer.transaction() as c:
            proof = await c.scalar(
                select(ResearchArtifact.document).where(
                    ResearchArtifact.fingerprint == block.dependency_evidence,
                    ResearchArtifact.kind == "paper-dependency-outage-review-v1",
                )
            )
        if (
            not isinstance(proof, dict)
            or digest(canonical(proof)) != block.dependency_evidence
            or proof.get("purpose") != study.approval.purpose
            or proof.get("start") != block.start.isoformat()
            or proof.get("end") != block.end.isoformat()
            or proof.get("approved") is not True
            or proof.get("excluded_seconds") != str(block.dependency_unavailable_seconds)
            or not proof.get("evidence")
        ):
            raise ValueError("dependency exclusion lacks retained matching outage evidence")
    await append(writer, study, "health", block.end, block.document())


async def study_status(c: AsyncConnection, study: PaperStudy, at: datetime) -> dict[str, object]:
    healthy = unhealthy = dependency = Decimal(0)
    faults: set[str] = set()
    discrepancies: set[str] = set()
    end = study.started_at
    signals: set[str] = set()
    completed: set[str] = set()
    async for event in history(c, study):
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise ValueError("invalid paper study payload")
        if event["kind"] == "health":
            block = HealthBlock.restore(payload)
            if block.start < end or block.end > at:
                raise ValueError("paper health chronology invalid")
            end = block.end
            healthy += block.healthy_seconds
            unhealthy += block.unhealthy_seconds
            dependency += block.dependency_unavailable_seconds
            faults.update(block.faults)
            discrepancies.update(block.financial_discrepancies)
        elif event["kind"] == "fault":
            faults.add(str(payload["reason"]))
        elif event["kind"] == "signal":
            decision = PaperDecision.restore(payload)
            if decision.signal and decision.signal.desired == "long":
                signals.add(str(decision.client_id))
        elif event["kind"] == "episode":
            if payload.get("entry_client_id") not in signals:
                raise ValueError("completed episode lacks a study signal")
            completed.add(str(payload["episode_id"]))
    elapsed = max(Decimal(0), Decimal(str((at - study.started_at).total_seconds())))
    missing = max(Decimal(0), elapsed - healthy - unhealthy - dependency)
    denominator = elapsed - dependency
    availability = healthy / denominator if denominator > 0 else None
    actual = study.approval.purpose == "actual"
    return {
        "study": study.document(),
        "as_of": at.isoformat(),
        "elapsed_seconds": str(elapsed),
        "healthy_seconds": str(healthy),
        "unhealthy_seconds": str(unhealthy),
        "dependency_unavailable_seconds": str(dependency),
        "unobserved_seconds": str(missing),
        "availability": str(availability) if availability is not None else None,
        "retained_faults": sorted(faults),
        "retained_financial_discrepancies": sorted(discrepancies),
        "observed_completed_signal_episodes": len(completed),
        "qualifying_days": int(elapsed // 86400) if actual else 0,
        "qualifying_episodes": len(completed) if actual else 0,
        "minimum_time_and_count_met": actual and elapsed >= 56 * 86400 and len(completed) >= 10,
        "reliability_target_met": availability is not None and availability >= Decimal("0.995"),
        "g4_pass": False,
        "promotion_evidence": actual,
        "live_execution_available": False,
    }


async def completed_episodes(writer: PaperWriter, study: PaperStudy) -> None:
    """Derive episode completion from reconciled durable cumulative fills, not notifications."""
    async with writer.transaction() as c:
        state = await writer.store.load(c)
        if state is None or not state.ready or state.accounting_invalidated or state.incident:
            return
        signals: set[str] = set()
        seen: set[str] = set()
        async for doc in history(c, study):
            payload = doc["payload"]
            if not isinstance(payload, dict):
                raise ValueError("invalid study event payload")
            if doc["kind"] == "signal":
                signals.add(str(payload["client_id"]))
            elif doc["kind"] == "episode":
                seen.add(str(payload["episode_id"]))
    for entry in state.orders:
        if (
            entry.intent.purpose != "entry"
            or entry.filled <= 0
            or entry.state not in TERMINAL
            or str(entry.intent.client_id) not in signals
            or str(entry.intent.episode_id) in seen
        ):
            continue
        related = [o for o in state.orders if o.intent.episode_id == entry.intent.episode_id]
        sold = sum((o.filled for o in related if o.intent.side == "sell"), Decimal(0))
        if sold == entry.filled and all(
            o.state in TERMINAL and o.state not in UNCERTAIN for o in related
        ):
            await append(
                writer,
                study,
                "episode",
                state.as_of,
                {
                    "episode_id": str(entry.intent.episode_id),
                    "entry_client_id": str(entry.intent.client_id),
                    "quantity": str(entry.filled),
                    "state_version": state.version,
                    "state_hash": fingerprint(state.document()),
                },
                identity=uuid5(study.id, "episode:" + str(entry.intent.episode_id)),
            )
