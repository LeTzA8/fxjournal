"""baseline schema

Revision ID: 20260307_0001
Revises:
Create Date: 2026-03-07 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("password", sa.String(length=255), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("verification_sent_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "CFD_Symbols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("contract_size", sa.Float(), nullable=False),
        sa.Column("pip_size", sa.Float(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )
    op.create_index(op.f("ix_CFD_Symbols_symbol"), "CFD_Symbols", ["symbol"], unique=False)

    op.create_table(
        "trade_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("external_account_id", sa.String(length=80), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trade_accounts_user_id"), "trade_accounts", ["user_id"], unique=False)
    op.create_index(
        "ix_trade_accounts_user_name",
        "trade_accounts",
        ["user_id", "name"],
        unique=False,
    )
    op.create_index(
        "ix_trade_accounts_user_external",
        "trade_accounts",
        ["user_id", "external_account_id"],
        unique=False,
    )

    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_account_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("lot_size", sa.Float(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("mt5_position", sa.String(length=64), nullable=True),
        sa.Column("import_signature", sa.String(length=80), nullable=True),
        sa.Column("trade_note", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["trade_account_id"], ["trade_accounts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trades_user_id"), "trades", ["user_id"], unique=False)
    op.create_index(op.f("ix_trades_trade_account_id"), "trades", ["trade_account_id"], unique=False)
    op.create_index(op.f("ix_trades_mt5_position"), "trades", ["mt5_position"], unique=False)
    op.create_index(op.f("ix_trades_import_signature"), "trades", ["import_signature"], unique=False)
    op.create_index(
        "ix_trades_user_trade_account",
        "trades",
        ["user_id", "trade_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_trades_user_mt5_position",
        "trades",
        ["user_id", "mt5_position"],
        unique=False,
    )
    op.create_index(
        "ix_trades_user_import_signature",
        "trades",
        ["user_id", "import_signature"],
        unique=False,
    )
    op.create_index(
        "ix_trades_user_account_mt5_position",
        "trades",
        ["user_id", "trade_account_id", "mt5_position"],
        unique=False,
    )
    op.create_index(
        "ix_trades_user_account_import_signature",
        "trades",
        ["user_id", "trade_account_id", "import_signature"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_trades_user_account_import_signature", table_name="trades")
    op.drop_index("ix_trades_user_account_mt5_position", table_name="trades")
    op.drop_index("ix_trades_user_import_signature", table_name="trades")
    op.drop_index("ix_trades_user_mt5_position", table_name="trades")
    op.drop_index("ix_trades_user_trade_account", table_name="trades")
    op.drop_index(op.f("ix_trades_import_signature"), table_name="trades")
    op.drop_index(op.f("ix_trades_mt5_position"), table_name="trades")
    op.drop_index(op.f("ix_trades_trade_account_id"), table_name="trades")
    op.drop_index(op.f("ix_trades_user_id"), table_name="trades")
    op.drop_table("trades")

    op.drop_index("ix_trade_accounts_user_external", table_name="trade_accounts")
    op.drop_index("ix_trade_accounts_user_name", table_name="trade_accounts")
    op.drop_index(op.f("ix_trade_accounts_user_id"), table_name="trade_accounts")
    op.drop_table("trade_accounts")

    op.drop_index(op.f("ix_CFD_Symbols_symbol"), table_name="CFD_Symbols")
    op.drop_table("CFD_Symbols")

    op.drop_table("users")
