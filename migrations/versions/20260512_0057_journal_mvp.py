"""Add admin conversational journal MVP tables

Revision ID: 20260512_0057
Revises: 20260510_0057
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260512_0057"
down_revision = "20260510_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journal_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_account_id", sa.Integer(), nullable=True),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_trade_pubkey", sa.String(length=64), nullable=True),
        sa.Column("scope_date", sa.Date(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("tags_json", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["trade_account_id"], ["trade_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_journal_sessions_scope_started", "journal_sessions", ["scope_type", "started_at"])
    op.create_index("ix_journal_sessions_started_at", "journal_sessions", ["started_at"])
    op.create_index("ix_journal_sessions_trade_account_id", "journal_sessions", ["trade_account_id"])
    op.create_index("ix_journal_sessions_user_id", "journal_sessions", ["user_id"])
    op.create_index("ix_journal_sessions_user_started", "journal_sessions", ["user_id", "started_at"])

    op.create_table(
        "journal_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model_used", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("citations_json", sa.Text(), nullable=True),
        sa.Column("feedback", sa.String(length=32), nullable=True),
        sa.Column("feedback_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["journal_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_journal_messages_created_at", "journal_messages", ["created_at"])
    op.create_index("ix_journal_messages_session_created", "journal_messages", ["session_id", "created_at"])
    op.create_index("ix_journal_messages_session_id", "journal_messages", ["session_id"])
    op.create_index("ix_journal_messages_user_id", "journal_messages", ["user_id"])
    op.create_index("ix_journal_messages_user_role_created", "journal_messages", ["user_id", "role", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_journal_messages_user_role_created", table_name="journal_messages")
    op.drop_index("ix_journal_messages_user_id", table_name="journal_messages")
    op.drop_index("ix_journal_messages_session_id", table_name="journal_messages")
    op.drop_index("ix_journal_messages_session_created", table_name="journal_messages")
    op.drop_index("ix_journal_messages_created_at", table_name="journal_messages")
    op.drop_table("journal_messages")

    op.drop_index("ix_journal_sessions_user_started", table_name="journal_sessions")
    op.drop_index("ix_journal_sessions_user_id", table_name="journal_sessions")
    op.drop_index("ix_journal_sessions_trade_account_id", table_name="journal_sessions")
    op.drop_index("ix_journal_sessions_started_at", table_name="journal_sessions")
    op.drop_index("ix_journal_sessions_scope_started", table_name="journal_sessions")
    op.drop_table("journal_sessions")
