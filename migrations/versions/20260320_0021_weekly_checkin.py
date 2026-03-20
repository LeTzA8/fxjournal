"""create weekly checkin table

Revision ID: 20260320_0021
Revises: 20260316_0020
Create Date: 2026-03-20 12:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260320_0021"
down_revision = "20260316_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weekly_checkin",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_account_id", sa.Integer(), nullable=False),
        sa.Column("week_start_utc", sa.DateTime(), nullable=False),
        sa.Column("week_end_utc", sa.DateTime(), nullable=False),
        sa.Column("emotional_state", sa.String(length=32), nullable=True),
        sa.Column("plan_adherence", sa.String(length=32), nullable=True),
        sa.Column("execution_quality", sa.String(length=32), nullable=True),
        sa.Column("additional_context", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["trade_account_id"], ["trade_accounts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_weekly_checkin_week_start_utc",
        "weekly_checkin",
        ["week_start_utc"],
        unique=False,
    )
    op.create_index("ix_weekly_checkin_user_id", "weekly_checkin", ["user_id"], unique=False)
    op.create_index(
        "ix_weekly_checkin_trade_account_id",
        "weekly_checkin",
        ["trade_account_id"],
        unique=False,
    )
    op.create_index(
        "uq_weekly_checkin_user_account_week_start",
        "weekly_checkin",
        ["user_id", "trade_account_id", "week_start_utc"],
        unique=True,
    )
    op.create_index("ix_weekly_checkin_created_at", "weekly_checkin", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_weekly_checkin_created_at", table_name="weekly_checkin")
    op.drop_index("uq_weekly_checkin_user_account_week_start", table_name="weekly_checkin")
    op.drop_index("ix_weekly_checkin_trade_account_id", table_name="weekly_checkin")
    op.drop_index("ix_weekly_checkin_user_id", table_name="weekly_checkin")
    op.drop_index("ix_weekly_checkin_week_start_utc", table_name="weekly_checkin")
    op.drop_table("weekly_checkin")
