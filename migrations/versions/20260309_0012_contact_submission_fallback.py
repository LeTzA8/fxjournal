"""add contact submission fallback storage

Revision ID: 20260309_0012
Revises: 20260308_0011
Create Date: 2026-03-09 00:45:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260309_0012"
down_revision = "20260308_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_submissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("contact_email", sa.String(length=120), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=120), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("delivery_sent", sa.Boolean(), nullable=False),
        sa.Column("delivery_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_contact_submissions_delivery_sent_created",
        "contact_submissions",
        ["delivery_sent", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_contact_submissions_user_id"),
        "contact_submissions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_contact_submissions_delivery_sent"),
        "contact_submissions",
        ["delivery_sent"],
        unique=False,
    )
    op.create_index(
        op.f("ix_contact_submissions_created_at"),
        "contact_submissions",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_contact_submissions_created_at"), table_name="contact_submissions")
    op.drop_index(op.f("ix_contact_submissions_delivery_sent"), table_name="contact_submissions")
    op.drop_index(op.f("ix_contact_submissions_user_id"), table_name="contact_submissions")
    op.drop_index("ix_contact_submissions_delivery_sent_created", table_name="contact_submissions")
    op.drop_table("contact_submissions")
