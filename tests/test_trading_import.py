from datetime import datetime
from io import BytesIO

from openpyxl import Workbook

from helpers import build_trade_import_dedupe_key
from trading import parse_mt5_xlsx_stream, parse_tradovate_csv_stream


def test_parse_mt5_xlsx_single_trade():
    """
    Fixed workbook:
      Section title row: Positions
      One EURUSD BUY row with explicit entry/exit/profit values.

    Expected result:
      Parser should return exactly 1 parsed trade row and no skips.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Positions"])
    sheet.append(
        [
            "Position",
            "Symbol",
            "Type",
            "Volume",
            "Open Price",
            "Close Price",
            "Profit",
            "Time",
            "Close Time",
        ]
    )
    sheet.append(
        [
            123456,
            "EURUSD",
            "buy",
            1.0,
            1.10000,
            1.10500,
            500.0,
            "2026-03-10 09:00:00",
            "2026-03-10 11:00:00",
        ]
    )

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    parsed, total, skipped = parse_mt5_xlsx_stream(buffer)

    assert total == 1
    assert skipped == 0
    assert len(parsed) == 1
    assert parsed[0]["symbol"] == "EURUSD"
    assert parsed[0]["mt5_position"] == "123456"
    assert parsed[0]["side"] == "BUY"
    assert parsed[0]["entry_price"] == 1.1
    assert parsed[0]["exit_price"] == 1.105


def test_parse_tradovate_csv_single_trade():
    """
    Fixed CSV:
      One MES contract with buy then sell timestamps in order.

    Expected result:
      Parser should produce one BUY trade for root symbol MES
      and preserve contract code MESM26.
    """
    csv_bytes = BytesIO(
        (
            "symbol,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp\n"
            "MESM26,111,222,1,5000.00,5002.50,50.00,2026-03-10T14:00:00-05:00,2026-03-10T14:30:00-05:00\n"
        ).encode("utf-8")
    )

    parsed, total, skipped = parse_tradovate_csv_stream(csv_bytes)

    assert total == 1
    assert skipped == 0
    assert len(parsed) == 1
    assert parsed[0]["symbol"] == "MES"
    assert parsed[0]["contract_code"] == "MESM26"
    assert parsed[0]["side"] == "BUY"
    assert parsed[0]["lot_size"] == 1.0
    assert parsed[0]["entry_price"] == 5000.0
    assert parsed[0]["exit_price"] == 5002.5


def test_trade_import_dedupe_keys_are_stable_and_sensitive():
    """
    Fixed input:
      Two identical CFD import rows and one row with a different MT5 position.

    Expected result:
      Identical rows must produce the same dedupe key.
      Changing the MT5 position must change the dedupe key.
    """
    opened_at = datetime(2026, 3, 10, 9, 0, 0)
    closed_at = datetime(2026, 3, 10, 11, 0, 0)

    key_a = build_trade_import_dedupe_key(
        account_type="CFD",
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1050,
        lot_size=1.0,
        opened_at=opened_at,
        closed_at=closed_at,
        pnl=500.0,
        mt5_position="123456",
    )
    key_b = build_trade_import_dedupe_key(
        account_type="CFD",
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1050,
        lot_size=1.0,
        opened_at=opened_at,
        closed_at=closed_at,
        pnl=500.0,
        mt5_position="123456",
    )
    key_c = build_trade_import_dedupe_key(
        account_type="CFD",
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1050,
        lot_size=1.0,
        opened_at=opened_at,
        closed_at=closed_at,
        pnl=500.0,
        mt5_position="999999",
    )

    assert key_a == key_b
    assert key_a != key_c
