"""Tests for the trade_chart_data API — futures proxy branch.

Verifies:
- Futures trades return a proxy_replay block
- No entry_price/exit_price/stop_loss/take_profit in chart markers for proxy trades
- execution block carries the actual futures fill data
- Existing CFD/MT5 trade behavior is unchanged
"""

from datetime import datetime

import pytest

from helpers.futures_proxy import (
    PROXY_STATUS_PENDING,
    PROXY_STATUS_UNAVAILABLE_NO_MAPPING,
    PROXY_STATUS_UNAVAILABLE_NO_MT5,
)
from models import MT5Account, Trade, TradeBars, TradeAccount, User, db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logged_in_user(client, username, email, *, account_type="FUTURES"):
    user = User(
        username=username,
        email=email,
        password="hashed",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()
    acct = TradeAccount(
        user_id=user.id,
        name="Test Acct",
        account_type=account_type,
        is_default=True,
    )
    db.session.add(acct)
    db.session.commit()

    with client.session_transaction() as sess:
        sess["user_id"] = user.id
        sess["username"] = username
        sess["display_timezone"] = "UTC"
        sess["active_trade_account_id"] = acct.id

    return user, acct


def _make_futures_trade(acct, *, proxy_status=PROXY_STATUS_PENDING, proxy_symbol="NAS100",
                        import_sig="tradovate-abc"):
    t = Trade(
        user_id=acct.user_id,
        trade_account_id=acct.id,
        symbol="NQ",
        contract_code="NQH6",
        side="BUY",
        entry_price=18000.0,
        exit_price=18050.0,
        stop_loss=17950.0,
        take_profit=18100.0,
        lot_size=1.0,
        pnl=250.0,
        opened_at=datetime(2026, 3, 10, 14, 0, 0),
        closed_at=datetime(2026, 3, 10, 15, 0, 0),
        import_signature=import_sig,
        source_timezone="America/Chicago",
        proxy_replay_symbol=proxy_symbol,
        proxy_replay_status=proxy_status,
    )
    db.session.add(t)
    db.session.commit()
    return t


def _make_cfd_trade(acct, *, with_mt5_position=True, with_bars=True):
    t = Trade(
        user_id=acct.user_id,
        trade_account_id=acct.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.09,
        exit_price=1.095,
        lot_size=1.0,
        pnl=500.0,
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 0, 0),
        mt5_position="12345678" if with_mt5_position else None,
    )
    db.session.add(t)
    db.session.flush()
    if with_bars:
        for i in range(5):
            bar = TradeBars(
                trade_id=t.id,
                timeframe="M5",
                bar_time=1741600000 + i * 300,
                open=1.09 + i * 0.0001,
                high=1.091 + i * 0.0001,
                low=1.089 + i * 0.0001,
                close=1.090 + i * 0.0001,
            )
            db.session.add(bar)
    db.session.commit()
    return t


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_chart_data_returns_proxy_pending_block(app_ctx, client):
    user, acct = _make_logged_in_user(client, "capi-pend", "capi-pend@test.com")
    trade = _make_futures_trade(acct, proxy_status=PROXY_STATUS_PENDING)

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["status"] == "proxy_pending"
    assert "proxy_replay" in data
    pr = data["proxy_replay"]
    assert pr["is_proxy"] is True
    assert pr["accuracy"] == "approximate"
    assert pr["warning_required"] is True
    assert pr["status"] == PROXY_STATUS_PENDING
    assert pr["proxy_symbol"] == "NAS100"


def test_chart_data_proxy_no_price_levels_in_markers(app_ctx, client):
    """markers block must NOT contain entry_price, exit_price, stop_loss, take_profit."""
    user, acct = _make_logged_in_user(client, "capi-nopr", "capi-nopr@test.com")
    trade = _make_futures_trade(acct)

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    data = resp.get_json()

    markers = data.get("markers", {})
    for forbidden_key in ("entry_price", "exit_price", "stop_loss", "take_profit"):
        assert forbidden_key not in markers, f"markers must not contain '{forbidden_key}' for proxy trades"


