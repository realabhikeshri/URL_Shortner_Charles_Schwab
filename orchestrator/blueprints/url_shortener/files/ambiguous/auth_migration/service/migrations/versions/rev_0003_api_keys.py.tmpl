# lineage: REQ-AMBIGUOUS / implement.auth_migration (see runs/ for the decision trail)
"""Add api_keys and links.owner_key_id (additive, reversible).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_prefix", "api_keys", ["prefix"], unique=True)
    with op.batch_alter_table("links") as batch:
        batch.add_column(sa.Column("owner_key_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_links_owner_key_id", "api_keys", ["owner_key_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_index("ix_links_owner_key_id", ["owner_key_id"])


def downgrade() -> None:
    with op.batch_alter_table("links") as batch:
        batch.drop_index("ix_links_owner_key_id")
        batch.drop_constraint("fk_links_owner_key_id", type_="foreignkey")
        batch.drop_column("owner_key_id")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_table("api_keys")
