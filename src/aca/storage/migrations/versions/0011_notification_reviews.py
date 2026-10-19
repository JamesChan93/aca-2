"""Operator review references exact terminal events; delivery evidence is retained."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_notification_reviews"
down_revision = "0010_scheduled_notices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "notification_review_target",
        "paper_notification_events",
        ["account_id", "number", "kind", "fingerprint"],
        schema="aca",
    )
    op.create_table(
        "paper_notification_reviews",
        sa.Column("account_id", sa.Uuid, primary_key=True),
        sa.Column("failure_number", sa.BigInteger, primary_key=True),
        sa.Column("failure_kind", sa.String(32), nullable=False),
        sa.Column("failure_hash", sa.String(64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "failure_number", "failure_kind", "failure_hash"],
            [
                "aca.paper_notification_events.account_id",
                "aca.paper_notification_events.number",
                "aca.paper_notification_events.kind",
                "aca.paper_notification_events.fingerprint",
            ],
        ),
        sa.CheckConstraint("failure_number >= 1", name="notification_review_number"),
        sa.CheckConstraint(
            "failure_kind IN ('abandoned','dead_letter')", name="notification_review_kind"
        ),
        sa.CheckConstraint(
            "octet_length(document::text) <= 2048", name="notification_review_bound"
        ),
        schema="aca",
    )
    op.execute(
        "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON aca.paper_notification_reviews FOR EACH STATEMENT "
        "EXECUTE FUNCTION aca.reject_history_mutation()"
    )
    op.execute("GRANT SELECT, INSERT ON aca.paper_notification_reviews TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Notification reviews are append-only; use a reviewed restore")
