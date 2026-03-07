"""add trade account pubkeys

Revision ID: 20260308_0007
Revises: 20260307_0006
Create Date: 2026-03-08 16:10:00
"""

from __future__ import annotations

from secrets import token_hex

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0007"
down_revision = "20260307_0006"
branch_labels = None
depends_on = None

TRADE_ACCOUNT_PUBKEY_BYTES = 12


def _generate_trade_account_pubkey(existing_pubkeys):
    while True:
        candidate = token_hex(TRADE_ACCOUNT_PUBKEY_BYTES)
        if candidate not in existing_pubkeys:
            existing_pubkeys.add(candidate)
            return candidate


def upgrade() -> None:
    op.add_column(
        "trade_accounts",
        sa.Column("pubkey", sa.String(length=24), nullable=True),
    )

    bind = op.get_bind()
    trade_accounts = sa.table(
        "trade_accounts",
        sa.column("id", sa.Integer()),
        sa.column("pubkey", sa.String(length=24)),
    )
    account_rows = bind.execute(
        sa.select(trade_accounts.c.id, trade_accounts.c.pubkey)
    ).mappings().all()
    existing_pubkeys = {
        row["pubkey"]
        for row in account_rows
        if row.get("pubkey")
    }
    for row in account_rows:
        if row.get("pubkey"):
            continue
        bind.execute(
            sa.update(trade_accounts)
            .where(trade_accounts.c.id == row["id"])
            .values(pubkey=_generate_trade_account_pubkey(existing_pubkeys))
        )

    with op.batch_alter_table("trade_accounts") as batch_op:
        batch_op.alter_column(
            "pubkey",
            existing_type=sa.String(length=24),
            nullable=False,
        )

    op.create_index(
        op.f("ix_trade_accounts_pubkey"),
        "trade_accounts",
        ["pubkey"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_trade_accounts_pubkey"), table_name="trade_accounts")
    with op.batch_alter_table("trade_accounts") as batch_op:
        batch_op.drop_column("pubkey")
