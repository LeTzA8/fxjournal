"""insert missing CFD symbols from defaults and merge broker aliases

Revision ID: 20260412_0043
Revises: 20260412_0042
Create Date: 2026-04-12 18:00:00

Inserts any symbol in ``trading.DEFAULT_CFD_SYMBOL_SPECS`` not already in
``CFD_Symbols`` (including USOIL/UKOIL, expanded FX/metals/crypto/equities, etc.),
then merges default aliases into existing and newly inserted rows.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260412_0043"
down_revision = "20260412_0042"
branch_labels = None
depends_on = None


def _normalize_symbol_token(symbol):
    return "".join(ch for ch in (symbol or "").upper() if ch.isalnum())


def _merge_default_aliases_into_cfd_symbols(bind):
    from trading import DEFAULT_CFD_SYMBOL_SPECS

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


# Symbols this revision may insert (for downgrade). Kept explicit so we do not
# delete admin-added custom symbols that reuse these tickers.
_INSERTED_SYMBOLS = (
    "USOIL",
    "UKOIL",
    "USDMXN",
    "USDZAR",
    "USDTRY",
    "USDSEK",
    "USDNOK",
    "USDSGD",
    "USDHKD",
    "EURTRY",
    "GBPTRY",
    "EURPLN",
    "USDPLN",
    "USDHUF",
    "USDCNH",
    "DOTUSD",
    "LINKUSD",
    "XPTUSD",
    "XPDUSD",
    "XAUEUR",
    "XAUGBP",
    "XAUAUD",
    "XAGEUR",
    "XAGGBP",
    "XAGAUD",
    "XNGUSD",
    "USDX",
    "VIX",
    "SMI20",
    "COCOA",
    "COFFEE",
    "COTTON",
    "SUGAR",
    "COPPER",
    "ALUMINIUM",
    "NICKEL",
    "ZINC",
    "LEAD",
    "AAPL",
    "TSLA",
    "NVDA",
    "META",
    "AMZN",
    "MSFT",
    "GOOGL",
    "NFLX",
    "AMD",
    "BABA",
    "NIO",
    "COIN",
    "PLTR",
)


def upgrade() -> None:
    bind = op.get_bind()
    from trading import DEFAULT_CFD_SYMBOL_SPECS

    cfd = sa.table(
        "CFD_Symbols",
        sa.column("symbol", sa.String(length=32)),
        sa.column("aliases", sa.Text()),
        sa.column("contract_size", sa.Float()),
        sa.column("pip_size", sa.Float()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )
    for spec in DEFAULT_CFD_SYMBOL_SPECS:
        sym = spec["symbol"]
        exists = bind.execute(
            sa.text('SELECT 1 FROM "CFD_Symbols" WHERE symbol = :s'),
            {"s": sym},
        ).scalar()
        if exists:
            continue
        alias_parts = [str(a).strip().upper() for a in (spec.get("aliases") or ()) if str(a).strip()]
        aliases_text = ",".join(alias_parts) if alias_parts else None
        op.bulk_insert(
            cfd,
            [
                {
                    "symbol": sym,
                    "aliases": aliases_text,
                    "contract_size": float(spec["contract_size"]),
                    "pip_size": float(spec["pip_size"]) if spec.get("pip_size") is not None else None,
                    "sort_order": int(spec["sort_order"]),
                    "is_active": True,
                }
            ],
        )
    _merge_default_aliases_into_cfd_symbols(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for sym in _INSERTED_SYMBOLS:
        bind.execute(
            sa.text('DELETE FROM "CFD_Symbols" WHERE symbol = :s'),
            {"s": sym},
        )
