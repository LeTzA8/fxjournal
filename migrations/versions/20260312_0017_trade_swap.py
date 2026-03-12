"""add swap column to trades

Revision ID: 20260312_0017
Revises: 20260311_0016
Create Date: 2026-03-12 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260312_0017"
down_revision = "20260311_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.add_column(sa.Column("swap", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_column("swap")
