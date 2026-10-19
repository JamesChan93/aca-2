"""Immutable decision evidence linked to exactly one paper reserve event."""

from uuid import UUID

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aca.domain.trading import fingerprint
from aca.domain.values import utc_datetime
from aca.portfolio.reserves import TaxReserveRecord
from aca.portfolio.state import free_cash, reserved_cash

MAX_RESERVES = 10_000
reserves = Table(
    "paper_tax_reserves",
    MetaData(schema="aca"),
    Column("account_id", Uuid, primary_key=True),
    Column("request_id", Uuid, primary_key=True),
    Column("event_version", Integer, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("amount_gbp", Numeric(38, 18), nullable=False),
    Column("previous_gbp", Numeric(38, 18), nullable=False),
    Column("document", JSONB, nullable=False),
    Column("fingerprint", String(64), nullable=False),
)


def restore_record(row: RowMapping) -> TaxReserveRecord:
    record = TaxReserveRecord.restore(row["document"])
    if (
        record.account_id != row["account_id"]
        or record.request_id != row["request_id"]
        or record.event_version != row["event_version"]
        or record.recorded_at != row["recorded_at"]
        or record.amount_gbp != row["amount_gbp"]
        or record.previous_gbp != row["previous_gbp"]
        or fingerprint(row["document"]) != row["fingerprint"]
    ):
        raise ValueError("tax cash reserve evidence is invalid")
    return record


async def for_event(c: AsyncConnection, account: UUID, version: int) -> TaxReserveRecord:
    from aca.storage.paper import events

    row = (
        (
            await c.execute(
                select(reserves).where(
                    reserves.c.account_id == account, reserves.c.event_version == version
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("tax cash reserve decision is missing")
    record = restore_record(row)
    event = (
        (
            await c.execute(
                select(events).where(events.c.account_id == account, events.c.version == version)
            )
        )
        .mappings()
        .one()
    )
    document = event["document"]
    changes = document.get("changes", {})
    if (
        event["kind"] != "tax_reserve_set"
        or event["source"] != f"tax-reserve:{record.request_id}"
        or event["at"] != record.recorded_at
        or document.get("format") != "paper_delta_v1"
        or document.get("journal_id") is not None
        or not isinstance(changes, dict)
        or set(changes) - {"version", "as_of", "tax_reserve_gbp"}
        or changes.get("tax_reserve_gbp") != record.document()["amount_gbp"]
        or fingerprint(
            {
                "previous": event["previous_hash"],
                "version": version,
                "kind": event["kind"],
                "source": event["source"],
                "state": document,
            }
        )
        != event["fingerprint"]
    ):
        raise ValueError("tax cash reserve event differs from its decision")
    return record


async def insert_record(c: AsyncConnection, record: TaxReserveRecord) -> None:
    document = record.document()
    await c.execute(
        reserves.insert().values(
            account_id=record.account_id,
            request_id=record.request_id,
            event_version=record.event_version,
            recorded_at=record.recorded_at,
            amount_gbp=record.amount_gbp,
            previous_gbp=record.previous_gbp,
            document=document,
            fingerprint=fingerprint(document),
        )
    )


async def read_reserves(
    engine: AsyncEngine, account: UUID, *, after_version: int = 0, limit: int = 20
) -> dict[str, object]:
    from aca.storage.paper import PaperStore

    if (
        type(after_version) is not int
        or after_version < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("tax cash reserve query exceeds budget")
    async with engine.connect() as c:
        await c.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        state = await PaperStore(engine, account).load(c)
        if state is None:
            raise ValueError("paper account not initialised")
        rows = (
            (
                await c.execute(
                    select(reserves)
                    .where(
                        reserves.c.account_id == account, reserves.c.event_version > after_version
                    )
                    .order_by(reserves.c.event_version)
                    .limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        items = [
            (await for_event(c, account, row["event_version"])).document() for row in rows[:limit]
        ]
        as_of = utc_datetime(await c.scalar(select(func.clock_timestamp())))
    return {
        "schema_version": 1,
        "mode": "paper",
        "account_id": str(account),
        "as_of": as_of.isoformat(),
        "state_version": state.version,
        "manual_tax_cash_reserve_gbp": str(state.tax_reserve_gbp),
        "cash_gbp": str(state.quantity("GBP")),
        "order_reserved_cash_gbp": str(reserved_cash(state)),
        "free_cash_gbp": str(free_cash(state)),
        "items": items,
        "next_after_version": rows[limit - 1]["event_version"] if len(rows) > limit else None,
        "tax_estimate": False,
        "tax_report_final": False,
        "promotion_evidence": False,
        "live_execution_available": False,
    }
