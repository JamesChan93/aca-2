"""Sealed paper checkpoint anchors; immutable legacy events are retained."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_checkpoints"
down_revision = "0004_paper"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_checkpoints",
        sa.Column("account_id", sa.Uuid, primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id", "version"], ["aca.paper_events.account_id", "aca.paper_events.version"]
        ),
        sa.CheckConstraint("version >= 1", name="checkpoint_version"),
        schema="aca",
    )
    op.execute("""
        CREATE FUNCTION aca.check_paper_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE e aca.paper_events;
        BEGIN
            SELECT * INTO STRICT e FROM aca.paper_events
              WHERE account_id=NEW.account_id AND version=NEW.version;
            IF e.fingerprint IS DISTINCT FROM NEW.event_hash OR
               e.document->>'format' IS DISTINCT FROM 'paper_delta_v1' OR
               e.document->>'state_hash' IS DISTINCT FROM NEW.state_hash OR
               (NEW.document->>'account_id')::uuid IS DISTINCT FROM NEW.account_id OR
               (NEW.document->>'version')::integer IS DISTINCT FROM NEW.version OR
               (NEW.document->>'as_of')::timestamptz IS DISTINCT FROM e.at THEN
                RAISE EXCEPTION 'invalid checkpoint anchor' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER checkpoint_anchor BEFORE INSERT ON aca.paper_checkpoints
        FOR EACH ROW EXECUTE FUNCTION aca.check_paper_checkpoint()
    """)
    op.execute("""
        CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE
        ON aca.paper_checkpoints FOR EACH STATEMENT
        EXECUTE FUNCTION aca.reject_history_mutation()
    """)
    op.execute("GRANT SELECT, INSERT ON aca.paper_checkpoints TO aca_runtime")
    op.create_index(
        "paper_event_journal_once",
        "paper_events",
        ["account_id", sa.text("(document->>'journal_id')")],
        unique=True,
        schema="aca",
        postgresql_where=sa.text(
            "document->>'format' = 'paper_delta_v1' AND document->>'journal_id' IS NOT NULL"
        ),
    )
    # Latest-by-channel bounded queries for the maintenance supervisor. Existing
    # public receipts remain the source evidence; no per-second duplicate capture.
    op.create_index(
        "market_receipts_channel_recent",
        "market_receipts",
        ["channel", sa.text("received_at DESC"), sa.text("id DESC")],
        schema="aca",
    )


def downgrade() -> None:
    raise RuntimeError("Checkpoint history is append-only; use a reviewed restore procedure")
