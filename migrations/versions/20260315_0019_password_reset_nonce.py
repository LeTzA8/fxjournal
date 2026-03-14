"""add password reset nonce to users

Revision ID: 20260315_0019
Revises: 20260314_0018
Create Date: 2026-03-15 19:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260315_0019"
down_revision = "20260314_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("password_reset_nonce", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("password_reset_nonce")
