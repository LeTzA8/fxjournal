"""add mt5 sync batch controls

Revision ID: 20260411_0039
Revises: 20260411_0040
Create Date: 2026-04-11 18:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260411_0039"
down_revision = "20260411_0040"
branch_labels = None
depends_on = None

BATCH_TABLE = "mt5_sync_batch"
REQUEST_TABLE = "mt5_access_request"


def upgrade() -> None:
    op.create_table(
        BATCH_TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("capacity_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_slots_claimed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_mt5_sync_batch_created_at", BATCH_TABLE, ["created_at"])
    op.create_index("ix_mt5_sync_batch_is_open", BATCH_TABLE, ["is_open"])
    op.create_index("ix_mt5_sync_batch_created_by_user_id", BATCH_TABLE, ["created_by_user_id"])
    op.create_index("ix_mt5_sync_batch_updated_by_user_id", BATCH_TABLE, ["updated_by_user_id"])
    op.create_index(
        "uq_mt5_sync_batch_one_open",
        BATCH_TABLE,
        ["is_open"],
        unique=True,
        sqlite_where=sa.text("is_open = 1"),
        postgresql_where=sa.text("is_open"),
    )

    op.add_column(
        REQUEST_TABLE,
        sa.Column(
            "batch_id",
            sa.Integer(),
            sa.ForeignKey("mt5_sync_batch.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_mt5_access_request_batch_id", REQUEST_TABLE, ["batch_id"])

    op.alter_column(BATCH_TABLE, "capacity_total", server_default=None)
    op.alter_column(BATCH_TABLE, "total_slots_claimed", server_default=None)
    op.alter_column(BATCH_TABLE, "is_open", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_mt5_access_request_batch_id", table_name=REQUEST_TABLE)
    op.drop_column(REQUEST_TABLE, "batch_id")

    op.drop_index("uq_mt5_sync_batch_one_open", table_name=BATCH_TABLE)
    op.drop_index("ix_mt5_sync_batch_updated_by_user_id", table_name=BATCH_TABLE)
    op.drop_index("ix_mt5_sync_batch_created_by_user_id", table_name=BATCH_TABLE)
    op.drop_index("ix_mt5_sync_batch_is_open", table_name=BATCH_TABLE)
    op.drop_index("ix_mt5_sync_batch_created_at", table_name=BATCH_TABLE)
    op.drop_table(BATCH_TABLE)
