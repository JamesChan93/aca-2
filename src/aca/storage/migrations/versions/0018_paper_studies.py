"""Separate approved studies from permanently operational-only paper history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018_paper_studies"
down_revision = "0017_research_holdout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_studies",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid,
            sa.ForeignKey("aca.paper_accounts.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("approval_id", sa.Uuid, unique=True, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint("octet_length(document::text) <= 262144"),
        schema="aca",
    )
    op.create_table(
        "paper_study_events",
        sa.Column("study_id", sa.Uuid, sa.ForeignKey("aca.paper_studies.id"), primary_key=True),
        sa.Column("number", sa.BigInteger, primary_key=True),
        sa.Column("event_id", sa.Uuid, nullable=False, unique=True),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint("number > 0 AND octet_length(document::text) <= 262144"),
        schema="aca",
    )
    for name in ("paper_studies", "paper_study_events"):
        op.execute(
            f"CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
            f"ON aca.{name} FOR EACH STATEMENT "
            "EXECUTE FUNCTION aca.reject_history_mutation()"
        )
        op.execute(f"GRANT SELECT, INSERT ON aca.{name} TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Study approvals and observation evidence cannot be erased")
