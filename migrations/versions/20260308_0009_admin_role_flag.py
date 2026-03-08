"""add user admin flag

Revision ID: 20260308_0009
Revises: 20260308_0008
Create Date: 2026-03-08 23:40:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0009"
down_revision = "20260308_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("is_admin", sa.Boolean(), nullable=True))
        batch_op.create_index(op.f("ix_users_is_admin"), ["is_admin"], unique=False)

    bind = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.Integer()),
        sa.column("is_admin", sa.Boolean()),
    )
    bind.execute(sa.update(users).values(is_admin=False))

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "is_admin",
            existing_type=sa.Boolean(),
            nullable=False,
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index(op.f("ix_users_is_admin"))
        batch_op.drop_column("is_admin")
