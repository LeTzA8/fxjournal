"""add trade account size

Revision ID: 20260307_0005
Revises: 20260307_0004
Create Date: 2026-03-07 05:25:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0005"
down_revision = "20260307_0004"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("trade_accounts", "account_size"):
        op.add_column(
            "trade_accounts",
            sa.Column("account_size", sa.Float(), nullable=True),
        )


def downgrade() -> None:
    if _has_column("trade_accounts", "account_size"):
        op.drop_column("trade_accounts", "account_size")
