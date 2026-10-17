"""Append-only reproducible research artefacts and trial events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_research"
down_revision = "0002_public_market"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_artifacts",
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="research_artifact_schema_v1"),
        schema="aca",
    )
    op.create_table(
        "research_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "artifact_fingerprint",
            sa.String(64),
            sa.ForeignKey("aca.research_artifacts.fingerprint"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        schema="aca",
    )
    op.create_index("ix_aca_research_events_run_id", "research_events", ["run_id"], schema="aca")
    for table in ("research_artifacts", "research_events"):
        op.execute(
            sa.text(
                "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON aca.{table} FOR EACH STATEMENT EXECUTE FUNCTION aca.reject_history_mutation()"
            )
        )
        op.execute(sa.text(f"GRANT SELECT, INSERT ON aca.{table} TO aca_runtime"))


def downgrade() -> None:
    raise RuntimeError("Research history is append-only; use a reviewed restore procedure")
