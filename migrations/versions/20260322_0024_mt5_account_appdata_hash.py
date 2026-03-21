"""
Revision ID: 20260322_0024
Revises: 20260321_0023
Create Date: 2026-03-22 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260322_0024"
down_revision = "20260321_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mt5_account") as batch_op:
        batch_op.add_column(sa.Column("appdata_hash", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mt5_account") as batch_op:
        batch_op.drop_column("appdata_hash")
