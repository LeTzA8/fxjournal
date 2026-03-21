"""create mt5 account table

Revision ID: 20260321_0022
Revises: 20260320_0021
Create Date: 2026-03-21 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260321_0022"
down_revision = "20260320_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mt5_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_account_id", sa.Integer(), nullable=False),
        sa.Column("account_number", sa.String(length=50), nullable=False),
        sa.Column("investor_password_encrypted", sa.Text(), nullable=False),
        sa.Column("server", sa.String(length=100), nullable=False),
        sa.Column("terminal_path", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["trade_account_id"], ["trade_accounts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mt5_account_user_id", "mt5_account", ["user_id"], unique=False)
    op.create_index(
        "ix_mt5_account_trade_account_id",
        "mt5_account",
        ["trade_account_id"],
        unique=False,
    )
    op.create_index("ix_mt5_account_is_active", "mt5_account", ["is_active"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_mt5_account_is_active", table_name="mt5_account")
    op.drop_index("ix_mt5_account_trade_account_id", table_name="mt5_account")
    op.drop_index("ix_mt5_account_user_id", table_name="mt5_account")
    op.drop_table("mt5_account")
