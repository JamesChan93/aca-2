"""Commit one holdout claim before work; never replay after uncertain completion."""

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from aca.research.artifacts import canonical, digest, save_document
from aca.research.gates import Bindings, Decision, Evidence, HoldoutReview, assess, read_freeze
from aca.storage.models import ResearchArtifact, ResearchHoldoutUse

ACTUAL_SCOPE = "actual:kraken-spot-gbp:2025-10-01:2026-10-01"


async def open_once(
    engine: AsyncEngine,
    root: Path,
    frozen_path: Path,
    report: dict[str, object],
    bindings: Bindings,
    evidence: Evidence,
    calibration: dict[str, object],
    work: Callable[[], Awaitable[dict[str, object]]],
    *,
    synthetic_campaign: UUID | None = None,
    reviews: Mapping[str, dict[str, object]],
) -> Path:
    frozen = read_freeze(frozen_path)
    if frozen.bindings != bindings:
        raise ValueError("freeze bindings changed before opening")
    decision = assess(report, bindings, evidence, calibration=calibration, reviews=reviews)
    if decision.status != "freeze_eligible" or frozen.decision != digest(
        canonical(decision.model_dump(mode="json"))
    ):
        raise ValueError("candidate no longer freeze eligible")
    if bindings.purpose == "synthetic":
        if synthetic_campaign is None:
            raise ValueError("synthetic workflow requires a campaign namespace")
        scope = f"synthetic:{synthetic_campaign}:2025-10-01:2026-10-01"
    else:
        if synthetic_campaign is not None:
            raise ValueError("synthetic namespace cannot open actual holdout")
        scope = ACTUAL_SCOPE
    # No candidate, dataset, code, root or lineage revision is part of the unique
    # actual key. Even relabelling the lineage cannot create another actual use.
    document: dict[str, object] = {
        "kind": "holdout-use-v1",
        "scope": scope,
        "freeze": frozen_path.stem,
        "bindings": bindings.model_dump(mode="json"),
        "at": datetime.now(UTC).isoformat(),
        "recovery": "consumed_even_on_failure; manual_review_no_reopening",
    }
    async with engine.begin() as connection:
        result = await connection.execute(
            insert(ResearchHoldoutUse)
            .values(
                scope=scope,
                freeze=frozen_path.stem,
                source_lineage=bindings.source_lineage,
                document=document,
                claimed_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[ResearchHoldoutUse.scope])
            .returning(ResearchHoldoutUse.scope)
        )
        if result.scalar_one_or_none() is None:
            raise ValueError("holdout already consumed; no replay or new-folder retry")
    # The transaction has committed before even the first holdout read.
    outcome = await work()
    reviewed = Decision(
        status="holdout_reviewed",
        bindings=bindings,
        report=digest(canonical(outcome)),
        reasons=(),
        inference=decision.inference,
        economic_hurdle=decision.economic_hurdle,
        statistical_pass=True,
        economic_pass=True,
    )
    document = HoldoutReview(
        decision=reviewed, scope=scope, freeze=frozen_path.stem, holdout_result=outcome
    ).model_dump(mode="json")
    fingerprint = digest(canonical(document))
    async with engine.begin() as connection:
        await connection.execute(
            insert(ResearchArtifact).values(
                fingerprint=fingerprint,
                schema_version=1,
                kind="research-holdout-review-v1",
                document=document,
                registered_at=datetime.now(UTC),
            )
        )
    return save_document(root / "decisions", document)
