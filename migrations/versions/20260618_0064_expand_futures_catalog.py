"""expand futures symbol catalog — energy, metals, FX, bonds, grains, vol, crypto

Revision ID: 20260618_0064
Revises: 20260602_0063
Create Date: 2026-06-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260618_0064"
down_revision = "20260602_0063"
branch_labels = None
depends_on = None


# (root_symbol, aliases, display_name, exchange, tick_size, tick_value, currency, sort_order, is_active)
# Tick specs are per-exchange minimums; displayed decimal places and snap grid derive from tick_size.
NEW_FUTURES_SYMBOLS = [
    # ── Energy ────────────────────────────────────────────────────────────────
    # NG  10,000 MMBtu  | $0.001/MMBtu  | $10.00/tick
    ("NG",  "NATGAS,NATURALGAS",       "Natural Gas",           "NYMEX", 0.001,       10.0,    "USD", 130, True),
    # HO  42,000 gal    | $0.0001/gal   | $4.20/tick
    ("HO",  "HEATINGOIL,ULSD",         "Heating Oil (NY ULSD)", "NYMEX", 0.0001,       4.2,    "USD", 140, True),
    # RB  42,000 gal    | $0.0001/gal   | $4.20/tick
    ("RB",  "GASOLINE,RBOB",           "RBOB Gasoline",         "NYMEX", 0.0001,       4.2,    "USD", 150, True),
    # BRN 1,000 bbl     | $0.01/bbl     | $10.00/tick
    ("BRN", "BRENT,BRENTOIL",          "Brent Crude Oil",       "ICE",   0.01,         10.0,   "USD", 160, True),

    # ── Metals ────────────────────────────────────────────────────────────────
    # SI  5,000 troy oz | $0.005/oz     | $25.00/tick
    ("SI",  "SILVER",                  "Silver",                "COMEX", 0.005,        25.0,   "USD", 170, True),
    # HG  25,000 lbs    | $0.0005/lb    | $12.50/tick
    ("HG",  "COPPER",                  "Copper",                "COMEX", 0.0005,       12.5,   "USD", 180, True),
    # PL  50 troy oz    | $0.10/oz      | $5.00/tick
    ("PL",  "PLATINUM",                "Platinum",              "NYMEX", 0.1,           5.0,   "USD", 190, True),
    # PA  100 troy oz   | $0.05/oz      | $5.00/tick
    ("PA",  "PALLADIUM",               "Palladium",             "NYMEX", 0.05,          5.0,   "USD", 200, True),

    # ── FX Futures (CME IMM) ───────────────────────────────────────────────────
    # 6E  125,000 EUR   | $0.00005/EUR  | $6.25/tick
    ("6E",  "EURO,EURUSD",             "Euro FX",               "CME",   0.00005,       6.25,  "USD", 210, True),
    # M6E 12,500 EUR    | $0.0001/EUR   | $1.25/tick
    ("M6E", "MICROEURO",               "Micro Euro FX",         "CME",   0.0001,        1.25,  "USD", 220, True),
    # 6B  62,500 GBP    | $0.0001/GBP   | $6.25/tick
    ("6B",  "GBPUSD,BPOUND",           "British Pound",         "CME",   0.0001,        6.25,  "USD", 230, True),
    # 6J  12,500,000 JPY | $0.0000005/JPY | $6.25/tick
    ("6J",  "YEN,JPYFUT",              "Japanese Yen",          "CME",   0.0000005,     6.25,  "USD", 240, True),
    # 6A  100,000 AUD   | $0.0001/AUD   | $10.00/tick
    ("6A",  "AUDFUT,AUDUSD",           "Australian Dollar",     "CME",   0.0001,       10.0,   "USD", 250, True),
    # 6C  100,000 CAD   | $0.00005/CAD  | $5.00/tick
    ("6C",  "CADFUT,CADUSD",           "Canadian Dollar",       "CME",   0.00005,       5.0,   "USD", 260, True),
    # 6S  125,000 CHF   | $0.0001/CHF   | $12.50/tick
    ("6S",  "CHFFUT,CHFUSD",           "Swiss Franc",           "CME",   0.0001,       12.5,   "USD", 270, True),
    # 6N  100,000 NZD   | $0.0001/NZD   | $10.00/tick
    ("6N",  "NZDFUT,NZDUSD",           "New Zealand Dollar",    "CME",   0.0001,       10.0,   "USD", 280, True),

    # ── Interest Rates / Bonds (CBOT) ─────────────────────────────────────────
    # ZN  $100,000 face | 1/64 pt       | $15.625/tick
    ("ZN",  "TENYEAR,10YR,10YEAR",     "10-Year T-Note",        "CBOT",  0.015625,     15.625, "USD", 290, True),
    # ZB  $100,000 face | 1/32 pt       | $31.25/tick
    ("ZB",  "TBOND,30YR,30YEAR",       "30-Year T-Bond",        "CBOT",  0.03125,      31.25,  "USD", 300, True),
    # ZF  $100,000 face | 1/128 pt      | $7.8125/tick
    ("ZF",  "FIVEYEAR,5YR,5YEAR",      "5-Year T-Note",         "CBOT",  0.0078125,     7.8125,"USD", 310, True),

    # ── Grains (CBOT) ─────────────────────────────────────────────────────────
    # ZC  5,000 bu      | ¼ cent/bu     | $12.50/tick
    ("ZC",  "CORN",                    "Corn",                  "CBOT",  0.25,         12.5,   "USD", 320, True),
    # ZW  5,000 bu      | ¼ cent/bu     | $12.50/tick
    ("ZW",  "WHEAT",                   "Wheat",                 "CBOT",  0.25,         12.5,   "USD", 330, True),
    # ZS  5,000 bu      | ¼ cent/bu     | $12.50/tick
    ("ZS",  "SOYBEANS,SOY",            "Soybeans",              "CBOT",  0.25,         12.5,   "USD", 340, True),
    # ZL  60,000 lbs    | $0.0001/lb    | $6.00/tick
    ("ZL",  "SOYBEANSOIL,BEANOIL",     "Soybean Oil",           "CBOT",  0.0001,        6.0,   "USD", 350, True),
    # ZM  100 short tons| $0.10/short ton | $10.00/tick
    ("ZM",  "SOYMEAL,BEANMEAL",        "Soybean Meal",          "CBOT",  0.1,          10.0,   "USD", 360, True),

    # ── Softs / Livestock (brief coverage) ────────────────────────────────────
    # KC  37,500 lbs    | $0.0005/lb    | $18.75/tick
    ("KC",  "COFFEE",                  "Coffee (Arabica)",      "ICE",   0.0005,       18.75,  "USD", 370, True),
    # SB  112,000 lbs   | $0.0001/lb    | $11.20/tick
    ("SB",  "SUGAR,SUGAR11",           "Sugar No. 11",          "ICE",   0.0001,       11.2,   "USD", 380, True),

    # ── Volatility ────────────────────────────────────────────────────────────
    # VX  $1,000 × VIX  | 0.05 VIX pts | $50.00/tick
    ("VX",  "VIX,VIXFUT",             "CBOE VIX",              "CFE",   0.05,         50.0,   "USD", 390, True),

    # ── Crypto Futures (CME) ───────────────────────────────────────────────────
    # BTC 5 BTC         | $5.00/BTC     | $25.00/tick
    ("BTC", "BITCOIN",                 "Bitcoin",               "CME",   5.0,          25.0,   "USD", 400, True),
    # MBT 0.1 BTC       | $5.00/BTC     | $0.50/tick
    ("MBT", "MICROBITCOIN",            "Micro Bitcoin",         "CME",   5.0,           0.5,   "USD", 410, True),
    # ETH 50 ETH        | $0.25/ETH     | $12.50/tick
    ("ETH", "ETHER,ETHEREUM",          "Ether",                 "CME",   0.25,         12.5,   "USD", 420, True),
    # MET 0.1 ETH       | $0.25/ETH     | $0.025/tick  (exact: $12.50 × 0.002)
    ("MET", "MICROETHER,MICROETH",     "Micro Ether",           "CME",   0.25,          0.025, "USD", 430, True),
]


def upgrade() -> None:
    futures_symbols = sa.table(
        "futures_symbols",
        sa.column("root_symbol", sa.String(length=16)),
        sa.column("aliases", sa.Text()),
        sa.column("display_name", sa.String(length=120)),
        sa.column("exchange", sa.String(length=64)),
        sa.column("tick_size", sa.Float()),
        sa.column("tick_value", sa.Float()),
        sa.column("currency", sa.String(length=16)),
        sa.column("sort_order", sa.Integer()),
        sa.column("is_active", sa.Boolean()),
    )
    op.bulk_insert(
        futures_symbols,
        [
            {
                "root_symbol": root_symbol,
                "aliases": aliases or None,
                "display_name": display_name,
                "exchange": exchange,
                "tick_size": tick_size,
                "tick_value": tick_value,
                "currency": currency,
                "sort_order": sort_order,
                "is_active": is_active,
            }
            for root_symbol, aliases, display_name, exchange, tick_size, tick_value, currency, sort_order, is_active in NEW_FUTURES_SYMBOLS
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    roots = [row[0] for row in NEW_FUTURES_SYMBOLS]
    conn.execute(
        sa.text("DELETE FROM futures_symbols WHERE root_symbol IN :roots"),
        {"roots": tuple(roots)},
    )
