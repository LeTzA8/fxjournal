"""add trade pubkeys, source timezone, and ai prompt history

Revision ID: 20260307_0006
Revises: 20260307_0005
Create Date: 2026-03-07 08:15:00
"""

from __future__ import annotations

from secrets import token_hex

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0006"
down_revision = "20260307_0005"
branch_labels = None
depends_on = None

TRADE_PUBKEY_BYTES = 12


def _generate_trade_pubkey(existing_pubkeys):
    while True:
        candidate = token_hex(TRADE_PUBKEY_BYTES)
        if candidate not in existing_pubkeys:
            existing_pubkeys.add(candidate)
            return candidate


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "trades",
        sa.Column("pubkey", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "trades",
        sa.Column("source_timezone", sa.String(length=64), nullable=True),
    )

    bind = op.get_bind()
    trades = sa.table(
        "trades",
        sa.column("id", sa.Integer()),
        sa.column("pubkey", sa.String(length=24)),
    )
    trade_rows = bind.execute(
        sa.select(trades.c.id, trades.c.pubkey)
    ).mappings().all()
    existing_pubkeys = {
        row["pubkey"]
        for row in trade_rows
        if row.get("pubkey")
    }
    for row in trade_rows:
        if row.get("pubkey"):
            continue
        bind.execute(
            sa.update(trades)
            .where(trades.c.id == row["id"])
            .values(pubkey=_generate_trade_pubkey(existing_pubkeys))
        )

    with op.batch_alter_table("trades") as batch_op:
        batch_op.alter_column(
            "pubkey",
            existing_type=sa.String(length=24),
            nullable=False,
        )
    op.create_index(op.f("ix_trades_pubkey"), "trades", ["pubkey"], unique=True)

    op.create_table(
        "ai_prompt_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prompt_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("source_path", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prompt_sha256"),
    )
    op.create_index(
        op.f("ix_ai_prompt_history_prompt_id"),
        "ai_prompt_history",
        ["prompt_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_prompt_history_prompt_sha256"),
        "ai_prompt_history",
        ["prompt_sha256"],
        unique=False,
    )

    op.create_table(
        "ai_generated_responses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("trade_account_id", sa.Integer(), nullable=True),
        sa.Column("prompt_history_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("trade_count_used", sa.Integer(), nullable=False),
        sa.Column("source_last_trade_id", sa.Integer(), nullable=True),
        sa.Column("period_start_utc", sa.DateTime(), nullable=True),
        sa.Column("period_end_utc", sa.DateTime(), nullable=True),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["prompt_history_id"], ["ai_prompt_history.id"]),
        sa.ForeignKeyConstraint(["trade_account_id"], ["trade_accounts.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ai_generated_responses_payload_hash"),
        "ai_generated_responses",
        ["payload_hash"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_generated_responses_prompt_history_id"),
        "ai_generated_responses",
        ["prompt_history_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_generated_responses_prompt_generated_at",
        "ai_generated_responses",
        ["prompt_history_id", "generated_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_generated_responses_trade_account_id"),
        "ai_generated_responses",
        ["trade_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_generated_responses_user_account_kind_generated_at",
        "ai_generated_responses",
        ["user_id", "trade_account_id", "kind", "generated_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_generated_responses_user_kind_generated_at",
        "ai_generated_responses",
        ["user_id", "kind", "generated_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_generated_responses_user_account_kind_period_start",
        "ai_generated_responses",
        ["user_id", "trade_account_id", "kind", "period_start_utc"],
        unique=False,
    )
    op.create_index(
        op.f("ix_ai_generated_responses_user_id"),
        "ai_generated_responses",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_generated_responses_user_id"), table_name="ai_generated_responses")
    op.drop_index(
        "ix_ai_generated_responses_user_kind_generated_at",
        table_name="ai_generated_responses",
    )
    op.drop_index(
        "ix_ai_generated_responses_user_account_kind_period_start",
        table_name="ai_generated_responses",
    )
    op.drop_index(
        "ix_ai_generated_responses_user_account_kind_generated_at",
        table_name="ai_generated_responses",
    )
    op.drop_index(
        op.f("ix_ai_generated_responses_trade_account_id"),
        table_name="ai_generated_responses",
    )
    op.drop_index(
        "ix_ai_generated_responses_prompt_generated_at",
        table_name="ai_generated_responses",
    )
    op.drop_index(
        op.f("ix_ai_generated_responses_prompt_history_id"),
        table_name="ai_generated_responses",
    )
    op.drop_index(
        op.f("ix_ai_generated_responses_payload_hash"),
        table_name="ai_generated_responses",
    )
    op.drop_table("ai_generated_responses")

    op.drop_index(op.f("ix_ai_prompt_history_prompt_sha256"), table_name="ai_prompt_history")
    op.drop_index(op.f("ix_ai_prompt_history_prompt_id"), table_name="ai_prompt_history")
    op.drop_table("ai_prompt_history")

    op.drop_index(op.f("ix_trades_pubkey"), table_name="trades")
    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_column("source_timezone")
        batch_op.drop_column("pubkey")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("last_login_at")
