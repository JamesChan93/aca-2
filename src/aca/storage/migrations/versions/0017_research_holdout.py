"""Durable one-time research holdout use, independent of artefact folders."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0017_research_holdout"
down_revision = "0016_history_segments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_holdout_uses",
        sa.Column("scope", sa.String(160), primary_key=True),
        sa.Column("freeze", sa.String(64), nullable=False),
        sa.Column("source_lineage", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(document::text) <= 262144"),
        schema="aca",
    )
    op.execute(
        "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON aca.research_holdout_uses FOR EACH STATEMENT "
        "EXECUTE FUNCTION aca.reject_history_mutation()"
    )
    op.execute("GRANT SELECT, INSERT ON aca.research_holdout_uses TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Holdout use cannot be erased; use a reviewed restore")
