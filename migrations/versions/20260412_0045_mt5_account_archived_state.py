"""add archived state for mt5 accounts

Revision ID: 20260412_0045
Revises: 20260412_0044
Create Date: 2026-04-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260412_0045"
down_revision = "20260412_0044"
branch_labels = None
depends_on = None

TABLE_NAME = "mt5_account"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("archive_reason", sa.String(length=32), nullable=True))
        batch_op.create_index("ix_mt5_account_archived_at", ["archived_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_index("ix_mt5_account_archived_at")
        batch_op.drop_column("archive_reason")
        batch_op.drop_column("archived_at")
