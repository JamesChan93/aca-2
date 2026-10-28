from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ConfigurationRecord(Base):
    __tablename__ = "configuration_versions"
    __table_args__ = (
        CheckConstraint("mode = 'paper'", name="configuration_paper_only"),
        CheckConstraint("schema_version = 1", name="configuration_schema_v1"),
        {"schema": "aca"},
    )
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int]
    mode: Mapped[str] = mapped_column(String(16))
    document: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = ({"schema": "aca"},)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event: Mapped[str] = mapped_column(String(80))
    actor: Mapped[str] = mapped_column(String(80))
    configuration_fingerprint: Mapped[str] = mapped_column(
        ForeignKey("aca.configuration_versions.fingerprint"),
    )
    detail: Mapped[dict[str, object]] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketReceipt(Base):
    __tablename__ = "market_receipts"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="market_receipt_schema_v1"),
        CheckConstraint("octet_length(payload) <= 2000000", name="market_payload_bound"),
        {"schema": "aca"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    schema_version: Mapped[int]
    epoch: Mapped[UUID] = mapped_column(index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    channel: Mapped[str] = mapped_column(String(80))
    valid: Mapped[bool]
    reason: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    normalized: Mapped[dict[str, object]] = mapped_column(JSONB)


class PublicTradeRecord(Base):
    __tablename__ = "public_trades"
    __table_args__ = (
        CheckConstraint("price > 0 AND quantity > 0", name="public_trade_positive"),
        CheckConstraint("symbol IN ('BTC/GBP', 'ETH/GBP')", name="public_trade_allowlist"),
        CheckConstraint("side IN ('buy', 'sell')", name="public_trade_side"),
        {"schema": "aca"},
    )
    venue: Mapped[str] = mapped_column(String(40), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    receipt_id: Mapped[UUID] = mapped_column(ForeignKey("aca.market_receipts.id"))
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    side: Mapped[str] = mapped_column(String(4))
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    quantity: Mapped[Decimal] = mapped_column(Numeric(38, 18))


class ResearchArtifact(Base):
    __tablename__ = "research_artifacts"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="research_artifact_schema_v1"),
        {"schema": "aca"},
    )
    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(40))
    document: Mapped[dict[str, object]] = mapped_column(JSONB)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchEvent(Base):
    __tablename__ = "research_events"
    __table_args__ = ({"schema": "aca"},)
    id: Mapped[UUID] = mapped_column(primary_key=True)
    run_id: Mapped[UUID] = mapped_column(index=True)
    artifact_fingerprint: Mapped[str] = mapped_column(
        ForeignKey("aca.research_artifacts.fingerprint"),
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ResearchHoldoutUse(Base):
    __tablename__ = "research_holdout_uses"
    __table_args__ = (
        CheckConstraint("octet_length(document::text) <= 262144"),
        {"schema": "aca"},
    )
    scope: Mapped[str] = mapped_column(String(160), primary_key=True)
    freeze: Mapped[str] = mapped_column(String(64))
    source_lineage: Mapped[str] = mapped_column(String(64))
    document: Mapped[dict[str, object]] = mapped_column(JSONB)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
