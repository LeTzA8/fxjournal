"""add weekly ai review two pass storage

Revision ID: 20260426_0051
Revises: 20260425_0050
Create Date: 2026-04-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260426_0051"
down_revision = "20260425_0050"
branch_labels = None
depends_on = None

TABLE_NAME = "ai_generated_responses"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column("pass_1_output", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("pass_2_output", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("prompt_version_pass_1", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("prompt_version_pass_2", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("model_used", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )

    op.execute(
        sa.text(
            """
            UPDATE ai_generated_responses
            SET
                pass_1_output = COALESCE(pass_1_output, response_text),
                model_used = COALESCE(model_used, model)
            """
        )
    )


def downgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column("created_at")
        batch_op.drop_column("model_used")
        batch_op.drop_column("prompt_version_pass_2")
        batch_op.drop_column("prompt_version_pass_1")
        batch_op.drop_column("pass_2_output")
        batch_op.drop_column("pass_1_output")
