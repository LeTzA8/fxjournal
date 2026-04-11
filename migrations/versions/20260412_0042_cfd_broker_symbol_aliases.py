"""merge expanded CFD broker symbol aliases into CFD_Symbols

Revision ID: 20260412_0042
Revises: 20260411_0039
Create Date: 2026-04-12 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260412_0042"
down_revision = "20260411_0039"
branch_labels = None
depends_on = None


def _normalize_symbol_token(symbol):
    return "".join(ch for ch in (symbol or "").upper() if ch.isalnum())


def upgrade() -> None:
    from trading import DEFAULT_CFD_SYMBOL_SPECS

    bind = op.get_bind()
    for spec in DEFAULT_CFD_SYMBOL_SPECS:
        sym = spec["symbol"]
        row = bind.execute(
            sa.text('SELECT aliases FROM "CFD_Symbols" WHERE symbol = :s'),
            {"s": sym},
        ).fetchone()
        if row is None:
            continue
        parts = [p.strip() for p in (row[0] or "").split(",") if p.strip()]
        norm_seen = {_normalize_symbol_token(p) for p in parts}
        norm_seen.add(_normalize_symbol_token(sym))
        for raw_alias in spec.get("aliases") or ():
            token = _normalize_symbol_token(raw_alias)
            if not token or token in norm_seen:
                continue
            parts.append(str(raw_alias).strip().upper())
            norm_seen.add(token)
        new_val = ",".join(parts) if parts else None
        bind.execute(
            sa.text('UPDATE "CFD_Symbols" SET aliases = :a WHERE symbol = :s'),
            {"a": new_val, "s": sym},
        )


def downgrade() -> None:
    pass
