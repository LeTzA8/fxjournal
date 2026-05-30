"""Tests for helpers/futures_proxy.py — resolver functions.

All tests run inside an app context with a real SQLite DB so that
FuturesSymbol queries work correctly.
"""

from datetime import datetime, timedelta

import pytest

from helpers.futures_proxy import (
    PROXY_STATUS_PENDING,
    PROXY_STATUS_UNAVAILABLE_NO_MAPPING,
    PROXY_STATUS_UNAVAILABLE_NO_MT5,
    compute_proxy_window_minutes,
    is_futures_trade,
    proxy_replay_api_block,
    resolve_proxy_cfd_symbol,
    resolve_proxy_status,
)
from models import FuturesSymbol, MT5Account, Trade, TradeAccount, User, db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(username, email):
    user = User(
        username=username,
        email=email,
        password="hashed",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()
    return user


def _make_futures_account(user_id, name="Futures Acct"):
    acct = TradeAccount(
        user_id=user_id,
        name=name,
        account_type="FUTURES",
        is_default=True,
    )
    db.session.add(acct)
    db.session.flush()
    return acct


def _make_cfd_account(user_id, name="CFD Acct"):
    acct = TradeAccount(
        user_id=user_id,
        name=name,
        account_type="CFD",
        is_default=True,
    )
    db.session.add(acct)
    db.session.flush()
    return acct


def _make_trade(account, symbol="NQ", contract_code="NQH6", opened_at=None, closed_at=None):
    now = datetime(2026, 3, 10, 14, 0, 0)
    t = Trade(
        user_id=account.user_id,
        trade_account_id=account.id,
        symbol=symbol,
        contract_code=contract_code,
        side="BUY",
        entry_price=18000.0,
        lot_size=1.0,
        opened_at=opened_at or now,
        closed_at=closed_at or (now + timedelta(hours=1)),
    )
    db.session.add(t)
    db.session.flush()
    return t


def _make_mt5_account(user_id):
    mt5 = MT5Account(
        user_id=user_id,
        account_number="12345",
        server="Broker-Live",
        is_active=True,
    )
    db.session.add(mt5)
    db.session.flush()
    return mt5


def _seed_futures_symbols():
    """Ensure futures_symbols table has the standard proxy mappings for tests."""
    from trading import clear_cfd_symbol_cache
    roots_with_proxy = {
        "NQ": "NAS100", "MNQ": "NAS100",
        "ES": "US500", "MES": "US500",
        "YM": "US30", "MYM": "US30",
        "GC": "XAUUSD", "MGC": "XAUUSD",
        "CL": "USOIL", "MCL": "USOIL",
    }
    for root, proxy in roots_with_proxy.items():
        existing = FuturesSymbol.query.filter_by(root_symbol=root).one_or_none()
        if existing:
            existing.proxy_cfd_symbol = proxy
        else:
            db.session.add(FuturesSymbol(
                root_symbol=root,
                tick_size=0.25,
                tick_value=5.0,
                proxy_cfd_symbol=proxy,
                is_active=True,
            ))
    # ZB has no proxy
    existing_zb = FuturesSymbol.query.filter_by(root_symbol="ZB").one_or_none()
    if not existing_zb:
        db.session.add(FuturesSymbol(
            root_symbol="ZB",
            tick_size=0.015625,
            tick_value=15.625,
            proxy_cfd_symbol=None,
            is_active=True,
        ))
    db.session.commit()
    clear_cfd_symbol_cache()


# ---------------------------------------------------------------------------
# resolve_proxy_cfd_symbol
# ---------------------------------------------------------------------------

def test_resolve_proxy_symbol_nq(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("NQ") == "NAS100"


def test_resolve_proxy_symbol_es(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("ES") == "US500"


def test_resolve_proxy_symbol_ym(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("YM") == "US30"


def test_resolve_proxy_symbol_mnq_micro_alias(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("MNQ") == "NAS100"


def test_resolve_proxy_symbol_gc_gold(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("GC") == "XAUUSD"


def test_resolve_proxy_symbol_cl_oil(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("CL") == "USOIL"


def test_resolve_proxy_symbol_no_mapping_zb(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("ZB") is None


def test_resolve_proxy_symbol_unknown_root(app_ctx):
    _seed_futures_symbols()
    assert resolve_proxy_cfd_symbol("XXXXXXX") is None


def test_resolve_proxy_symbol_empty_string(app_ctx):
    assert resolve_proxy_cfd_symbol("") is None


# ---------------------------------------------------------------------------
# resolve_proxy_status
# ---------------------------------------------------------------------------

def test_resolve_proxy_status_no_mapping(app_ctx):
    _seed_futures_symbols()
    user = _make_user("proxy-nomap", "proxy-nomap@test.com")
    acct = _make_futures_account(user.id)
    trade = _make_trade(acct, symbol="ZB", contract_code="ZBM6")
    _make_mt5_account(user.id)
    db.session.commit()

    result = resolve_proxy_status(trade, user)
    assert result["status"] == PROXY_STATUS_UNAVAILABLE_NO_MAPPING
    assert result["proxy_symbol"] is None
    assert result["window_minutes"] is None


def test_resolve_proxy_status_no_mt5(app_ctx):
    _seed_futures_symbols()
    user = _make_user("proxy-nomt5", "proxy-nomt5@test.com")
    acct = _make_futures_account(user.id)
    trade = _make_trade(acct, symbol="NQ", contract_code="NQH6")
    db.session.commit()  # no MT5 account for this user

    result = resolve_proxy_status(trade, user)
    assert result["status"] == PROXY_STATUS_UNAVAILABLE_NO_MT5
    assert result["proxy_symbol"] == "NAS100"
    assert result["window_minutes"] is None


def test_resolve_proxy_status_pending(app_ctx):
    _seed_futures_symbols()
    user = _make_user("proxy-pending", "proxy-pending@test.com")
    acct = _make_futures_account(user.id)
    trade = _make_trade(acct, symbol="NQ", contract_code="NQH6")
    _make_mt5_account(user.id)
    db.session.commit()

    result = resolve_proxy_status(trade, user)
    assert result["status"] == PROXY_STATUS_PENDING
    assert result["proxy_symbol"] == "NAS100"
    assert isinstance(result["window_minutes"], dict)
    assert "pre" in result["window_minutes"]
    assert "post" in result["window_minutes"]


# ---------------------------------------------------------------------------
# compute_proxy_window_minutes
# ---------------------------------------------------------------------------

def test_compute_proxy_window_scalp(app_ctx):
    """Trade <= 30 min → 90/90."""
    now = datetime(2026, 3, 10, 14, 0, 0)
    trade = Trade(opened_at=now, closed_at=now + timedelta(minutes=15))
    result = compute_proxy_window_minutes(trade)
    assert result == {"pre": 90, "post": 90}


def test_compute_proxy_window_intraday(app_ctx):
    """Trade 30 min < duration <= 4 h → 180/180."""
    now = datetime(2026, 3, 10, 14, 0, 0)
    trade = Trade(opened_at=now, closed_at=now + timedelta(hours=2))
    result = compute_proxy_window_minutes(trade)
    assert result == {"pre": 180, "post": 180}


def test_compute_proxy_window_long_intraday(app_ctx):
    """Trade 4 h < duration <= 24 h → 360/180."""
    now = datetime(2026, 3, 10, 9, 0, 0)
    trade = Trade(opened_at=now, closed_at=now + timedelta(hours=8))
    result = compute_proxy_window_minutes(trade)
    assert result == {"pre": 360, "post": 180}


def test_compute_proxy_window_multiday(app_ctx):
    """Trade > 24 h → 720/360."""
    now = datetime(2026, 3, 10, 9, 0, 0)
    trade = Trade(opened_at=now, closed_at=now + timedelta(hours=30))
    result = compute_proxy_window_minutes(trade)
    assert result == {"pre": 720, "post": 360}


def test_compute_proxy_window_none_times(app_ctx):
    """Missing times → default 180/180."""
    trade = Trade()
    result = compute_proxy_window_minutes(trade)
    assert result == {"pre": 180, "post": 180}


# ---------------------------------------------------------------------------
# is_futures_trade
# ---------------------------------------------------------------------------

def test_is_futures_trade_true(app_ctx):
    user = _make_user("isfut-yes", "isfut-yes@test.com")
    acct = _make_futures_account(user.id)
    trade = _make_trade(acct, contract_code="NQH6")
    db.session.commit()
    # Reload with relationship
    trade = Trade.query.get(trade.id)
    assert is_futures_trade(trade) is True


def test_is_futures_trade_false_cfd_account(app_ctx):
    user = _make_user("isfut-cfd", "isfut-cfd@test.com")
    acct = _make_cfd_account(user.id)
    t = Trade(
        user_id=user.id,
        trade_account_id=acct.id,
        symbol="EURUSD",
        contract_code=None,
        side="BUY",
        entry_price=1.1,
        lot_size=1.0,
        opened_at=datetime(2026, 3, 10, 14, 0),
        closed_at=datetime(2026, 3, 10, 15, 0),
    )
    db.session.add(t)
    db.session.commit()
    t = Trade.query.get(t.id)
    assert is_futures_trade(t) is False


def test_is_futures_trade_false_no_contract_code(app_ctx):
    user = _make_user("isfut-noc", "isfut-noc@test.com")
    acct = _make_futures_account(user.id)
    t = Trade(
        user_id=user.id,
        trade_account_id=acct.id,
        symbol="NQ",
        contract_code=None,
        side="BUY",
        entry_price=18000.0,
        lot_size=1.0,
        opened_at=datetime(2026, 3, 10, 14, 0),
        closed_at=datetime(2026, 3, 10, 15, 0),
    )
    db.session.add(t)
    db.session.commit()
    t = Trade.query.get(t.id)
    assert is_futures_trade(t) is False


# ---------------------------------------------------------------------------
# proxy_replay_api_block
# ---------------------------------------------------------------------------

def test_proxy_replay_api_block_non_futures_returns_none(app_ctx):
    user = _make_user("apib-cfd", "apib-cfd@test.com")
    acct = _make_cfd_account(user.id)
    t = Trade(
        user_id=user.id,
        trade_account_id=acct.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        lot_size=1.0,
        opened_at=datetime(2026, 3, 10, 14, 0),
        closed_at=datetime(2026, 3, 10, 15, 0),
    )
    db.session.add(t)
    db.session.commit()
    t = Trade.query.get(t.id)
    assert proxy_replay_api_block(t) is None


def test_proxy_replay_api_block_futures_returns_block(app_ctx):
    _seed_futures_symbols()
    user = _make_user("apib-fut", "apib-fut@test.com")
    acct = _make_futures_account(user.id)
    trade = _make_trade(acct, contract_code="NQH6")
    trade.proxy_replay_symbol = "NAS100"
    trade.proxy_replay_status = PROXY_STATUS_PENDING
    db.session.commit()
    trade = Trade.query.get(trade.id)

    block = proxy_replay_api_block(trade)
    assert block is not None
    assert block["is_proxy"] is True
    assert block["accuracy"] == "approximate"
    assert block["warning_required"] is True
    assert block["proxy_symbol"] == "NAS100"
    assert block["status"] == PROXY_STATUS_PENDING
    assert "NAS100" in block["disclaimer"]
