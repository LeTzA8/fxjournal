from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import trading
from trading import (
    aggregate_ohlc_bars,
    build_rr_summary,
    build_trade_analytics,
    calc_pnl_values,
    derive_exit_price,
    resolve_pips,
    resolve_ticks,
)


def make_trade(**overrides):
    trade_account = SimpleNamespace(account_type=overrides.pop("account_type", "CFD"))
    base = {
        "symbol": "EURUSD",
        "side": "BUY",
        "entry_price": 1.10000,
        "exit_price": 1.10500,
        "stop_loss": None,
        "take_profit": None,
        "lot_size": 1.0,
        "contract_code": None,
        "trade_account": trade_account,
        "pnl": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_calc_pnl_eurusd_buy():
    """
    Fixed input:
      Symbol: EURUSD
      Side: BUY
      Entry: 1.10000, Exit: 1.10500
      Lot size: 1.0

    Expected result:
      Price moved 0.00500 in our favor.
      PnL = 0.00500 * 100,000 = 500.00 USD
    """
    result = calc_pnl_values("EURUSD", "BUY", 1.10000, 1.10500, 1.0)
    assert result == pytest.approx(500.0)


def test_calc_pnl_usdjpy_sell():
    """
    Fixed input:
      Symbol: USDJPY
      Side: SELL
      Entry: 150.00, Exit: 149.00
      Lot size: 1.0

    Expected result:
      We sold high and bought back lower by 1.00 JPY.
      USDJPY quote conversion uses 1 / exit_price = 1 / 149.
      PnL = 1.00 * 100,000 * (1/149) = 671.14094...
    """
    result = calc_pnl_values("USDJPY", "SELL", 150.00, 149.00, 1.0)
    assert round(result, 5) == 671.14094


def test_calc_pnl_xauusd_buy():
    """
    Fixed input:
      Symbol: XAUUSD
      Side: BUY
      Entry: 2000.0, Exit: 2010.0
      Lot size: 1.0

    Expected result:
      Gold contract size in this app is 100.
      PnL = 10.0 * 100 = 1000.0 USD
    """
    result = calc_pnl_values("XAUUSD", "BUY", 2000.0, 2010.0, 1.0)
    assert result == 1000.0


def test_calc_pnl_xagusd_buy_confirms_metal_support():
    """
    Fixed input:
      Symbol: XAGUSD
      Side: BUY
      Entry: 25.0, Exit: 26.0
      Lot size: 1.0

    Expected result:
      Silver contract size in this app is 5,000.
      PnL = 1.0 * 5,000 = 5,000.0 USD
    """
    result = calc_pnl_values("XAGUSD", "BUY", 25.0, 26.0, 1.0)
    assert result == 5000.0


def test_calc_pnl_btcusd_buy():
    """
    Fixed input:
      Symbol: BTCUSD
      Side: BUY
      Entry: 60000.0, Exit: 61000.0
      Lot size: 1.0

    Expected result:
      Crypto CFD support uses a 1.0 contract size.
      PnL = 1000.0 USD
    """
    result = calc_pnl_values("BTCUSD", "BUY", 60000.0, 61000.0, 1.0)
    assert result == 1000.0


def test_calc_pnl_mes_buy_requires_seeded_futures_symbol(monkeypatch):
    """
    Fixed input:
      Contract: MESM26
      Side: BUY
      Entry: 5000.00, Exit: 5002.50
      Lot size: 1

    Expected result:
      MES tick size = 0.25, tick value = 5.00.
      Price moved 2.50 points = 10 ticks.
      PnL = 10 * 5 = 50.0 USD
    """
    monkeypatch.setattr(
        trading,
        "get_futures_symbol_spec",
        lambda symbol=None, contract_code=None: {
            "root_symbol": "MES",
            "tick_size": 0.25,
            "tick_value": 5.0,
        },
    )

    result = calc_pnl_values(
        "MES",
        "BUY",
        5000.0,
        5002.5,
        1.0,
        instrument_type="FUTURES",
        contract_code="MESM26",
    )
    assert result == pytest.approx(50.0)


def test_derive_exit_price_eurusd_buy_from_pnl():
    """
    Fixed input:
      Entry: 1.10000
      Desired PnL: 500.00
      EURUSD 1.0 lot => 100,000 units

    Expected result:
      Exit must be 1.10500 to produce +500.00 USD.
    """
    result = derive_exit_price("EURUSD", "BUY", 1.10000, 1.0, 500.0)
    assert result == 1.105


def test_derive_exit_price_btcusd_buy_from_pnl():
    result = derive_exit_price("BTCUSD", "BUY", 60000.0, 1.0, 1000.0)
    assert result == 61000.0


def test_derive_exit_price_mes_buy_from_pnl(monkeypatch):
    """
    Fixed input:
      Entry: 5000.00
      Desired PnL: 50.00
      MES tick size/value = 0.25 / 5.00

    Expected result:
      50 / 5 = 10 ticks => 10 * 0.25 = 2.50 points
      Exit = 5002.50
    """
    monkeypatch.setattr(
        trading,
        "get_futures_symbol_spec",
        lambda symbol=None, contract_code=None: {
            "root_symbol": "MES",
            "tick_size": 0.25,
            "tick_value": 5.0,
        },
    )

    result = derive_exit_price(
        "MES",
        "BUY",
        5000.0,
        1.0,
        50.0,
        instrument_type="FUTURES",
        contract_code="MESM26",
    )
    assert result == pytest.approx(5002.5)


def test_resolve_pips_for_standard_and_jpy_pairs():
    eurusd_trade = make_trade()
    usdjpy_trade = make_trade(
        symbol="USDJPY",
        side="SELL",
        entry_price=150.00,
        exit_price=149.00,
    )

    assert resolve_pips(eurusd_trade) == pytest.approx(50.0)
    assert resolve_pips(usdjpy_trade) == pytest.approx(100.0)


def test_default_cfd_aliases_have_unique_normalized_keys():
    seen = {}
    for spec in trading.DEFAULT_CFD_SYMBOL_SPECS:
        sym = spec["symbol"]
        for raw in (sym,) + tuple(spec.get("aliases") or ()):
            key = trading.normalize_symbol(raw)
            if not key:
                continue
            assert key not in seen or seen[key] == sym, (key, seen.get(key), sym)
            seen[key] = sym


def test_collect_active_cfd_alias_key_conflicts_empty_when_valid():
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(id=1, symbol="XAUUSD", aliases="GOLD", is_active=True),
        SimpleNamespace(id=2, symbol="EURUSD", aliases="EU", is_active=True),
    ]
    assert trading.collect_active_cfd_alias_key_conflicts(rows) == []


