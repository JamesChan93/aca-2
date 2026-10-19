"""Trusted, synthetic fixture for scripts/restore_drill.py; absent from the wheel.

Runs only inside the disposable cluster's network namespace, as the test UID.
Runtime work uses aca_runtime; explicit creation/catalog work uses the test admin.
"""

import asyncio
import hashlib
import json
import re
import sys
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from process_worker import AT, market, proposal
from psycopg import sql
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from aca.domain.trading import OrderIntent, Valuation, VenueOrder
from aca.execution.paper import PaperExecution, SimulatedCrash
from aca.operations.config import DatabaseSettings, Settings
from aca.operations.provision import provision_roles
from aca.operations.secrets import read_secret
from aca.portfolio.reserves import TaxReserveRequest
from aca.portfolio.state import reserved_cash
from aca.risk.execution import DispatchDenied
from aca.simulation.paper import PaperExchange
from aca.storage.database import FoundationStore, create_engine, verify_runtime
from aca.storage.migrate import migrate
from aca.storage.paper import PaperStore, PaperWriter, accounts, fills

CASES = {
    "validated": UUID("a7fb85bd-1565-487c-a0de-5b2b5a705bb1"),
    "uncertain": UUID("7f4db6c0-a2c4-4584-83b7-1bf6a290120d"),
    "unposted_fill": UUID("a6024018-01e3-4303-9a15-c79cdaeb208a"),
    "hard_halt": UUID("984a5ed3-2d7f-432f-9ea5-bbdcd5f48d87"),
}
PROBE_ROLE = "aca_restore_probe"
D = Decimal


def settings_for(name: str) -> Settings:
    if not re.fullmatch(r"aca_test_restore_[a-f0-9]{12}", name):
        raise ValueError("restore fixture requires its disposable database namespace")
    return Settings(
        database=DatabaseSettings(
            name=name,
            runtime_password_file="/run/secrets/runtime_password",
            migration_password_file="/run/secrets/migration_password",
        )
    )


def admin(settings: Settings, *, control: bool = False) -> psycopg.Connection[tuple[Any, ...]]:
    db = settings.database
    return psycopg.connect(
        host="127.0.0.1",
        port=5432,
        dbname="aca" if control else db.name,
        user="aca_admin",
        password=read_secret(Path("/run/secrets/admin_password")).get_secret_value(),
        connect_timeout=3,
        autocommit=True,
        options="-c timezone=UTC -c statement_timeout=5000 -c lock_timeout=5000",
    )


def create_database(settings: Settings) -> None:
    with admin(settings, control=True) as c:
        c.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(settings.database.name)
            )
        )
    provision_roles(settings, Path("/run/secrets/admin_password"))


