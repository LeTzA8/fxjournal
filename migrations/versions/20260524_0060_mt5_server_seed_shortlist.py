"""add mt5 server seed shortlist

Revision ID: 20260524_0060
Revises: 20260524_0059
Create Date: 2026-05-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260524_0060"
down_revision = "20260524_0059"
branch_labels = None
depends_on = None

TABLE_NAME = "mt5_server_seed_shortlist"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_name", sa.String(length=100), nullable=False),
        sa.Column("server_key", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("first_failed_at", sa.DateTime(), nullable=False),
        sa.Column("last_failed_at", sa.DateTime(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_error_snippet", sa.Text(), nullable=True),
        sa.Column("last_mt5_account_id", sa.Integer(), nullable=True),
        sa.Column("seeded_at", sa.DateTime(), nullable=True),
        sa.Column("seeded_by_user_id", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["last_mt5_account_id"],
            ["mt5_account.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["seeded_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_mt5_server_seed_shortlist_server_key",
        TABLE_NAME,
        ["server_key"],
        unique=True,
    )
    op.create_index(
        "ix_mt5_server_seed_shortlist_status_last_failed",
        TABLE_NAME,
        ["status", "last_failed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_mt5_server_seed_shortlist_status_last_failed", table_name=TABLE_NAME)
    op.drop_index("ix_mt5_server_seed_shortlist_server_key", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
