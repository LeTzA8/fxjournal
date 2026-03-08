"""add signup controls and referral codes

Revision ID: 20260308_0008
Revises: 20260308_0007
Create Date: 2026-03-08 22:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0008"
down_revision = "20260308_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("signup_status", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("signup_code_used", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("approved_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("approved_by_user_id", sa.Integer(), nullable=True))
        batch_op.create_index(op.f("ix_users_signup_status"), ["signup_status"], unique=False)
        batch_op.create_index(op.f("ix_users_signup_code_used"), ["signup_code_used"], unique=False)
        batch_op.create_index(op.f("ix_users_approved_by_user_id"), ["approved_by_user_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_users_approved_by_user_id_users",
            "users",
            ["approved_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    bind = op.get_bind()
    users = sa.table(
        "users",
        sa.column("id", sa.Integer()),
        sa.column("signup_status", sa.String(length=16)),
        sa.column("approved_at", sa.DateTime()),
    )
    bind.execute(
        sa.update(users).values(
            signup_status="approved",
            approved_at=sa.func.current_timestamp(),
        )
    )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "signup_status",
            existing_type=sa.String(length=16),
            nullable=False,
            existing_nullable=True,
        )

    op.create_table(
        "signup_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(op.f("ix_signup_codes_code"), "signup_codes", ["code"], unique=True)
    op.create_index(
        op.f("ix_signup_codes_created_by_user_id"),
        "signup_codes",
        ["created_by_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_signup_codes_created_by_user_id"), table_name="signup_codes")
    op.drop_index(op.f("ix_signup_codes_code"), table_name="signup_codes")
    op.drop_table("signup_codes")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("fk_users_approved_by_user_id_users", type_="foreignkey")
        batch_op.drop_index(op.f("ix_users_approved_by_user_id"))
        batch_op.drop_index(op.f("ix_users_signup_code_used"))
        batch_op.drop_index(op.f("ix_users_signup_status"))
        batch_op.drop_column("approved_by_user_id")
        batch_op.drop_column("approved_at")
        batch_op.drop_column("signup_code_used")
        batch_op.drop_column("signup_status")
