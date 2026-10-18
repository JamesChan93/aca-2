"""Commit-ordered public journal, durable paper replay cursors and venue indexes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_replay"
down_revision = "0005_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "public_replay_events",
        sa.Column("number", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "receipt_id",
            sa.Uuid,
            sa.ForeignKey("aca.market_receipts.id"),
            unique=True,
            nullable=False,
        ),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint("number > 0", name="public_replay_number"),
        schema="aca",
    )
    op.create_table(
        "paper_replay_cursors",
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
        sa.Column("number", sa.BigInteger, primary_key=True),
        sa.Column("previous_hash", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint("number >= 0", name="paper_replay_cursor_number"),
        schema="aca",
    )
    for table in ("public_replay_events", "paper_replay_cursors"):
        op.execute(
            sa.text(
                "CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON aca.{table} FOR EACH STATEMENT "
                "EXECUTE FUNCTION aca.reject_history_mutation()"
            )
        )
        op.execute(sa.text(f"GRANT SELECT, INSERT ON aca.{table} TO aca_runtime"))
    op.execute("GRANT USAGE ON SEQUENCE aca.public_replay_events_number_seq TO aca_runtime")
    op.create_check_constraint(
        "paper_venue_known_kind",
        "paper_venue_events",
        "kind IN ('order','fill','market_book','market_trade')",
        schema="aca",
    )
    # Latest order versions and last book per instrument, without materialising
    # the public observation prefix on each venue command/snapshot.
    op.create_index(
        "paper_venue_order_heads",
        "paper_venue_events",
        ["account_id", sa.text("(document['intent']->>'client_id')"), sa.text("number DESC")],
        schema="aca",
        postgresql_where=sa.text("kind = 'order'"),
    )
    op.create_index(
        "paper_venue_books",
        "paper_venue_events",
        ["account_id", sa.text("(document->>'symbol')"), sa.text("number DESC")],
        schema="aca",
        postgresql_where=sa.text("kind = 'market_book'"),
    )
    op.create_index(
        "paper_venue_fills",
        "paper_venue_events",
        ["account_id", "number"],
        schema="aca",
        postgresql_where=sa.text("kind = 'fill'"),
    )


def downgrade() -> None:
    raise RuntimeError("Replay evidence is append-only; use a reviewed restore procedure")
