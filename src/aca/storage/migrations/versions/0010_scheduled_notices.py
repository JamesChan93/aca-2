"""Durable scheduled notices and terminal receipts, separate from order state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_scheduled_notices"
down_revision = "0009_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_notices",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.UniqueConstraint("account_id", "key", name="paper_notice_key"),
        sa.UniqueConstraint("id", "account_id", name="paper_notice_identity"),
        sa.CheckConstraint("kind IN ('daily','critical')", name="paper_notice_kind"),
        sa.CheckConstraint("octet_length(document::text) <= 8192", name="paper_notice_bound"),
        schema="aca",
    )
    op.create_index(
        "paper_notice_queue", "paper_notices", ["account_id", "kind", "created_at"], schema="aca"
    )
    op.create_table(
        "paper_notice_receipts",
        sa.Column("notice_id", sa.Uuid, primary_key=True),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(
            ["notice_id", "account_id"], ["aca.paper_notices.id", "aca.paper_notices.account_id"]
        ),
        sa.CheckConstraint(
            "outcome IN ('delivered','abandoned','dead_letter','superseded')",
            name="paper_notice_outcome",
        ),
        schema="aca",
    )
    for table in ("paper_notices", "paper_notice_receipts"):
        op.execute(
            f"CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE ON aca.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aca.reject_history_mutation()"
        )
        op.execute(f"GRANT SELECT, INSERT ON aca.{table} TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Scheduled notice history is append-only; use a reviewed restore")
