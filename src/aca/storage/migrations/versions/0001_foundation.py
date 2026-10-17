"""Paper-only configuration and append-only audit history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "configuration_versions",
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode = 'paper'", name="configuration_paper_only"),
        sa.CheckConstraint("schema_version = 1", name="configuration_schema_v1"),
        schema="aca",
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", sa.String(80), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column(
            "configuration_fingerprint",
            sa.String(64),
            sa.ForeignKey("aca.configuration_versions.fingerprint"),
            nullable=False,
        ),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        schema="aca",
    )
    op.create_index("ix_aca_audit_events_run_id", "audit_events", ["run_id"], schema="aca")
    op.create_index(
        "ix_aca_audit_events_occurred_at", "audit_events", ["occurred_at"], schema="aca"
    )
    op.execute("""
        CREATE FUNCTION aca.reject_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'history is append-only' USING ERRCODE = '55000';
        END $$
    """)
    for table in ("configuration_versions", "audit_events"):
        op.execute(
            sa.text(
                f"CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON aca.{table} FOR EACH STATEMENT EXECUTE FUNCTION aca.reject_history_mutation()"
            )
        )
    op.execute("REVOKE ALL ON SCHEMA aca FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA aca TO aca_runtime")
    op.execute(
        "GRANT SELECT, INSERT ON aca.configuration_versions, aca.audit_events TO aca_runtime"
    )
    op.execute("GRANT SELECT ON aca.alembic_version TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Audit history cannot be downgraded; use a reviewed restore procedure")