def test_collect_active_cfd_alias_key_conflicts_on_stealing_alias():
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(id=1, symbol="XAUUSD", aliases="GOLD", is_active=True),
        SimpleNamespace(id=2, symbol="EURUSD", aliases="EU", is_active=True),
    ]
    conflicts = trading.collect_active_cfd_alias_key_conflicts(
        rows,
        updated_row_id=2,
        updated_aliases_text="GOLD",
    )
    assert len(conflicts) == 1
    assert "GOLD" in conflicts[0]
    assert "XAUUSD" in conflicts[0]
    assert "EURUSD" in conflicts[0]


def test_format_cfd_aliases_for_storage_normalizes():
    assert trading.format_cfd_aliases_for_storage(" gold , XAU , GOLD ") == "GOLD,XAU"
    assert trading.format_cfd_aliases_for_storage("  ,  ") is None


def test_crypto_and_metal_aliases_and_formatting():
    assert trading.canonicalize_symbol("BTCUSDT") == "BTCUSD"
    assert trading.canonicalize_symbol("gold") == "XAUUSD"
    assert trading.canonicalize_symbol("SILVER") == "XAGUSD"
    assert trading.canonicalize_symbol("XAU") == "XAUUSD"
    assert trading.canonicalize_symbol("EURUSDR") == "EURUSD"
    assert trading.canonicalize_symbol("XAUUSDMICRO") == "XAUUSD"
    assert trading.canonicalize_symbol("WTI") == "USOIL"
    assert trading.canonicalize_symbol("BRENT") == "UKOIL"
    assert trading.canonicalize_symbol("PLATINUM") == "XPTUSD"
    assert trading.canonicalize_symbol("DXY") == "USDX"
    assert trading.cfd_mt5_symbol_name_candidates("XAUUSD")[0] == "XAUUSD"
    assert "GOLD" in trading.cfd_mt5_symbol_name_candidates("XAUUSD")
    assert "XAUUSD.r" in trading.cfd_mt5_symbol_name_candidates("XAUUSD")
    assert trading.format_trade_price(0.12345, "DOGEUSD") == "0.12345"
    assert trading.format_trade_price(2500.125, "XAUUSD") == "2500.12"


