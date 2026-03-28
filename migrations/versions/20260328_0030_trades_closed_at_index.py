"""add closed_at index for user trade account queries

Revision ID: 20260328_0030
Revises: 20260323_0029
Create Date: 2026-03-28 23:50:00
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "20260328_0030"
down_revision = "20260323_0029"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_trades_user_account_closed_at"
TABLE_NAME = "trades"
COLUMNS = ["user_id", "trade_account_id", "closed_at"]


def upgrade() -> None:
    op.create_index(INDEX_NAME, TABLE_NAME, COLUMNS, unique=False)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
