"""preserve mt5 accounts as orphaned rows when parents are deleted

Revision ID: 20260322_0025
Revises: 20260322_0024
Create Date: 2026-03-22 23:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260322_0025"
down_revision = "20260322_0024"
branch_labels = None
depends_on = None


SQLITE_FK_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

MT5_FK_SPECS = (
    {
        "table": "mt5_account",
        "column": "user_id",
        "referred_table": "users",
        "postgres_name": "mt5_account_user_id_fkey",
    },
    {
        "table": "mt5_account",
        "column": "trade_account_id",
        "referred_table": "trade_accounts",
        "postgres_name": "mt5_account_trade_account_id_fkey",
    },
)


def _sqlite_fk_name(spec: dict[str, str]) -> str:
    return f"fk_{spec['table']}_{spec['column']}_{spec['referred_table']}"


def _get_postgres_fk_name(bind, table_name: str, column_name: str) -> str | None:
    return bind.execute(
        sa.text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
              AND tc.table_name = :table_name
              AND kcu.column_name = :column_name
            ORDER BY tc.constraint_name
            LIMIT 1
            """
        ),
        {
            "table_name": table_name,
            "column_name": column_name,
        },
    ).scalar()


def _replace_postgres_fk(bind, spec: dict[str, str], ondelete: str) -> None:
    existing_name = _get_postgres_fk_name(bind, spec["table"], spec["column"])
    constraint_name = existing_name or spec["postgres_name"]
    if existing_name:
        op.drop_constraint(existing_name, spec["table"], type_="foreignkey")

    op.create_foreign_key(
        constraint_name,
        spec["table"],
        spec["referred_table"],
        [spec["column"]],
        ["id"],
        ondelete=ondelete,
    )


def _replace_sqlite_mt5_fks(*, ondelete: str, nullable: bool) -> None:
    with op.batch_alter_table("mt5_account", naming_convention=SQLITE_FK_NAMING) as batch_op:
        for spec in MT5_FK_SPECS:
            batch_op.drop_constraint(_sqlite_fk_name(spec), type_="foreignkey")
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=nullable)
        batch_op.alter_column("trade_account_id", existing_type=sa.Integer(), nullable=nullable)
        for spec in MT5_FK_SPECS:
            batch_op.create_foreign_key(
                _sqlite_fk_name(spec),
                spec["referred_table"],
                [spec["column"]],
                ["id"],
                ondelete=ondelete,
            )


def _ensure_mt5_accounts_not_null_ready(bind) -> None:
    null_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM mt5_account
            WHERE user_id IS NULL
               OR trade_account_id IS NULL
            """
        )
    ).scalar_one()
    if null_count:
        raise RuntimeError(
            "Cannot downgrade mt5_account foreign keys while orphaned MT5 account rows still exist."
        )


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        _replace_sqlite_mt5_fks(ondelete="SET NULL", nullable=True)
        return

    op.alter_column("mt5_account", "user_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("mt5_account", "trade_account_id", existing_type=sa.Integer(), nullable=True)
    for spec in MT5_FK_SPECS:
        _replace_postgres_fk(bind, spec, ondelete="SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    _ensure_mt5_accounts_not_null_ready(bind)

    if bind.dialect.name == "sqlite":
        _replace_sqlite_mt5_fks(ondelete="CASCADE", nullable=False)
        return

    for spec in MT5_FK_SPECS:
        _replace_postgres_fk(bind, spec, ondelete="CASCADE")
    op.alter_column("mt5_account", "trade_account_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("mt5_account", "user_id", existing_type=sa.Integer(), nullable=False)
