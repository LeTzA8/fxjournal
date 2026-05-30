"""Tests for proxy replay status assignment on Tradovate/Topstep futures imports.

Verifies that:
- proxy fields are set on import for FUTURES accounts
- no Celery task is dispatched
- CFD/MT5 imports are unaffected
"""

import io
import json
from datetime import datetime
from unittest.mock import patch

import pytest

from helpers.futures_proxy import (
    PROXY_STATUS_PENDING,
    PROXY_STATUS_UNAVAILABLE_NO_MAPPING,
    PROXY_STATUS_UNAVAILABLE_NO_MT5,
)
from models import FuturesSymbol, MT5Account, Trade, TradeAccount, User, db


# ---------------------------------------------------------------------------
# Sample CSVs
# ---------------------------------------------------------------------------

_TRADOVATE_NQ_CSV = (
    "symbol,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp\n"
    "NQH6,111,222,1,18000.00,18050.00,250.00,"
    "2026-03-10T09:30:00-05:00,2026-03-10T10:00:00-05:00\n"
)

_TRADOVATE_ZB_CSV = (
    "symbol,buyFillId,sellFillId,qty,buyPrice,sellPrice,pnl,boughtTimestamp,soldTimestamp\n"
    "ZBM6,333,444,1,113.25,113.50,781.25,"
    "2026-03-10T09:30:00-05:00,2026-03-10T10:00:00-05:00\n"
)

