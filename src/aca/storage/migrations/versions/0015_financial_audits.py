"""Operator-owned financial audit attestations; runtime cannot create trust."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_financial_audits"
down_revision = "0014_accounting_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_financial_audits",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "version"],
            ["aca.paper_events.account_id", "aca.paper_events.version"],
        ),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("fingerprint ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("octet_length(document::text) <= 2100000"),
        schema="aca",
    )
    op.create_index(
        "paper_financial_audits_account",
        "paper_financial_audits",
        ["account_id", "version"],
        schema="aca",
    )
    op.execute(
        "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON aca.paper_financial_audits FOR EACH STATEMENT "
        "EXECUTE FUNCTION aca.reject_history_mutation()"
    )
    # No runtime INSERT/UPDATE/DELETE, no SECURITY DEFINER insertion function,
    # no environment setting or self-issued checkpoint can confer this authority.
    op.execute("REVOKE ALL ON aca.paper_financial_audits FROM PUBLIC, aca_runtime")
    op.execute("GRANT SELECT ON aca.paper_financial_audits TO aca_runtime")
    op.execute("""
        CREATE FUNCTION aca.check_financial_audit() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE e aca.paper_events%ROWTYPE; n bigint; head integer;
        BEGIN
            SELECT * INTO STRICT e FROM aca.paper_events
              WHERE account_id=NEW.account_id AND version=NEW.version;
            SELECT max(version) INTO head FROM aca.paper_events WHERE account_id=NEW.account_id;
            SELECT count(*) INTO n FROM aca.paper_checkpoints
              WHERE account_id=NEW.account_id AND version<=NEW.version;
            IF NEW.document->>'audit_version' IS DISTINCT FROM 'paper-financial-prefix-v1'
               OR NEW.document->>'id' IS DISTINCT FROM NEW.id::text
               OR NEW.document->>'account_id' IS DISTINCT FROM NEW.account_id::text
               OR (NEW.document->>'version')::integer IS DISTINCT FROM NEW.version
               OR (NEW.document->>'recorded_at')::timestamptz IS DISTINCT FROM NEW.recorded_at
               OR NEW.document->>'event_hash' IS DISTINCT FROM e.fingerprint
               OR (NEW.document->>'checkpoint_count')::bigint IS DISTINCT FROM n
               OR head IS DISTINCT FROM NEW.version
               OR NEW.document->'pruning_authorised' IS DISTINCT FROM 'false'::jsonb
               OR NEW.document->'live_execution_available' IS DISTINCT FROM 'false'::jsonb
            THEN RAISE EXCEPTION 'financial audit anchor differs from retained evidence'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER financial_audit_anchor BEFORE INSERT ON aca.paper_financial_audits "
        "FOR EACH ROW EXECUTE FUNCTION aca.check_financial_audit()"
    )


def downgrade() -> None:
    raise RuntimeError("Financial audit evidence is append-only; use a reviewed restore")
