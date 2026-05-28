"""add qa_test_accounts sidecar for waitlist CTA fixtures

Revision ID: 20260528_0061
Revises: 20260524_0060
Create Date: 2026-05-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260528_0061"
down_revision = "20260524_0060"
branch_labels = None
depends_on = None

TABLE_NAME = "qa_test_accounts"


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("scenario_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("fixture_version", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("test_path", sa.String(length=255), nullable=True),
        sa.Column("expected_cta_json", sa.Text(), nullable=True),
        sa.Column("last_seeded_at", sa.DateTime(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_verified_result", sa.String(length=32), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", name="uq_qa_test_accounts_user_id"),
        sa.UniqueConstraint("scenario_key", name="uq_qa_test_accounts_scenario_key"),
    )
    op.create_index("ix_qa_test_accounts_user_id", TABLE_NAME, ["user_id"])
    op.create_index("ix_qa_test_accounts_scenario_key", TABLE_NAME, ["scenario_key"])


def downgrade() -> None:
    op.drop_index("ix_qa_test_accounts_scenario_key", table_name=TABLE_NAME)
    op.drop_index("ix_qa_test_accounts_user_id", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
