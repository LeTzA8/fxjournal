"""
Revision ID: 20260310_0014
Revises: 20260310_0013
Create Date: 2026-03-10 17:10:00
"""

from __future__ import annotations

import hashlib

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260310_0014"
down_revision = "20260310_0013"
branch_labels = None
depends_on = None


def _quantize(value, digits=8):
    if value is None:
        return None
    return round(float(value), digits)


def _build_import_dedupe_key(row):
    mt5_position = (row.mt5_position or "").strip()
    if mt5_position:
        payload = f"cfd:{mt5_position}"
    else:
        duplicate_key = (
            (row.contract_code or row.symbol or "").strip().upper(),
            (row.side or "").strip().upper(),
            _quantize(row.entry_price),
            _quantize(row.exit_price),
            _quantize(row.lot_size),
            _quantize(row.pnl, digits=2),
            row.opened_at.isoformat() if row.opened_at else None,
            row.closed_at.isoformat() if row.closed_at else None,
        )
        payload = "futures:" + repr(duplicate_key)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    with op.batch_alter_table("trades") as batch_op:
        batch_op.add_column(sa.Column("import_dedupe_key", sa.String(length=64), nullable=True))
        batch_op.create_index(op.f("ix_trades_import_dedupe_key"), ["import_dedupe_key"], unique=False)

    bind = op.get_bind()
    metadata = sa.MetaData()
    trades = sa.Table(
        "trades",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer()),
        sa.Column("trade_account_id", sa.Integer()),
        sa.Column("symbol", sa.String(length=20)),
        sa.Column("contract_code", sa.String(length=24)),
        sa.Column("side", sa.String(length=10)),
        sa.Column("entry_price", sa.Float()),
        sa.Column("exit_price", sa.Float()),
        sa.Column("lot_size", sa.Float()),
        sa.Column("pnl", sa.Float()),
        sa.Column("mt5_position", sa.String(length=64)),
        sa.Column("import_signature", sa.String(length=80)),
        sa.Column("opened_at", sa.DateTime()),
        sa.Column("closed_at", sa.DateTime()),
        sa.Column("import_dedupe_key", sa.String(length=64)),
    )

    imported_rows = bind.execute(
        sa.select(
            trades.c.id,
            trades.c.user_id,
            trades.c.trade_account_id,
            trades.c.symbol,
            trades.c.contract_code,
            trades.c.side,
            trades.c.entry_price,
            trades.c.exit_price,
            trades.c.lot_size,
            trades.c.pnl,
            trades.c.mt5_position,
            trades.c.import_signature,
            trades.c.opened_at,
            trades.c.closed_at,
        ).where(
            sa.or_(
                trades.c.import_signature.isnot(None),
                trades.c.mt5_position.isnot(None),
            )
        ).order_by(trades.c.id.asc())
    ).fetchall()

    seen_keys = set()
    for row in imported_rows:
        dedupe_key = _build_import_dedupe_key(row)
        scoped_key = (row.user_id, row.trade_account_id, dedupe_key)
        stored_key = dedupe_key
        if scoped_key in seen_keys:
            stored_key = hashlib.sha256(f"{dedupe_key}:legacydup:{row.id}".encode("utf-8")).hexdigest()
        else:
            seen_keys.add(scoped_key)

        bind.execute(
            trades.update()
            .where(trades.c.id == row.id)
            .values(import_dedupe_key=stored_key)
        )

    op.create_index(
        "uq_trades_user_account_import_dedupe",
        "trades",
        ["user_id", "trade_account_id", "import_dedupe_key"],
        unique=True,
        sqlite_where=sa.text("import_dedupe_key IS NOT NULL"),
        postgresql_where=sa.text("import_dedupe_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_trades_user_account_import_dedupe", table_name="trades")
    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_index(op.f("ix_trades_import_dedupe_key"))
        batch_op.drop_column("import_dedupe_key")