def test_get_symbol_options_include_common_crypto_symbols():
    options = trading.get_symbol_options("CFD")

    assert "BTCUSD" in options
    assert "ETHUSD" in options
    assert "DOGEUSD" in options


def test_resolve_ticks_for_futures_trade(monkeypatch):
    trade = make_trade(
        symbol="MES",
        side="BUY",
        entry_price=5000.0,
        exit_price=5002.5,
        account_type="FUTURES",
        contract_code="MESM26",
    )

    monkeypatch.setattr(
        trading,
        "get_futures_symbol_spec",
        lambda symbol=None, contract_code=None: {
            "root_symbol": "MES",
            "tick_size": 0.25,
            "tick_value": 5.0,
        },
    )

    assert resolve_ticks(trade) == pytest.approx(10.0)


def test_build_rr_summary_returns_empty_state_until_three_valid_trades():
    trades = [
        make_trade(entry_price=100.0, exit_price=110.0, stop_loss=95.0, take_profit=115.0),
        make_trade(entry_price=100.0, exit_price=108.0, stop_loss=95.0, take_profit=112.0),
    ]

    summary = build_rr_summary(trades)

    assert summary["trades_with_data"] == 2
    assert summary["avg_planned_rr"] == 2.7
    assert summary["avg_actual_rr"] == 1.8
    assert summary["rr_capture_ratio"] == 0.67
    assert summary["advice"] == "Early RR read only. The numbers are live, but wait for at least 3 valid trades before trusting the pattern."


def test_build_rr_summary_returns_mid_tier_capture_advice():
    trades = [
        make_trade(entry_price=100.0, exit_price=112.0, stop_loss=90.0, take_profit=120.0),
        make_trade(entry_price=100.0, exit_price=108.0, stop_loss=90.0, take_profit=115.0),
        make_trade(entry_price=100.0, exit_price=109.0, stop_loss=95.0, take_profit=110.0),
    ]

    summary = build_rr_summary(trades)

    assert summary["trades_with_data"] == 3
    assert summary["avg_planned_rr"] == 1.83
    assert summary["avg_actual_rr"] == 1.27
    assert summary["rr_capture_ratio"] == 0.69
    assert summary["advice"] == "You're close to your planned RR but leaving some on the table. Tighten your exit process - trust the levels you set pre-trade."


def test_build_rr_summary_counts_losing_buy_trades_as_negative_real_rr():
    trades = [
        make_trade(entry_price=100.0, exit_price=95.0, stop_loss=95.0, take_profit=110.0, side="BUY"),
        make_trade(entry_price=100.0, exit_price=108.0, stop_loss=95.0, take_profit=110.0, side="BUY"),
        make_trade(entry_price=100.0, exit_price=110.0, stop_loss=95.0, take_profit=110.0, side="BUY"),
    ]

    summary = build_rr_summary(trades)

    assert summary["trades_with_data"] == 3
    assert summary["avg_planned_rr"] == 2.0
    assert summary["avg_actual_rr"] == 0.87
    assert summary["rr_capture_ratio"] == 0.43


