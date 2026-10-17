import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aca.operations.config import Settings
from aca.storage.database import DatabaseSafetyError, FoundationStore, create_engine, verify_runtime
from aca.storage.migrate import migrate
from aca.storage.models import AuditEvent

pytestmark = pytest.mark.integration


async def test_migration_idempotence_and_restart_persistence(database_settings: Settings) -> None:
    await migrate(database_settings)
    run_id = uuid4()
    engine = create_engine(database_settings)
    try:
        await verify_runtime(engine)
        store = FoundationStore(engine)
        await store.record_startup(database_settings, run_id)
        await store.record_event(
            run_id=run_id,
            fingerprint=database_settings.fingerprint(),
            event="test_event",
            occurred_at=datetime.now(UTC),
            detail={"value": "safe", "password": "private-value"},
        )
    finally:
        await engine.dispose()
    restarted = create_engine(database_settings)
    try:
        store = FoundationStore(restarted)
        async with store.sessions() as session:
            events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.run_id == run_id,
                    )
                )
            ).all()
        assert len(events) == 2
        assert (
            next(item for item in events if item.event == "test_event").detail["password"]
            == "[REDACTED]"
        )
        assert all(item.occurred_at.tzinfo is not None for item in events)
    finally:
        await restarted.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE public.forbidden(id int)",
        "CREATE SCHEMA forbidden",
        "CREATE TEMP TABLE forbidden(id int)",
        "DELETE FROM aca.audit_events",
        "UPDATE aca.configuration_versions SET mode = 'paper'",
        "TRUNCATE aca.audit_events",
        "DROP TABLE aca.audit_events",
        "CREATE ROLE forbidden",
    ],
)
async def test_runtime_cannot_change_schema_or_history(
    database_settings: Settings,
    statement: str,
) -> None:
    engine = create_engine(database_settings)
    try:
        async with engine.begin() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def test_history_triggers_reject_mutation_even_for_owner(database_settings: Settings) -> None:
    engine = create_engine(database_settings, "migration")
    try:
        for statement in (
            "UPDATE aca.audit_events SET actor = 'changed'",
            "DELETE FROM aca.audit_events",
            "TRUNCATE aca.audit_events",
        ):
            async with engine.connect() as connection:
                with pytest.raises(DBAPIError):
                    await connection.execute(text(statement))
                await connection.rollback()
    finally:
        await engine.dispose()


async def test_failed_event_transaction_leaves_no_partial_record(
    database_settings: Settings,
) -> None:
    engine = create_engine(database_settings)
    try:
        store = FoundationStore(engine)
        run_id = uuid4()
        await store.record_startup(database_settings, run_id)
        with pytest.raises(DBAPIError):
            await store.record_event(
                run_id=run_id,
                fingerprint="missing" * 9,
                event="invalid_foreign_key",
                occurred_at=datetime.now(UTC),
                detail={},
            )
        async with store.sessions() as session:
            events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.run_id == run_id,
                    )
                )
            ).all()
        assert len(events) == 1
    finally:
        await engine.dispose()


async def test_independent_concurrent_sessions(database_settings: Settings) -> None:
    engine = create_engine(database_settings)
    try:
        store = FoundationStore(engine)
        run_ids = [uuid4() for _ in range(8)]
        await asyncio.gather(
            *(store.record_startup(database_settings, run_id) for run_id in run_ids)
        )
        async with store.sessions() as session:
            events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.run_id.in_(run_ids),
                    )
                )
            ).all()
        assert len(events) == 8
    finally:
        await engine.dispose()


async def test_unexpected_runtime_grant_fails_readiness(database_settings: Settings) -> None:
    migration_engine = create_engine(database_settings, "migration")
    runtime_engine = create_engine(database_settings)
    try:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "GRANT UPDATE ON aca.configuration_versions TO aca_runtime",
                )
            )
        with pytest.raises(DatabaseSafetyError):
            await verify_runtime(runtime_engine)
    finally:
        async with migration_engine.begin() as connection:
            await connection.execute(
                text(
                    "REVOKE UPDATE ON aca.configuration_versions FROM aca_runtime",
                )
            )
        await runtime_engine.dispose()
        await migration_engine.dispose()
