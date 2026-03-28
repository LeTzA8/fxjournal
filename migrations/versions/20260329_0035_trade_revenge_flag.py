"""add trade revenge flag

Revision ID: 20260329_0035
Revises: 20260329_0034
Create Date: 2026-03-29 23:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260329_0035"
down_revision = "20260329_0034"
branch_labels = None
depends_on = None


TABLE_NAME = "trades"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(
            sa.Column("is_revenge", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.alter_column(
            "is_revenge",
            existing_type=sa.Boolean(),
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column("is_revenge")
