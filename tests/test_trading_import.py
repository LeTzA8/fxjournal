from datetime import datetime
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook

from helpers.core import (
    build_normalized_trade_insert_batch,
    build_trade_import_dedupe_key,
    create_trade_profile,
)
from models import FuturesSymbol, Trade, TradeAccount, User, db
from trading import (
    calculate_trade_net_pnl,
    clear_cfd_symbol_cache,
    parse_mt5_xlsx_stream,
    parse_topstep_csv_stream,
    parse_tradovate_csv_stream,
)


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


def test_parse_mt5_xlsx_separates_commission_and_swap():
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
            "Commission",
            "Swap",
            "Time",
            "Close Time",
        ]
    )
    sheet.append(
        [
            987654,
            "EURUSD",
            "sell",
            1.0,
            1.10000,
            1.09860,
            140.0,
            -3.5,
            -0.8,
            "2026-03-10 09:00:00",
            "2026-03-10 10:24:00",
        ]
    )

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    parsed, total, skipped = parse_mt5_xlsx_stream(buffer)

    assert total == 1
    assert skipped == 0
    assert len(parsed) == 1
    assert parsed[0]["commission"] == -3.5
    assert parsed[0]["swap"] == -0.8


def test_parse_mt5_xlsx_uses_explicit_timezone_offsets():
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
            "Time",
            "Close Time",
        ]
    )
    sheet.append(
        [
            112233,
            "EURUSD",
            "buy",
            1.0,
            1.10000,
            1.10100,
            "2026-03-10T09:00:00+02:00",
            "2026-03-10T11:00:00+02:00",
        ]
    )

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)

    parsed, total, skipped = parse_mt5_xlsx_stream(buffer)

    assert total == 1
    assert skipped == 0
    assert len(parsed) == 1
    assert parsed[0]["opened_at"] == datetime(2026, 3, 10, 7, 0, 0)
    assert parsed[0]["closed_at"] == datetime(2026, 3, 10, 9, 0, 0)
    assert parsed[0]["source_timezone"] == "UTC+02:00"


def test_parse_mt5_xlsx_does_not_assume_timezone_for_naive_timestamps():
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
            "Time",
            "Close Time",
        ]
    )
    sheet.append(
        [
            221133,
            "EURUSD",
            "buy",
            1.0,
            1.10000,
            1.10100,
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
    assert parsed[0]["opened_at"] is None
    assert parsed[0]["closed_at"] is None
    assert parsed[0]["source_timezone"] is None


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


def test_parse_topstep_csv_combines_fees_and_commissions_for_net_pnl():
    csv_bytes = BytesIO(
        (
            "Id,ContractName,EnteredAt,ExitedAt,EntryPrice,ExitPrice,Fees,Commissions,PnL,Size,Type\n"
            "2665990315,MESM6,06/01/2026 09:00:00 +08:00,06/01/2026 09:05:00 +08:00,"
            "5900.00,5916.50,2.96,2.00,330.00,4,Long\n"
        ).encode("utf-8")
    )

    parsed, total, skipped = parse_topstep_csv_stream(csv_bytes)

    assert total == 1
    assert skipped == 0
    assert len(parsed) == 1
    assert parsed[0]["symbol"] == "MES"
    assert parsed[0]["contract_code"] == "MESM6"
    assert parsed[0]["commission"] == 4.96
    assert calculate_trade_net_pnl(parsed[0]["pnl"], parsed[0]["commission"]) == 325.04


def test_futures_duplicate_import_refreshes_cost_fields(app_ctx):
    user = User(
        username="futures-cost-refresh-user",
        email="futures-cost-refresh@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Imported Futures",
        account_type="FUTURES",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()
    if FuturesSymbol.query.filter_by(root_symbol="MES").first() is None:
        db.session.add(
            FuturesSymbol(
                root_symbol="MES",
                tick_size=0.25,
                tick_value=5.0,
                display_name="MES",
                exchange="CME",
                currency="USD",
                sort_order=1,
                is_active=True,
            )
        )
    db.session.flush()
    clear_cfd_symbol_cache()

    opened_at = datetime(2026, 6, 1, 1, 0, 0)
    closed_at = datetime(2026, 6, 1, 1, 5, 0)
    existing_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="MES",
        contract_code="MESM6",
        side="BUY",
        entry_price=5900.0,
        exit_price=5916.5,
        lot_size=4.0,
        pnl=330.0,
        commission=2.96,
        opened_at=opened_at,
        closed_at=closed_at,
    )
    duplicate_existing_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="MES",
        contract_code="MESM6",
        side="BUY",
        entry_price=5900.0,
        exit_price=5916.5,
        lot_size=4.0,
        pnl=330.0,
        commission=2.96,
        opened_at=opened_at,
        closed_at=closed_at,
    )
    db.session.add(existing_trade)
    db.session.add(duplicate_existing_trade)
    db.session.commit()

    batch_result = build_normalized_trade_insert_batch(
        user_id=user.id,
        trade_account=trade_account,
        rows=[
            {
                "symbol": "MES",
                "contract_code": "MESM6",
                "side": "BUY",
                "entry_price": 5900.0,
                "exit_price": 5916.5,
                "lot_size": 4.0,
                "pnl": 330.0,
                "commission": 4.96,
                "opened_at": opened_at,
                "closed_at": closed_at,
            }
        ],
        import_signature="topstep_20260601_010500_abcd1234",
        default_system_trade_note="Imported from Topstep Export CSV",
    )

    assert batch_result["insert_batch"] == []
    assert batch_result["duplicate_count"] == 1
    assert batch_result["duplicate_cost_updates"] == 2
    assert existing_trade.commission == 4.96
    assert duplicate_existing_trade.commission == 4.96
    assert calculate_trade_net_pnl(existing_trade.pnl, existing_trade.commission) == 325.04


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


def test_build_normalized_trade_insert_batch_stores_import_notes_as_system_notes(app_ctx):
    user = User(
        username="import-system-note-user",
        email="import-system-note@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Imported CFD",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()

    batch_result = build_normalized_trade_insert_batch(
        user_id=user.id,
        trade_account=trade_account,
        rows=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 1.1000,
                "exit_price": 1.1010,
                "lot_size": 1.0,
                "pnl": 100.0,
                "opened_at": datetime(2026, 3, 10, 9, 0, 0),
                "closed_at": datetime(2026, 3, 10, 10, 0, 0),
                "mt5_position": "123456",
                "trade_note": "Broker export comment",
            }
        ],
        import_signature="mt5_20260310_100000_abcd1234",
        default_system_trade_note="Imported from MT5 Positions",
    )

    trade = batch_result["insert_batch"][0]

    assert trade.trade_note is None
    assert trade.system_trade_note == "Broker export comment"


