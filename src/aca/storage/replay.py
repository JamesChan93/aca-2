"""Ordered public evidence and append-only account replay cursors."""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, MetaData, String, Table, Uuid
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(schema="aca")
PUBLIC_REPLAY_LOCK = 72149386100511831
public_events = Table(
    "public_replay_events",
    metadata,
    Column("number", BigInteger, primary_key=True, autoincrement=True),
    Column("receipt_id", Uuid, ForeignKey("aca.market_receipts.id"), nullable=False, unique=True),
    Column("document", JSONB, nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
)
cursors = Table(
    "paper_replay_cursors",
    metadata,
    Column("account_id", Uuid, ForeignKey("aca.paper_accounts.id"), primary_key=True),
    Column("number", BigInteger, primary_key=True),
    Column("previous_hash", String(64), nullable=False),
    Column("fingerprint", String(64), nullable=False),
    Column("document", JSONB, nullable=False),
)
