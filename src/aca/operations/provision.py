"""Explicit local database role setup, separate from application startup."""

from pathlib import Path

import psycopg
from psycopg import sql

from aca.operations.config import Settings
from aca.operations.secrets import read_secret
from aca.storage.database import DatabaseSafetyError


def provision_roles(settings: Settings, admin_password_file: Path) -> None:
    db = settings.database
    admin_password = read_secret(admin_password_file).get_secret_value()
    credentials = (
        (db.runtime_user, read_secret(Path(db.runtime_password_file)).get_secret_value()),
        (db.migration_user, read_secret(Path(db.migration_password_file)).get_secret_value()),
    )
    with psycopg.connect(
        host=db.host,
        port=db.port,
        dbname=db.name,
        user=db.admin_user,
        password=admin_password,
        connect_timeout=db.connect_timeout_seconds,
        options="-c statement_timeout=5000 -c lock_timeout=5000",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(741600)")
            for role, password in credentials:
                cursor.execute(
                    "SELECT rolsuper OR rolcreatedb OR rolcreaterole "
                    "OR rolreplication OR rolbypassrls "
                    "FROM pg_roles WHERE rolname = %s",
                    (role,),
                )
                existing = cursor.fetchone()
                if existing is None:
                    cursor.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                            "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                        ).format(sql.Identifier(role), sql.Literal(password))
                    )
                elif existing[0]:
                    raise DatabaseSafetyError("existing application role is overprivileged")
            cursor.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(db.name),
                )
            )
            cursor.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            for role, _ in credentials:
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(db.name),
                        sql.Identifier(role),
                    )
                )
            cursor.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(db.name),
                    sql.Identifier(db.migration_user),
                )
            )
