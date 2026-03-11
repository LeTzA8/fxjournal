"""enforce trade_account_id NOT NULL and drop redundant single-column indexes

Redundant indexes removed (covered by existing composite indexes):

  trades:
    ix_trades_user_id           -> ix_trades_user_trade_account (user_id, trade_account_id)
    ix_trades_trade_profile_id  -> ix_trades_user_trade_profile (user_id, trade_profile_id)

  trade_accounts:
    ix_trade_accounts_user_id   -> ix_trade_accounts_user_name (user_id, name)

Note: ix_trades_trade_account_id, ix_trades_mt5_position, ix_trades_import_signature
were never created in Postgres (Alembic skipped them), so they are not dropped here.

Revision ID: 20260311_0015
Revises: 20260310_0014
Create Date: 2026-03-11 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260311_0015"
down_revision = "20260310_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Reassign any orphan trades (trade_account_id IS NULL) to the
    #    user's default account before tightening the NOT NULL constraint.
    # ------------------------------------------------------------------
    bind = op.get_bind()

    trades = sa.table(
        "trades",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("trade_account_id", sa.Integer()),
    )
    trade_accounts = sa.table(
        "trade_accounts",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("is_default", sa.Boolean()),
    )

    orphan_rows = bind.execute(
        sa.select(trades.c.id, trades.c.user_id).where(
            trades.c.trade_account_id.is_(None)
        )
    ).fetchall()

    if orphan_rows:
        for user_id in {row.user_id for row in orphan_rows}:
            account = bind.execute(
                sa.select(trade_accounts.c.id)
                .where(
                    sa.and_(
                        trade_accounts.c.user_id == user_id,
                        trade_accounts.c.is_default == sa.true(),
                    )
                )
                .limit(1)
            ).first()

            if account is None:
                account = bind.execute(
                    sa.select(trade_accounts.c.id)
                    .where(trade_accounts.c.user_id == user_id)
                    .limit(1)
                ).first()

            if account is None:
                # No accounts exist for this user — skip and let the NOT NULL
                # constraint surface the problem rather than silently drop data.
                continue

            bind.execute(
                trades.update()
                .where(
                    sa.and_(
                        trades.c.user_id == user_id,
                        trades.c.trade_account_id.is_(None),
                    )
                )
                .values(trade_account_id=account.id)
            )

    # ------------------------------------------------------------------
    # 2. Enforce NOT NULL on trade_account_id.
    # ------------------------------------------------------------------
    with op.batch_alter_table("trades") as batch_op:
        batch_op.alter_column(
            "trade_account_id",
            existing_type=sa.Integer(),
            nullable=False,
        )

    # ------------------------------------------------------------------
    # 3. Drop redundant single-column indexes (covered by composites).
    # ------------------------------------------------------------------
    op.drop_index("ix_trades_user_id", table_name="trades")
    op.drop_index("ix_trades_trade_profile_id", table_name="trades")
    op.drop_index("ix_trade_accounts_user_id", table_name="trade_accounts")


def downgrade() -> None:
    # Revert NOT NULL → nullable.
    with op.batch_alter_table("trades") as batch_op:
        batch_op.alter_column(
            "trade_account_id",
            existing_type=sa.Integer(),
            nullable=True,
        )

    # Restore single-column indexes.
    op.create_index("ix_trades_user_id", "trades", ["user_id"], unique=False)
    op.create_index("ix_trades_trade_profile_id", "trades", ["trade_profile_id"], unique=False)
    op.create_index("ix_trade_accounts_user_id", "trade_accounts", ["user_id"], unique=False)
