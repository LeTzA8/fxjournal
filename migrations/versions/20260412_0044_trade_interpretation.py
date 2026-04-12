"""move bundle and behavior flags to trade_interpretation + history

Revision ID: 20260412_0044
Revises: 20260412_0043
Create Date: 2026-04-12

Separates raw trade facts on ``trades`` from interpreted state
(bundle link, revenge/reactive/corrective flags). Append-only
``trade_interpretation_history`` records each change for audit and future tuning.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260412_0044"
down_revision = "20260412_0043"
branch_labels = None
depends_on = None

INDEX_BUNDLE = "ix_trades_bundle_pubkey"


def upgrade() -> None:
    op.create_table(
        "trade_interpretation",
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("bundle_pubkey", sa.String(length=24), nullable=True),
        sa.Column("is_revenge", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_reactive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_corrective", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("trade_id"),
    )
    op.create_index(
        "ix_trade_interpretation_bundle_pubkey",
        "trade_interpretation",
        ["bundle_pubkey"],
        unique=False,
    )

    op.create_table(
        "trade_interpretation_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_id", sa.Integer(), nullable=False),
        sa.Column("bundle_pubkey", sa.String(length=24), nullable=True),
        sa.Column("is_revenge", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_reactive", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_corrective", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["trade_id"], ["trades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_trade_interpretation_history_trade_id"),
        "trade_interpretation_history",
        ["trade_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_trade_interpretation_history_user_id"),
        "trade_interpretation_history",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_trade_interpretation_history_trade_id_created",
        "trade_interpretation_history",
        ["trade_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_trade_interpretation_history_created_at"),
        "trade_interpretation_history",
        ["created_at"],
        unique=False,
    )

    bind = op.get_bind()
    now_expr = "datetime('now')" if bind.dialect.name == "sqlite" else "CURRENT_TIMESTAMP"

    op.execute(
        sa.text(
            f"""
            INSERT INTO trade_interpretation (
                trade_id, bundle_pubkey, is_revenge, is_reactive, is_corrective, updated_at
            )
            SELECT
                id,
                NULLIF(TRIM(bundle_pubkey), ''),
                is_revenge,
                is_reactive,
                is_corrective,
                {now_expr}
            FROM trades
            WHERE
                (bundle_pubkey IS NOT NULL AND TRIM(bundle_pubkey) != '')
                OR is_revenge
                OR is_reactive
                OR is_corrective
            """
        )
    )

    op.execute(
        sa.text(
            """
            INSERT INTO trade_interpretation_history (
                trade_id, bundle_pubkey, is_revenge, is_reactive, is_corrective, source, user_id, created_at
            )
            SELECT
                trade_id,
                bundle_pubkey,
                is_revenge,
                is_reactive,
                is_corrective,
                'migration',
                NULL,
                updated_at
            FROM trade_interpretation
            """
        )
    )

    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_index(INDEX_BUNDLE)
        batch_op.drop_column("bundle_pubkey")
        batch_op.drop_column("is_reactive")
        batch_op.drop_column("is_corrective")
        batch_op.drop_column("is_revenge")


def downgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.add_column(
            sa.Column("is_revenge", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("is_corrective", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("is_reactive", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("bundle_pubkey", sa.String(length=24), nullable=True))
        batch_op.create_index(INDEX_BUNDLE, ["bundle_pubkey"], unique=False)
        batch_op.alter_column("is_revenge", server_default=None)
        batch_op.alter_column("is_corrective", server_default=None)
        batch_op.alter_column("is_reactive", server_default=None)

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute(
            sa.text(
                """
                UPDATE trades
                SET
                    bundle_pubkey = (
                        SELECT ti.bundle_pubkey FROM trade_interpretation ti
                        WHERE ti.trade_id = trades.id
                    ),
                    is_revenge = COALESCE((
                        SELECT ti.is_revenge FROM trade_interpretation ti
                        WHERE ti.trade_id = trades.id
                    ), 0),
                    is_reactive = COALESCE((
                        SELECT ti.is_reactive FROM trade_interpretation ti
                        WHERE ti.trade_id = trades.id
                    ), 0),
                    is_corrective = COALESCE((
                        SELECT ti.is_corrective FROM trade_interpretation ti
                        WHERE ti.trade_id = trades.id
                    ), 0)
                WHERE id IN (SELECT trade_id FROM trade_interpretation)
                """
            )
        )
    else:
        op.execute(
            sa.text(
                """
                UPDATE trades AS t
                SET
                    bundle_pubkey = ti.bundle_pubkey,
                    is_revenge = ti.is_revenge,
                    is_reactive = ti.is_reactive,
                    is_corrective = ti.is_corrective
                FROM trade_interpretation AS ti
                WHERE t.id = ti.trade_id
                """
            )
        )

    op.drop_index(op.f("ix_trade_interpretation_history_created_at"), table_name="trade_interpretation_history")
    op.drop_index("ix_trade_interpretation_history_trade_id_created", table_name="trade_interpretation_history")
    op.drop_index(op.f("ix_trade_interpretation_history_user_id"), table_name="trade_interpretation_history")
    op.drop_index(op.f("ix_trade_interpretation_history_trade_id"), table_name="trade_interpretation_history")
    op.drop_table("trade_interpretation_history")
    op.drop_index("ix_trade_interpretation_bundle_pubkey", table_name="trade_interpretation")
    op.drop_table("trade_interpretation")
