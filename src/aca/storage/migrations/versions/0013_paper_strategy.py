"""Immutable operational strategy selection and permanent decision claims."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013_paper_strategy"
down_revision = "0012_tax_cash_reserves"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_strategy_selections",
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint(
            "document->>'operational_only' = 'true' "
            "AND document->>'promotion_evidence' = 'false' "
            "AND document->>'live_execution_available' = 'false'",
            name="selection_operational_only",
        ),
        sa.CheckConstraint("octet_length(document::text) <= 4096", name="selection_bound"),
        schema="aca",
    )
    op.create_table(
        "paper_strategy_decisions",
        sa.Column(
            "account_id",
            sa.Uuid,
            sa.ForeignKey("aca.paper_strategy_selections.account_id"),
            primary_key=True,
        ),
        sa.Column("watermark", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("client_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint("octet_length(document::text) <= 65536", name="strategy_decision_bound"),
        schema="aca",
    )
    op.create_table(
        "paper_strategy_results",
        sa.Column(
            "client_id",
            sa.Uuid,
            sa.ForeignKey("aca.paper_strategy_decisions.client_id"),
            primary_key=True,
        ),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint("octet_length(document::text) <= 4096", name="strategy_result_bound"),
        schema="aca",
    )
    for table in (
        "paper_strategy_selections",
        "paper_strategy_decisions",
        "paper_strategy_results",
    ):
        op.execute(
            "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
            f"ON aca.{table} FOR EACH STATEMENT "
            "EXECUTE FUNCTION aca.reject_history_mutation()"
        )
        op.execute(f"GRANT SELECT, INSERT ON aca.{table} TO aca_runtime")
    op.create_index(
        "strategy_bootstrap_candles",
        "market_receipts",
        [
            sa.text("(normalized['candles'][0]->>'symbol')"),
            sa.text("((normalized['candles'][0]->>'interval_minutes')::integer)"),
            sa.text("received_at DESC"),
            sa.text("id DESC"),
        ],
        schema="aca",
        postgresql_where=sa.text("channel = 'rest:closed_candles' AND valid"),
    )
    op.create_index(
        "strategy_local_candles",
        "market_receipts",
        [
            sa.text("(normalized['candle']->>'symbol')"),
            sa.text("((normalized['candle']->>'interval_minutes')::integer)"),
            sa.text("received_at DESC"),
            sa.text("id DESC"),
        ],
        schema="aca",
        postgresql_where=sa.text("channel = 'local:closed_candle' AND valid"),
    )


def downgrade() -> None:
    raise RuntimeError("Strategy decisions are permanent; use a reviewed restore")
