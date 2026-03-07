"""seed cfd symbols

Revision ID: 20260307_0002
Revises: 20260307_0001
Create Date: 2026-03-07 00:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0002"
down_revision = "20260307_0001"
branch_labels = None
depends_on = None


CFD_SYMBOLS = [
    ("EURUSD", "EU", 100000.0, 0.0001, 10, True),
    ("GBPUSD", "GU", 100000.0, 0.0001, 20, True),
    ("USDJPY", "UJ", 100000.0, 0.01, 30, True),
    ("AUDUSD", "", 100000.0, 0.0001, 40, True),
    ("USDCAD", "", 100000.0, 0.0001, 50, True),
    ("USDCHF", "", 100000.0, 0.0001, 60, True),
    ("NZDUSD", "", 100000.0, 0.0001, 70, True),
    ("EURJPY", "", 100000.0, 0.01, 80, True),
    ("GBPJPY", "", 100000.0, 0.01, 90, True),
    ("AUDJPY", "", 100000.0, 0.01, 100, True),
    ("CADJPY", "", 100000.0, 0.01, 110, True),
    ("CHFJPY", "", 100000.0, 0.01, 120, True),
    ("NZDJPY", "", 100000.0, 0.01, 130, True),
    ("EURGBP", "", 100000.0, 0.0001, 140, True),
    ("EURCHF", "", 100000.0, 0.0001, 150, True),
    ("EURAUD", "", 100000.0, 0.0001, 160, True),
    ("EURNZD", "", 100000.0, 0.0001, 170, True),
    ("EURCAD", "", 100000.0, 0.0001, 180, True),
    ("GBPCHF", "", 100000.0, 0.0001, 190, True),
    ("GBPAUD", "", 100000.0, 0.0001, 200, True),
    ("GBPCAD", "", 100000.0, 0.0001, 210, True),
    ("GBPNZD", "", 100000.0, 0.0001, 220, True),
    ("AUDCAD", "", 100000.0, 0.0001, 230, True),
    ("AUDCHF", "", 100000.0, 0.0001, 240, True),
    ("AUDNZD", "", 100000.0, 0.0001, 250, True),
    ("CADCHF", "", 100000.0, 0.0001, 260, True),
    ("NZDCHF", "", 100000.0, 0.0001, 270, True),
    ("NZDCAD", "", 100000.0, 0.0001, 280, True),
    ("XAUUSD", "", 100.0, None, 290, True),
    ("XAGUSD", "", 5000.0, None, 300, True),
    ("US500", "SPX500,SP500,US500CASH,US500INDEX", 1.0, None, 310, True),
    ("NAS100", "US100,USTEC,NAS100CASH,NASDAQ100", 1.0, None, 320, True),
    ("US30", "DJ30,DJIA,WS30,US30CASH", 1.0, None, 330, True),
    ("GER40", "DE40,DAX40,GER30,DE30,DAX", 1.0, None, 340, True),
    ("UK100", "FTSE100,UKX,U100", 1.0, None, 350, True),
    ("FRA40", "CAC40,FR40", 1.0, None, 360, True),
    ("EU50", "STOXX50,EUSTX50,SX5E", 1.0, None, 370, True),
    ("JP225", "N225,NI225,JP225CASH", 1.0, None, 380, True),
    ("HK50", "HSI,HSI50,HK50CASH", 1.0, None, 390, True),
    ("CHN50", "CN50,CHINA50,CHINAA50,A50", 1.0, None, 400, True),
    ("AUS200", "AU200,ASX200", 1.0, None, 410, True),
    ("ESP35", "IBEX35,ES35", 1.0, None, 420, True),
    ("IT40", "ITA40", 1.0, None, 430, True),
]


def upgrade() -> None:
    cfd_symbols = sa.table(
        "CFD_Symbols",
        sa.column("symbol", sa.String(length=32)),
        sa.column("aliases", sa.Text()),
        sa.column("contract_size", sa.Float()),
        sa.column("pip_size", sa.Float()),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        cfd_symbols,
        [
            {
                "symbol": symbol,
                "aliases": aliases or None,
                "contract_size": contract_size,
                "pip_size": pip_size,
                "sort_order": sort_order,
                "is_active": is_active,
            }
            for symbol, aliases, contract_size, pip_size, sort_order, is_active in CFD_SYMBOLS
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for symbol, *_rest in CFD_SYMBOLS:
        bind.execute(
            sa.text('DELETE FROM "CFD_Symbols" WHERE symbol = :symbol'),
            {"symbol": symbol},
        )
