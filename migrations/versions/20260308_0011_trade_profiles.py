"""add trade profiles and version links

Revision ID: 20260308_0011
Revises: 20260308_0010
Create Date: 2026-03-08 23:59:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260308_0011"
down_revision = "20260308_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trade_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pubkey", sa.String(length=24), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("current_version_number", sa.Integer(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pubkey"),
    )
    op.create_index(op.f("ix_trade_profiles_pubkey"), "trade_profiles", ["pubkey"], unique=False)
    op.create_index(op.f("ix_trade_profiles_user_id"), "trade_profiles", ["user_id"], unique=False)
    op.create_index("ix_trade_profiles_user_name", "trade_profiles", ["user_id", "name"], unique=False)

    op.create_table(
        "trade_profile_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trade_profile_id", sa.Integer(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["trade_profile_id"], ["trade_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trade_profile_id",
            "version_number",
            name="uq_trade_profile_versions_profile_version",
        ),
    )
    op.create_index(
        op.f("ix_trade_profile_versions_trade_profile_id"),
        "trade_profile_versions",
        ["trade_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_trade_profile_versions_profile_created",
        "trade_profile_versions",
        ["trade_profile_id", "created_at"],
        unique=False,
    )

    with op.batch_alter_table("trades") as batch_op:
        batch_op.add_column(sa.Column("trade_profile_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("trade_profile_version_id", sa.Integer(), nullable=True))
        batch_op.create_index(op.f("ix_trades_trade_profile_id"), ["trade_profile_id"], unique=False)
        batch_op.create_index(
            op.f("ix_trades_trade_profile_version_id"),
            ["trade_profile_version_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_trades_user_trade_profile",
            ["user_id", "trade_profile_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_trades_trade_profile_id_trade_profiles",
            "trade_profiles",
            ["trade_profile_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_trades_trade_profile_version_id_trade_profile_versions",
            "trade_profile_versions",
            ["trade_profile_version_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_constraint("fk_trades_trade_profile_version_id_trade_profile_versions", type_="foreignkey")
        batch_op.drop_constraint("fk_trades_trade_profile_id_trade_profiles", type_="foreignkey")
        batch_op.drop_index("ix_trades_user_trade_profile")
        batch_op.drop_index(op.f("ix_trades_trade_profile_version_id"))
        batch_op.drop_index(op.f("ix_trades_trade_profile_id"))
        batch_op.drop_column("trade_profile_version_id")
        batch_op.drop_column("trade_profile_id")

    op.drop_index("ix_trade_profile_versions_profile_created", table_name="trade_profile_versions")
    op.drop_index(op.f("ix_trade_profile_versions_trade_profile_id"), table_name="trade_profile_versions")
    op.drop_table("trade_profile_versions")

    op.drop_index("ix_trade_profiles_user_name", table_name="trade_profiles")
    op.drop_index(op.f("ix_trade_profiles_user_id"), table_name="trade_profiles")
    op.drop_index(op.f("ix_trade_profiles_pubkey"), table_name="trade_profiles")
    op.drop_table("trade_profiles")
