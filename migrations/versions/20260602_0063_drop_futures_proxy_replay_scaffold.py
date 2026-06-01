"""drop futures CFD-proxy replay scaffold columns

Revision ID: 20260602_0063
Revises: 20260530_0062
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260602_0063"
down_revision = "20260530_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_trades_account_proxy_status", table_name="trades")
    op.drop_column("trades", "proxy_replay_window_minutes")
    op.drop_column("trades", "proxy_replay_status")
    op.drop_column("trades", "proxy_replay_symbol")
    op.drop_column("futures_symbols", "proxy_cfd_symbol")


def downgrade() -> None:
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
    op.add_column(
        "futures_symbols",
        sa.Column("proxy_cfd_symbol", sa.String(length=32), nullable=True),
    )
