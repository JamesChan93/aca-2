"""Append-only paper account events, balanced journals and separate venue records."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData(schema="aca")
accounts = sa.Table(
    "paper_accounts",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("mode", sa.String(10), nullable=False),
)
events = sa.Table(
    "paper_events",
    metadata,
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True),
    sa.Column("id", sa.Uuid, nullable=False, unique=True),
    sa.Column("source", sa.String(160), nullable=False),
    sa.Column("kind", sa.String(60), nullable=False),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("previous_hash", sa.String(64), nullable=False),
    sa.Column("fingerprint", sa.String(64), nullable=False),
    sa.Column("document", JSONB, nullable=False),
)
journals = sa.Table(
    "paper_journals",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), nullable=False),
    sa.Column("source", sa.String(160), nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("document", JSONB, nullable=False),
)
lines = sa.Table(
    "paper_journal_lines",
    metadata,
    sa.Column("journal_id", sa.Uuid, sa.ForeignKey("aca.paper_journals.id"), primary_key=True),
    sa.Column("number", sa.Integer, primary_key=True),
    sa.Column("account", sa.String(40), nullable=False),
    sa.Column("gbp", sa.Numeric(38, 18), nullable=False),
)
movements = sa.Table(
    "paper_movements",
    metadata,
    sa.Column("journal_id", sa.Uuid, sa.ForeignKey("aca.paper_journals.id"), primary_key=True),
    sa.Column("number", sa.Integer, primary_key=True),
    sa.Column("asset", sa.String(3), nullable=False),
    sa.Column("quantity", sa.Numeric(38, 18), nullable=False),
)
venue_events = sa.Table(
    "paper_venue_events",
    metadata,
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
    sa.Column("number", sa.Integer, primary_key=True),
    sa.Column("source", sa.String(160), nullable=False),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("document", JSONB, nullable=False),
)
outbox = sa.Table(
    "paper_alerts",
    metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), nullable=False),
    sa.Column("event_id", sa.Uuid, sa.ForeignKey("aca.paper_events.id"), nullable=False),
    sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("category", sa.String(60), nullable=False),
    sa.Column("document", JSONB, nullable=False),
)
fills = sa.Table(
    "paper_fills",
    metadata,
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
    sa.Column("id", sa.String(100), primary_key=True),
    sa.Column(
        "journal_id", sa.Uuid, sa.ForeignKey("aca.paper_journals.id"), nullable=False, unique=True
    ),
    sa.Column("document", JSONB, nullable=False),
)
order_identities = sa.Table(
    "paper_order_identities",
    metadata,
    sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
    sa.Column("client_id", sa.Uuid, primary_key=True),
    sa.Column("document", JSONB, nullable=False),
)


revision = "0004_paper"
down_revision = "0003_research"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(op.get_bind())
    op.create_check_constraint(
        "paper_accounts_mode", "paper_accounts", "mode = 'paper'", schema="aca"
    )
    op.create_check_constraint("paper_event_version", "paper_events", "version >= 1", schema="aca")
    op.create_unique_constraint(
        "paper_event_source", "paper_events", ["account_id", "source"], schema="aca"
    )
    op.create_unique_constraint(
        "paper_journal_source", "paper_journals", ["account_id", "source"], schema="aca"
    )
    op.create_unique_constraint(
        "paper_venue_source", "paper_venue_events", ["account_id", "source"], schema="aca"
    )
    op.create_check_constraint(
        "paper_movement_asset", "paper_movements", "asset IN ('GBP','BTC','ETH')", schema="aca"
    )
    op.create_check_constraint(
        "paper_line_number", "paper_journal_lines", "number >= 0", schema="aca"
    )
    op.create_check_constraint(
        "paper_movement_number", "paper_movements", "number >= 0", schema="aca"
    )
    op.execute("""
        CREATE FUNCTION aca.check_paper_journal() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE j uuid; n bigint; total numeric; d jsonb;
        BEGIN
            IF TG_TABLE_NAME = 'paper_journals' THEN
                j := NEW.id;
            ELSE
                j := NEW.journal_id;
            END IF;
            SELECT count(*), coalesce(sum(gbp),0) INTO n,total
            FROM aca.paper_journal_lines WHERE journal_id=j;
            SELECT document INTO d FROM aca.paper_journals WHERE id=j;
            IF jsonb_typeof(d->'lines') IS DISTINCT FROM 'array' OR
               jsonb_typeof(d->'movements') IS DISTINCT FROM 'array' THEN
                RAISE EXCEPTION 'invalid journal evidence' USING ERRCODE='23514';
            END IF;
            IF n < 2 OR total <> 0 OR n <> jsonb_array_length(d->'lines') THEN
                RAISE EXCEPTION 'unbalanced paper journal' USING ERRCODE='23514';
            END IF;
            IF EXISTS (
                SELECT 1 FROM aca.paper_journal_lines l WHERE l.journal_id=j AND (
                    (d->'lines'->l.number->>'account') IS DISTINCT FROM l.account OR
                    (d->'lines'->l.number->>'gbp')::numeric IS DISTINCT FROM l.gbp)
            ) THEN
                RAISE EXCEPTION 'journal lines differ from evidence' USING ERRCODE='23514';
            END IF;
            SELECT count(*) INTO n FROM aca.paper_movements WHERE journal_id=j;
            IF n <> jsonb_array_length(d->'movements') OR EXISTS (
                SELECT 1 FROM aca.paper_movements m WHERE m.journal_id=j AND (
                    (d->'movements'->m.number->>'asset') IS DISTINCT FROM m.asset OR
                    (d->'movements'->m.number->>'quantity')::numeric IS DISTINCT FROM m.quantity)
            ) THEN
                RAISE EXCEPTION 'asset movements differ from evidence' USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END $$
    """)
    for table in ("paper_journals", "paper_journal_lines", "paper_movements"):
        op.execute(
            sa.text(
                f"CREATE CONSTRAINT TRIGGER balanced_journal AFTER INSERT ON aca.{table} "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION aca.check_paper_journal()"
            )
        )
    for table in metadata.sorted_tables:
        op.execute(
            sa.text(
                "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON aca.{table.name} FOR EACH STATEMENT "
                "EXECUTE FUNCTION aca.reject_history_mutation()"
            )
        )
        op.execute(sa.text(f"GRANT SELECT, INSERT ON aca.{table.name} TO aca_runtime"))


def downgrade() -> None:
    raise RuntimeError("Paper accounting is append-only; use a reviewed restore procedure")