def allow_probe(settings: Settings) -> None:
    with admin(settings) as c:
        c.execute(
            "CREATE ROLE aca_restore_probe NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )


def stop_at(expected: str) -> Callable[[str], None]:
    def stop(point: str) -> None:
        if point == expected:
            raise SimulatedCrash(point)

    return stop


async def seed(settings: Settings) -> None:
    create_database(settings)
    await migrate(settings)
    allow_probe(settings)
    engine = create_engine(settings)
    try:
        await verify_runtime(engine)
        await FoundationStore(engine).record_startup(settings, uuid4())
        for case, account in CASES.items():
            store = PaperStore(engine, account)
            async with PaperWriter(store) as writer:
                venue = PaperExchange(store)
                execution = PaperExecution(writer, venue)
                await execution.initialise(AT)
                await execution.cashflow("restore-opening", D(1000), AT)
                await execution.mark(
                    (
                        Valuation(
                            asset="BTC", bid_gbp=D(999), observed_at=AT, source="restore-fixture"
                        ),
                    ),
                    AT,
                )
                assert (await execution.recover(AT)).complete
                if case == "validated":
                    state = await execution._state()
                    await execution.reserve_tax_cash(
                        TaxReserveRequest(
                            request_id=UUID("7ac9ded4-768e-46a2-bfae-96f818e4b070"),
                            actor=UUID("2b1571b8-d9e3-4da2-89d9-4ee8aec77678"),
                            expected_version=state.version,
                            amount_gbp=D(100),
                            evidence_sha256="1" * 64,
                            reason="Synthetic restore fixture manual tax cash reserve",
                        ),
                        at=AT,
                    )
                order = await execution.queue(proposal(), market(), AT, time_in_force="FOK")
                if case == "validated":
                    continue
                point = (
                    "after_submitting_before_send"
                    if case == "uncertain"
                    else "after_send_before_ack"
                )
                execution.fault = stop_at(point)
                try:
                    await execution.dispatch(order.intent.client_id, market(), AT)
                except SimulatedCrash:
                    pass
                else:
                    raise AssertionError("fixture did not stop at a consumed command")
                if case == "uncertain":
                    continue
                at = AT + timedelta(milliseconds=300)
                evidence = market(at)
                await venue.advance(evidence.book, evidence.rules, at)
                if case == "unposted_fill":
                    continue
                # This fixture deliberately lost its acknowledgement. Follow the
                # recovery path before constructing the already-posted halt case.
                outcome = await execution.recover(at)
                assert outcome.complete, outcome.differences
                # Include a real checkpoint, not just a short event prefix.
                for index in range(130):
                    at = AT + timedelta(milliseconds=400 + index)
                    await execution.mark(
                        (
                            Valuation(
                                asset="BTC",
                                bid_gbp=D(999),
                                observed_at=at,
                                source="restore-fixture",
                            ),
                        ),
                        at,
                    )
                await execution.halt(
                    "Synthetic restore fixture manual halt", AT + timedelta(seconds=1)
                )
    finally:
        await engine.dispose()
    with admin(settings) as c:
        c.execute("GRANT SELECT ON aca.paper_fills TO aca_restore_probe")


def digest_rows(rows: list[str]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = row.encode()
        digest.update(str(len(encoded)).encode() + b":" + encoded)
    return digest.hexdigest()


def constraint_text(definition: str) -> str:
    """Normalise PostgreSQL's two equivalent unbounded varchar-literal arrays.

    pg_dump reparses ARRAY['x'::varchar]::text[] as ARRAY['x'::varchar::text].
    Only this complete CHECK shape is admitted; never strip arbitrary casts.
    """
    literal = r"'(?:[^']|'')*'"
    for element_cast, array_cast in (
        (r"::character varying", r"::text\[\]"),
        (r"::character varying::text", ""),
    ):
        element = literal + element_cast
        match = re.fullmatch(
            r"CHECK \(([a-z_][a-z0-9_]*)::text = ANY \(ARRAY\[("
            + element
            + "(?:, "
            + element
            + r")*)\]"
            + array_cast
            + r"\)\)",
            definition,
        )
        if match:
            values = re.findall(literal, match[2])
            return f"CHECK ({match[1]}::text = ANY (ARRAY[{', '.join(values)}]))"
    return definition


def snapshot_id(value: str) -> str:
    if not re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+", value):
        raise ValueError("invalid exported snapshot identifier")
    return value


def database_snapshot(settings: Settings, snapshot: str | None = None) -> dict[str, Any]:
    """Small fixture only: bounded rows, deterministic JSON, no password catalog."""
    tables: dict[str, Any] = {}
    sequences: dict[str, Any] = {}
    maxima: dict[str, int | None] = {}
    with admin(settings) as c, c.transaction():
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if snapshot is not None:
            c.execute(
                sql.SQL("SET TRANSACTION SNAPSHOT {}").format(sql.Literal(snapshot_id(snapshot)))
            )
        names = c.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='aca' AND c.relkind='r' ORDER BY c.relname"
        ).fetchall()
        for (name,) in names:
            rows = [
                r[0]
                for r in c.execute(
                    sql.SQL(
                        "SELECT to_jsonb(t)::text FROM aca.{} t "
                        "ORDER BY to_jsonb(t)::text LIMIT 10001"
                    ).format(sql.Identifier(name))
                ).fetchall()
            ]
            if len(rows) > 10000 or sum(len(r) for r in rows) > 16 * 1024 * 1024:
                raise ValueError("restore fixture exceeds snapshot budget")
            tables[name] = {"rows": len(rows), "sha256": digest_rows(rows)}
        for (name,) in c.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='aca' AND c.relkind='S' ORDER BY c.relname"
        ).fetchall():
            definition = c.execute(
                "SELECT increment_by,cache_size,cycle,min_value FROM pg_sequences "
                "WHERE schemaname='aca' AND sequencename=%s",
                (name,),
            ).fetchone()
            if definition != (1, 1, False, 1):
                raise ValueError("restore fixture requires positive cache-one noncycling sequences")
            sequences[name] = list(
                c.execute(
                    sql.SQL("SELECT last_value, is_called FROM aca.{}").format(sql.Identifier(name))
                ).fetchone()
                or ()
            )
            owned = c.execute(
                "SELECT t.relname,a.attname FROM pg_class s "
                "JOIN pg_namespace n ON n.oid=s.relnamespace "
                "JOIN pg_depend d ON d.objid=s.oid AND d.classid='pg_class'::regclass "
                "AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') "
                "JOIN pg_class t ON t.oid=d.refobjid "
                "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid "
                "WHERE n.nspname='aca' AND s.relname=%s",
                (name,),
            ).fetchone()
            if owned is None:
                raise ValueError("restore fixture requires an owned sequence")
            maximum = c.execute(
                sql.SQL("SELECT max({}) FROM aca.{}").format(
                    sql.Identifier(owned[1]), sql.Identifier(owned[0])
                )
            ).fetchone()
            assert maximum is not None
            maxima[name] = maximum[0]
        # Object definitions and role *names* avoid unstable per-cluster OIDs.
        catalog_queries = {
            "schema": (
                "SELECT nspname, pg_get_userbyid(nspowner), nspacl::text FROM pg_namespace WHERE "
                "nspname IN ('aca','public')"
            ),
            "relations": (
                "SELECT "
                "c.relname,c.relkind,pg_get_userbyid(c.relowner),c.relacl::text,"
                "c.relrowsecurity,c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE "
                "n.nspname='aca'"
            ),
            "columns": (
                "SELECT "
                "c.relname,a.attnum,a.attname,format_type(a.atttypid,a.atttypmod),"
                "a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid) "
                "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON "
                "n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND "
                "d.adnum=a.attnum WHERE n.nspname='aca' AND c.relkind IN ('r','S') AND a.attnum>0 "
                "AND NOT a.attisdropped"
            ),
            "constraints": (
                "SELECT c.relname,k.conname,pg_get_constraintdef(k.oid,true),k.convalidated,"
                "k.conenforced,k.condeferrable,k.condeferred FROM "
                "pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON "
                "n.oid=c.relnamespace WHERE n.nspname='aca'"
            ),
            "indexes": "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='aca'",
            "sequence_definitions": (
                "SELECT sequencename,sequenceowner,data_type,start_value,min_value,max_value,"
                "increment_by,cycle,cache_size FROM pg_sequences WHERE schemaname='aca'"
            ),
            "triggers": (
                "SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid,true) FROM "
                "pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON "
                "n.oid=c.relnamespace WHERE n.nspname='aca' AND NOT t.tgisinternal"
            ),
            "functions": (
                "SELECT "
                "p.proname,pg_get_functiondef(p.oid),pg_get_userbyid(p.proowner),p.proacl::text "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='aca'"
            ),
            "default_privileges": (
                "SELECT pg_get_userbyid(d.defaclrole),n.nspname,d.defaclobjtype,d.defaclacl::text "
                "FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace"
            ),
            "roles": (
                "SELECT "
                "rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,"
                "rolreplication,rolbypassrls "
                "FROM pg_roles WHERE rolname IN "
                "('aca_admin','aca_migrator','aca_runtime','aca_restore_probe')"
            ),
            "database_privileges": (
                "SELECT datacl::text,pg_get_userbyid(datdba) FROM pg_database WHERE "
                "datname=current_database()"
            ),
        }
        records = {
            key: [list(row) for row in c.execute(query)] for key, query in catalog_queries.items()
        }
        for row in records["constraints"]:
            row[2] = constraint_text(row[2])
        catalog = {
            key: digest_rows(sorted(json.dumps(row, default=str) for row in rows))
            for key, rows in records.items()
        }
        constraint_definitions = sorted(records["constraints"])
    return {
        "tables": tables,
        "sequences": sequences,
        "sequence_row_maxima": maxima,
        "catalog_sha256": catalog,
        "constraint_definitions": constraint_definitions,
    }


