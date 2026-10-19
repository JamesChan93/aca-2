"""Upgrade real legacy projections without rewriting their financial evidence."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy import Connection, select, text
from test_paper_execution import AT, D
from test_tax_cash_reserves import request

from aca.domain.trading import Valuation
from aca.execution.paper import PaperExecution
from aca.operations.config import Settings
from aca.operations.provision import provision_roles
from aca.operations.secrets import read_secret
from aca.simulation.paper import PaperExchange
from aca.storage import migrate as migration_module
from aca.storage.database import DatabaseSafetyError, create_engine, verify_runtime
from aca.storage.migrate import migrate
from aca.storage.paper import PaperStore, PaperWriter, checkpoints, events
from aca.storage.reserves import read_reserves

pytestmark = pytest.mark.integration


def legacy_schema(c: Connection) -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(migration_module.__file__).parent / "migrations")
    )
    config.attributes["connection"] = c
    command.upgrade(config, "0011_notification_reviews")


async def test_upgrade_preserves_legacy_hashes_checkpoints_and_balances(
    database_settings: Settings,
) -> None:
    db = database_settings.database
    # database_settings is created by the loopback-only disposable-test fixture.
    assert db.host in {"localhost", "127.0.0.1", "::1"} and db.name.startswith("aca_test_")
    name = "aca_test_upgrade_" + uuid4().hex[:12]
    admin_file = Path(db.migration_password_file).parent / "admin"
    password = read_secret(admin_file).get_secret_value()
    with psycopg.connect(
        host=db.host,
        port=db.port,
        dbname=db.name,
        user="aca_admin",
        password=password,
        autocommit=True,
        connect_timeout=3,
    ) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    settings = database_settings.model_copy(
        update={"database": db.model_copy(update={"name": name})}
    )
    runtime, migration = create_engine(settings), create_engine(settings, "migration")
    try:
        provision_roles(settings, admin_file)
        async with migration.begin() as c:
            await c.execute(text("CREATE SCHEMA aca"))
            await c.run_sync(legacy_schema)
        with pytest.raises(DatabaseSafetyError, match="schema revision"):
            await verify_runtime(runtime)
        store = PaperStore(runtime, uuid4())
        async with PaperWriter(store) as writer:
            service = PaperExecution(writer, PaperExchange(store))
            await service.initialise(AT)
            await service.cashflow("legacy-opening", D(1000), AT)
            assert (await service.recover(AT)).complete
            for i in range(130):
                await service.mark(
                    (
                        Valuation(
                            asset="BTC", bid_gbp=D(999), observed_at=AT, source=f"legacy-mark:{i}"
                        ),
                    ),
                    AT,
                )
            async with writer.transaction() as c:
                state = await store.audit(c)
                assert state is not None and state.version > 128
                history = await store.history(c)
                prefix = list(
                    await c.execute(
                        select(events.c.version, events.c.fingerprint)
                        .where(events.c.account_id == store.account_id)
                        .order_by(events.c.version)
                    )
                )
                anchors = list(
                    await c.execute(
                        select(checkpoints.c.version, checkpoints.c.state_hash)
                        .where(checkpoints.c.account_id == store.account_id)
                        .order_by(checkpoints.c.version)
                    )
                )
        await migrate(settings)
        await verify_runtime(runtime)
        async with PaperWriter(store) as writer:
            async with writer.transaction() as c:
                assert await store.audit(c) == state
                assert await store.history(c) == history
            empty = await read_reserves(runtime, store.account_id)
            assert empty["items"] == [] and D(str(empty["manual_tax_cash_reserve_gbp"])) == 0
            service = PaperExecution(writer, PaperExchange(store))
            decision = request(state.version)
            assert (await service.reserve_tax_cash(decision, at=AT))["inserted"] is True
            assert (await service.reserve_tax_cash(decision, at=AT))["inserted"] is False
            async with writer.transaction() as c:
                assert (await store.audit(c)).tax_reserve_gbp == 100  # type: ignore[union-attr]
                after_prefix = list(
                    await c.execute(
                        select(events.c.version, events.c.fingerprint)
                        .where(
                            events.c.account_id == store.account_id,
                            events.c.version <= state.version,
                        )
                        .order_by(events.c.version)
                    )
                )
                after_anchors = list(
                    await c.execute(
                        select(checkpoints.c.version, checkpoints.c.state_hash)
                        .where(
                            checkpoints.c.account_id == store.account_id,
                            checkpoints.c.version <= state.version,
                        )
                        .order_by(checkpoints.c.version)
                    )
                )
                assert after_prefix == prefix and after_anchors == anchors
                assert await store.history(c) == history
    finally:
        await runtime.dispose()
        await migration.dispose()
        with psycopg.connect(
            host=db.host,
            port=db.port,
            dbname=db.name,
            user="aca_admin",
            password=password,
            autocommit=True,
            connect_timeout=3,
        ) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
