"""scrub orphaned mt5 records and store mt5 consent metadata

Revision ID: 20260322_0028
Revises: 20260322_0027
Create Date: 2026-03-22 23:59:59
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260322_0028"
down_revision = "20260322_0027"
branch_labels = None
depends_on = None


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mask_account_number(account_number) -> str:
    text_value = str(account_number or "").strip()
    if text_value.startswith("cleanup-"):
        return text_value[:50]
    digits_only = "".join(character for character in text_value if character.isdigit())
    suffix = digits_only[-4:] if digits_only else (text_value[-4:] if text_value else "unknown")
    return f"cleanup-{suffix}"[:50]


def _scrub_existing_orphaned_mt5_rows(bind) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT id, account_number
            FROM mt5_account
            WHERE user_id IS NULL
               OR trade_account_id IS NULL
            """
        )
    ).mappings().all()
    if not rows:
        return

    marked_at = _utcnow_naive()
    for row in rows:
        bind.execute(
            sa.text(
                """
                UPDATE mt5_account
                SET user_id = NULL,
                    trade_account_id = NULL,
                    account_number = :account_number,
                    investor_password_encrypted = NULL,
                    is_active = :is_active,
                    cleanup_marked_at = COALESCE(cleanup_marked_at, :cleanup_marked_at),
                    mt5_consent_accepted_at = NULL,
                    mt5_consent_version = NULL
                WHERE id = :row_id
                """
            ),
            {
                "row_id": row["id"],
                "account_number": _mask_account_number(row["account_number"]),
                "is_active": False,
                "cleanup_marked_at": marked_at,
            },
        )


def _ensure_no_cleanup_only_mt5_rows(bind) -> None:
    cleanup_count = bind.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM mt5_account
            WHERE user_id IS NULL
               OR trade_account_id IS NULL
               OR investor_password_encrypted IS NULL
            """
        )
    ).scalar_one()
    if cleanup_count:
        raise RuntimeError(
            "Cannot downgrade while cleanup-only MT5 rows still exist. Delete them first."
        )


def upgrade() -> None:
    bind = op.get_bind()

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("mt5_account") as batch_op:
            batch_op.alter_column(
                "investor_password_encrypted",
                existing_type=sa.Text(),
                nullable=True,
            )
            batch_op.add_column(sa.Column("cleanup_marked_at", sa.DateTime(), nullable=True))
            batch_op.add_column(sa.Column("mt5_consent_accepted_at", sa.DateTime(), nullable=True))
            batch_op.add_column(sa.Column("mt5_consent_version", sa.String(length=32), nullable=True))
            batch_op.create_index(
                "ix_mt5_account_cleanup_marked_at",
                ["cleanup_marked_at"],
                unique=False,
            )
    else:
        op.alter_column(
            "mt5_account",
            "investor_password_encrypted",
            existing_type=sa.Text(),
            nullable=True,
        )
        op.add_column("mt5_account", sa.Column("cleanup_marked_at", sa.DateTime(), nullable=True))
        op.add_column("mt5_account", sa.Column("mt5_consent_accepted_at", sa.DateTime(), nullable=True))
        op.add_column("mt5_account", sa.Column("mt5_consent_version", sa.String(length=32), nullable=True))
        op.create_index(
            "ix_mt5_account_cleanup_marked_at",
            "mt5_account",
            ["cleanup_marked_at"],
            unique=False,
        )

    _scrub_existing_orphaned_mt5_rows(bind)


def downgrade() -> None:
    bind = op.get_bind()
    _ensure_no_cleanup_only_mt5_rows(bind)

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("mt5_account") as batch_op:
            batch_op.drop_index("ix_mt5_account_cleanup_marked_at")
            batch_op.drop_column("mt5_consent_version")
            batch_op.drop_column("mt5_consent_accepted_at")
            batch_op.drop_column("cleanup_marked_at")
            batch_op.alter_column(
                "investor_password_encrypted",
                existing_type=sa.Text(),
                nullable=False,
            )
        return

    op.drop_index("ix_mt5_account_cleanup_marked_at", table_name="mt5_account")
    op.drop_column("mt5_account", "mt5_consent_version")
    op.drop_column("mt5_account", "mt5_consent_accepted_at")
    op.drop_column("mt5_account", "cleanup_marked_at")
    op.alter_column(
        "mt5_account",
        "investor_password_encrypted",
        existing_type=sa.Text(),
        nullable=False,
    )
