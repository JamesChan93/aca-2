"""Append-only paper notification delivery evidence, isolated from financial history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_notifications"
down_revision = "0008_historical_risk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("paper_alert_event", "paper_alerts", ["event_id"], schema="aca")
    op.create_index("paper_alert_account", "paper_alerts", ["account_id", "event_id"], schema="aca")
    op.create_table(
        "paper_notification_events",
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
        sa.Column("number", sa.BigInteger, primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint("number >= 1", name="notification_number"),
        sa.CheckConstraint(
            "octet_length(document::text) <= 4096", name="notification_document_bound"
        ),
        schema="aca",
    )
    op.execute(
        "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON aca.paper_notification_events FOR EACH STATEMENT "
        "EXECUTE FUNCTION aca.reject_history_mutation()"
    )
    op.execute("GRANT SELECT, INSERT ON aca.paper_notification_events TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Notification history is append-only; use a reviewed restore")
