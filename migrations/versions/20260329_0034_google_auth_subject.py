"""add google auth subject to users

Revision ID: 20260329_0034
Revises: 20260329_0033
Create Date: 2026-03-29 23:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260329_0034"
down_revision = "20260329_0033"
branch_labels = None
depends_on = None


TABLE_NAME = "users"
CONSTRAINT_NAME = "uq_users_google_sub"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column("google_sub", sa.String(length=255), nullable=True))
        batch_op.create_unique_constraint(CONSTRAINT_NAME, ["google_sub"])


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_constraint(CONSTRAINT_NAME, type_="unique")
        batch_op.drop_column("google_sub")
