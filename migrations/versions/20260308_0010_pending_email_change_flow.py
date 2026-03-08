"""add pending email change fields

Revision ID: 20260308_0010
Revises: 20260308_0009
Create Date: 2026-03-08 23:58:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0010"
down_revision = "20260308_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("created_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("pending_email", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("pending_email_change_requested_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("pending_email_change_current_verified_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("pending_email_change_new_verified_at", sa.DateTime(), nullable=True))
        batch_op.create_index(op.f("ix_users_pending_email"), ["pending_email"], unique=False)

    bind = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )
    bind.execute(sa.update(users).values(created_at=sa.func.current_timestamp()))

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "created_at",
            existing_type=sa.DateTime(),
            nullable=False,
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index(op.f("ix_users_pending_email"))
        batch_op.drop_column("pending_email_change_new_verified_at")
        batch_op.drop_column("pending_email_change_current_verified_at")
        batch_op.drop_column("pending_email_change_requested_at")
        batch_op.drop_column("pending_email")
        batch_op.drop_column("created_at")
