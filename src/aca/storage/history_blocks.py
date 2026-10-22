"""Lossless compressed SQL segments. No credentials, files or executable payloads.

Only the separate operator can insert blocks or move rows. Complete audits merge
these original rows with retained anchors; online report reuse requires a separate
semantic certificate. A block by itself never confers economic authority.
"""

import asyncio
import hashlib
import json
import zlib
from collections.abc import AsyncGenerator, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    String,
    Table,
    Text,
    Uuid,
    cast,
    func,
    literal,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from aca.domain.trading import fingerprint
from aca.storage.archive_format import MAX_SEGMENT_BYTES, object_from_bytes
from aca.storage.paper import events, metadata, venue_events
from aca.storage.replay import cursors, public_events, valuations

blocks = Table(
    "paper_history_blocks",
    metadata,
    Column("account_id", Uuid, primary_key=True),
    Column("kind", String(80), primary_key=True),
    Column("first", BigInteger, primary_key=True),
    Column("last", BigInteger, nullable=False),
    Column("count", Integer, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("document_digest", String(64), nullable=False),
    Column("first_at", DateTime(timezone=True)),
    Column("last_at", DateTime(timezone=True)),
    Column("payload", LargeBinary, nullable=False),
    Column("document", JSONB, nullable=False),
)
certificates = Table(
    "paper_history_certificates",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("number", BigInteger, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
)
TABLES = {
    "financial": (events, "version"),
    "valuation": (valuations, "number"),
    "cursor": (cursors, "number"),
    "source": (public_events, "number"),
    "venue": (venue_events, "number"),
}
MAX_BLOCKS = 100000
BLOCK_ROWS = 256


async def has_blocks(c: AsyncConnection, account: UUID) -> bool:
    # Legacy upgrade audits run before this optional storage representation exists.
    if await c.scalar(text("SELECT to_regclass('aca.paper_history_blocks')")) is None:
        return False
    return (
        await c.scalar(select(blocks.c.first).where(blocks.c.account_id == account).limit(1))
        is not None
    )


def encode_rows(rows: list[dict[str, Any]]) -> bytes:
    def default(value: Any) -> str:
        if isinstance(value, (UUID, Decimal)):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        raise ValueError("unsupported history row type")

    data = (
        json.dumps(
            {"rows": rows}, sort_keys=True, separators=(",", ":"), default=default, allow_nan=False
        )
        + "\n"
    ).encode()
    if len(data) > MAX_SEGMENT_BYTES or not 1 <= len(rows) <= BLOCK_ROWS:
        raise ValueError("history block decoded budget exceeded")
    return zlib.compress(data, 9)


def decode_rows(row: Mapping[Any, Any]) -> list[dict[str, Any]]:
    payload = bytes(row["payload"])
    if hashlib.sha256(payload).hexdigest() != row["digest"]:
        raise ValueError("history block payload digest mismatch")
    decoder = zlib.decompressobj()
    data = decoder.decompress(payload, MAX_SEGMENT_BYTES + 1)
    if (
        len(data) > MAX_SEGMENT_BYTES
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("history block compression boundary mismatch")
    document = object_from_bytes(data)
    values = document.get("rows")
    if not isinstance(values, list) or len(values) != row["count"]:
        raise ValueError("history block row count mismatch")
    key = TABLES[row["kind"]][1]
    numbers = [value[key] for value in values]
    if numbers != sorted(set(numbers)) or numbers[0] != row["first"] or numbers[-1] != row["last"]:
        raise ValueError("history block key range mismatch")
    for value in values:
        if "account_id" in value:
            value["account_id"] = UUID(value["account_id"])
        if "id" in value:
            value["id"] = UUID(value["id"])
        if "receipt_id" in value:
            value["receipt_id"] = UUID(value["receipt_id"])
        for name in ("at", "published_at"):
            if name in value:
                value[name] = datetime.fromisoformat(value[name])
    return values


async def sql_rows(
    c: AsyncConnection, account: UUID, kind: str, lower: int = -1, upper: int | None = None
) -> AsyncGenerator[dict[str, Any]]:
    table, key = TABLES[kind]
    while True:
        query = select(table).where(table.c[key] > lower)
        if kind != "source":
            query = query.where(table.c.account_id == account)
        if upper is not None:
            query = query.where(table.c[key] <= upper)
        page_size = 8 if kind == "source" else BLOCK_ROWS
        page = (await c.execute(query.order_by(table.c[key]).limit(page_size))).mappings().all()
        for item in page:
            lower = item[key]
            yield dict(item)
        if len(page) < page_size:
            return


async def archived_rows(
    c: AsyncConnection, account: UUID, kind: str, lower: int = -1, upper: int | None = None
) -> AsyncGenerator[dict[str, Any]]:
    cursor = -1
    while True:
        query = select(blocks).where(
            blocks.c.account_id == account,
            blocks.c.kind == kind,
            blocks.c.first > cursor,
            blocks.c.last > lower,
        )
        if upper is not None:
            query = query.where(blocks.c.first <= upper)
        page = (await c.execute(query.order_by(blocks.c.first).limit(4))).mappings().all()
        for row in page:
            cursor = row["first"]
            for item in decode_rows(row):
                number = item[TABLES[kind][1]]
                if number > lower and (upper is None or number <= upper):
                    yield item
            await asyncio.sleep(0)
        if len(page) < 4:
            return


async def all_rows(
    c: AsyncConnection, account: UUID, kind: str, lower: int = -1, upper: int | None = None
) -> AsyncGenerator[dict[str, Any]]:
    """Merge sorted lossless segments and SQL; retained anchors must agree exactly."""
    archived, retained = (
        archived_rows(c, account, kind, lower, upper),
        sql_rows(c, account, kind, lower, upper),
    )
    try:
        a, b = await anext(archived, None), await anext(retained, None)
        key = TABLES[kind][1]
        while a is not None or b is not None:
            if b is None or (a is not None and a[key] < b[key]):
                yield a  # type: ignore[misc]
                a = await anext(archived, None)
            elif a is None or b[key] < a[key]:
                yield b
                b = await anext(retained, None)
            else:
                if {k: v for k, v in a.items() if k != "raw_receipt"} != b:
                    raise ValueError("retained history anchor differs from its lossless block")
                yield a
                a, b = await anext(archived, None), await anext(retained, None)
    finally:
        await archived.aclose()
        await retained.aclose()


def descriptor(row: Mapping[Any, Any]) -> dict[str, Any]:
    result = {
        name: row[name] for name in ("kind", "first", "last", "count", "digest", "document_digest")
    }
    for name in ("first_at", "last_at"):
        result[name] = row[name].isoformat() if row[name] is not None else None
    return result


async def manifest(c: AsyncConnection, account: UUID) -> tuple[str, int]:
    """Small ordered descriptors, bounded independently of decoded row history."""
    rows = (
        (
            await c.execute(
                select(
                    blocks.c.kind,
                    blocks.c.first,
                    blocks.c.last,
                    blocks.c.count,
                    blocks.c.digest,
                    blocks.c.document_digest,
                    blocks.c.first_at,
                    blocks.c.last_at,
                )
                .where(blocks.c.account_id == account)
                .order_by(blocks.c.kind, blocks.c.first)
                .limit(MAX_BLOCKS + 1)
            )
        )
        .mappings()
        .all()
    )
    if len(rows) > MAX_BLOCKS:
        raise ValueError("history segment catalogue budget exceeded")
    return fingerprint([descriptor(row) for row in rows]), len(rows)


def document_digest(document: Any) -> Any:
    value = literal(document, type_=JSONB) if isinstance(document, dict) else document
    return func.encode(func.sha256(func.convert_to(cast(value, Text), "UTF8")), "hex")
