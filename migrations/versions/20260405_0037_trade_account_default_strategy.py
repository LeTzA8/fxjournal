"""trade account optional default strategy for import sync

Revision ID: 20260405_0037
Revises: 20260403_0036
Create Date: 2026-04-05 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260405_0037"
down_revision = "20260403_0036"
branch_labels = None
depends_on = None


TABLE = "trade_accounts"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.add_column(sa.Column("default_trade_profile_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            op.f("ix_trade_accounts_default_trade_profile_id"),
            ["default_trade_profile_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_trade_accounts_default_trade_profile_id_trade_profiles",
            "trade_profiles",
            ["default_trade_profile_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch_op:
        batch_op.drop_constraint(
            "fk_trade_accounts_default_trade_profile_id_trade_profiles",
            type_="foreignkey",
        )
        batch_op.drop_index(op.f("ix_trade_accounts_default_trade_profile_id"))
        batch_op.drop_column("default_trade_profile_id")