async def economics(settings: Settings, snapshot: str | None = None) -> dict[str, Any]:
    engine = create_engine(settings)
    result: dict[str, Any] = {}
    try:
        await verify_runtime(engine)
        async with engine.connect() as c:
            await c.execution_options(isolation_level="REPEATABLE READ")
            await c.execute(text("SET TRANSACTION READ ONLY"))
            if snapshot is not None:
                await c.exec_driver_sql("SET TRANSACTION SNAPSHOT '" + snapshot_id(snapshot) + "'")
            if set(await c.scalars(select(accounts.c.id))) != set(CASES.values()):
                raise AssertionError("unexpected fixture accounts")
            for case, account in CASES.items():
                store = PaperStore(engine, account)
                state = await store.audit(c)
                assert state is not None
                result[case] = {
                    "balances": [b.model_dump(mode="json") for b in state.balances],
                    "reserved_gbp": str(reserved_cash(state)),
                    "tax_cash_reserve_gbp": str(state.tax_reserve_gbp),
                    "kill": state.kill.document(),
                    "orders": [
                        {
                            "client_id": str(o.intent.client_id),
                            "state": o.state,
                            "purpose": o.intent.purpose,
                            "filled": str(o.filled),
                        }
                        for o in state.orders
                    ],
                    "journals": [j.document() for j in await store.history(c)],
                    "fills": list(
                        await c.scalars(
                            select(fills.c.document)
                            .where(fills.c.account_id == account)
                            .order_by(fills.c.id)
                        )
                    ),
                }
    finally:
        await engine.dispose()
    return result


