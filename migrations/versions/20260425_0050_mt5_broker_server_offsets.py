"""add mt5 broker server offset cache

Revision ID: 20260425_0050
Revises: 20260419_0049
Create Date: 2026-04-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260425_0050"
down_revision = "20260419_0049"
branch_labels = None
depends_on = None

TABLE_NAME = "mt5_broker_server_offset"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_name", sa.String(length=100), nullable=False),
        sa.Column("server_key", sa.String(length=100), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=False),
        sa.Column("probe_symbol", sa.String(length=64), nullable=True),
        sa.Column(
            "probed_at",
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
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_mt5_broker_server_offset_server_key",
        TABLE_NAME,
        ["server_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mt5_broker_server_offset_server_key", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
