"""Separate decision cutoffs from publication time for concurrent capture."""

import sqlalchemy as sa
from alembic import op

revision = "0007_replay_cutoff"
down_revision = "0006_replay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An insertion timestamp, not a claim about the instant of transaction commit.
    # Pre-existing immutable rows are older than this migration's observation.
    op.add_column(
        "public_replay_events",
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        schema="aca",
    )


def downgrade() -> None:
    raise RuntimeError("Replay evidence is append-only; use a reviewed restore procedure")
