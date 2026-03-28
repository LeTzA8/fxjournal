"""add trade account bundle review state

Revision ID: 20260329_0033
Revises: 20260329_0032
Create Date: 2026-03-29 22:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260329_0033"
down_revision = "20260329_0032"
branch_labels = None
depends_on = None


TABLE_NAME = "trade_accounts"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column("bundle_review_requested_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("bundle_review_completed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column("bundle_review_completed_at")
        batch_op.drop_column("bundle_review_requested_at")
