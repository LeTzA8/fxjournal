"""add ai response metadata json

Revision ID: 20260403_0036
Revises: 20260329_0035
Create Date: 2026-04-03 20:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260403_0036"
down_revision = "20260329_0035"
branch_labels = None
depends_on = None


TABLE_NAME = "ai_generated_responses"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column("response_meta_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column("response_meta_json")
