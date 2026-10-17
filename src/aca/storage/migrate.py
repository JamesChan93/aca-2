from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from aca.operations.config import Settings
from aca.storage.database import DatabaseSafetyError, create_engine


def _upgrade(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


async def migrate(settings: Settings) -> None:
    engine = create_engine(settings, "migration")
    try:
        async with engine.begin() as connection:
            role = await connection.scalar(text("SELECT current_user"))
            if role != settings.database.migration_user:
                raise DatabaseSafetyError("migration requires the dedicated migration role")
            await connection.execute(text("SELECT pg_advisory_xact_lock(741601)"))
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS aca"))
            await connection.run_sync(_upgrade)
    finally:
        await engine.dispose()
