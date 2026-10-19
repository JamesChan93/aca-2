"""Permanent operational selections and consume-before-action strategy decisions."""

from datetime import datetime, timedelta
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import Field, model_validator
from sqlalchemy import Column, DateTime, ForeignKey, MetaData, String, Table, Uuid, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from aca import __version__
from aca.domain.market import Symbol
from aca.domain.trading import Record, fingerprint
from aca.domain.values import UtcDateTime
from aca.operations.config import RiskPolicy
from aca.storage.paper import PaperWriter
from aca.strategies.slow import StrategySignal, Variant

metadata = MetaData(schema="aca")
selections = Table(
    "paper_strategy_selections",
    metadata,
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), primary_key=True),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
)
decisions = Table(
    "paper_strategy_decisions",
    metadata,
    Column(
        "account_id", Uuid, ForeignKey("aca.paper_strategy_selections.account_id"), primary_key=True
    ),
    Column("watermark", DateTime(timezone=True), primary_key=True),
    Column("client_id", Uuid, nullable=False, unique=True),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
)
results = Table(
    "paper_strategy_results",
    metadata,
    Column(
        "client_id", Uuid, ForeignKey("aca.paper_strategy_decisions.client_id"), primary_key=True
    ),
    Column("document", JSONB, nullable=False),
    Column("fingerprint", String(64), nullable=False),
)


class PaperSelection(Record):
    account_id: UUID
    strategy: Variant
    strategy_version: Literal["slow-v1"] = "slow-v1"
    symbol: Symbol
    interval_minutes: Literal[240, 1440]
    entry_time_in_force: Literal["GTD", "FOK"] = "GTD"
    selected_at: UtcDateTime
    application_version: str = __version__
    policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    operational_only: Literal[True] = True
    promotion_evidence: Literal[False] = False
    qualifying_days: Literal[0] = 0
    qualifying_episodes: Literal[0] = 0
    live_execution_available: Literal[False] = False

    def identity(self, watermark: datetime) -> UUID:
        return uuid5(self.account_id, fingerprint(self.document()) + ":" + watermark.isoformat())


class PaperDecision(Record):
    account_id: UUID
    selection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    watermark: UtcDateTime
    decision_at: UtcDateTime
    recorded_at: UtcDateTime
    client_id: UUID
    signal: StrategySignal | None
    reason: str | None = None
    candle_hash: str | None = None
    # Bind every selected candle to its original stored receipt and full document.
    sources: tuple[tuple[UUID, str], ...] = Field(default=(), max_length=201)
    operational_only: Literal[True] = True
    promotion_evidence: Literal[False] = False

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if self.signal is not None and (
            self.reason is not None
            or self.signal.data_watermark != self.watermark
            or self.signal.decision_at != self.decision_at
            or not self.decision_at <= self.recorded_at < self.signal.expires_at
            or self.signal.expires_at > self.watermark + timedelta(seconds=12)
            or self.candle_hash is None
            or len(self.sources) < 200
        ):
            raise ValueError("strategy signal/decision evidence mismatch")
        return self