def test_build_normalized_trade_insert_batch_uses_default_system_note_when_import_note_missing(app_ctx):
    user = User(
        username="import-default-note-user",
        email="import-default-note@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Imported CFD",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()

    batch_result = build_normalized_trade_insert_batch(
        user_id=user.id,
        trade_account=trade_account,
        rows=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 1.1000,
                "exit_price": 1.1010,
                "lot_size": 1.0,
                "pnl": 100.0,
                "opened_at": datetime(2026, 3, 10, 9, 0, 0),
                "closed_at": datetime(2026, 3, 10, 10, 0, 0),
                "mt5_position": "123456",
                "trade_note": "",
            }
        ],
        import_signature="mt5_20260310_100000_abcd1234",
        default_system_trade_note="Imported from MT5 Positions",
    )

    trade = batch_result["insert_batch"][0]

    assert trade.trade_note is None
    assert trade.system_trade_note == "Imported from MT5 Positions"


def test_build_normalized_trade_insert_batch_applies_account_default_strategy(app_ctx):
    user = User(
        username="import-default-strategy-user",
        email="import-default-strategy@example.com",
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    profile, version = create_trade_profile(user.id, "Default Playbook", "Test")
    db.session.commit()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Imported CFD",
        account_type="CFD",
        is_default=True,
        default_trade_profile_id=profile.id,
    )
    db.session.add(trade_account)
    db.session.commit()

    batch_result = build_normalized_trade_insert_batch(
        user_id=user.id,
        trade_account=trade_account,
        rows=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 1.1000,
                "exit_price": 1.1010,
                "lot_size": 1.0,
                "pnl": 100.0,
                "opened_at": datetime(2026, 3, 10, 9, 0, 0),
                "closed_at": datetime(2026, 3, 10, 10, 0, 0),
                "mt5_position": "123456",
                "trade_note": "",
            }
        ],
        import_signature="mt5_20260310_100000_abcd1234",
        default_system_trade_note="Imported from MT5 Positions",
    )

    trade = batch_result["insert_batch"][0]
    assert trade.trade_profile_id == profile.id
    assert trade.trade_profile_version_id == version.id


def test_build_normalized_trade_insert_batch_skips_default_strategy_from_other_user(app_ctx):
    owner = User(
        username="import-bad-default-owner",
        email="import-bad-default-owner@example.com",
        password="hashed-password",
    )
    other = User(
        username="import-bad-default-other",
        email="import-bad-default-other@example.com",
        password="hashed-password",
    )
    db.session.add_all([owner, other])
    db.session.flush()

    foreign_profile, _ = create_trade_profile(other.id, "Other user playbook", None)
    db.session.commit()

    trade_account = TradeAccount(
        user_id=owner.id,
        name="Imported CFD",
        account_type="CFD",
        is_default=True,
        default_trade_profile_id=foreign_profile.id,
    )
    db.session.add(trade_account)
    db.session.commit()

    batch_result = build_normalized_trade_insert_batch(
        user_id=owner.id,
        trade_account=trade_account,
        rows=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 1.1000,
                "exit_price": 1.1010,
                "lot_size": 1.0,
                "pnl": 100.0,
                "opened_at": datetime(2026, 3, 10, 9, 0, 0),
                "closed_at": datetime(2026, 3, 10, 10, 0, 0),
                "mt5_position": "223456",
                "trade_note": "",
            }
        ],
        import_signature="mt5_20260310_100000_abcd1234",
        default_system_trade_note="Imported from MT5 Positions",
    )

    trade = batch_result["insert_batch"][0]
    assert trade.trade_profile_id is None
    assert trade.trade_profile_version_id is None
