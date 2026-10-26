"""Publish local immutable evidence to PostgreSQL. No NumPy dependency or network reads."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from aca.research.artifacts import canonical, digest, read_json
from aca.research.datasets import instant
from aca.storage.models import ResearchArtifact, ResearchEvent


def _paths(root: Path) -> list[Path]:
    return sorted(
        [
            *root.glob("datasets/*/manifest.json"),
            *root.glob("reports/*.json"),
            *root.glob("freezes/*.json"),
            *root.glob("events/*.json"),
            *root.glob("packets/*.json"),
            *root.glob("decisions/*.json"),
            *root.glob("calibrations/*.json"),
            *root.glob("evaluations/*.json"),
        ]
    )


async def register(root: Path, engine: AsyncEngine) -> dict[str, int]:
    paths = await asyncio.to_thread(_paths, root)
    if len(paths) > 10_000:
        raise ValueError("registry batch exceeds budget")
    artifacts = events = 0
    for path in paths:
        document = await asyncio.to_thread(read_json, path)
        fingerprint = digest(canonical(document))
        expected = path.parent.name if path.name == "manifest.json" else path.stem
        if fingerprint != expected:
            raise ValueError("local evidence fingerprint mismatch")
        is_event = path.parent.name == "events"
        kind = (
            "trial_event"
            if is_event
            else "dataset"
            if path.name == "manifest.json"
            else str(document.get("kind"))
        )
        if kind not in {
            "trial_event",
            "dataset",
            "research_report",
            "holdout_report",
            "candidate_freeze",
            "research_review_packet",
            "candidate-freeze-v2",
            "corrected-evaluation-v1",
            "research-decision-v1",
            "research-holdout-review-v1",
            "synthetic_statistical_calibration",
        }:
            raise ValueError("unsupported research artefact kind")
        if kind == "candidate-freeze-v2":
            from aca.research.gates import Freeze

            Freeze.model_validate_json(canonical(document))
        elif kind == "corrected-evaluation-v1":
            from aca.research.gates import Evaluation

            Evaluation.model_validate_json(canonical(document))
        elif kind == "research-decision-v1":
            from aca.research.gates import Decision

            Decision.model_validate_json(canonical(document))
        elif kind == "research-holdout-review-v1":
            from aca.research.gates import HoldoutReview

            HoldoutReview.model_validate_json(canonical(document))
        if kind == "research_review_packet":
            from aca.research.bundles import verify

            await asyncio.to_thread(verify, path)
        async with engine.begin() as connection:
            result = await connection.execute(
                insert(ResearchArtifact)
                .values(
                    fingerprint=fingerprint,
                    schema_version=1,
                    kind=kind,
                    document=document,
                    registered_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(index_elements=[ResearchArtifact.fingerprint])
                .returning(ResearchArtifact.fingerprint)
            )
            artifacts += int(result.scalar_one_or_none() is not None)
            previous = (
                await connection.execute(
                    select(ResearchArtifact.document).where(
                        ResearchArtifact.fingerprint == fingerprint
                    )
                )
            ).scalar_one()
            if previous != document:
                raise ValueError("conflicting registered artefact")
            if is_event:
                result = await connection.execute(
                    insert(ResearchEvent)
                    .values(
                        id=UUID(str(document["id"])),
                        run_id=UUID(str(document["run_id"])),
                        artifact_fingerprint=fingerprint,
                        status=str(document["status"]),
                        occurred_at=instant(document["at"]),
                    )
                    .on_conflict_do_nothing(index_elements=[ResearchEvent.id])
                    .returning(ResearchEvent.id)
                )
                events += int(result.scalar_one_or_none() is not None)
                previous_hash = (
                    await connection.execute(
                        select(ResearchEvent.artifact_fingerprint).where(
                            ResearchEvent.id == UUID(str(document["id"]))
                        )
                    )
                ).scalar_one()
                if previous_hash != fingerprint:
                    raise ValueError("conflicting research event ID")
    return {"artifacts_inserted": artifacts, "events_inserted": events}
