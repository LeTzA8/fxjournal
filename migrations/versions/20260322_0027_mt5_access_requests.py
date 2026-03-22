"""add mt5 access request workflow

Revision ID: 20260322_0027
Revises: 20260322_0026
Create Date: 2026-03-22 23:59:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260322_0027"
down_revision = "20260322_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mt5_access_request",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_account_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("request_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trade_account_id"], ["trade_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mt5_access_request_user_id", "mt5_access_request", ["user_id"], unique=False)
    op.create_index(
        "ix_mt5_access_request_trade_account_id",
        "mt5_access_request",
        ["trade_account_id"],
        unique=False,
    )
    op.create_index("ix_mt5_access_request_status", "mt5_access_request", ["status"], unique=False)
    op.create_index(
        "ix_mt5_access_request_reviewed_by_user_id",
        "mt5_access_request",
        ["reviewed_by_user_id"],
        unique=False,
    )
    op.create_index("ix_mt5_access_request_created_at", "mt5_access_request", ["created_at"], unique=False)
    op.create_index(
        "ix_mt5_access_request_status_created",
        "mt5_access_request",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_mt5_access_request_pending_trade_account",
        "mt5_access_request",
        ["trade_account_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_mt5_access_request_pending_trade_account",
        table_name="mt5_access_request",
    )
    op.drop_index("ix_mt5_access_request_status_created", table_name="mt5_access_request")
    op.drop_index("ix_mt5_access_request_created_at", table_name="mt5_access_request")
    op.drop_index("ix_mt5_access_request_reviewed_by_user_id", table_name="mt5_access_request")
    op.drop_index("ix_mt5_access_request_status", table_name="mt5_access_request")
    op.drop_index("ix_mt5_access_request_trade_account_id", table_name="mt5_access_request")
    op.drop_index("ix_mt5_access_request_user_id", table_name="mt5_access_request")
    op.drop_table("mt5_access_request")
