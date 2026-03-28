"""add trade flags and bundle pubkey

Revision ID: 20260329_0032
Revises: 20260328_0031
Create Date: 2026-03-29 00:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260329_0032"
down_revision = "20260328_0031"
branch_labels = None
depends_on = None


TABLE_NAME = "trades"
INDEX_NAME = "ix_trades_bundle_pubkey"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(
            sa.Column("is_corrective", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("is_reactive", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(sa.Column("bundle_pubkey", sa.String(length=24), nullable=True))
        batch_op.create_index(INDEX_NAME, ["bundle_pubkey"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_index(INDEX_NAME)
        batch_op.drop_column("bundle_pubkey")
        batch_op.drop_column("is_reactive")
        batch_op.drop_column("is_corrective")
