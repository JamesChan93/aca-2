from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text
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
