"""add weekly review chat messages

Revision ID: 20260426_0052
Revises: 20260426_0051
Create Date: 2026-04-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260426_0052"
down_revision = "20260426_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weekly_review_chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_account_id", sa.Integer(), nullable=False),
        sa.Column("ai_response_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("model_used", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["ai_response_id"], ["ai_generated_responses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trade_account_id"], ["trade_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_weekly_review_chat_messages_ai_response_id",
        "weekly_review_chat_messages",
        ["ai_response_id"],
    )
    op.create_index(
        "ix_weekly_review_chat_messages_created_at",
        "weekly_review_chat_messages",
        ["created_at"],
    )
    op.create_index(
        "ix_weekly_review_chat_messages_trade_account_id",
        "weekly_review_chat_messages",
        ["trade_account_id"],
    )
    op.create_index(
        "ix_weekly_review_chat_messages_user_id",
        "weekly_review_chat_messages",
        ["user_id"],
    )
    op.create_index(
        "ix_weekly_review_chat_review_created",
        "weekly_review_chat_messages",
        ["ai_response_id", "created_at"],
    )
    op.create_index(
        "ix_weekly_review_chat_user_account_review",
        "weekly_review_chat_messages",
        ["user_id", "trade_account_id", "ai_response_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_weekly_review_chat_user_account_review", table_name="weekly_review_chat_messages")
    op.drop_index("ix_weekly_review_chat_review_created", table_name="weekly_review_chat_messages")
    op.drop_index("ix_weekly_review_chat_messages_user_id", table_name="weekly_review_chat_messages")
    op.drop_index("ix_weekly_review_chat_messages_trade_account_id", table_name="weekly_review_chat_messages")
    op.drop_index("ix_weekly_review_chat_messages_created_at", table_name="weekly_review_chat_messages")
    op.drop_index("ix_weekly_review_chat_messages_ai_response_id", table_name="weekly_review_chat_messages")
    op.drop_table("weekly_review_chat_messages")
