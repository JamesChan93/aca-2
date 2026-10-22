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
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Uuid,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aca.accounting.ledger import apply_journals
from aca.domain.trading import ZERO, Balance, Journal, PortfolioSnapshot, fingerprint
from aca.portfolio.reserves import TaxReserveRecord, validate_reserve_transition
from aca.storage.archive_format import ArchiveReference, FileIdentity
from aca.storage.paper_projection import (
    CHECKPOINT_INTERVAL,
    delta,
    journal_links,
    restore_event,
    state_document,
)
from aca.storage.reserves import for_event, insert_record

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
checkpoints = Table(
    "paper_checkpoints",
    metadata,
    Column("account_id", Uuid, primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("event_hash", String(64), nullable=False),
    Column("state_hash", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["account_id", "version"], ["aca.paper_events.account_id", "aca.paper_events.version"]
    ),
)


def lock_key(account_id: UUID) -> int:
    return int.from_bytes(
        hashlib.sha256(b"aca-paper-v1" + account_id.bytes).digest()[:8], signed=True
    )


class WriterUnavailable(RuntimeError):
    """Writer ownership or its connection has not been demonstrated."""


class PaperStore:
    def __init__(
        self,
        engine: AsyncEngine,
        account_id: UUID,
        *,
        replay_archive: ArchiveReference | None = None,
        audit_prefix: UUID | None = None,
    ) -> None:
        self.engine, self.account_id = engine, account_id
        self.replay_archive = replay_archive
        self.audit_prefix = audit_prefix
        self.archive_identity: FileIdentity | None = None

    def _restore_row(
        self,
        row: RowMapping,
        previous: str,
        number: int,
        state: PortfolioSnapshot | None,
    ) -> tuple[PortfolioSnapshot, str]:
        doc = row["document"]
        if not isinstance(doc, dict):
            raise ValueError("invalid paper event document")
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
        restored = restore_event(state, doc)
        if (
            restored.account_id != self.account_id
            or restored.version != number
            or restored.as_of != row["at"]
            or (state is not None and restored.as_of < state.as_of)
        ):
            raise ValueError("paper state/account version/time mismatch")
        if row["kind"] == "tax_reserve_set":
            if doc.get("format") != "paper_delta_v1":
                raise ValueError("tax cash decision requires a versioned delta")
        elif restored.tax_reserve_gbp != (state.tax_reserve_gbp if state else ZERO):
            raise ValueError("tax cash reserve changed without a decision")
        return restored, expected

    async def ledger_balances(self, connection: AsyncConnection) -> tuple[Balance, ...]:
        """Independent SQL aggregation of sealed journal quantities and costs."""
        quantities = dict(
            (
                await connection.execute(
                    select(movements.c.asset, func.sum(movements.c.quantity))
                    .join(journals)
                    .where(journals.c.account_id == self.account_id)
                    .group_by(movements.c.asset)
                )
            ).all()
        )
        costs = dict(
            (
                await connection.execute(
                    select(lines.c.account, func.sum(lines.c.gbp))
                    .join(journals)
                    .where(journals.c.account_id == self.account_id)
                    .group_by(lines.c.account)
                )
            ).all()
        )
        return tuple(
            Balance(
                asset=a, quantity=quantities.get(a, ZERO), cost_gbp=costs.get(f"asset:{a}", ZERO)
            )
            for a in ("GBP", "BTC", "ETH")
        )

    async def audit(
        self, connection: AsyncConnection, *, full: bool = False
    ) -> PortfolioSnapshot | None:
        """Audit all history, or a protected operator-audited prefix and its suffix.

        An explicitly required prefix must validate; it never silently falls back
        to a different authority. ``full=True`` always reconstructs every event.
        Journal balance reconciliation remains independent in both paths.
        """
        previous = "0" * 64
        state: PortfolioSnapshot | None = None
        cursor = 0
        if self.audit_prefix is not None and not full:
            from aca.storage.audit_prefix import read_prefix

            accepted = await read_prefix(connection, self)
            state, previous, cursor = accepted.state, accepted.event_hash, accepted.version
        prefix_version = cursor
        # Pin a high water so a separate read service cannot accidentally audit
        # a mixture of prefixes while the owner appends new records.
        head = await connection.scalar(
            select(func.max(events.c.version)).where(events.c.account_id == self.account_id)
        )
        if prefix_version > (head or 0):
            raise ValueError("financial audit prefix is ahead of retained history")
        if prefix_version:
            from aca.storage.audit_prefix import MAX_AUDIT_SUFFIX_EVENTS

            if (head or 0) - prefix_version > MAX_AUDIT_SUFFIX_EVENTS:
                raise ValueError("financial audit suffix exceeds budget; renew the operator audit")
        while cursor < (head or 0):
            rows = (
                (
                    await connection.execute(
                        select(events)
                        .where(
                            events.c.account_id == self.account_id,
                            events.c.version > cursor,
                            events.c.version <= head,
                        )
                        .order_by(events.c.version)
                        .limit(4 * CHECKPOINT_INTERVAL)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                raise ValueError("paper audit prefix is incomplete")
            for row in rows:
                before = state
                state, previous = self._restore_row(row, previous, cursor + 1, before)
                if row["document"].get("format") in {"paper_delta_v1", "paper_delta_v2"}:
                    await self._verify_increment(connection, before, state, row)
                cursor += 1
        if state is not None:
            if await self.ledger_balances(connection) != state.balances:
                raise ValueError("journal and portfolio projection differ")
            # Verify *all* checkpoint anchors, including ones superseded by a
            # newer checkpoint. The fast path relies on this immutable prefix.
            checkpoint_cursor = prefix_version
            while True:
                checkpoint_rows = (
                    (
                        await connection.execute(
                            select(checkpoints)
                            .where(
                                checkpoints.c.account_id == self.account_id,
                                checkpoints.c.version > checkpoint_cursor,
                            )
                            .order_by(checkpoints.c.version)
                            .limit(16)
                        )
                    )
                    .mappings()
                    .all()
                )
                if not checkpoint_rows:
                    break
                anchors = {
                    row["version"]: row
                    for row in (
                        (
                            await connection.execute(
                                select(events).where(
                                    events.c.account_id == self.account_id,
                                    events.c.version.in_(
                                        [row["version"] for row in checkpoint_rows]
                                    ),
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                }
                for checkpoint_row in checkpoint_rows:
                    anchor = anchors.get(checkpoint_row["version"])
                    if anchor is None:
                        raise ValueError("paper checkpoint anchor missing")
                    self._restore_checkpoint(checkpoint_row, anchor)
                    checkpoint_cursor = checkpoint_row["version"]
        return state

    async def _verify_increment(
        self,
        connection: AsyncConnection,
        old: PortfolioSnapshot | None,
        state: PortfolioSnapshot,
        row: RowMapping,
    ) -> None:
        document = row["document"]
        history: list[Journal] = []
        for ident in journal_links(document):
            doc = await connection.scalar(
                select(journals.c.document).where(
                    journals.c.account_id == self.account_id, journals.c.id == ident
                )
            )
            if doc is None:
                raise ValueError("paper event journal missing")
            journal = Journal.restore(doc)
            if journal.occurred_at > state.as_of:
                raise ValueError("paper journal occurs after its event")
            history.append(journal)
        # Both snapshots have just passed typed restoration. An empty journal
        # batch cannot change canonical balances; reuse that exact tuple rather
        # than reconstructing and revalidating the same six Decimal fields for
        # every maintenance observation. Noncanonical legacy prefixes still use
        # the original reconstruction, including its GBP quantity/cost check.
        if (
            not history
            and old is not None
            and tuple(balance.asset for balance in old.balances) == ("GBP", "BTC", "ETH")
            and old.quantity("GBP") == old.cost("GBP")
        ):
            expected_balances = old.balances
        else:
            expected_balances = apply_journals(old.balances if old else (), history)
        if state.balances != expected_balances:
            raise ValueError("journal and incremental projection differ")
        from aca.storage.accounting import verify_operation

        await verify_operation(connection, self.account_id, old, state, row["kind"], tuple(history))
        if row["kind"] == "tax_reserve_set":
            record = await for_event(connection, self.account_id, state.version)
            validate_reserve_transition(old, state, record)
        elif state.tax_reserve_gbp != (old.tax_reserve_gbp if old else ZERO):
            raise ValueError("tax cash reserve changed without a decision")

    async def _checkpoint(self, connection: AsyncConnection, row: RowMapping) -> PortfolioSnapshot:
        anchor = (
            (
                await connection.execute(
                    select(events).where(
                        events.c.account_id == self.account_id, events.c.version == row["version"]
                    )
                )
            )
            .mappings()
            .one()
        )
        return self._restore_checkpoint(row, anchor)

    def _restore_checkpoint(self, row: RowMapping, anchor: RowMapping) -> PortfolioSnapshot:
        doc = row["document"]
        if not isinstance(doc, dict) or fingerprint(doc) != row["state_hash"]:
            raise ValueError("corrupt paper checkpoint")
        state = PortfolioSnapshot.restore(doc)
        state_document(state)
        envelope = anchor["document"]
        if (
            state.account_id != self.account_id
            or state.version != row["version"]
            or anchor["fingerprint"] != row["event_hash"]
            or envelope.get("state_hash") != row["state_hash"]
            or state.as_of != anchor["at"]
            or fingerprint(
                {
                    "previous": anchor["previous_hash"],
                    "version": state.version,
                    "kind": anchor["kind"],
                    "source": anchor["source"],
                    "state": envelope,
                }
            )
            != anchor["fingerprint"]
        ):
            raise ValueError("paper checkpoint anchor mismatch")
        return state

    async def load(self, connection: AsyncConnection) -> PortfolioSnapshot | None:
        checkpoint = (
            (
                await connection.execute(
                    select(checkpoints)
                    .where(checkpoints.c.account_id == self.account_id)
                    .order_by(checkpoints.c.version.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if checkpoint is None:
            return await self.audit(connection)  # legacy 0004 prefix, once before first append
        state = await self._checkpoint(connection, checkpoint)
        previous = str(checkpoint["event_hash"])
        rows = (
            (
                await connection.execute(
                    select(events)
                    .where(events.c.account_id == self.account_id, events.c.version > state.version)
                    .order_by(events.c.version)
                    .limit(CHECKPOINT_INTERVAL)
                )
            )
            .mappings()
            .all()
        )
        if len(rows) >= CHECKPOINT_INTERVAL:
            raise ValueError("paper checkpoint tail exceeds replay budget")
        for row in rows:
            old = state
            state, previous = self._restore_row(row, previous, old.version + 1, old)
            await self._verify_increment(connection, old, state, row)
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
        tax_reserve: TaxReserveRecord | None = None,
        additional_journals: tuple[Journal, ...] = (),
    ) -> None:
        # Call only inside the owning writer transaction. Unique versions/source
        # keys give a second barrier against duplicate stale transitions.
        expected_version = 1 if previous is None else previous.version + 1
        if state.version != expected_version or state.account_id != self.account_id:
            raise ValueError("nonsequential paper state")
        PortfolioSnapshot.restore(state.document())
        batch = ((journal,) if journal else ()) + additional_journals
        expected_balances = apply_journals(previous.balances if previous else (), batch)
        if state.balances != expected_balances:
            raise ValueError("journal and next projection differ")
        from aca.storage.accounting import verify_operation

        await verify_operation(connection, self.account_id, previous, state, kind, batch)
        if kind == "tax_reserve_set":
            if tax_reserve is None or journal is not None:
                raise ValueError("tax cash reserve requires a non-financial decision")
            validate_reserve_transition(previous, state, tax_reserve)
            if source != f"tax-reserve:{tax_reserve.request_id}":
                raise ValueError("tax cash reserve source differs from its decision")
        elif tax_reserve is not None or state.tax_reserve_gbp != (
            previous.tax_reserve_gbp if previous else ZERO
        ):
            raise ValueError("tax cash reserve changed without a decision")
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
        head = (
            await connection.execute(
                select(events.c.version, events.c.fingerprint)
                .where(events.c.account_id == self.account_id)
                .order_by(events.c.version.desc())
                .limit(1)
            )
        ).one_or_none()
        if (head[0] if head else 0) != (previous.version if previous else 0):
            raise ValueError("paper append state is stale")
        prev_hash = head[1] if head else "0" * 64
        event_id = uuid4()
        doc = delta(
            previous,
            state,
            journal.id if journal else None,
            tuple(j.id for j in additional_journals),
        )
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
        if tax_reserve is not None:
            await insert_record(connection, tax_reserve)
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
        for journal in batch:
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
        if state.version % CHECKPOINT_INTERVAL == 0 or not await connection.scalar(
            select(checkpoints.c.version)
            .where(checkpoints.c.account_id == self.account_id)
            .limit(1)
        ):
            await connection.execute(
                checkpoints.insert().values(
                    account_id=self.account_id,
                    version=state.version,
                    event_hash=hashed,
                    state_hash=fingerprint(state.document()),
                    document=state.document(),
                )
            )

    async def status(self) -> dict[str, object]:
        async with self.engine.connect() as connection:
            await connection.execution_options(isolation_level="REPEATABLE READ")
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
        self.archive_checked = False
        self.archive_faulted = False

    async def __aenter__(self) -> PaperWriter:
        connection = await self.store.engine.connect()
        try:
            await connection.execution_options(isolation_level="REPEATABLE READ")
            if not await connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key(self.store.account_id)}
            ):
                raise WriterUnavailable("another paper account writer is active")
            self.pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.commit()
            self.connection = connection
            async with self.transaction() as c:
                if self.store.replay_archive is None:
                    await self.store.audit(c)
                else:
                    # File integrity alone cannot authorise commands. Reconstruct
                    # balances/units/flows against the complete financial history
                    # before granting this writer to its caller.
                    from aca.reporting.historical import read_performance

                    read = await read_performance(c, self.store)
                    if read.financial.accounting_invalidated or self.store.archive_identity is None:
                        raise WriterUnavailable(
                            "archive writer requires valid historical accounting"
                        )
            self.archive_checked = self.store.replay_archive is not None
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
        self.archive_checked = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.mutex:
            connection = self.connection
            if connection is None or connection.closed or connection.invalidated:
                raise WriterUnavailable("paper writer connection unavailable")
            if self.archive_faulted:
                raise WriterUnavailable("required replay archive fault is latched for this writer")
            if self.archive_checked:
                reference, identity = self.store.replay_archive, self.store.archive_identity
                if reference is None or identity is None:
                    self.archive_faulted = True
                    raise WriterUnavailable("required replay archive has not been verified")
                try:
                    reference.check_identity(identity)
                except ValueError, OSError:
                    self.archive_faulted = True
                    raise
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
