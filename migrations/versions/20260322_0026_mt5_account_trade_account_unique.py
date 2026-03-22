"""enforce one mt5 account per trade account

Revision ID: 20260322_0026
Revises: 20260322_0025
Create Date: 2026-03-22 23:50:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260322_0026"
down_revision = "20260322_0025"
branch_labels = None
depends_on = None


UNIQUE_CONSTRAINT_NAME = "uq_mt5_account_trade_account_id"
TRADE_ACCOUNT_INDEX_NAME = "ix_mt5_account_trade_account_id"


def _find_duplicate_trade_account_ids(bind) -> list[int]:
    rows = bind.execute(
        sa.text(
            """
            SELECT trade_account_id
            FROM mt5_account
            WHERE trade_account_id IS NOT NULL
            GROUP BY trade_account_id
            HAVING COUNT(*) > 1
            ORDER BY trade_account_id
            """
        )
    ).fetchall()
    return [int(row[0]) for row in rows]


def upgrade() -> None:
    bind = op.get_bind()
    duplicate_trade_account_ids = _find_duplicate_trade_account_ids(bind)
    if duplicate_trade_account_ids:
        duplicate_list = ", ".join(str(value) for value in duplicate_trade_account_ids)
        raise RuntimeError(
            "Cannot enforce one MT5 account per trade account until duplicate MT5 rows are cleaned up "
            f"for trade_account_id(s): {duplicate_list}"
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("mt5_account") as batch_op:
            batch_op.drop_index(TRADE_ACCOUNT_INDEX_NAME)
            batch_op.create_unique_constraint(
                UNIQUE_CONSTRAINT_NAME,
                ["trade_account_id"],
            )
        return

    op.drop_index(TRADE_ACCOUNT_INDEX_NAME, table_name="mt5_account")
    op.create_unique_constraint(
        UNIQUE_CONSTRAINT_NAME,
        "mt5_account",
        ["trade_account_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("mt5_account") as batch_op:
            batch_op.drop_constraint(UNIQUE_CONSTRAINT_NAME, type_="unique")
            batch_op.create_index(
                TRADE_ACCOUNT_INDEX_NAME,
                ["trade_account_id"],
                unique=False,
            )
        return

    op.drop_constraint(UNIQUE_CONSTRAINT_NAME, "mt5_account", type_="unique")
    op.create_index(TRADE_ACCOUNT_INDEX_NAME, "mt5_account", ["trade_account_id"], unique=False)