async def selection(c: AsyncConnection, account_id: UUID) -> PaperSelection | None:
    row = (
        (await c.execute(select(selections).where(selections.c.account_id == account_id)))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    value = PaperSelection.restore(row["document"])
    if value.account_id != account_id or fingerprint(value.document()) != row["fingerprint"]:
        raise ValueError("corrupt paper strategy selection")
    if value.policy_hash != fingerprint(RiskPolicy().model_dump(mode="json")):
        raise ValueError("selected paper policy differs from runtime")
    return value


async def select_strategy(writer: PaperWriter, value: PaperSelection) -> PaperSelection:
    if writer.store.account_id != value.account_id:
        raise ValueError("strategy selection account mismatch")
    async with writer.transaction() as c:
        prior = await selection(c, value.account_id)
        if prior is not None:
            if prior.model_copy(update={"selected_at": value.selected_at}) != value:
                raise ValueError("strategy selection is immutable; use a new operational account")
            return prior
        state = await writer.store.load(c)
        if state is None or state.orders or state.quantity("BTC") or state.quantity("ETH"):
            raise ValueError("select a strategy on an initialised flat account without orders")
        if value.selected_at < state.as_of:
            raise ValueError("strategy selection time precedes account state")
        await c.execute(
            selections.insert().values(
                account_id=value.account_id,
                document=value.document(),
                fingerprint=fingerprint(value.document()),
            )
        )
        return value


def restore_decision(row: RowMapping) -> PaperDecision:
    value = PaperDecision.restore(row["document"])
    if (
        value.account_id != row["account_id"]
        or value.watermark != row["watermark"]
        or value.client_id != row["client_id"]
        or fingerprint(value.document()) != row["fingerprint"]
    ):
        raise ValueError("corrupt paper strategy decision")
    return value


async def claim(writer: PaperWriter, value: PaperDecision) -> bool:
    """A committed claim is never retried, even if the process dies before queueing."""
    async with writer.transaction() as c:
        chosen = await selection(c, writer.store.account_id)
        if (
            chosen is None
            or value.account_id != chosen.account_id
            or value.selection_hash != fingerprint(chosen.document())
            or value.client_id != chosen.identity(value.watermark)
            or int(value.watermark.timestamp()) % (chosen.interval_minutes * 60)
            or value.watermark.microsecond
            or (
                value.signal is not None
                and (
                    value.signal.strategy != chosen.strategy
                    or value.signal.symbol != chosen.symbol
                    or value.signal.strategy_version != chosen.strategy_version
                )
            )
        ):
            raise ValueError("strategy decision binding mismatch")
        row = (
            (
                await c.execute(
                    select(decisions).where(
                        decisions.c.account_id == value.account_id,
                        decisions.c.watermark == value.watermark,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            restore_decision(row)
            return False
        await c.execute(
            decisions.insert().values(
                account_id=value.account_id,
                watermark=value.watermark,
                client_id=value.client_id,
                document=value.document(),
                fingerprint=fingerprint(value.document()),
            )
        )
        return True


async def finish(
    writer: PaperWriter, client_id: UUID, outcome: str, at: datetime, reason: str | None = None
) -> None:
    doc = {
        "schema_version": 1,
        "client_id": str(client_id),
        "outcome": outcome,
        "reason": reason,
        "at": at.isoformat(),
        "promotion_evidence": False,
    }
    async with writer.transaction() as c:
        owner = await c.scalar(
            select(decisions.c.account_id).where(decisions.c.client_id == client_id)
        )
        if owner != writer.store.account_id:
            raise ValueError("strategy result account mismatch")
        await c.execute(
            results.insert().values(client_id=client_id, document=doc, fingerprint=fingerprint(doc))
        )


async def strategy_status(c: AsyncConnection, account_id: UUID) -> dict[str, object]:
    chosen = await selection(c, account_id)
    rows = (
        (
            await c.execute(
                select(
                    decisions,
                    results.c.document.label("result"),
                    results.c.fingerprint.label("result_hash"),
                )
                .outerjoin(results, results.c.client_id == decisions.c.client_id)
                .where(decisions.c.account_id == account_id)
                .order_by(decisions.c.watermark.desc())
                .limit(20)
            )
        )
        .mappings()
        .all()
    )
    items = []
    for row in rows:
        value = restore_decision(row)
        if chosen is None or value.selection_hash != fingerprint(chosen.document()):
            raise ValueError("decision selection changed")
        result = row["result"]
        if result is not None and fingerprint(result) != row["result_hash"]:
            raise ValueError("strategy result checksum mismatch")
        items.append({"decision": value.document(), "result": result, "replay_permitted": False})
    return {
        "selection": chosen.document() if chosen else None,
        "decisions": items,
        "qualifying_days": 0,
        "qualifying_episodes": 0,
        "promotion_evidence": False,
    }
