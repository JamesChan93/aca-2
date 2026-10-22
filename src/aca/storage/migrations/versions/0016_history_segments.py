"""Operator-only lossless storage and audited report summaries."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_history_segments"
down_revision = "0015_financial_audits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_history_certificates",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), nullable=False),
        sa.Column("number", sa.BigInteger, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint("number >= 0"),
        sa.CheckConstraint("octet_length(document::text) <= 2100000"),
        schema="aca",
    )
    op.create_table(
        "paper_history_blocks",
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("aca.paper_accounts.id"), primary_key=True),
        sa.Column("kind", sa.String(80), primary_key=True),
        sa.Column("first", sa.BigInteger, primary_key=True),
        sa.Column("last", sa.BigInteger, nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("document_digest", sa.String(64), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True)),
        sa.Column("last_at", sa.DateTime(timezone=True)),
        sa.Column("payload", sa.LargeBinary, nullable=False),
        sa.Column("document", JSONB, nullable=False),
        sa.CheckConstraint('"last" >= "first" AND count BETWEEN 1 AND 256'),
        sa.CheckConstraint("octet_length(payload) <= 16065536"),
        sa.CheckConstraint("octet_length(document::text) <= 2100000"),
        schema="aca",
    )
    op.create_index(
        "paper_history_calendar",
        "paper_history_blocks",
        ["account_id", "kind", "last_at", "first_at"],
        schema="aca",
    )
    for name in ("paper_history_certificates", "paper_history_blocks"):
        op.execute(
            f"CREATE TRIGGER reject_mutation BEFORE UPDATE OR DELETE OR TRUNCATE ON aca.{name} "
            "FOR EACH STATEMENT EXECUTE FUNCTION aca.reject_history_mutation()"
        )
        op.execute(f"REVOKE ALL ON aca.{name} FROM PUBLIC, aca_runtime")
        op.execute(f"GRANT SELECT ON aca.{name} TO aca_runtime")


def downgrade() -> None:
    raise RuntimeError("Lossless history blocks are permanent evidence; use a reviewed restore")
