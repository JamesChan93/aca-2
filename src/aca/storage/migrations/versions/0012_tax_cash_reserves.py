"""Seal manual tax-cash decisions together with their paper state event."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_tax_cash_reserves"
down_revision = "0011_notification_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_tax_reserves",
        sa.Column("account_id", sa.Uuid, primary_key=True),
        sa.Column("request_id", sa.Uuid, primary_key=True),
        sa.Column("event_version", sa.Integer, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount_gbp", sa.Numeric(38, 18), nullable=False),
        sa.Column("previous_gbp", sa.Numeric(38, 18), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "event_version"],
            ["aca.paper_events.account_id", "aca.paper_events.version"],
        ),
        sa.UniqueConstraint("account_id", "event_version", name="tax_reserve_event"),
        sa.CheckConstraint("event_version >= 2", name="tax_reserve_version"),
        sa.CheckConstraint(
            "amount_gbp >= 0 AND previous_gbp >= 0 AND amount_gbp <> previous_gbp",
            name="tax_reserve_amounts",
        ),
        sa.CheckConstraint("octet_length(document::text) <= 4096", name="tax_reserve_bound"),
        schema="aca",
    )
    op.execute(
        "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
        "ON aca.paper_tax_reserves FOR EACH STATEMENT "
        "EXECUTE FUNCTION aca.reject_history_mutation()"
    )
    op.execute("GRANT SELECT, INSERT ON aca.paper_tax_reserves TO aca_runtime")
    op.execute("""
        CREATE FUNCTION aca.seal_tax_cash_reserve() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE e aca.paper_events%ROWTYPE; r aca.paper_tax_reserves%ROWTYPE;
        BEGIN
            IF TG_TABLE_NAME = 'paper_events' THEN
                e := NEW;
                IF e.kind <> 'tax_reserve_set' THEN
                    -- Legacy full-state evidence is still readable/insertable,
                    -- but cannot introduce or erase a decision through SQL.
                    IF e.document ? 'tax_reserve_gbp'
                       AND (e.document->>'tax_reserve_gbp')::numeric IS DISTINCT FROM
                           COALESCE((SELECT amount_gbp FROM aca.paper_tax_reserves
                                     WHERE account_id=e.account_id AND event_version<e.version
                                     ORDER BY event_version DESC LIMIT 1), 0)
                    THEN RAISE EXCEPTION 'tax cash change requires decision'; END IF;
                    IF e.document->'changes' ? 'tax_reserve_gbp'
                       AND NOT (e.version = 1
                                AND (e.document->'changes'->>'tax_reserve_gbp')::numeric = 0)
                    THEN RAISE EXCEPTION 'tax cash change requires decision'; END IF;
                    RETURN NEW;
                END IF;
                SELECT * INTO r FROM aca.paper_tax_reserves
                    WHERE account_id = e.account_id AND event_version = e.version;
                IF NOT FOUND THEN RAISE EXCEPTION 'tax cash decision missing'; END IF;
            ELSE
                r := NEW;
                SELECT * INTO STRICT e FROM aca.paper_events
                    WHERE account_id = r.account_id AND version = r.event_version;
            END IF;
            IF e.kind <> 'tax_reserve_set'
               OR e.source <> 'tax-reserve:' || r.request_id::text
               OR e.at <> r.recorded_at
               OR e.document->>'format' IS DISTINCT FROM 'paper_delta_v1'
               OR e.document->>'journal_id' IS NOT NULL
               OR ((e.document->'changes') - ARRAY['version','as_of','tax_reserve_gbp']) <> '{}'
               OR (e.document->'changes'->>'tax_reserve_gbp')::numeric
                    IS DISTINCT FROM r.amount_gbp
               OR (r.document->>'amount_gbp')::numeric IS DISTINCT FROM r.amount_gbp
               OR (r.document->>'previous_gbp')::numeric IS DISTINCT FROM r.previous_gbp
               OR (r.document->>'expected_version')::integer IS DISTINCT FROM e.version - 1
               OR (r.document->>'event_version')::integer IS DISTINCT FROM e.version
               OR r.document->>'account_id' IS DISTINCT FROM e.account_id::text
               OR r.document->>'request_id' IS DISTINCT FROM r.request_id::text
               OR (r.document->>'recorded_at')::timestamptz IS DISTINCT FROM e.at
            THEN RAISE EXCEPTION 'tax cash decision and event differ'; END IF;
            RETURN NEW;
        END $$
    """)
    for table in ("paper_events", "paper_tax_reserves"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER seal_tax_cash_reserve AFTER INSERT ON aca.{table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION aca.seal_tax_cash_reserve()"
        )


def downgrade() -> None:
    raise RuntimeError("Tax cash decisions are append-only; use a reviewed restore")