class NoCommands(PaperExchange):
    attempts = 0

    async def submit(self, intent: OrderIntent, at: datetime) -> VenueOrder:
        self.attempts += 1
        raise AssertionError("restore recovery must not submit")

    async def cancel(self, client_id: UUID, at: datetime) -> VenueOrder:
        self.attempts += 1
        raise AssertionError("fixture recovery must not cancel")


async def recover(settings: Settings) -> dict[str, Any]:
    before = await economics(settings)
    engine = create_engine(settings)
    try:
        for case, account in CASES.items():
            store = PaperStore(engine, account)
            async with PaperWriter(store) as writer:
                venue = NoCommands(store)
                execution = PaperExecution(writer, venue)
                at = AT + timedelta(seconds=3)
                outcome = await execution.recover(at)
                state = await execution._state()
                original = next(o for o in state.orders if o.intent.purpose == "entry")
                try:
                    await execution.dispatch(original.intent.client_id, market(at), at)
                except DispatchDenied:
                    pass
                else:
                    raise AssertionError("restored command was dispatchable")
                if case in {"uncertain", "hard_halt"}:
                    try:
                        await execution.queue(proposal(at), market(at), at)
                    except DispatchDenied:
                        pass
                    else:
                        raise AssertionError("restored halt/uncertainty allowed a new entry")
                assert venue.attempts == 0
                assert outcome.complete == (case != "uncertain")
    finally:
        await engine.dispose()
    after = await economics(settings)
    assert after["validated"]["orders"][0]["state"] == "EXPIRED"
    assert D(after["validated"]["reserved_gbp"]) == 0
    assert D(after["validated"]["tax_cash_reserve_gbp"]) == 100
    assert after["uncertain"]["orders"][0]["state"] == "UNKNOWN"
    assert D(after["uncertain"]["reserved_gbp"]) > 0
    assert after["hard_halt"]["kill"] == before["hard_halt"]["kill"]
    for case in ("validated", "uncertain", "hard_halt"):
        assert after[case]["tax_cash_reserve_gbp"] == before[case]["tax_cash_reserve_gbp"]
        assert after[case]["balances"] == before[case]["balances"]
        assert after[case]["fills"] == before[case]["fills"]
        assert after[case]["journals"] == before[case]["journals"]
    filled = after["unposted_fill"]
    assert len(filled["fills"]) == 1 and len(filled["journals"]) == 2
    fill = filled["fills"][0]
    balances = {b["asset"]: D(b["quantity"]) for b in filled["balances"]}
    assert balances["BTC"] == D(fill["quantity"])
    assert balances["GBP"] == D(1000) - D(fill["quantity"]) * D(fill["price"]) - sum(
        D(f["quantity"]) for f in fill["fees"]
    )
    for case in ("unposted_fill", "hard_halt"):
        children = [o for o in after[case]["orders"] if o["purpose"] == "protection"]
        assert len(children) == 1 and children[0]["state"] == "OPEN"
    return {"live_execution_available": False, "economics": after, "venue_commands": 0}


async def permissions(settings: Settings) -> None:
    engine = create_engine(settings)
    try:
        await verify_runtime(engine)
        for statement in (
            "CREATE TABLE public.restore_forbidden(id int)",
            "CREATE SCHEMA restore_forbidden",
            "CREATE TEMP TABLE restore_forbidden(id int)",
            "DELETE FROM aca.paper_journals",
            "UPDATE aca.paper_events SET kind='changed'",
            "TRUNCATE aca.paper_fills",
        ):
            async with engine.connect() as c:
                try:
                    await c.execute(text(statement))
                except DBAPIError:
                    await c.rollback()
                else:
                    await c.rollback()
                    raise AssertionError("restored runtime privilege permitted mutation")
    finally:
        await engine.dispose()
    owner = create_engine(settings, "migration")
    try:
        async with owner.connect() as c:
            try:
                await c.execute(text("UPDATE aca.paper_events SET kind='changed'"))
            except DBAPIError:
                await c.rollback()
            else:
                await c.rollback()
                raise AssertionError("restored history trigger did not reject mutation")
    finally:
        await owner.dispose()


async def main() -> None:
    action, name = sys.argv[1:]
    settings = settings_for(name)
    if action == "prepare":
        create_database(settings)
    elif action == "seed":
        await seed(settings)
    elif action == "allow_probe":
        allow_probe(settings)
    elif action == "permissions":
        await permissions(settings)
    elif action == "recover":
        print(json.dumps(await recover(settings), sort_keys=True))
        return
    elif action not in {"inspect", "inspect_empty"}:
        raise ValueError("unknown restore fixture action")
    result = {"live_execution_available": False, **database_snapshot(settings)}
    if action in {"inspect", "seed"}:
        result["economics"] = await economics(settings)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("restore fixture failed: " + type(exc).__name__, file=sys.stderr)
        for frame in traceback.extract_tb(exc.__traceback__):
            print(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}", file=sys.stderr)
        raise SystemExit(1) from None
