"""add stop_loss, take_profit, commission to trades; timezone to users; drop trade_profiles.short_description

Revision ID: 20260311_0016
Revises: 20260311_0015
Create Date: 2026-03-11 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260311_0016"
down_revision = "20260311_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. New columns on trades.
    # ------------------------------------------------------------------
    with op.batch_alter_table("trades") as batch_op:
        batch_op.add_column(sa.Column("stop_loss", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("take_profit", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("commission", sa.Float(), nullable=True))

    # ------------------------------------------------------------------
    # 2. User-level timezone preference.
    # ------------------------------------------------------------------
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("timezone", sa.String(length=64), nullable=True))

    # ------------------------------------------------------------------
    # 3. Drop the denormalized short_description from trade_profiles.
    #    The canonical description lives in trade_profile_versions and is
    #    always kept in sync, so no data migration is required.
    # ------------------------------------------------------------------
    with op.batch_alter_table("trade_profiles") as batch_op:
        batch_op.drop_column("short_description")


def downgrade() -> None:
    with op.batch_alter_table("trade_profiles") as batch_op:
        batch_op.add_column(sa.Column("short_description", sa.Text(), nullable=True))

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("timezone")

    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_column("commission")
        batch_op.drop_column("take_profit")
        batch_op.drop_column("stop_loss")
