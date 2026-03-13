"""seed crypto cfd symbols

Revision ID: 20260314_0018
Revises: 20260312_0017
Create Date: 2026-03-14 23:10:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260314_0018"
down_revision = "20260312_0017"
branch_labels = None
depends_on = None


CRYPTO_CFD_SYMBOLS = [
    ("BTCUSD", "BTCUSDT,XBTUSD,XBTUSDT", 1.0, None, 440, True),
    ("ETHUSD", "ETHUSDT", 1.0, None, 450, True),
    ("SOLUSD", "SOLUSDT", 1.0, None, 460, True),
    ("XRPUSD", "XRPUSDT", 1.0, None, 470, True),
    ("ADAUSD", "ADAUSDT", 1.0, None, 480, True),
    ("DOGEUSD", "DOGEUSDT", 1.0, None, 490, True),
    ("LTCUSD", "LTCUSDT", 1.0, None, 500, True),
    ("BCHUSD", "BCHUSDT", 1.0, None, 510, True),
    ("BNBUSD", "BNBUSDT", 1.0, None, 520, True),
]


def upgrade() -> None:
    bind = op.get_bind()
    for symbol, aliases, contract_size, pip_size, sort_order, is_active in CRYPTO_CFD_SYMBOLS:
        params = {
            "symbol": symbol,
            "aliases": aliases or None,
            "contract_size": contract_size,
            "pip_size": pip_size,
            "sort_order": sort_order,
            "is_active": is_active,
        }
        result = bind.execute(
            sa.text(
                """
                UPDATE "CFD_Symbols"
                SET aliases = :aliases,
                    contract_size = :contract_size,
                    pip_size = :pip_size,
                    sort_order = :sort_order,
                    is_active = :is_active
                WHERE symbol = :symbol
                """
            ),
            params,
        )
        if result.rowcount == 0:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO "CFD_Symbols"
                        (symbol, aliases, contract_size, pip_size, sort_order, is_active)
                    VALUES
                        (:symbol, :aliases, :contract_size, :pip_size, :sort_order, :is_active)
                    """
                ),
                params,
            )


def downgrade() -> None:
    bind = op.get_bind()
    for symbol, *_rest in CRYPTO_CFD_SYMBOLS:
        bind.execute(
            sa.text('DELETE FROM "CFD_Symbols" WHERE symbol = :symbol'),
            {"symbol": symbol},
        )
