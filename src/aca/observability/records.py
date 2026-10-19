"""Immutable notification journal contracts, separate from financial state."""

from uuid import UUID

from pydantic import Field
from sqlalchemy import BigInteger, Column, DateTime, MetaData, String, Table, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from aca.domain.values import UtcDateTime
from aca.operations.config import StrictModel

history = Table(
    "paper_notification_events",
    MetaData(schema="aca"),
    Column("account_id", Uuid, primary_key=True),
    Column("number", BigInteger, primary_key=True),
    Column("at", DateTime(timezone=True)),
    Column("kind", String(32)),
    Column("previous_hash", String(64)),
    Column("fingerprint", String(64)),
    Column("document", JSONB),
)
MAX_ATTEMPTS = 8


class DeliveryState(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    account_id: UUID
    destination_hash: str = Field(pattern="^[a-f0-9]{64}$")
    enabled: bool = True
    cursor: int = Field(default=0, ge=0)
    attempts: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    not_before: UtcDateTime
    outcome: str = Field(default="configured", pattern="^[a-z_]{1,40}$")
    alert_id: UUID | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    message_id: str | None = Field(default=None, pattern="^[0-9]{17,20}$")
    notice_id: UUID | None = None
