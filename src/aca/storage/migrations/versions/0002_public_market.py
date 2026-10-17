"""Append-only public market receipts and deduplicated trades."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_public_market"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_receipts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("epoch", sa.Uuid(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel", sa.String(80), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(80), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("normalized", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="market_receipt_schema_v1"),
        sa.CheckConstraint("octet_length(payload) <= 2000000", name="market_payload_bound"),
        schema="aca",
    )
    op.create_index("ix_aca_market_receipts_epoch", "market_receipts", ["epoch"], schema="aca")
    op.create_index(
        "ix_aca_market_receipts_received_at", "market_receipts", ["received_at"], schema="aca"
    )
    op.create_table(
        "public_trades",
        sa.Column("venue", sa.String(40), primary_key=True),
        sa.Column("symbol", sa.String(40), primary_key=True),
        sa.Column("trade_id", sa.String(80), primary_key=True),
        sa.Column("receipt_id", sa.Uuid(), sa.ForeignKey("aca.market_receipts.id"), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
        sa.CheckConstraint("price > 0 AND quantity > 0", name="public_trade_positive"),
        sa.CheckConstraint("symbol IN ('BTC/GBP', 'ETH/GBP')", name="public_trade_allowlist"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="public_trade_side"),
        schema="aca",
    )
    op.create_index("ix_aca_public_trades_event_at", "public_trades", ["event_at"], schema="aca")
    for table in ("market_receipts", "public_trades"):
        op.execute(
            sa.text(
                "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON aca.{table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION aca.reject_history_mutation()"
            )
        )
        op.execute(sa.text(f"GRANT SELECT, INSERT ON aca.{table} TO aca_runtime"))


def downgrade() -> None:
    raise RuntimeError("Market evidence is append-only; use a reviewed restore procedure")
