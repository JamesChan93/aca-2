from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import URL, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from aca.domain.values import utc_datetime
from aca.observability.logging import redact
from aca.operations.config import Settings
from aca.operations.secrets import read_secret
from aca.storage.models import AuditEvent, ConfigurationRecord

SCHEMA_REVISION = "0014_accounting_operations"


class DatabaseSafetyError(RuntimeError):
    """The database has not demonstrated the required foundation properties."""


def create_engine(
    settings: Settings, role: Literal["runtime", "migration"] = "runtime"
) -> AsyncEngine:
    db = settings.database
    secret_path = db.runtime_password_file if role == "runtime" else db.migration_password_file
    password = read_secret(Path(secret_path)).get_secret_value()
    url = URL.create(
        "postgresql+psycopg",
        host=db.host,
        port=db.port,
        database=db.name,
        username=db.runtime_user if role == "runtime" else db.migration_user,
        password=password,
    )
    return create_async_engine(
        url,
        echo=False,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        pool_timeout=3,
        connect_args={
            "connect_timeout": db.connect_timeout_seconds,
            "options": "-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=5000",
            "application_name": "aca-foundation",
        },
    )


async def verify_runtime(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        identity = (
            await connection.execute(
                text(
                    "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                    "rolbypassrls, "
                    "has_database_privilege(current_database(), 'CREATE') AS db_create, "
                    "has_database_privilege(current_database(), 'TEMP') AS db_temp, "
                    "has_schema_privilege('aca', 'CREATE') AS schema_create "
                    "FROM pg_roles WHERE rolname = current_user",
                )
            )
        ).one()
        if identity[0] != "aca_runtime" or any(identity[1:]):
            raise DatabaseSafetyError("runtime database role is overprivileged")
        membership = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_auth_members WHERE member = "
                "(SELECT oid FROM pg_roles WHERE rolname = current_user)",
            )
        )
        if membership:
            raise DatabaseSafetyError("runtime database role must not have role memberships")
        unsafe_tables = await connection.scalar(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON c.relnamespace = n.oid "
                "WHERE n.nspname = 'aca' AND c.relkind IN ('r', 'p') AND "
                "(pg_get_userbyid(c.relowner) <> 'aca_migrator' OR "
                "has_table_privilege(c.oid, 'UPDATE,DELETE,TRUNCATE,TRIGGER'))",
            )
        )
        if unsafe_tables:
            raise DatabaseSafetyError("runtime table ownership or privileges are unsafe")
        revision = await connection.scalar(text("SELECT version_num FROM aca.alembic_version"))
        if revision != SCHEMA_REVISION:
            raise DatabaseSafetyError("database schema revision is not supported")


class FoundationStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def record_startup(self, settings: Settings, run_id: UUID) -> None:
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            await session.execute(
                insert(ConfigurationRecord)
                .values(
                    fingerprint=settings.fingerprint(),
                    schema_version=1,
                    mode="paper",
                    document=settings.document(),
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=[ConfigurationRecord.fingerprint])
            )
            session.add(
                AuditEvent(
                    id=uuid4(),
                    run_id=run_id,
                    occurred_at=now,
                    event="foundation_started",
                    actor="system",
                    configuration_fingerprint=settings.fingerprint(),
                    detail={"phase": 1, "live_execution_available": False},
                )
            )

    async def record_event(
        self,
        *,
        run_id: UUID,
        fingerprint: str,
        event: str,
        occurred_at: datetime,
        detail: dict[str, object],
    ) -> None:
        timestamp = utc_datetime(occurred_at)
        safe_detail = redact(detail)
        if not isinstance(safe_detail, dict):
            raise ValueError("audit detail must be an object")
        async with self.sessions.begin() as session:
            session.add(
                AuditEvent(
                    id=uuid4(),
                    run_id=run_id,
                    occurred_at=timestamp,
                    event=event,
                    actor="system",
                    configuration_fingerprint=fingerprint,
                    detail=safe_detail,
                )
            )

    async def list_events(self, limit: int = 20) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise ValueError("audit limit must be between 1 and 100")
        async with self.sessions() as session:
            events = (
                await session.scalars(
                    select(AuditEvent)
                    .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id)
                    .limit(limit),
                )
            ).all()
            return [
                {
                    "id": str(item.id),
                    "run_id": str(item.run_id),
                    "occurred_at": utc_datetime(item.occurred_at).isoformat(),
                    "event": item.event,
                    "configuration_fingerprint": item.configuration_fingerprint,
                    "detail": item.detail,
                }
                for item in events
            ]
