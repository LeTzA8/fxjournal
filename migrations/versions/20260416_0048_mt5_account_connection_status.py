"""add connection_status and connection_error_message to mt5_account

Revision ID: 20260416_0048
Revises: 20260413_0047
Create Date: 2026-04-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260416_0048"
down_revision = "20260413_0047"
branch_labels = None
depends_on = None

TABLE_NAME = "mt5_account"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(
            sa.Column(
                "connection_status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
            )
        )
        batch_op.add_column(
            sa.Column("connection_error_message", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column("connection_error_message")
        batch_op.drop_column("connection_status")
