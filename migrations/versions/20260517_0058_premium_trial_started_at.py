"""Add shared premium trial start timestamp

Revision ID: 20260517_0058
Revises: 20260512_0057
Create Date: 2026-05-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260517_0058"
down_revision = "20260512_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("premium_trial_started_at", sa.DateTime(), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            """
            UPDATE users
            SET premium_trial_started_at = (
                SELECT MIN(mt5_account.mt5_trial_started_at)
                FROM mt5_account
                WHERE mt5_account.user_id = users.id
                  AND mt5_account.mt5_trial_started_at IS NOT NULL
            )
            WHERE premium_trial_started_at IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM mt5_account
                  WHERE mt5_account.user_id = users.id
                    AND mt5_account.mt5_trial_started_at IS NOT NULL
              )
            """
        )
    else:
        op.execute(
            """
            UPDATE users
            SET premium_trial_started_at = mt5_started.first_started_at
            FROM (
                SELECT user_id, MIN(mt5_trial_started_at) AS first_started_at
                FROM mt5_account
                WHERE mt5_trial_started_at IS NOT NULL
                GROUP BY user_id
            ) AS mt5_started
            WHERE users.id = mt5_started.user_id
              AND users.premium_trial_started_at IS NULL
            """
        )


def downgrade() -> None:
    op.drop_column("users", "premium_trial_started_at")
