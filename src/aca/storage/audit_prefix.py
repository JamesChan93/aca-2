"""Operator-sealed financial audits. Runtime has SELECT permission only.

These records attest to a complete audit of retained, append-only SQL evidence.
They are neither backups nor authority to remove records or activate trading.
"""

import hashlib
import importlib.metadata
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Table,
    Uuid,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

import aca
from aca.domain.trading import PortfolioSnapshot, Record, Time, fingerprint
from aca.operations.config import RiskPolicy, Settings
from aca.storage.database import create_engine, verify_runtime
from aca.storage.paper import PaperStore, checkpoints, events, lock_key, metadata
from aca.storage.paper_projection import state_document

prefixes = Table(
    "paper_financial_audits",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("account_id", Uuid, nullable=False),
    Column("version", Integer, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["account_id", "version"], ["aca.paper_events.account_id", "aca.paper_events.version"]
    ),
)

MAX_AUDIT_SUFFIX_EVENTS = 100000


def package_fingerprint() -> str:
    """Bind reuse to exact installed Python bytes, independently of image tags."""
    package = Path(aca.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def policy_fingerprint() -> str:
    return fingerprint(RiskPolicy().model_dump(mode="json"))


def environment_fingerprint() -> str:
    """Financial decoding/SQL semantics must not change under the same source."""
    return fingerprint(
        {
            "python": platform.python_version(),
            "implementation": sys.implementation.name,
            "financial_dependencies": {
                name: importlib.metadata.version(name)
                for name in ("pydantic", "pydantic-core", "SQLAlchemy", "psycopg", "psycopg-binary")
            },
        }
    )


class FinancialAuditPrefix(Record):
    audit_version: Literal["paper-financial-prefix-v1"] = "paper-financial-prefix-v1"
    id: UUID
    account_id: UUID
    version: int = Field(ge=1)
    event_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: PortfolioSnapshot
    checkpoint_count: int = Field(ge=0)
    recorded_at: Time
    actor: UUID
    reason: str = Field(min_length=10, max_length=240)
    application_version: str
    package_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    predecessor_history: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    historical_economics_audited: Literal[False] = False
    pruning_authorised: Literal[False] = False
    live_execution_available: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.state.account_id != self.account_id
            or self.state.version != self.version
            or fingerprint(state_document(self.state)) != self.state_hash
        ):
            raise ValueError("financial audit prefix state mismatch")
        return self


async def capture_prefix(
    c: AsyncConnection,
    state: PortfolioSnapshot,
    actor: UUID,
    reason: str,
    *,
    previous_history: UUID | None = None,
) -> FinancialAuditPrefix:
    """Capture already audited state in the same locked read snapshot.

    This constructs a record only. Persisting it requires the separate migration
    authority. Both callers first obtain state through a complete mandatory audit;
    compaction publishes this record atomically with its historical certificate.
    """
    event_hash = await c.scalar(
        select(events.c.fingerprint).where(
            events.c.account_id == state.account_id, events.c.version == state.version
        )
    )
    count = await c.scalar(
        select(func.count())
        .select_from(checkpoints)
        .where(
            checkpoints.c.account_id == state.account_id,
            checkpoints.c.version <= state.version,
        )
    )
    if not isinstance(event_hash, str) or not isinstance(count, int):
        raise ValueError("financial audit evidence is incomplete")
    record = FinancialAuditPrefix(
        id=uuid4(),
        account_id=state.account_id,
        version=state.version,
        event_hash=event_hash,
        state_hash=fingerprint(state_document(state)),
        state=state,
        checkpoint_count=count,
        recorded_at=datetime.now(UTC),
        predecessor_history=previous_history,
        actor=actor,
        reason=reason,
        application_version=aca.__version__,
        package_sha256=package_fingerprint(),
        policy_hash=policy_fingerprint(),
        environment_hash=environment_fingerprint(),
    )
    return record


