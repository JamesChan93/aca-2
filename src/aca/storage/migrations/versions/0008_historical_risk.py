"""Append-only historical paper valuation and risk evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_historical_risk"
down_revision = "0007_replay_cutoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_replay_valuations",
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
        sa.Column("number", sa.BigInteger, primary_key=True),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint("number >= 0", name="historical_replay_number"),
        schema="aca",
    )
    op.execute(
        "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON aca.paper_replay_valuations FOR EACH STATEMENT "
        "EXECUTE FUNCTION aca.reject_history_mutation()"
    )
    op.execute("GRANT SELECT, INSERT ON aca.paper_replay_valuations TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Historical risk evidence is append-only; use a reviewed restore")
