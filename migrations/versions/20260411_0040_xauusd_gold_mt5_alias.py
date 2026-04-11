"""cfd symbol XAUUSD: add GOLD alias for MT5 broker naming

Revision ID: 20260411_0040
Revises: 20260411_0038
Create Date: 2026-04-11 20:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260411_0040"
down_revision = "20260411_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text('SELECT aliases FROM "CFD_Symbols" WHERE symbol = :sym'),
        {"sym": "XAUUSD"},
    ).fetchone()
    if row is None:
        return
    current = (row[0] or "").strip()
    parts = [p.strip() for p in current.split(",") if p.strip()]
    normalized = {p.upper() for p in parts}
    if "GOLD" in normalized:
        return
    parts.append("GOLD")
    new_aliases = ",".join(parts)
    bind.execute(
        sa.text('UPDATE "CFD_Symbols" SET aliases = :aliases WHERE symbol = :sym'),
        {"aliases": new_aliases, "sym": "XAUUSD"},
    )


def downgrade() -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text('SELECT aliases FROM "CFD_Symbols" WHERE symbol = :sym'),
        {"sym": "XAUUSD"},
    ).fetchone()
    if row is None or row[0] is None:
        return
    parts = [p.strip() for p in str(row[0]).split(",") if p.strip() and p.strip().upper() != "GOLD"]
    new_val = ",".join(parts) if parts else None
    bind.execute(
        sa.text('UPDATE "CFD_Symbols" SET aliases = :aliases WHERE symbol = :sym'),
        {"aliases": new_val, "sym": "XAUUSD"},
    )
