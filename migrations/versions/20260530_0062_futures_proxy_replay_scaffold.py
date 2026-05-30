"""futures proxy replay scaffold — proxy fields on trades and futures_symbols

Revision ID: 20260530_0062
Revises: 20260528_0061
Create Date: 2026-05-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260530_0062"
down_revision = "20260528_0061"
branch_labels = None
depends_on = None

# Default futures-root → canonical CFD proxy symbol mapping.
# NULL proxy_cfd_symbol means no proxy is available for that root (e.g. ZB bonds).
_PROXY_SEED = [
    ("NQ",  "NAS100"),
    ("MNQ", "NAS100"),
    ("ES",  "US500"),
    ("MES", "US500"),
    ("YM",  "US30"),
    ("MYM", "US30"),
    ("RTY", "US2000"),
    ("M2K", "US2000"),
    ("GC",  "XAUUSD"),
    ("MGC", "XAUUSD"),
    ("CL",  "USOIL"),
    ("MCL", "USOIL"),
]


def upgrade() -> None:
    # --- trades: three new nullable columns ---
    op.add_column(
        "trades",
        sa.Column("proxy_replay_symbol", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "trades",
        sa.Column("proxy_replay_status", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "trades",
        sa.Column("proxy_replay_window_minutes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_trades_account_proxy_status",
        "trades",
        ["trade_account_id", "proxy_replay_status"],
    )

    # --- futures_symbols: one new nullable column + seed ---
    op.add_column(
        "futures_symbols",
        sa.Column("proxy_cfd_symbol", sa.String(length=32), nullable=True),
    )

    futures_symbols = sa.table(
        "futures_symbols",
        sa.column("root_symbol", sa.String),
        sa.column("proxy_cfd_symbol", sa.String),
    )
    for root, proxy in _PROXY_SEED:
        op.execute(
            futures_symbols.update()
            .where(futures_symbols.c.root_symbol == root)
            .values(proxy_cfd_symbol=proxy)
        )


def downgrade() -> None:
    op.drop_index("ix_trades_account_proxy_status", table_name="trades")
    op.drop_column("trades", "proxy_replay_window_minutes")
    op.drop_column("trades", "proxy_replay_status")
    op.drop_column("trades", "proxy_replay_symbol")
    op.drop_column("futures_symbols", "proxy_cfd_symbol")
