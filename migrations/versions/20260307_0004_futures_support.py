"""add futures account support

Revision ID: 20260307_0004
Revises: 20260307_0003
Create Date: 2026-03-07 02:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0004"
down_revision = "20260307_0003"
branch_labels = None
depends_on = None


FUTURES_SYMBOLS = [
    ("ES", "", "E-mini S&P 500", "CME", 0.25, 12.5, "USD", 10, True),
    ("MES", "", "Micro E-mini S&P 500", "CME", 0.25, 1.25, "USD", 20, True),
    ("NQ", "", "E-mini Nasdaq-100", "CME", 0.25, 5.0, "USD", 30, True),
    ("MNQ", "", "Micro E-mini Nasdaq-100", "CME", 0.25, 0.5, "USD", 40, True),
    ("YM", "", "E-mini Dow", "CBOT", 1.0, 5.0, "USD", 50, True),
    ("MYM", "", "Micro E-mini Dow", "CBOT", 1.0, 0.5, "USD", 60, True),
    ("RTY", "", "E-mini Russell 2000", "CME", 0.1, 5.0, "USD", 70, True),
    ("M2K", "", "Micro E-mini Russell 2000", "CME", 0.1, 0.5, "USD", 80, True),
    ("CL", "", "Crude Oil", "NYMEX", 0.01, 10.0, "USD", 90, True),
    ("MCL", "", "Micro Crude Oil", "NYMEX", 0.01, 1.0, "USD", 100, True),
    ("GC", "", "Gold", "COMEX", 0.1, 10.0, "USD", 110, True),
    ("MGC", "", "Micro Gold", "COMEX", 0.1, 1.0, "USD", 120, True),
]


def upgrade() -> None:
    op.add_column(
        "trade_accounts",
        sa.Column("account_type", sa.String(length=16), nullable=False, server_default="CFD"),
    )
    op.add_column(
        "trades",
        sa.Column("contract_code", sa.String(length=24), nullable=True),
    )

    op.create_table(
        "futures_symbols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("root_symbol", sa.String(length=16), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("tick_size", sa.Float(), nullable=False),
        sa.Column("tick_value", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=16), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root_symbol"),
    )
    op.create_index(
        op.f("ix_futures_symbols_root_symbol"),
        "futures_symbols",
        ["root_symbol"],
        unique=False,
    )

    futures_symbols = sa.table(
        "futures_symbols",
        sa.column("root_symbol", sa.String(length=16)),
        sa.column("aliases", sa.Text()),
        sa.column("display_name", sa.String(length=120)),
        sa.column("exchange", sa.String(length=64)),
        sa.column("tick_size", sa.Float()),
        sa.column("tick_value", sa.Float()),
        sa.column("currency", sa.String(length=16)),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        futures_symbols,
        [
            {
                "root_symbol": root_symbol,
                "aliases": aliases or None,
                "display_name": display_name,
                "exchange": exchange,
                "tick_size": tick_size,
                "tick_value": tick_value,
                "currency": currency,
                "sort_order": sort_order,
                "is_active": is_active,
            }
            for root_symbol, aliases, display_name, exchange, tick_size, tick_value, currency, sort_order, is_active in FUTURES_SYMBOLS
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_futures_symbols_root_symbol"), table_name="futures_symbols")
    op.drop_table("futures_symbols")
    op.drop_column("trades", "contract_code")
    op.drop_column("trade_accounts", "account_type")
