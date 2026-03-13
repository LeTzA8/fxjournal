from types import SimpleNamespace

import pytest

import trading
from trading import build_rr_summary, calc_pnl_values, derive_exit_price, resolve_pips, resolve_ticks


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


def test_crypto_and_metal_aliases_and_formatting():
    assert trading.canonicalize_symbol("BTCUSDT") == "BTCUSD"
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
    assert summary["advice"] == "Set stop loss and take profit on your trades to unlock RR analysis."


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
