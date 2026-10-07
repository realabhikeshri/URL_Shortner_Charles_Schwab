# lineage: REQ-BF-002 / implement.migration_0002 (see runs/ for the decision trail)
"""Add links.expires_at and the clicks table (additive, reversible).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("links") as batch:
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "clicks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "link_id",
            sa.Integer(),
            sa.ForeignKey("links.id", ondelete="CASCADE", name="fk_clicks_link_id"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("visitor_hash", sa.String(length=64), nullable=False),
        sa.Column("referrer_host", sa.String(length=255), nullable=True),
        sa.Column("ua_family", sa.String(length=32), nullable=False),
    )
    op.create_index("ix_clicks_link_day", "clicks", ["link_id", "day"])
    op.create_index("ix_clicks_occurred_at", "clicks", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_clicks_occurred_at", table_name="clicks")
    op.drop_index("ix_clicks_link_day", table_name="clicks")
    op.drop_table("clicks")
    with op.batch_alter_table("links") as batch:
        batch.drop_column("expires_at")