def test_build_rr_summary_handles_sell_direction_correctly():
    trades = [
        make_trade(entry_price=100.0, exit_price=95.0, stop_loss=105.0, take_profit=90.0, side="SELL"),
        make_trade(entry_price=100.0, exit_price=103.0, stop_loss=105.0, take_profit=90.0, side="SELL"),
        make_trade(entry_price=100.0, exit_price=90.0, stop_loss=105.0, take_profit=90.0, side="SELL"),
    ]

    summary = build_rr_summary(trades)

    assert summary["trades_with_data"] == 3
    assert summary["avg_planned_rr"] == 2.0
    assert summary["avg_actual_rr"] == 0.8
    assert summary["rr_capture_ratio"] == 0.4


def test_build_rr_summary_excludes_invalid_stop_geometry():
    trades = [
        make_trade(entry_price=100.0, exit_price=120.0, stop_loss=101.0, take_profit=120.0, side="BUY"),
        make_trade(entry_price=100.0, exit_price=108.0, stop_loss=95.0, take_profit=110.0, side="BUY"),
        make_trade(entry_price=100.0, exit_price=110.0, stop_loss=95.0, take_profit=110.0, side="BUY"),
    ]

    summary = build_rr_summary(trades)

    assert summary["trades_with_data"] == 2
    assert summary["avg_planned_rr"] == 2.0
    assert summary["avg_actual_rr"] == 1.8
    assert summary["rr_capture_ratio"] == 0.9


