"""Real PostgreSQL evidence persistence, idempotency and runtime restrictions."""

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from aca.operations.config import Settings
from aca.research.artifacts import canonical, read_json, save_document
from aca.research.registry import register
from aca.research.runner import event
from aca.storage.database import create_engine
from aca.storage.models import ResearchArtifact, ResearchEvent

pytestmark = pytest.mark.integration


async def test_registry_persistence_idempotency_and_conflicts(
    database_settings: Settings,
    tmp_path: Path,
) -> None:
    run_id = str(uuid4())
    path = event(tmp_path, run_id, "synthetic_trial", "started", {"test": True})
    engine = create_engine(database_settings)
    try:
        assert await register(tmp_path, engine) == {"artifacts_inserted": 1, "events_inserted": 1}
        assert await register(tmp_path, engine) == {"artifacts_inserted": 0, "events_inserted": 0}
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(ResearchEvent)
                    .where(ResearchEvent.run_id == run_id)
                )
                == 1
            )
        await engine.dispose()
        engine = create_engine(database_settings)
        assert await register(tmp_path, engine) == {"artifacts_inserted": 0, "events_inserted": 0}
        document = read_json(path)
        document["status"] = "altered"
        save_document(tmp_path / "events", document)
        with pytest.raises(ValueError, match="conflicting research event"):
            await register(tmp_path, engine)
        # The conflicting artefact/event transaction rolls back together.
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(ResearchArtifact)
                    .where(ResearchArtifact.kind == "trial_event")
                )
                == 1
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("table", ["research_artifacts", "research_events"])
@pytest.mark.parametrize("verb", ["DELETE FROM", "TRUNCATE"])
async def test_research_history_is_append_only(
    database_settings: Settings,
    table: str,
    verb: str,
) -> None:
    engine = create_engine(database_settings)
    try:
        async with engine.begin() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text(f"{verb} aca.{table}"))
    finally:
        await engine.dispose()


async def test_tampered_local_registry_rejected(
    database_settings: Settings, tmp_path: Path
) -> None:
    path = event(tmp_path, str(uuid4()), "synthetic", "started", {})
    path.write_bytes(canonical({"altered": True}))
    engine = create_engine(database_settings)
    try:
        with pytest.raises(ValueError, match="fingerprint"):
            await register(tmp_path, engine)
    finally:
        await engine.dispose()
