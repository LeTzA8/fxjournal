"""add mt5 vm sync alert tracking

Revision ID: 20260413_0046
Revises: 20260412_0045
Create Date: 2026-04-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260413_0046"
down_revision = "20260412_0045"
branch_labels = None
depends_on = None

TABLE_NAME = "mt5_account"
VM_STATE_TABLE = "mt5_sync_vm_state"


def upgrade() -> None:
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.add_column(sa.Column("vm_id", sa.String(length=64), nullable=True))
    op.create_table(
        VM_STATE_TABLE,
        sa.Column("vm_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column(
            "vm_alert_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table(VM_STATE_TABLE)
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_column("vm_id")
