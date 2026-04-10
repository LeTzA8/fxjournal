"""add trade_bars table for MT5 OHLC bar cache

Revision ID: 20260411_0038
Revises: 20260405_0037
Create Date: 2026-04-11 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260411_0038"
down_revision = "20260405_0037"
branch_labels = None
depends_on = None

TABLE = "trade_bars"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "trade_id",
            sa.Integer(),
            sa.ForeignKey("trades.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("bar_time", sa.Integer(), nullable=False),
        sa.Column("open", sa.Float(), nullable=False),
        sa.Column("high", sa.Float(), nullable=False),
        sa.Column("low", sa.Float(), nullable=False),
        sa.Column("close", sa.Float(), nullable=False),
        sa.Column("tick_volume", sa.Integer(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_trade_bars_trade_id", TABLE, ["trade_id"])
    op.create_index(
        "uq_trade_bars_trade_timeframe_bartime",
        TABLE,
        ["trade_id", "timeframe", "bar_time"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_trade_bars_trade_timeframe_bartime", table_name=TABLE)
    op.drop_index("ix_trade_bars_trade_id", table_name=TABLE)
    op.drop_table(TABLE)
