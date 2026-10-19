"""Seal paper transfer/correction evidence and irreversible unit invalidation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_accounting_operations"
down_revision = "0013_paper_strategy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_accounting_operations",
        sa.Column("account_id", sa.Uuid, primary_key=True),
        sa.Column("request_id", sa.Uuid, primary_key=True),
        sa.Column("event_version", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("external_reference", sa.String(120)),
        sa.Column("target_journal", sa.Uuid, sa.ForeignKey("aca.paper_journals.id")),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "event_version"],
            ["aca.paper_events.account_id", "aca.paper_events.version"],
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint("account_id", "event_version"),
        sa.UniqueConstraint("account_id", "external_reference"),
        sa.UniqueConstraint("account_id", "target_journal"),
        sa.CheckConstraint(
            "event_version >= 2 AND kind::text IN ('asset_transfer','accounting_correction')"
        ),
        sa.CheckConstraint(
            "(kind='asset_transfer' AND external_reference IS NOT NULL AND target_journal IS NULL) "
            "OR (kind='accounting_correction' AND external_reference IS NULL "
            "AND target_journal IS NOT NULL)"
        ),
        sa.CheckConstraint("octet_length(document::text) <= 32768"),
        schema="aca",
    )
    op.execute(
        "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON aca.paper_accounting_operations FOR EACH STATEMENT "
        "EXECUTE FUNCTION aca.reject_history_mutation()"
    )
    op.execute("GRANT SELECT, INSERT ON aca.paper_accounting_operations TO aca_runtime")
    op.execute("""
        CREATE FUNCTION aca.seal_accounting_operation() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE e aca.paper_events%ROWTYPE; r aca.paper_accounting_operations%ROWTYPE;
                prior boolean; changed jsonb; ids jsonb;
        BEGIN
            IF TG_TABLE_NAME = 'paper_events' THEN
                e := NEW;
            ELSE
                r := NEW;
                SELECT * INTO STRICT e FROM aca.paper_events
                  WHERE account_id=r.account_id AND version=r.event_version;
            END IF;
            changed := COALESCE(e.document->'changes', e.document);
            SELECT EXISTS(SELECT 1 FROM aca.paper_accounting_operations
                WHERE account_id=e.account_id AND kind='accounting_correction'
                  AND event_version < e.version) INTO prior;
            IF prior THEN
                IF (changed ? 'accounting_invalidated'
                    AND changed->'accounting_invalidated' <> 'true')
                   OR changed->'ready' = 'true'
                   OR (changed ? 'kill' AND changed->'kill'->'hard' IS DISTINCT FROM 'true')
                THEN RAISE EXCEPTION 'accounting invalidation cannot be cleared'; END IF;
            ELSIF e.kind <> 'accounting_correction'
                AND changed->'accounting_invalidated' = 'true' THEN
                RAISE EXCEPTION 'invalidation requires correction evidence';
            END IF;
            IF e.kind NOT IN ('asset_transfer','accounting_correction') THEN
                IF TG_TABLE_NAME <> 'paper_events' THEN
                    RAISE EXCEPTION 'invalid accounting event'; END IF;
                RETURN NEW;
            END IF;
            SELECT * INTO STRICT r FROM aca.paper_accounting_operations
                WHERE account_id=e.account_id AND event_version=e.version;
            IF r.kind <> e.kind OR e.source <> 'accounting:' || r.request_id::text
               OR r.document->>'request_id' IS DISTINCT FROM r.request_id::text
               OR r.document->'request'->>'request_id' IS DISTINCT FROM r.request_id::text
               OR r.document->>'account_id' IS DISTINCT FROM e.account_id::text
               OR r.document->>'kind' IS DISTINCT FROM e.kind
               OR (r.document->>'recorded_at')::timestamptz IS DISTINCT FROM e.at
               OR (r.document->>'event_version')::integer IS DISTINCT FROM e.version
               OR (r.document->'request'->>'expected_version')::integer IS DISTINCT FROM e.version-1
               OR r.document->>'resulting_state_hash' IS DISTINCT FROM e.document->>'state_hash'
            THEN RAISE EXCEPTION 'accounting event and decision differ'; END IF;
            IF e.kind='accounting_correction' THEN
                IF e.document->>'format' IS DISTINCT FROM 'paper_delta_v2'
                   OR (NOT prior AND changed->'accounting_invalidated' IS DISTINCT FROM 'true')
                   OR changed->'ready' = 'true'
                   OR (NOT prior AND changed->'kill'->'hard' IS DISTINCT FROM 'true')
                   OR r.document->'request'->>'target_journal'
                      IS DISTINCT FROM r.target_journal::text
                THEN RAISE EXCEPTION 'correction must invalidate units and halt'; END IF;
                ids := e.document->'journal_ids';
                IF jsonb_array_length(ids) IS DISTINCT FROM 2 THEN
                    RAISE EXCEPTION 'correction requires two postings'; END IF;
            ELSE
                ids := jsonb_build_array(e.document->>'journal_id');
                IF r.document->'request'->>'external_reference'
                    IS DISTINCT FROM r.external_reference
                THEN RAISE EXCEPTION 'transfer reference differs'; END IF;
            END IF;
            IF jsonb_array_length(r.document->'postings') IS DISTINCT FROM jsonb_array_length(ids)
               OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(ids) WITH ORDINALITY a(id,n)
                    LEFT JOIN aca.paper_journals j ON j.id=a.id::uuid
                    WHERE j.id IS NULL OR j.account_id<>e.account_id
                       OR j.document IS DISTINCT FROM r.document->'postings'->(a.n::integer-1))
            THEN RAISE EXCEPTION 'accounting postings differ'; END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("paper_events", "paper_accounting_operations"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER seal_accounting_operation AFTER INSERT ON aca.{table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION aca.seal_accounting_operation()"
        )


def downgrade() -> None:
    raise RuntimeError("Accounting evidence is append-only; use a reviewed restore")