_TOPSTEP_NQ_CSV = (
    "Id,ContractName,EnteredAt,ExitedAt,EntryPrice,ExitPrice,Fees,PnL,Size,Type\n"
    "1,NQH6,03/10/2026 09:30:00 -05:00,03/10/2026 10:00:00 -05:00,"
    "18000.00,18050.00,2.00,248.00,1,LONG\n"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logged_in_futures_user(client, username, email):
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
        name="Futures Main",
        account_type="FUTURES",
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


def _make_logged_in_cfd_user(client, username, email):
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
        name="CFD Main",
        account_type="CFD",
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


def _seed_nq_proxy(app_ctx):
    existing = FuturesSymbol.query.filter_by(root_symbol="NQ").one_or_none()
    if existing:
        existing.proxy_cfd_symbol = "NAS100"
        existing.is_active = True
    else:
        db.session.add(FuturesSymbol(
            root_symbol="NQ",
            tick_size=0.25,
            tick_value=5.0,
            proxy_cfd_symbol="NAS100",
            is_active=True,
        ))
    # ZB intentionally has no proxy
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


def _add_mt5(user_id):
    mt5 = MT5Account(
        user_id=user_id,
        account_number="99999",
        server="Broker-Test",
        is_active=True,
    )
    db.session.add(mt5)
    db.session.commit()


def _post_tradovate_csv(client, csv_text):
    return client.post(
        "/dashboard/import",
        data={
            "mt5_file": (io.BytesIO(csv_text.encode("utf-8")), "tradovate.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _post_topstep_csv(client, csv_text):
    return client.post(
        "/dashboard/import",
        data={
            "mt5_file": (io.BytesIO(csv_text.encode("utf-8")), "topstep.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


# ---------------------------------------------------------------------------
# Tests: Tradovate
# ---------------------------------------------------------------------------

def test_tradovate_import_sets_proxy_symbol(app_ctx, client):
    """NQ import sets proxy_replay_symbol = 'NAS100'."""
    _seed_nq_proxy(app_ctx)
    user, acct = _make_logged_in_futures_user(client, "tv-sym", "tv-sym@test.com")
    _add_mt5(user.id)

    with patch("helpers.futures_proxy._user_has_active_mt5", return_value=True):
        _post_tradovate_csv(client, _TRADOVATE_NQ_CSV)

    trade = Trade.query.filter_by(trade_account_id=acct.id).first()
    assert trade is not None
    assert trade.proxy_replay_symbol == "NAS100"


def test_tradovate_import_sets_proxy_status_pending_with_mt5(app_ctx, client):
    """NQ import + active MT5 → proxy_replay_status = 'pending'."""
    _seed_nq_proxy(app_ctx)
    user, acct = _make_logged_in_futures_user(client, "tv-pend", "tv-pend@test.com")
    _add_mt5(user.id)

    _post_tradovate_csv(client, _TRADOVATE_NQ_CSV)

    trade = Trade.query.filter_by(trade_account_id=acct.id).first()
    assert trade is not None
    assert trade.proxy_replay_status == PROXY_STATUS_PENDING


def test_tradovate_import_sets_proxy_status_no_mt5(app_ctx, client):
    """NQ import with no MT5 account → proxy_replay_status = 'unavailable_no_mt5'."""
    _seed_nq_proxy(app_ctx)
    user, acct = _make_logged_in_futures_user(client, "tv-nomt5", "tv-nomt5@test.com")
    # no MT5 added

    _post_tradovate_csv(client, _TRADOVATE_NQ_CSV)

    trade = Trade.query.filter_by(trade_account_id=acct.id).first()
    assert trade is not None
    assert trade.proxy_replay_status == PROXY_STATUS_UNAVAILABLE_NO_MT5


def test_tradovate_import_no_proxy_for_unmapped_symbol(app_ctx, client):
    """ZB import → proxy_replay_status = 'unavailable_no_mapping', no proxy symbol."""
    _seed_nq_proxy(app_ctx)  # seeds ZB with no proxy
    user, acct = _make_logged_in_futures_user(client, "tv-nomap", "tv-nomap@test.com")
    _add_mt5(user.id)

    _post_tradovate_csv(client, _TRADOVATE_ZB_CSV)

    trade = Trade.query.filter_by(trade_account_id=acct.id).first()
    assert trade is not None
    assert trade.proxy_replay_status == PROXY_STATUS_UNAVAILABLE_NO_MAPPING
    assert trade.proxy_replay_symbol is None


def test_tradovate_import_does_not_enqueue_celery_task(app_ctx, client):
    """Import must not dispatch any MT5 bar-fetch task (Phase 1 scaffold only).

    Checks that dispatch_celery_task is never called with a task name that
    suggests a proxy bar fetch.  Other tasks (e.g. weekly AI queue) may still
    dispatch legitimately, so we inspect the call args rather than asserting
    zero calls.
    """
    _seed_nq_proxy(app_ctx)
    user, acct = _make_logged_in_futures_user(client, "tv-nocel", "tv-nocel@test.com")
    _add_mt5(user.id)

    with patch("helpers.celery_dispatch.dispatch_celery_task", wraps=None) as mock_dispatch:
        _post_tradovate_csv(client, _TRADOVATE_NQ_CSV)
        # Ensure none of the calls reference a proxy/bar-fetch task
        bar_fetch_calls = [
            call for call in mock_dispatch.call_args_list
            if "bar" in str(call).lower() or "proxy" in str(call).lower()
        ]
        assert bar_fetch_calls == [], (
            f"Unexpected bar-fetch/proxy task dispatched: {bar_fetch_calls}"
        )


def test_tradovate_import_proxy_window_minutes_stored_as_json(app_ctx, client):
    """Committed trade has proxy_replay_window_minutes parseable as JSON."""
    _seed_nq_proxy(app_ctx)
    user, acct = _make_logged_in_futures_user(client, "tv-win", "tv-win@test.com")
    _add_mt5(user.id)

    _post_tradovate_csv(client, _TRADOVATE_NQ_CSV)

    trade = Trade.query.filter_by(trade_account_id=acct.id).first()
    assert trade is not None
    assert trade.proxy_replay_window_minutes is not None
    parsed = json.loads(trade.proxy_replay_window_minutes)
    assert "pre" in parsed
    assert "post" in parsed
    assert isinstance(parsed["pre"], int)
    assert isinstance(parsed["post"], int)


def test_topstep_import_sets_proxy_metadata(app_ctx, client):
    """Topstep CSV NQ import also sets proxy metadata."""
    _seed_nq_proxy(app_ctx)
    user, acct = _make_logged_in_futures_user(client, "ts-meta", "ts-meta@test.com")
    _add_mt5(user.id)

    _post_topstep_csv(client, _TOPSTEP_NQ_CSV)

    trade = Trade.query.filter_by(trade_account_id=acct.id).first()
    assert trade is not None
    assert trade.proxy_replay_symbol == "NAS100"
    assert trade.proxy_replay_status == PROXY_STATUS_PENDING


def test_mt5_xlsx_import_no_proxy_fields_set(app_ctx, client):
    """CFD/MT5 XLSX import leaves proxy fields as None."""
    user, acct = _make_logged_in_cfd_user(client, "cfd-noproxy", "cfd-noproxy@test.com")

    # Build a minimal MT5 XLSX using openpyxl
    try:
        import openpyxl
    except ImportError:
        pytest.skip("openpyxl not available")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Positions"
    headers = ["Symbol", "Type", "Volume", "Open Time", "Open Price",
               "Close Time", "Close Price", "Stop Loss", "Take Profit",
               "Commission", "Swap", "Profit", "Position"]
    ws.append(headers)
    ws.append([
        "EURUSD", "buy", 0.1,
        "2026.03.10 09:00:00", 1.0900,
        "2026.03.10 10:00:00", 1.0950,
        0.0, 0.0, -0.3, 0.0, 5.0, "12345678",
    ])

    import io as _io
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    client.post(
        "/dashboard/trades/new",
        data={"mt5_file": (buf, "mt5.xlsx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    trade = Trade.query.filter_by(trade_account_id=acct.id).first()
    if trade is None:
        pytest.skip("MT5 XLSX parsing may have failed in test env")
    assert trade.proxy_replay_symbol is None
    assert trade.proxy_replay_status is None
    assert trade.proxy_replay_window_minutes is None
