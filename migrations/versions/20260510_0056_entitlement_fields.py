"""Add plan entitlement fields to users and MT5 trial fields to mt5_account

Adds plan_tier / plan_grandfathered to users for tiered access control,
and mt5_trial_started_at / sync_paused_at / sync_pause_reason to mt5_account
for MT5 trial lifecycle management.

Grandfathers existing users who already have an active MT5 sync so they
are not disrupted when billing eventually launches.

Revision ID: 20260510_0056
Revises: 20260510_0055
Create Date: 2026-05-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260510_0056"
down_revision = "20260510_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users: plan tier + grandfathered flag ─────────────────────────────────
    op.add_column(
        "users",
        sa.Column("plan_tier", sa.String(length=32), nullable=False, server_default="free"),
    )
    op.add_column(
        "users",
        sa.Column("plan_grandfathered", sa.Boolean(), nullable=False, server_default="false"),
    )

    # ── mt5_account: trial lifecycle fields ───────────────────────────────────
    op.add_column("mt5_account", sa.Column("mt5_trial_started_at", sa.DateTime(), nullable=True))
    op.add_column("mt5_account", sa.Column("sync_paused_at", sa.DateTime(), nullable=True))
    op.add_column("mt5_account", sa.Column("sync_pause_reason", sa.String(length=64), nullable=True))

    # ── Grandfather existing beta users ───────────────────────────────────────
    # Any user who has had at least one successful MT5 sync (last_synced_at IS NOT NULL)
    # is grandfathered — they will not see trial expiry before billing launches.
    op.execute(
        "UPDATE users SET plan_grandfathered = TRUE "
        "WHERE id IN ("
        "    SELECT DISTINCT user_id FROM mt5_account "
        "    WHERE last_synced_at IS NOT NULL AND user_id IS NOT NULL"
        ")"
    )


def downgrade() -> None:
    op.drop_column("mt5_account", "sync_pause_reason")
    op.drop_column("mt5_account", "sync_paused_at")
    op.drop_column("mt5_account", "mt5_trial_started_at")
    op.drop_column("users", "plan_grandfathered")
    op.drop_column("users", "plan_tier")
