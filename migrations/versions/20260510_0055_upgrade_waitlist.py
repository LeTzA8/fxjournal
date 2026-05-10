"""add upgrade waitlist entries table

Revision ID: 20260510_0055
Revises: 20260426_0054
Create Date: 2026-05-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260510_0055"
down_revision = "20260426_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "upgrade_waitlist_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("tier_intent", sa.String(length=32), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_upgrade_waitlist_entries_email",
        "upgrade_waitlist_entries",
        ["email"],
    )
    op.create_index(
        "ix_upgrade_waitlist_entries_user_id",
        "upgrade_waitlist_entries",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_upgrade_waitlist_entries_user_id", table_name="upgrade_waitlist_entries")
    op.drop_index("ix_upgrade_waitlist_entries_email", table_name="upgrade_waitlist_entries")
    op.drop_table("upgrade_waitlist_entries")
