"""add last_full_history_sync_at to mt5_account

Revision ID: 20260419_0049
Revises: 20260416_0048
Create Date: 2026-04-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260419_0049"
down_revision = "20260416_0048"
branch_labels = None
depends_on = None

TABLE_NAME = "mt5_account"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column("last_full_history_sync_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column("last_full_history_sync_at")
