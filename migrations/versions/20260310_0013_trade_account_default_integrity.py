"""
Revision ID: 20260310_0013
Revises: 20260309_0012
Create Date: 2026-03-10 16:35:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260310_0013"
down_revision = "20260309_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicate_default_user_ids = [
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT user_id
                FROM trade_accounts
                WHERE is_default IS TRUE
                GROUP BY user_id
                HAVING COUNT(*) > 1
                """
            )
        ).fetchall()
    ]

    for user_id in duplicate_default_user_ids:
        default_rows = bind.execute(
            sa.text(
                """
                SELECT id
                FROM trade_accounts
                WHERE user_id = :user_id AND is_default IS TRUE
                ORDER BY id ASC
                """
            ),
            {"user_id": user_id},
        ).fetchall()
        keep_id = default_rows[0][0]
        bind.execute(
            sa.text(
                """
                UPDATE trade_accounts
                SET is_default = 0
                WHERE user_id = :user_id
                  AND is_default IS TRUE
                  AND id <> :keep_id
                """
            ),
            {"user_id": user_id, "keep_id": keep_id},
        )

    op.create_index(
        "uq_trade_accounts_one_default_per_user",
        "trade_accounts",
        ["user_id"],
        unique=True,
        sqlite_where=sa.text("is_default IS TRUE"),
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("uq_trade_accounts_one_default_per_user", table_name="trade_accounts")