def test_chart_data_proxy_markers_have_time_anchors(app_ctx, client):
    """markers must contain entry_time and exit_time (UTC epoch)."""
    user, acct = _make_logged_in_user(client, "capi-times", "capi-times@test.com")
    trade = _make_futures_trade(acct)

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    data = resp.get_json()

    markers = data["markers"]
    assert markers.get("entry_time") is not None
    assert markers.get("exit_time") is not None
    assert isinstance(markers["entry_time"], int)
    assert isinstance(markers["exit_time"], int)


def test_chart_data_proxy_execution_block_has_futures_prices(app_ctx, client):
    """execution block carries actual futures fill prices."""
    user, acct = _make_logged_in_user(client, "capi-exec", "capi-exec@test.com")
    trade = _make_futures_trade(acct)

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    data = resp.get_json()

    assert "execution" in data
    ex = data["execution"]
    assert ex["entry_price"] == 18000.0
    assert ex["exit_price"] == 18050.0
    assert ex["pnl"] == 250.0
    assert ex["contract_code"] == "NQH6"
    assert ex["symbol"] == "NQ"
    assert ex["side"] == "BUY"
    assert ex["execution_source"] == "tradovate_csv"


def test_chart_data_proxy_topstep_execution_source(app_ctx, client):
    """Import signature starting with 'topstep' → execution_source = 'topstep_csv'."""
    user, acct = _make_logged_in_user(client, "capi-tops", "capi-tops@test.com")
    trade = _make_futures_trade(acct, import_sig="topstep-xyz")

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    data = resp.get_json()
    assert data["execution"]["execution_source"] == "topstep_csv"


def test_chart_data_proxy_unavailable_no_mapping(app_ctx, client):
    user, acct = _make_logged_in_user(client, "capi-nomap", "capi-nomap@test.com")
    trade = _make_futures_trade(
        acct,
        proxy_status=PROXY_STATUS_UNAVAILABLE_NO_MAPPING,
        proxy_symbol=None,
    )

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    data = resp.get_json()
    assert data["status"] == "proxy_unavailable"
    assert data["proxy_replay"]["status"] == PROXY_STATUS_UNAVAILABLE_NO_MAPPING


def test_chart_data_proxy_unavailable_no_mt5(app_ctx, client):
    user, acct = _make_logged_in_user(client, "capi-nomt5", "capi-nomt5@test.com")
    trade = _make_futures_trade(
        acct,
        proxy_status=PROXY_STATUS_UNAVAILABLE_NO_MT5,
        proxy_symbol="NAS100",
    )

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    data = resp.get_json()
    assert data["status"] == "proxy_unavailable"
    assert data["proxy_replay"]["status"] == PROXY_STATUS_UNAVAILABLE_NO_MT5


def test_chart_data_normal_mt5_cfd_trade_unchanged(app_ctx, client):
    """Existing CFD/MT5 trade with bars must still return status='ready'."""
    user, acct = _make_logged_in_user(
        client, "capi-cfd", "capi-cfd@test.com", account_type="CFD"
    )
    trade = _make_cfd_trade(acct)

    resp = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    data = resp.get_json()
    assert data["status"] == "ready"
    assert "proxy_replay" not in data
    # CFD trade should have price-level markers
    markers = data["markers"]
    assert markers.get("entry_price") is not None


def test_chart_data_cfd_trade_no_bars_still_returns_unavailable(app_ctx, client):
    """Non-futures trade without mt5_position returns {'status':'unavailable'}."""
    user, acct = _make_logged_in_user(
        client, "capi-cfd2", "capi-cfd2@test.com", account_type="CFD"
    )
    # Trade with no mt5_position and no bars — raw manual entry
    t = Trade(
        user_id=user.id,
        trade_account_id=acct.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.09,
        lot_size=1.0,
        opened_at=datetime(2026, 3, 10, 9, 0),
        closed_at=datetime(2026, 3, 10, 10, 0),
        mt5_position=None,
    )
    db.session.add(t)
    db.session.commit()

    resp = client.get(f"/api/trades/{t.pubkey}/chart-data")
    data = resp.get_json()
    assert data["status"] == "unavailable"
    assert "proxy_replay" not in data
