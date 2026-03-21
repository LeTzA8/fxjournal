"""apply foreign key delete rules across journal tables

Revision ID: 20260321_0023
Revises: 20260321_0022
Create Date: 2026-03-21 16:00:00
"""

from __future__ import annotations

from collections import defaultdict

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260321_0023"
down_revision = "20260321_0022"
branch_labels = None
depends_on = None


SQLITE_FK_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

CASCADE_FK_SPECS = (
    {
        "table": "trades",
        "column": "user_id",
        "referred_table": "users",
        "postgres_name": "trades_user_id_fkey",
    },
    {
        "table": "trades",
        "column": "trade_account_id",
        "referred_table": "trade_accounts",
        "postgres_name": "trades_trade_account_id_fkey",
    },
    {
        "table": "trade_accounts",
        "column": "user_id",
        "referred_table": "users",
        "postgres_name": "trade_accounts_user_id_fkey",
    },
    {
        "table": "trade_profiles",
        "column": "user_id",
        "referred_table": "users",
        "postgres_name": "trade_profiles_user_id_fkey",
    },
    {
        "table": "trade_profile_versions",
        "column": "trade_profile_id",
        "referred_table": "trade_profiles",
        "postgres_name": "trade_profile_versions_trade_profile_id_fkey",
    },
    {
        "table": "user_profile",
        "column": "user_id",
        "referred_table": "users",
        "postgres_name": "user_profile_user_id_fkey",
    },
    {
        "table": "weekly_checkin",
        "column": "user_id",
        "referred_table": "users",
        "postgres_name": "weekly_checkin_user_id_fkey",
    },
    {
        "table": "weekly_checkin",
        "column": "trade_account_id",
        "referred_table": "trade_accounts",
        "postgres_name": "weekly_checkin_trade_account_id_fkey",
    },
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

SET_NULL_FK_SPECS = (
    {
        "table": "ai_generated_responses",
        "column": "user_id",
        "referred_table": "users",
        "postgres_name": "ai_generated_responses_user_id_fkey",
    },
    {
        "table": "ai_generated_responses",
        "column": "trade_account_id",
        "referred_table": "trade_accounts",
        "postgres_name": "ai_generated_responses_trade_account_id_fkey",
    },
    {
        "table": "ai_generated_responses",
        "column": "prompt_history_id",
        "referred_table": "ai_prompt_history",
        "postgres_name": "ai_generated_responses_prompt_history_id_fkey",
    },
    {
        "table": "trades",
        "column": "trade_profile_id",
        "referred_table": "trade_profiles",
        "postgres_name": "fk_trades_trade_profile_id_trade_profiles",
    },
    {
        "table": "trades",
        "column": "trade_profile_version_id",
        "referred_table": "trade_profile_versions",
        "postgres_name": "fk_trades_trade_profile_version_id_trade_profile_versions",
    },
)


def _sqlite_fk_name(spec: dict[str, str]) -> str:
    return f"fk_{spec['table']}_{spec['column']}_{spec['referred_table']}"


def _group_fk_specs(*spec_groups: tuple[dict[str, str], ...]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for group in spec_groups:
        for spec in group:
            grouped[spec["table"]].append(spec)
    return grouped


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


def _replace_postgres_fk(bind, spec: dict[str, str], ondelete: str | None = None) -> None:
    table_name = spec["table"]
    existing_name = _get_postgres_fk_name(bind, table_name, spec["column"])
    constraint_name = existing_name or spec["postgres_name"]
    if existing_name:
        op.drop_constraint(existing_name, table_name, type_="foreignkey")

    kwargs = {}
    if ondelete is not None:
        kwargs["ondelete"] = ondelete

    op.create_foreign_key(
        constraint_name,
        table_name,
        spec["referred_table"],
        [spec["column"]],
        ["id"],
        **kwargs,
    )


def _replace_sqlite_fks(
    table_name: str,
    specs: list[dict[str, str]],
    ondelete: str | None = None,
    nullable_columns: dict[str, bool] | None = None,
) -> None:
    nullable_columns = nullable_columns or {}
    with op.batch_alter_table(table_name, naming_convention=SQLITE_FK_NAMING) as batch_op:
        for spec in specs:
            batch_op.drop_constraint(_sqlite_fk_name(spec), type_="foreignkey")
        for column_name, nullable in nullable_columns.items():
            batch_op.alter_column(
                column_name,
                existing_type=sa.Integer(),
                nullable=nullable,
            )
        for spec in specs:
            kwargs = {}
            if ondelete is not None:
                kwargs["ondelete"] = ondelete
            batch_op.create_foreign_key(
                _sqlite_fk_name(spec),
                spec["referred_table"],
                [spec["column"]],
                ["id"],
                **kwargs,
            )


def _ensure_ai_generated_responses_not_null_ready(bind) -> None:
    null_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM ai_generated_responses
            WHERE user_id IS NULL
               OR prompt_history_id IS NULL
            """
        )
    ).scalar_one()
    if null_count:
        raise RuntimeError(
            "Cannot downgrade ai_generated_responses foreign keys while anonymized rows "
            "still contain NULL user_id or prompt_history_id values."
        )


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        grouped_cascade_specs = _group_fk_specs(CASCADE_FK_SPECS)
        grouped_set_null_specs = _group_fk_specs(SET_NULL_FK_SPECS)
        cascade_table_order = (
            "trades",
            "trade_accounts",
            "trade_profiles",
            "trade_profile_versions",
            "user_profile",
            "weekly_checkin",
            "mt5_account",
        )
        for table_name in cascade_table_order:
            specs = grouped_cascade_specs.get(table_name, [])
            if specs:
                _replace_sqlite_fks(table_name, specs, ondelete="CASCADE")
        trade_set_null_specs = grouped_set_null_specs.get("trades", [])
        if trade_set_null_specs:
            _replace_sqlite_fks("trades", trade_set_null_specs, ondelete="SET NULL")
        _replace_sqlite_fks(
            "ai_generated_responses",
            grouped_set_null_specs.get("ai_generated_responses", []),
            ondelete="SET NULL",
            nullable_columns={
                "user_id": True,
                "prompt_history_id": True,
            },
        )
        return

    op.alter_column(
        "ai_generated_responses",
        "user_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.alter_column(
        "ai_generated_responses",
        "prompt_history_id",
        existing_type=sa.Integer(),
        nullable=True,
    )

    for spec in CASCADE_FK_SPECS:
        _replace_postgres_fk(bind, spec, ondelete="CASCADE")
    for spec in SET_NULL_FK_SPECS:
        _replace_postgres_fk(bind, spec, ondelete="SET NULL")


def downgrade() -> None:
    bind = op.get_bind()
    _ensure_ai_generated_responses_not_null_ready(bind)

    if bind.dialect.name == "sqlite":
        grouped_cascade_specs = _group_fk_specs(CASCADE_FK_SPECS)
        grouped_set_null_specs = _group_fk_specs(SET_NULL_FK_SPECS)
        cascade_table_order = (
            "trades",
            "trade_accounts",
            "trade_profiles",
            "trade_profile_versions",
            "user_profile",
            "weekly_checkin",
            "mt5_account",
        )
        for table_name in cascade_table_order:
            specs = grouped_cascade_specs.get(table_name, [])
            if specs:
                _replace_sqlite_fks(table_name, specs)
        trade_set_null_specs = grouped_set_null_specs.get("trades", [])
        if trade_set_null_specs:
            _replace_sqlite_fks("trades", trade_set_null_specs)
        _replace_sqlite_fks(
            "ai_generated_responses",
            grouped_set_null_specs.get("ai_generated_responses", []),
            nullable_columns={
                "user_id": False,
                "prompt_history_id": False,
            },
        )
        return

    for spec in CASCADE_FK_SPECS:
        _replace_postgres_fk(bind, spec)
    for spec in SET_NULL_FK_SPECS:
        _replace_postgres_fk(bind, spec)

    op.alter_column(
        "ai_generated_responses",
        "prompt_history_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column(
        "ai_generated_responses",
        "user_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
