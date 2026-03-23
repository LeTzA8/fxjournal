"""store ai response payload snapshots

Revision ID: 20260323_0029
Revises: 20260322_0028
Create Date: 2026-03-23 19:15:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260323_0029"
down_revision = "20260322_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("ai_generated_responses") as batch_op:
            batch_op.add_column(sa.Column("payload_json", sa.Text(), nullable=True))
        return

    op.add_column("ai_generated_responses", sa.Column("payload_json", sa.Text(), nullable=True))


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("ai_generated_responses") as batch_op:
            batch_op.drop_column("payload_json")
        return

    op.drop_column("ai_generated_responses", "payload_json")
