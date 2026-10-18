"""Append-only paper state, journals, independent venue evidence and alert outbox."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Uuid,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aca.accounting.ledger import balances
from aca.domain.trading import Journal, PortfolioSnapshot, fingerprint

metadata = MetaData(schema="aca")
accounts = Table(
    "paper_accounts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("mode", String(10), nullable=False),
)
events = Table(
    "paper_events",
    metadata,
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("id", Uuid, nullable=False, unique=True),
    Column("source", String(160), nullable=False),
    Column("kind", String(60), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("previous_hash", String(64), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
)
journals = Table(
    "paper_journals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), nullable=False),
    Column("source", String(160), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("document", JSONB, nullable=False),
)
lines = Table(
    "paper_journal_lines",
    metadata,
    Column("journal_id", Uuid, ForeignKey("aca.paper_journals.id"), primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("account", String(40), nullable=False),
    Column("gbp", Numeric(38, 18), nullable=False),
)
movements = Table(
    "paper_movements",
    metadata,
    Column("journal_id", Uuid, ForeignKey("aca.paper_journals.id"), primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("asset", String(3), nullable=False),
    Column("quantity", Numeric(38, 18), nullable=False),
)
venue_events = Table(
    "paper_venue_events",
    metadata,
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("source", String(160), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("document", JSONB, nullable=False),
)
outbox = Table(
    "paper_alerts",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), nullable=False),
    Column("event_id", Uuid, ForeignKey("aca.paper_events.id"), nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("category", String(60), nullable=False),
    Column("document", JSONB, nullable=False),
)
fills = Table(
    "paper_fills",
    metadata,
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), primary_key=True),
    Column("id", String(100), primary_key=True),
    Column("journal_id", Uuid, ForeignKey("aca.paper_journals.id"), nullable=False, unique=True),
    Column("document", JSONB, nullable=False),
)
order_identities = Table(
    "paper_order_identities",
    metadata,
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), primary_key=True),
    Column("client_id", Uuid, primary_key=True),
    Column("document", JSONB, nullable=False),
)


def lock_key(account_id: UUID) -> int:
    return int.from_bytes(
        hashlib.sha256(b"aca-paper-v1" + account_id.bytes).digest()[:8], signed=True
    )


class WriterUnavailable(RuntimeError):
    """Writer ownership or its connection has not been demonstrated."""


class PaperStore:
    def __init__(self, engine: AsyncEngine, account_id: UUID) -> None:
        self.engine, self.account_id = engine, account_id

    async def load(self, connection: AsyncConnection) -> PortfolioSnapshot | None:
        rows = (
            (
                await connection.execute(
                    select(events)
                    .where(events.c.account_id == self.account_id)
                    .order_by(events.c.version)
                )
            )
            .mappings()
            .all()
        )
        previous = "0" * 64
        state: PortfolioSnapshot | None = None
        for number, row in enumerate(rows, 1):
            doc = row["document"]
            expected = fingerprint(
                {
                    "previous": previous,
                    "version": number,
                    "kind": row["kind"],
                    "source": row["source"],
                    "state": doc,
                }
            )
            if (
                row["version"] != number
                or row["previous_hash"] != previous
                or expected != row["fingerprint"]
            ):
                raise ValueError("corrupt paper event chain")
            state = PortfolioSnapshot.restore(doc)
            if state.account_id != self.account_id or state.version != number:
                raise ValueError("paper state/account version mismatch")
            previous = expected
        if state is not None:
            rebuilt = balances(await self.history(connection))
            if rebuilt != state.balances and (rebuilt or state.balances):
                # Empty journal history reconstructs the three zero balances.
                if any(b.quantity or b.cost_gbp for b in rebuilt) or state.balances:
                    raise ValueError("journal and portfolio projection differ")
        return state

    async def history(self, connection: AsyncConnection) -> tuple[Journal, ...]:
        docs = (
            (
                await connection.execute(
                    select(journals.c.document)
                    .where(journals.c.account_id == self.account_id)
                    .order_by(journals.c.occurred_at, journals.c.id)
                )
            )
            .scalars()
            .all()
        )
        return tuple(Journal.restore(doc) for doc in docs)

    async def append(
        self,
        connection: AsyncConnection,
        previous: PortfolioSnapshot | None,
        state: PortfolioSnapshot,
        *,
        source: str,
        kind: str,
        journal: Journal | None = None,
        alert: bool = False,
    ) -> None:
        # Call only inside the owning writer transaction. Unique versions/source
        # keys give a second barrier against duplicate stale transitions.
        expected_version = 1 if previous is None else previous.version + 1
        if state.version != expected_version or state.account_id != self.account_id:
            raise ValueError("nonsequential paper state")
        PortfolioSnapshot.restore(state.document())
        if previous is not None:
            if state.as_of < previous.as_of:
                raise ValueError("paper event time regressed")
            from aca.execution.lifecycle import validate_transition

            for old_order in previous.orders:
                validate_transition(old_order, state.order(old_order.intent.client_id))
            if previous.kill.policy_epoch == state.kill.policy_epoch:
                if (previous.kill.hard and not state.kill.hard) or (
                    previous.kill.soft and not state.kill.soft
                ):
                    raise ValueError("risk latch cannot clear in the same epoch")
            elif kind != "manual_epoch_review":
                raise ValueError("policy epoch requires an audited manual review")
            if state.units != previous.units and journal is None:
                raise ValueError("investment units changed without a journal")
        if previous is None:
            await connection.execute(
                accounts.insert().values(id=self.account_id, created_at=state.as_of, mode="paper")
            )
        prev_hash = (
            await connection.scalar(
                select(events.c.fingerprint)
                .where(events.c.account_id == self.account_id)
                .order_by(events.c.version.desc())
                .limit(1)
            )
            or "0" * 64
        )
        event_id = uuid4()
        doc = state.document()
        hashed = fingerprint(
            {
                "previous": prev_hash,
                "version": state.version,
                "kind": kind,
                "source": source,
                "state": doc,
            }
        )
        await connection.execute(
            events.insert().values(
                account_id=self.account_id,
                version=state.version,
                id=event_id,
                source=source,
                kind=kind,
                at=state.as_of,
                previous_hash=prev_hash,
                fingerprint=hashed,
                document=doc,
            )
        )
        old_ids = {o.intent.client_id for o in previous.orders} if previous else set()
        for order in state.orders:
            if order.intent.client_id not in old_ids:
                await connection.execute(
                    order_identities.insert().values(
                        account_id=self.account_id,
                        client_id=order.intent.client_id,
                        document=order.intent.document(),
                    )
                )
        if journal is not None:
            await connection.execute(
                journals.insert().values(
                    id=journal.id,
                    account_id=self.account_id,
                    source=journal.source,
                    occurred_at=journal.occurred_at,
                    document=journal.document(),
                )
            )
            await connection.execute(
                lines.insert(),
                [
                    dict(journal_id=journal.id, number=n, account=line.account, gbp=line.gbp)
                    for n, line in enumerate(journal.lines)
                ],
            )
            if journal.movements:
                await connection.execute(
                    movements.insert(),
                    [
                        dict(journal_id=journal.id, number=n, asset=m.asset, quantity=m.quantity)
                        for n, m in enumerate(journal.movements)
                    ],
                )
        if alert:
            await connection.execute(
                outbox.insert().values(
                    id=uuid4(),
                    account_id=self.account_id,
                    event_id=event_id,
                    at=state.as_of,
                    category=kind,
                    document={
                        "schema_version": 1,
                        "mode": "paper",
                        "version": state.version,
                        "account_id": str(self.account_id),
                        "category": kind,
                        "incident": state.incident,
                        "hard_halt": state.kill.hard,
                        "live_execution_available": False,
                    },
                )
            )

    async def status(self) -> dict[str, object]:
        async with self.engine.connect() as connection:
            state = await self.load(connection)
        return {
            "schema_version": 1,
            "mode": "paper",
            "live_execution_available": False,
            "state": state.document() if state else None,
        }


class PaperWriter:
    """One dedicated session; an invalidated connection never silently rearms."""

    def __init__(self, store: PaperStore) -> None:
        self.store = store
        self.connection: AsyncConnection | None = None
        self.pid: int | None = None
        self.mutex = asyncio.Lock()

    async def __aenter__(self) -> PaperWriter:
        connection = await self.store.engine.connect()
        try:
            if not await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key(self.store.account_id)}
            ):
                raise WriterUnavailable("another paper account writer is active")
            self.pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.commit()
            self.connection = connection
            return self
        except BaseException:
            await connection.invalidate()
            await connection.close()
            raise

    async def __aexit__(self, *args: object) -> None:
        if self.connection is not None:
            # Physically discard the session; pooled ROLLBACK alone does not
            # release session advisory locks.
            await self.connection.invalidate()
            await self.connection.close()
        self.connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.mutex:
            connection = self.connection
            if connection is None or connection.closed or connection.invalidated:
                raise WriterUnavailable("paper writer connection unavailable")
            async with connection.begin():
                if await connection.scalar(text("SELECT pg_backend_pid()")) != self.pid:
                    raise WriterUnavailable("paper writer connection changed")
                yield connection


async def read_alerts(store: PaperStore, limit: int = 20) -> list[dict[str, object]]:
    if not 1 <= limit <= 100:
        raise ValueError("alert query exceeds budget")
    async with store.engine.connect() as connection:
        docs = (
            (
                await connection.execute(
                    select(outbox.c.document)
                    .where(outbox.c.account_id == store.account_id)
                    .order_by(outbox.c.at.desc(), outbox.c.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return list(docs)
