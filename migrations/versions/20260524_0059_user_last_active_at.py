"""Add user last active timestamp

Revision ID: 20260524_0059
Revises: 20260517_0058
Create Date: 2026-05-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260524_0059"
down_revision = "20260517_0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_active_at", sa.DateTime(), nullable=True))
    op.create_index("ix_users_last_active_at", "users", ["last_active_at"])


def downgrade() -> None:
    op.drop_index("ix_users_last_active_at", table_name="users")
    op.drop_column("users", "last_active_at")