def test_build_trade_analytics_uses_close_time_for_realized_curves_and_weekly_pnl():
    trade_account = SimpleNamespace(account_type="CFD", account_size=None)
    trade = SimpleNamespace(
        id=1,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10500,
        stop_loss=None,
        take_profit=None,
        lot_size=1.0,
        contract_code=None,
        trade_account=trade_account,
        pnl=500.0,
        commission=0.0,
        swap=0.0,
        opened_at=datetime(2026, 3, 14, 9, 0, 0),
        closed_at=datetime(2026, 3, 23, 10, 0, 0),
    )

    analytics = build_trade_analytics(
        [trade],
        display_timezone_name="UTC",
        now_utc=datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert analytics["summary"]["weekly_pnl"] == pytest.approx(500.0)
    assert analytics["daily_equity_curve"] == [
        {"date": "2026-03-23", "label": "23 Mar", "equity": 500.0}
    ]
    assert analytics["equity_curve"][0]["date"] == "2026-03-23 10:00"
    assert analytics["closed_records"][0]["realized_at_local"] == datetime(
        2026,
        3,
        23,
        10,
        0,
        0,
        tzinfo=timezone.utc,
    )


def test_build_trade_analytics_keeps_open_trade_with_running_pnl_out_of_closed_records():
    trade_account = SimpleNamespace(account_type="CFD", account_size=None)
    open_trade = SimpleNamespace(
        id=1,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=None,
        stop_loss=None,
        take_profit=None,
        lot_size=1.0,
        contract_code=None,
        trade_account=trade_account,
        pnl=150.0,
        commission=0.0,
        swap=0.0,
        opened_at=datetime(2026, 3, 23, 9, 0, 0),
        closed_at=None,
    )
    closed_trade = SimpleNamespace(
        id=2,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.2500,
        exit_price=1.2450,
        stop_loss=None,
        take_profit=None,
        lot_size=1.0,
        contract_code=None,
        trade_account=trade_account,
        pnl=80.0,
        commission=0.0,
        swap=0.0,
        opened_at=datetime(2026, 3, 22, 9, 0, 0),
        closed_at=datetime(2026, 3, 23, 10, 0, 0),
    )

    analytics = build_trade_analytics(
        [open_trade, closed_trade],
        display_timezone_name="UTC",
        now_utc=datetime(2026, 3, 23, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert analytics["summary"]["open_trades"] == 1
    assert analytics["summary"]["closed_trades"] == 1
    assert analytics["summary"]["weekly_pnl"] == pytest.approx(80.0)
    assert len(analytics["closed_records"]) == 1
    assert analytics["closed_records"][0]["trade"].id == 2


def test_build_trade_analytics_merges_bundled_trades_without_losing_fee_math():
    trade_account = SimpleNamespace(account_type="CFD", account_size=None)
    bundled_trades = [
        SimpleNamespace(
            id=1,
            pubkey="trade-1",
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            stop_loss=None,
            take_profit=None,
            lot_size=0.5,
            contract_code=None,
            trade_account=trade_account,
            pnl=30.0,
            commission=-1.0,
            swap=0.0,
            bundle_pubkey="bundle-1",
            opened_at=datetime(2026, 3, 10, 10, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 20, 0),
            is_corrective=False,
            is_reactive=True,
            trade_note="First leg.",
            system_trade_note=None,
        ),
        SimpleNamespace(
            id=2,
            pubkey="trade-2",
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1005,
            exit_price=1.1015,
            stop_loss=None,
            take_profit=None,
            lot_size=0.5,
            contract_code=None,
            trade_account=trade_account,
            pnl=20.0,
            commission=-2.0,
            swap=1.0,
            bundle_pubkey="bundle-1",
            opened_at=datetime(2026, 3, 10, 10, 5, 0),
            closed_at=datetime(2026, 3, 10, 10, 25, 0),
            is_corrective=False,
            is_reactive=True,
            trade_note="Second leg.",
            system_trade_note=None,
        ),
    ]

    analytics = build_trade_analytics(
        bundled_trades,
        display_timezone_name="UTC",
        now_utc=datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert analytics["summary"]["total_trades"] == 1
    assert analytics["summary"]["closed_trades"] == 1
    assert analytics["summary"]["gross_profit"] == pytest.approx(50.0)
    assert analytics["summary"]["net_pnl"] == pytest.approx(48.0)
    assert analytics["closed_records"][0]["raw_pnl"] == pytest.approx(50.0)
    assert analytics["closed_records"][0]["pnl"] == pytest.approx(48.0)
    assert analytics["summary"]["cost_drag"] == pytest.approx(2.0)
    assert analytics["summary"]["cost_drag_coverage"] == 1


def test_build_trade_analytics_cost_drag_zero_without_fee_fields():
    trade_account = SimpleNamespace(account_type="CFD", account_size=None)
    trade = SimpleNamespace(
        id=1,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10500,
        stop_loss=None,
        take_profit=None,
        lot_size=1.0,
        contract_code=None,
        trade_account=trade_account,
        pnl=100.0,
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 0, 0),
    )

    analytics = build_trade_analytics(
        [trade],
        display_timezone_name="UTC",
        now_utc=datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert analytics["summary"]["cost_drag"] == pytest.approx(0.0)
    assert analytics["summary"]["cost_drag_coverage"] == 0


def test_aggregate_ohlc_bars_merges_m5_into_m15_bucket():
    m5 = [
        {"time": 1000, "open": 1.0, "high": 1.02, "low": 0.99, "close": 1.01},
        {"time": 1100, "open": 1.01, "high": 1.03, "low": 1.0, "close": 1.02},
        {"time": 1200, "open": 1.02, "high": 1.04, "low": 1.01, "close": 1.03},
    ]
    out = aggregate_ohlc_bars(m5, 900)
    assert len(out) == 1
    assert out[0]["time"] == 900
    assert out[0]["open"] == pytest.approx(1.0)
    assert out[0]["high"] == pytest.approx(1.04)
    assert out[0]["low"] == pytest.approx(0.99)
    assert out[0]["close"] == pytest.approx(1.03)
