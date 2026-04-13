"""account cash flows for deposits, withdrawals, adjustments

Revision ID: 20260413_0047
Revises: 20260413_0046
Create Date: 2026-04-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260413_0047"
down_revision = "20260413_0046"
branch_labels = None
depends_on = None

TABLE_NAME = "account_cash_flows"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "trade_account_id",
            sa.Integer(),
            sa.ForeignKey("trade_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("flow_type", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_account_cash_flows_account_occurred",
        TABLE_NAME,
        ["trade_account_id", "occurred_at"],
    )
    op.create_index(
        "ix_account_cash_flows_user_id",
        TABLE_NAME,
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_cash_flows_user_id", table_name=TABLE_NAME)
    op.drop_index("ix_account_cash_flows_account_occurred", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
