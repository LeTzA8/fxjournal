"""create user profile onboarding table

Revision ID: 20260316_0020
Revises: 20260315_0019
Create Date: 2026-03-16 11:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260316_0020"
down_revision = "20260315_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trading_style", sa.String(length=50), nullable=True),
        sa.Column("instruments", sa.String(length=200), nullable=True),
        sa.Column("experience_level", sa.String(length=50), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_profile")
