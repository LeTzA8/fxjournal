"""index weekly_review_chat_messages for user role created_at

Revision ID: 20260426_0053
Revises: 20260426_0052
Create Date: 2026-04-26
"""

from __future__ import annotations

from alembic import op


revision = "20260426_0053"
down_revision = "20260426_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_weekly_review_chat_user_role_created",
        "weekly_review_chat_messages",
        ["user_id", "role", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_weekly_review_chat_user_role_created", table_name="weekly_review_chat_messages")