async def read_prefix(c: AsyncConnection, store: PaperStore) -> FinancialAuditPrefix:
    if not await c.scalar(
        text(
            "SELECT current_user = 'aca_runtime' AND NOT "
            "has_any_column_privilege('aca.paper_financial_audits', 'INSERT')"
        )
    ):
        raise ValueError("financial audit prefix reader must have no acceptance authority")
    row = (
        (await c.execute(select(prefixes).where(prefixes.c.id == store.audit_prefix)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("required financial audit prefix is missing")
    record = FinancialAuditPrefix.restore(row["document"])
    if (
        fingerprint(record.document()) != row["fingerprint"]
        or record.id != row["id"]
        or record.account_id != row["account_id"]
        or record.account_id != store.account_id
        or record.version != row["version"]
        or record.recorded_at != row["recorded_at"]
        or record.application_version != aca.__version__
        or record.package_sha256 != package_fingerprint()
        or record.policy_hash != policy_fingerprint()
        or record.environment_hash != environment_fingerprint()
    ):
        raise ValueError("financial audit prefix identity/code/policy mismatch")
    anchor = (
        (
            await c.execute(
                select(events).where(
                    events.c.account_id == store.account_id, events.c.version == record.version
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        anchor is None
        or anchor["fingerprint"] != record.event_hash
        or anchor["at"] != record.state.as_of
        or (
            anchor["document"].get("state_hash")
            if "format" in anchor["document"]
            # Legacy full snapshots predate added default fields. Compare their
            # typed meaning while the envelope below still hashes exact raw bytes.
            else fingerprint(PortfolioSnapshot.restore(anchor["document"]).document())
        )
        != record.state_hash
        or fingerprint(
            {
                "previous": anchor["previous_hash"],
                "version": anchor["version"],
                "kind": anchor["kind"],
                "source": anchor["source"],
                "state": anchor["document"],
            }
        )
        != record.event_hash
    ):
        raise ValueError("financial audit prefix event anchor mismatch")
    count = await c.scalar(
        select(func.count())
        .select_from(checkpoints)
        .where(
            checkpoints.c.account_id == store.account_id,
            checkpoints.c.version <= record.version,
        )
    )
    if count != record.checkpoint_count:
        raise ValueError("financial audit prefix checkpoint set changed")
    # No pruning is supported by this contract. Detect a partial restore even
    # when its final anchor survived. Runtime cannot replace or delete members
    # of this immutable set; SQL counts avoid decoding its documents in Python.
    count = await c.scalar(
        select(func.count())
        .select_from(events)
        .where(
            events.c.account_id == store.account_id,
            events.c.version <= record.version,
        )
    )
    from aca.storage.history_blocks import blocks

    archived = (
        (
            await c.execute(
                select(blocks.c.first, blocks.c.last, blocks.c.count, blocks.c.document).where(
                    blocks.c.account_id == store.account_id,
                    blocks.c.kind == "financial",
                    blocks.c.first <= record.version,
                )
            )
        )
        .mappings()
        .all()
    )
    count = (count or 0) + sum(
        row["count"]
        if row["last"] <= record.version
        else sum(v <= record.version for v in row["document"]["versions"])
        for row in archived
    )
    if count != record.version:
        raise ValueError("financial audit prefix retained event set is incomplete")
    return record


async def accept_prefix(
    settings: Settings,
    account_id: UUID,
    actor: UUID,
    reason: str,
    expected_version: int,
    *,
    previous_history: UUID | None = None,
) -> dict[str, object]:
    """Explicit operator operation, using separately supplied migration credentials.

    The full audit runs through a read-only runtime connection. The migration
    connection holds the same advisory lock as a writer until its insert commits.
    Append-only evidence plus that lock lets the audit result cross transactions;
    no exchange request, projection update or permission change occurs.
    """
    if not 10 <= len(reason) <= 240:
        raise ValueError("financial audit reason must be 10..240 characters")
    if type(expected_version) is not int or expected_version < 1:
        raise ValueError("financial audit requires a positive expected version")
    runtime = create_engine(settings)
    try:
        authority = create_engine(settings, "migration")
    except BaseException:
        await runtime.dispose()
        raise
    store = PaperStore(runtime, account_id, history_prefix=previous_history)
    try:
        package_hash = package_fingerprint()
        environment_hash = environment_fingerprint()
        await verify_runtime(runtime)
        async with authority.connect() as admin:
            # A session lock is intentionally retained across the read-only audit.
            try:
                if await admin.scalar(text("SELECT current_user")) != "aca_migrator":
                    raise ValueError("financial audit acceptance requires the migration role")
                if not await admin.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key(account_id)}
                ):
                    raise ValueError("stop the paper writer before accepting a financial audit")
                await admin.commit()
                async with runtime.connect() as c:
                    await c.execution_options(
                        isolation_level="REPEATABLE READ", postgresql_readonly=True
                    )
                    state = await store.audit(c, full=previous_history is None)
                    if state is None:
                        raise ValueError("financial audit requires an initialised account")
                    if state.version != expected_version:
                        raise ValueError(
                            "financial audit expected version differs from current head"
                        )
                    record = await capture_prefix(
                        c, state, actor, reason, previous_history=previous_history
                    )
                    if (
                        package_fingerprint() != package_hash
                        or record.environment_hash != environment_hash
                    ):
                        raise ValueError(
                            "financial audit code/environment changed during verification"
                        )
                async with admin.begin():
                    await admin.execute(
                        prefixes.insert().values(
                            id=record.id,
                            account_id=account_id,
                            version=record.version,
                            recorded_at=record.recorded_at,
                            fingerprint=fingerprint(record.document()),
                            document=record.document(),
                        )
                    )
                return {**record.document(), "fingerprint": fingerprint(record.document())}
            finally:
                # Do not return a pooled connection with a session lock attached,
                # including on cancellation or an interrupted/failed audit.
                await admin.invalidate()
    finally:
        await runtime.dispose()
        await authority.dispose()
