import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from aca.operations.config import DatabaseSettings, Settings
from aca.operations.provision import provision_roles
from aca.storage.migrate import migrate


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get("REQUIRE_POSTGRES") == "1" and not os.environ.get("TEST_DATABASE_URL"):
        raise pytest.UsageError("REQUIRE_POSTGRES=1 requires TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def database_settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Settings]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to an isolated local PostgreSQL admin connection")
    params = conninfo_to_dict(url)
    if params.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("integration tests only create databases on loopback PostgreSQL")
    if params.get("user") != "aca_admin":
        pytest.fail("integration tests require a dedicated aca_admin test cluster")
    directory = tmp_path_factory.mktemp("credentials")
    directory.chmod(0o700)
    passwords = {
        "admin": str(params.get("password", "test-admin-password-not-for-production")),
        "runtime": "test-runtime-password-not-for-production",
        "migration": "test-migrator-password-not-for-production",
    }
    for kind, variable in (
        ("runtime", "TEST_RUNTIME_PASSWORD_FILE"),
        ("migration", "TEST_MIGRATION_PASSWORD_FILE"),
    ):
        if variable in os.environ:
            passwords[kind] = Path(os.environ[variable]).read_text().strip()
    for name, value in passwords.items():
        path = directory / name
        path.write_text(value)
        path.chmod(0o600)
    name = "aca_test_" + uuid4().hex[:12]
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings = Settings(
        database=DatabaseSettings(
            host=str(params["host"]),
            port=int(str(params.get("port", "5432"))),
            name=name,
            runtime_password_file=str(directory / "runtime"),
            migration_password_file=str(directory / "migration"),
        )
    )
    try:
        provision_roles(settings, directory / "admin")
        import asyncio

        asyncio.run(migrate(settings))
        yield settings
    finally:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def configuration_file(tmp_path: Path) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text('mode = "paper"\nenvironment = "development"\n')
    return path
