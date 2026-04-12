import os
import uuid
from datetime import datetime, timedelta, timezone
import logging
import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import celery_workers.mt5_sync_tasks as mt5_sync_module
from celery_workers.mt5_sync_tasks import (
    _adjust_mt5_unix_epoch,
    _probe_mt5_server_delta_minutes,
    _reference_utc_unix_seconds,
    aggregate_deals_to_trades,
    _positions_to_open_trades,
    sync_mt5_account,
)
from celery_app import celery
from helpers.core import delete_users_with_related_data
from helpers.utils import decrypt_password, encrypt_password
from models import MT5Account, Trade, TradeAccount, TradeBars, User, db


def _create_user_with_account(*, username, email, account_name="Main Account"):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name=account_name,
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()
    return user, trade_account


def _log_in_root_admin(client, *, email="root-admin@example.com", username="root-admin"):
    os.environ["ADMIN_USER_EMAILS"] = email
    user, trade_account = _create_user_with_account(
        username=username,
        email=email,
    )
    user.is_admin = True
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = trade_account.id

    return user, trade_account


def _create_mt5_account(*, user_id, trade_account_id, account_number="12345678"):
    mt5_account = MT5Account(
        user_id=user_id,
        trade_account_id=trade_account_id,
        account_number=account_number,
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        is_active=True,
    )
    db.session.add(mt5_account)
    db.session.commit()
    return mt5_account


def _deal(**overrides):
    payload = {
        "position_id": 1001,
        "entry": 0,
        "type": 0,
        "symbol": "XAUUSD",
        "price": 3000.0,
        "volume": 1.0,
        "time": 1_710_000_000,
        "profit": 0.0,
        "commission": 0.0,
        "swap": 0.0,
        "comment": "",
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_mt5_bar_epoch_adjustment_matches_deal_timestamp_normalization():
    """fetch_trade_bars applies the same offset as deal ingest so bars align with opened_at/closed_at."""
    raw = 1_717_200_000
    assert int(_adjust_mt5_unix_epoch(raw, offset_minutes=120)) == raw - 7200
    assert int(_adjust_mt5_unix_epoch(raw, offset_minutes=-60)) == raw + 3600


def test_reference_utc_unix_seconds_uses_ntp_and_caches(monkeypatch):
    class _FakeClient:
        calls = 0

        def request(self, server, version=3, timeout=2.5):
            _FakeClient.calls += 1
            return SimpleNamespace(tx_time=1_700_000_000.0)

    monkeypatch.setenv("FXJ_MT5_TIME_REFERENCE", "ntp")
    monkeypatch.setenv("FXJ_NTP_SERVERS", "time.google.com")
    monkeypatch.setenv("FXJ_NTP_CACHE_SECONDS", "60")
    monkeypatch.setattr(mt5_sync_module, "ntplib", SimpleNamespace(NTPClient=lambda: _FakeClient()))
    monkeypatch.setattr(
        mt5_sync_module,
        "_NTP_REFERENCE_CACHE",
        {
            "expires_at": 0.0,
            "unix_seconds": None,
            "source": "vm_fallback",
            "server": None,
            "vm_skew_seconds": None,
        },
    )

    first = _reference_utc_unix_seconds()
    second = _reference_utc_unix_seconds()

    assert first[0] == pytest.approx(1_700_000_000.0)
    assert first[1] == "ntp"
    assert first[2] == "time.google.com"
    assert _FakeClient.calls == 1
    assert second[0] == pytest.approx(first[0])
    assert second[1] == "ntp"


def test_reference_utc_unix_seconds_falls_back_to_vm(monkeypatch):
    monkeypatch.setenv("FXJ_MT5_TIME_REFERENCE", "ntp")
    monkeypatch.setenv("FXJ_NTP_CACHE_SECONDS", "0")
    monkeypatch.setattr(mt5_sync_module, "ntplib", None)
    monkeypatch.setattr(
        mt5_sync_module,
        "_NTP_REFERENCE_CACHE",
        {
            "expires_at": 0.0,
            "unix_seconds": None,
            "source": "vm_fallback",
            "server": None,
            "vm_skew_seconds": None,
        },
    )

    value, source, server, skew = _reference_utc_unix_seconds()

    assert value > 0
    assert source == "vm_fallback"
    assert server is None
    assert skew == 0.0


def test_probe_mt5_server_delta_minutes_uses_reference_time():
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: SimpleNamespace(time=1_700_007_200)
    )
    delta = _probe_mt5_server_delta_minutes(
        fake_mt5,
        preferred_symbol="EURUSD",
        reference_utc_unix_seconds=1_700_000_000,
    )
    assert delta == 120


def test_aggregate_deals_to_trades_closes_position_with_multiple_exit_deals():
    trades = aggregate_deals_to_trades(
        [
            _deal(price=3000.0, volume=1.0, time=1_710_000_000, comment="entry"),
            _deal(
                entry=1,
                price=3010.0,
                volume=0.4,
                time=1_710_003_600,
                profit=40.0,
                commission=-1.0,
                comment="tp1",
            ),
            _deal(
                entry=1,
                price=3020.0,
                volume=0.6,
                time=1_710_007_200,
                profit=120.0,
                commission=-1.5,
                swap=-0.5,
                comment="final",
            ),
        ]
    )

    assert len(trades) == 1
    assert trades[0]["symbol"] == "XAUUSD"
    assert trades[0]["entry_price"] == pytest.approx(3000.0)
    assert trades[0]["exit_price"] == pytest.approx(3016.0)
    assert trades[0]["lot_size"] == pytest.approx(1.0)
    assert trades[0]["pnl"] == pytest.approx(160.0)
    assert trades[0]["commission"] == pytest.approx(-2.5)
    assert trades[0]["swap"] == pytest.approx(-0.5)
    assert trades[0]["trade_note"] == "final"
    assert trades[0]["closed_at"] == "2024-03-09T18:00:00+00:00"
    assert trades[0]["is_open"] is False


def test_aggregate_deals_to_trades_treats_extra_exit_entries_as_closes():
    trades = aggregate_deals_to_trades(
        [
            _deal(position_id=2002, price=1.25, volume=0.5, time=1_710_000_000, symbol="EURUSD"),
            _deal(
                position_id=2002,
                entry=3,
                price=1.255,
                volume=0.5,
                time=1_710_003_600,
                profit=25.0,
                commission=-0.4,
                symbol="EURUSD",
                comment="close by",
            ),
        ],
        extra_exit_entries=(3,),
    )

    assert len(trades) == 1
    assert trades[0]["symbol"] == "EURUSD"
    assert trades[0]["lot_size"] == pytest.approx(0.5)
    assert trades[0]["exit_price"] == pytest.approx(1.255)
    assert trades[0]["pnl"] == pytest.approx(25.0)
    assert trades[0]["trade_note"] == "close by"
    assert trades[0]["is_open"] is False


def test_aggregate_deals_to_trades_emits_close_only_row_when_entry_is_outside_window():
    trades = aggregate_deals_to_trades(
        [
            _deal(
                position_id=3003,
                entry=1,
                type=1,
                price=1.255,
                volume=0.5,
                time=1_710_003_600,
                profit=25.0,
                commission=-0.4,
                symbol="EURUSD",
                comment="weekend close",
            ),
        ],
    )

    assert len(trades) == 1
    assert trades[0]["symbol"] == "EURUSD"
    assert trades[0]["entry_price"] is None
    assert trades[0]["opened_at"] is None
    assert trades[0]["exit_price"] == pytest.approx(1.255)
    assert trades[0]["lot_size"] == pytest.approx(0.5)
    assert trades[0]["pnl"] == pytest.approx(25.0)
    assert trades[0]["trade_note"] == "weekend close"
    assert trades[0]["is_open"] is False


def test_aggregate_deals_to_trades_open_position_no_exits():
    trades = aggregate_deals_to_trades(
        [
            _deal(
                position_id=5005,
                entry=0,
                type=0,
                price=1.085,
                volume=0.5,
                time=1_710_000_000,
                commission=-0.25,
                symbol="EURUSD",
                comment="buy entry",
            ),
        ],
    )

    assert len(trades) == 1
    assert trades[0]["symbol"] == "EURUSD"
    assert trades[0]["side"] == "BUY"
    assert trades[0]["entry_price"] == pytest.approx(1.085)
    assert trades[0]["exit_price"] is None
    assert trades[0]["closed_at"] is None
    assert trades[0]["lot_size"] == pytest.approx(0.5)
    assert trades[0]["pnl"] is None
    assert trades[0]["is_open"] is True


def test_aggregate_deals_to_trades_partially_closed_position_is_open():
    trades = aggregate_deals_to_trades(
        [
            _deal(
                position_id=6006,
                entry=0,
                type=0,
                price=1.085,
                volume=1.0,
                time=1_710_000_000,
                commission=-0.5,
                symbol="EURUSD",
            ),
            _deal(
                position_id=6006,
                entry=1,
                type=1,
                price=1.09,
                volume=0.4,
                time=1_710_003_600,
                profit=20.0,
                commission=-0.2,
                symbol="EURUSD",
                comment="partial tp",
            ),
        ],
    )

    assert len(trades) == 1
    assert trades[0]["is_open"] is True
    assert trades[0]["exit_price"] is None
    assert trades[0]["closed_at"] is None
    assert trades[0]["lot_size"] == pytest.approx(1.0)


def test_positions_to_open_trades_maps_fields():
    pos = SimpleNamespace(
        identifier=7007,
        type=0,
        symbol="XAUUSD",
        price_open=2300.0,
        volume=0.1,
        commission=-1.5,
        swap=-0.5,
        sl=2280.0,
        tp=2350.0,
        time=1_710_000_000,
        comment="gold long",
    )

    trades = _positions_to_open_trades([pos], position_type_buy=0)

    assert len(trades) == 1
    t = trades[0]
    assert t["mt5_position"] == "7007"
    assert t["symbol"] == "XAUUSD"
    assert t["side"] == "BUY"
    assert t["entry_price"] == pytest.approx(2300.0)
    assert t["lot_size"] == pytest.approx(0.1)
    assert t["commission"] == pytest.approx(-1.5)
    assert t["swap"] == pytest.approx(-0.5)
    assert t["stop_loss"] == pytest.approx(2280.0)
    assert t["take_profit"] == pytest.approx(2350.0)
    assert t["exit_price"] is None
    assert t["closed_at"] is None
    assert t["is_open"] is True
    assert t["trade_note"] == "gold long"


def test_sync_mt5_account_skips_when_same_account_is_already_locked(monkeypatch):
    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: False)

    result = sync_mt5_account.run(123)

    assert result == {"skipped": "sync already running"}


def test_sync_mt5_account_logs_task_context(app_ctx, monkeypatch, caplog):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-log-user",
        email="mt5-log@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="44445555",
    )

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=int(mt5_account.account_number)),
        history_deals_get=lambda *args, **kwargs: [
            _deal(position_id=4004, price=1.25, time=1_710_000_000, symbol="EURUSD"),
            _deal(
                position_id=4004,
                entry=1,
                price=1.255,
                time=1_710_003_600,
                symbol="EURUSD",
                profit=25.0,
                comment="close",
            ),
        ],
        positions_get=lambda: [],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "saved": 0,
                "updated": 1,
                "skipped": 0,
                "errors": 0,
                "skip_reasons": {
                    "close_only_without_existing_open": 0,
                    "existing_already_closed_or_no_state_change": 0,
                    "batch_duplicate_mt5_position": 0,
                    "batch_validation_skipped": 0,
                    "batch_symbol_validation_failed": 0,
                },
            }

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    caplog.set_level(logging.INFO, logger="celery_workers.mt5_sync_tasks")

    result = sync_mt5_account.run(mt5_account.id)

    assert result["saved"] == 0
    assert result["updated"] == 1
    assert result["skipped"] == 0
    assert result["errors"] == 0
    assert "MT5 Sync" in caplog.text
    assert f"Main Account [ID: {trade_account.id}]" in caplog.text
    assert f"DB {mt5_account.id} / Login {mt5_account.account_number}" in caplog.text
    assert "Trigger" in caplog.text
    assert "Trade Rows" in caplog.text
    assert "Closed Rows" in caplog.text
    assert "Skip Reasons" in caplog.text
    assert "Duration" in caplog.text


def test_sync_mt5_account_logs_skip_debug_after_table_not_inside_ascii_cell(app_ctx, monkeypatch, caplog):
    """skip_debug JSON must not be embedded in the ASCII table (blows column width on VM)."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")
    monkeypatch.setenv("FXJ_ASCII_LOG_MAX_WIDTH", "0")

    user, trade_account = _create_user_with_account(
        username="mt5-skipdbg-user",
        email="mt5-skipdbg@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="77778888",
    )
    mt5_account.last_synced_at = datetime(2024, 6, 1, 12, 0, 0)
    db.session.add(mt5_account)
    db.session.commit()

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=int(mt5_account.account_number)),
        history_deals_get=lambda *args, **kwargs: [],
        positions_get=lambda: [],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    long_reason = "x" * 5000

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "saved": 0,
                "updated": 0,
                "skipped": 2,
                "errors": 1,
                "skip_reasons": {
                    "close_only_without_existing_open": 0,
                    "existing_already_closed_or_no_state_change": 0,
                    "batch_duplicate_mt5_position": 0,
                    "batch_validation_skipped": 1,
                    "batch_symbol_validation_failed": 1,
                    "incoming_close_validation_failed": 0,
                },
                "skip_debug": [{"reason": long_reason, "mt5_position": "1"}],
            }

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    caplog.set_level(logging.WARNING, logger="celery_workers.mt5_sync_tasks")

    sync_mt5_account.run(mt5_account.id)

    table_block = caplog.text.split("MT5 Sync")[1].split("MT5 sync skip_debug")[0]
    assert len(table_block) < 3000, "ASCII table should not contain multi-kB skip_debug cell"
    assert "Skip debug rows (count)" in caplog.text
    assert "MT5 sync skip_debug mt5_account_id=" in caplog.text
    assert long_reason in caplog.text


def test_sync_mt5_account_quiet_idle_noop_skips_ascii_table(app_ctx, monkeypatch, caplog):
    """Rolling beat-style run with only benign skips logs one line (no big ASCII table)."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-noop-user",
        email="mt5-noop@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="66667777",
    )
    mt5_account.last_synced_at = datetime(2024, 6, 1, 12, 0, 0)
    db.session.add(mt5_account)
    db.session.commit()

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=int(mt5_account.account_number)),
        history_deals_get=lambda *args, **kwargs: [],
        positions_get=lambda: [],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "saved": 0,
                "updated": 0,
                "skipped": 46,
                "errors": 0,
                "skip_reasons": {
                    "close_only_without_existing_open": 0,
                    "existing_already_closed_or_no_state_change": 46,
                    "incoming_close_validation_failed": 0,
                    "batch_duplicate_mt5_position": 0,
                    "batch_validation_skipped": 0,
                    "batch_symbol_validation_failed": 0,
                },
            }

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    caplog.set_level(logging.INFO, logger="celery_workers.mt5_sync_tasks")

    sync_mt5_account.run(mt5_account.id)

    assert "MT5 sync noop mt5_account_id=" in caplog.text
    assert "skipped=46" in caplog.text
    assert "Skip Reasons" not in caplog.text


def test_internal_mt5_sync_worrisome_skip_debug_omits_benign_skips(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="skipdbg-w-user",
        email="skipdbg-w@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12121212",
    )
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.0,
        exit_price=1.01,
        lot_size=0.01,
        opened_at=datetime(2026, 3, 21, 2, 0, 0),
        closed_at=datetime(2026, 3, 21, 4, 0, 0),
        mt5_position="11111222",
    )
    db.session.add(trade)
    db.session.commit()

    payload = {
        "mt5_account_id": mt5_account.id,
        "include_skip_reasons": True,
        "skip_debug_mode": "worrisome",
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.0,
                "exit_price": 1.01,
                "lot_size": 0.01,
                "pnl": 0.0,
                "commission": 0.0,
                "swap": 0.0,
                "opened_at": "2026-03-21T02:00:00+00:00",
                "closed_at": "2026-03-21T04:00:00+00:00",
                "mt5_position": 11111222,
                "trade_note": "",
            }
        ],
    }
    response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["skipped"] == 1
    assert body["skip_reasons"]["existing_already_closed_or_no_state_change"] == 1
    assert "skip_debug" not in body


def test_sync_mt5_account_shifts_history_window_to_mt5_server_time(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-window-user",
        email="mt5-window@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="51515151",
    )

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    broker_now = datetime.now(timezone.utc) + timedelta(hours=3)
    history_call = {}

    def _capture_history_window(from_date, to_date):
        history_call["from_date"] = from_date
        history_call["to_date"] = to_date
        return []

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=int(mt5_account.account_number)),
        history_deals_get=_capture_history_window,
        positions_get=lambda: [],
        symbol_info_tick=lambda symbol: SimpleNamespace(time=int(broker_now.timestamp())),
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 0, "updated": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    sync_mt5_account.run(mt5_account.id)

    captured_to_date = history_call["to_date"]
    captured_from_date = history_call["from_date"]
    utc_now = datetime.now(timezone.utc)

    assert captured_to_date > utc_now + timedelta(hours=2, minutes=55)
    assert captured_to_date < utc_now + timedelta(hours=3, minutes=5)
    assert captured_from_date < captured_to_date


def test_sync_mt5_account_picks_up_running_trade_from_positions_get(app_ctx, monkeypatch, caplog):
    """positions_get() is called and running positions are included in the sync payload."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-positions-user",
        email="mt5-positions@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="55556666",
    )

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    open_position = SimpleNamespace(
        identifier=9001,
        type=0,
        symbol="EURUSD",
        price_open=1.085,
        volume=0.1,
        commission=-0.5,
        swap=0.0,
        sl=0.0,
        tp=0.0,
        time=1_710_000_000,
        comment="",
    )

    captured_payload = {}

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=int(mt5_account.account_number)),
        history_deals_get=lambda *args, **kwargs: [],
        positions_get=lambda: [open_position],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}

    def capture_post(url, json=None, **kwargs):
        captured_payload.update(json or {})
        return DummyResponse()

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", capture_post)

    sync_mt5_account.run(mt5_account.id)

    trades_sent = captured_payload.get("trades", [])
    assert len(trades_sent) == 1
    assert trades_sent[0]["mt5_position"] == "9001"
    assert trades_sent[0]["is_open"] is True
    assert trades_sent[0]["entry_price"] == pytest.approx(1.085)
    assert trades_sent[0]["closed_at"] is None


def test_sync_mt5_account_retries_when_mt5_session_is_on_wrong_login(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-wrong-login-user",
        email="mt5-wrong-login@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="10101010",
    )

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    shutdown_calls = []
    post_calls = []

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=20202020),
        history_deals_get=lambda *args, **kwargs: [],
        positions_get=lambda: [],
        shutdown=lambda: shutdown_calls.append(True),
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    def _fake_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        raise AssertionError("requests.post should not run when the MT5 login is wrong")

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", _fake_post)
    retry_calls = []

    def _fake_retry(exc=None, **kwargs):
        retry_calls.append({"exc": exc, **kwargs})
        return exc

    monkeypatch.setattr(sync_mt5_account, "retry", _fake_retry)

    with pytest.raises(RuntimeError, match="Wrong MT5 account logged in during sync"):
        sync_mt5_account.run(mt5_account.id)

    assert post_calls == []
    assert shutdown_calls == [True]
    assert retry_calls[0]["countdown"] == 30


def test_encrypt_password_round_trip_requires_key(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        encrypt_password("secret")

    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    encrypted = encrypt_password("secret")

    assert encrypted != "secret"
    assert decrypt_password(encrypted) == "secret"


def test_internal_mt5_sync_requires_shared_secret(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-user",
        email="sync-user@example.com",
    )
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)

    payload = {"mt5_account_id": mt5_account.id, "trades": []}

    missing_secret = client.post("/api/internal/mt5/sync", json=payload)
    wrong_secret = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "wrong"},
    )

    assert missing_secret.status_code == 403
    assert wrong_secret.status_code == 403


def test_internal_mt5_sync_updates_trade_account_size_from_broker_equity(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-size-equity",
        email="sync-size-equity@example.com",
    )
    trade_account.account_size = 1000.0
    db.session.commit()
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)

    resp = client.post(
        "/api/internal/mt5/sync",
        json={
            "mt5_account_id": mt5_account.id,
            "trades": [],
            "broker_equity": 52340.5,
            "broker_balance": 50000.0,
        },
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert resp.status_code == 200
    db.session.expire_all()
    ta = db.session.get(TradeAccount, trade_account.id)
    assert ta.account_size == pytest.approx(52340.5)


def test_internal_mt5_sync_updates_trade_account_size_from_balance_when_no_equity(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-size-balance",
        email="sync-size-balance@example.com",
    )
    trade_account.account_size = 100.0
    db.session.commit()
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)

    resp = client.post(
        "/api/internal/mt5/sync",
        json={
            "mt5_account_id": mt5_account.id,
            "trades": [],
            "broker_balance": 8800.25,
        },
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert resp.status_code == 200
    db.session.expire_all()
    ta = db.session.get(TradeAccount, trade_account.id)
    assert ta.account_size == pytest.approx(8800.25)


def test_internal_mt5_sync_preserves_account_size_without_broker_fields(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-size-preserve",
        email="sync-size-preserve@example.com",
    )
    trade_account.account_size = 777.0
    db.session.commit()
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)

    resp = client.post(
        "/api/internal/mt5/sync",
        json={"mt5_account_id": mt5_account.id, "trades": []},
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert resp.status_code == 200
    db.session.expire_all()
    ta = db.session.get(TradeAccount, trade_account.id)
    assert ta.account_size == pytest.approx(777.0)


def test_internal_mt5_sync_empty_payload_keeps_last_synced_null_until_rows_arrive(app_ctx, client, monkeypatch):
    """First successful API call with zero trade rows must not flip last_synced (full-history retry)."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-empty-first",
        email="sync-empty-first@example.com",
    )
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)
    assert mt5_account.last_synced_at is None

    empty = client.post(
        "/api/internal/mt5/sync",
        json={"mt5_account_id": mt5_account.id, "trades": []},
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert empty.status_code == 200
    db.session.expire_all()
    assert db.session.get(MT5Account, mt5_account.id).last_synced_at is None


def test_chunked_history_deals_get_queries_mt5_in_slices(monkeypatch):
    from celery_workers.mt5_sync_tasks import _chunked_history_deals_get

    monkeypatch.setenv("FXJ_MT5_HISTORY_CHUNK_DAYS", "30")
    calls = []

    def fake_get(fr, to):
        calls.append((fr, to))
        return []

    mt5 = SimpleNamespace(history_deals_get=fake_get)
    date_from = datetime(2000, 1, 1, tzinfo=timezone.utc)
    date_to = datetime(2020, 6, 1, tzinfo=timezone.utc)
    deals, chunks = _chunked_history_deals_get(mt5, date_from, date_to)

    assert deals == []
    assert chunks > 1
    assert len(calls) == chunks
    assert calls[0][0] == date_from
    assert calls[-1][1] == date_to


def test_internal_mt5_sync_saves_and_skips_duplicates(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-sync-user",
        email="mt5-sync@example.com",
    )
    second_account = TradeAccount(
        user_id=user.id,
        name="Second CFD Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(second_account)
    db.session.commit()

    first_mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="11111111",
    )
    second_mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=second_account.id,
        account_number="22222222",
    )

    payload = {
        "mt5_account_id": first_mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 50.0,
                "commission": -0.5,
                "swap": 0.0,
                "stop_loss": 1.08,
                "take_profit": 1.095,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 12345678,
                "trade_note": "",
            }
        ],
    }

    first_response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    duplicate_response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    second_account_response = client.post(
        "/api/internal/mt5/sync",
        json={**payload, "mt5_account_id": second_mt5_account.id},
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert first_response.status_code == 200
    assert first_response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}
    assert duplicate_response.status_code == 200
    assert duplicate_response.get_json() == {"saved": 0, "updated": 0, "skipped": 1, "errors": 0}
    assert second_account_response.status_code == 200
    assert second_account_response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}

    first_trade = Trade.query.filter_by(trade_account_id=trade_account.id, mt5_position="12345678").one()
    second_trade = Trade.query.filter_by(trade_account_id=second_account.id, mt5_position="12345678").one()
    first_mt5_account = db.session.get(MT5Account, first_mt5_account.id)
    second_mt5_account = db.session.get(MT5Account, second_mt5_account.id)

    assert first_trade.import_signature is None
    assert first_trade.import_dedupe_key is None
    assert first_trade.source_timezone == "UTC"
    assert first_trade.trade_note is None
    assert first_trade.system_trade_note == "Auto-imported via MT5 sync"
    assert second_trade.import_signature is None
    assert second_trade.import_dedupe_key is None
    assert second_trade.trade_note is None
    assert second_trade.system_trade_note == "Auto-imported via MT5 sync"
    assert first_mt5_account.last_synced_at is not None
    assert second_mt5_account.last_synced_at is not None


def test_internal_mt5_sync_maps_gold_symbol_to_xauusd(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-gold-user",
        email="mt5-gold@example.com",
    )
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)

    payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "gold",
                "side": "buy",
                "entry_price": 3000.0,
                "exit_price": 3010.0,
                "lot_size": 0.01,
                "pnl": 10.0,
                "commission": -0.5,
                "swap": 0.0,
                "stop_loss": 2990.0,
                "take_profit": 3020.0,
                "opened_at": "2026-04-10T08:00:00+00:00",
                "closed_at": "2026-04-10T10:00:00+00:00",
                "mt5_position": 88776655,
                "trade_note": "",
            }
        ],
    }

    response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert response.status_code == 200
    assert response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}
    trade_row = Trade.query.filter_by(trade_account_id=trade_account.id, mt5_position="88776655").one()
    assert trade_row.symbol == "XAUUSD"


def test_internal_mt5_sync_refresh_timestamps_updates_closed_trade(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="ts-refresh-user",
        email="ts-refresh@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="33333333",
    )
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.0,
        exit_price=1.01,
        lot_size=0.01,
        opened_at=datetime(2026, 3, 21, 2, 0, 0),
        closed_at=datetime(2026, 3, 21, 4, 0, 0),
        mt5_position="99999999",
    )
    db.session.add(trade)
    db.session.commit()

    payload = {
        "mt5_account_id": mt5_account.id,
        "refresh_closed_trade_timestamps": True,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 1.0,
                "exit_price": 1.01,
                "lot_size": 0.01,
                "pnl": 0.0,
                "commission": 0.0,
                "swap": 0.0,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 99999999,
                "trade_note": "",
            }
        ],
    }
    response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["saved"] == 0
    assert body["updated"] == 0
    assert body["skipped"] == 0
    assert body["errors"] == 0
    assert body.get("timestamp_refreshes") == 1

    db.session.refresh(trade)
    assert trade.opened_at == datetime(2026, 3, 21, 8, 0, 0)
    assert trade.closed_at == datetime(2026, 3, 21, 10, 0, 0)


def test_internal_mt5_sync_refresh_timestamps_accepts_close_only_row_for_closed_trade(
    app_ctx, client, monkeypatch
):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="ts-refresh-close-only-user",
        email="ts-refresh-close-only@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="35353535",
    )
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.0,
        exit_price=1.01,
        lot_size=0.01,
        opened_at=datetime(2026, 3, 21, 2, 0, 0),
        closed_at=datetime(2026, 3, 21, 4, 0, 0),
        mt5_position="12121212",
    )
    db.session.add(trade)
    db.session.commit()

    payload = {
        "mt5_account_id": mt5_account.id,
        "refresh_closed_trade_timestamps": True,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "",
                "entry_price": None,
                "exit_price": 1.01,
                "lot_size": 0.01,
                "pnl": 0.0,
                "commission": 0.0,
                "swap": 0.0,
                "opened_at": None,
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 12121212,
                "trade_note": "close only",
                "is_open": False,
            }
        ],
    }
    response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["saved"] == 0
    assert body["updated"] == 0
    assert body["skipped"] == 0
    assert body["errors"] == 0
    assert body.get("timestamp_refreshes") == 1

    db.session.refresh(trade)
    assert trade.opened_at == datetime(2026, 3, 21, 2, 0, 0)
    assert trade.closed_at == datetime(2026, 3, 21, 10, 0, 0)


def test_internal_mt5_trade_bars_requires_shared_secret(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="trade-bars-secret-user",
        email="trade-bars-secret@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="91919191",
    )
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.101,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position="919191",
    )
    db.session.add(trade)
    db.session.commit()

    payload = {
        "mt5_account_id": mt5_account.id,
        "trade_id": trade.id,
        "timeframe": "M5",
        "bars": [],
    }
    missing_secret = client.post("/api/internal/mt5/trade-bars", json=payload)
    wrong_secret = client.post(
        "/api/internal/mt5/trade-bars",
        json=payload,
        headers={"X-Sync-Secret": "wrong"},
    )

    assert missing_secret.status_code == 403
    assert wrong_secret.status_code == 403


def test_internal_mt5_trade_bars_replaces_existing_timeframe_rows(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="trade-bars-replace-user",
        email="trade-bars-replace@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="82828282",
    )
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.101,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position="828282",
    )
    db.session.add(trade)
    db.session.commit()

    first_payload = {
        "mt5_account_id": mt5_account.id,
        "trade_id": trade.id,
        "timeframe": "M5",
        "bars": [
            {"time": 1_700_000_000, "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 100},
            {"time": 1_700_000_300, "open": 1.1005, "high": 1.102, "low": 1.1, "close": 1.1015, "tick_volume": 120},
        ],
    }
    second_payload = {
        "mt5_account_id": mt5_account.id,
        "trade_id": trade.id,
        "timeframe": "M5",
        "bars": [
            {"time": 1_700_000_600, "open": 1.1015, "high": 1.103, "low": 1.101, "close": 1.1025, "tick_volume": 140},
        ],
    }

    first_response = client.post(
        "/api/internal/mt5/trade-bars",
        json=first_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    second_response = client.post(
        "/api/internal/mt5/trade-bars",
        json=second_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    rows = TradeBars.query.filter_by(trade_id=trade.id, timeframe="M5").order_by(TradeBars.bar_time.asc()).all()

    assert first_response.status_code == 200
    assert first_response.get_json()["saved"] == 2
    assert second_response.status_code == 200
    assert second_response.get_json()["saved"] == 1
    assert len(rows) == 1
    assert rows[0].bar_time == 1_700_000_600
    assert rows[0].close == pytest.approx(1.1025)


def test_internal_mt5_sync_inserts_new_running_trade(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-running-insert-user",
        email="mt5-running-insert@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="77778888",
    )

    open_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": None,
                "lot_size": 0.01,
                "pnl": None,
                "commission": -0.25,
                "swap": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-20T08:00:00+00:00",
                "closed_at": None,
                "mt5_position": 88880001,
                "trade_note": "running",
                "is_open": True,
            }
        ],
    }

    response = client.post(
        "/api/internal/mt5/sync",
        json=open_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    trade = Trade.query.filter_by(
        trade_account_id=trade_account.id,
        mt5_position="88880001",
    ).one()

    assert response.status_code == 200
    assert response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}
    assert trade.entry_price == pytest.approx(1.085)
    assert trade.exit_price is None
    assert trade.closed_at is None
    assert trade.pnl is None
    assert trade.trade_note is None
    assert trade.system_trade_note == "running"


def test_internal_mt5_sync_updates_existing_open_trade_when_close_arrives(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-open-close-user",
        email="mt5-open-close@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="12121212",
    )

    open_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": None,
                "lot_size": 0.01,
                "pnl": None,
                "commission": -0.25,
                "swap": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": None,
                "mt5_position": 99999999,
                "trade_note": "open",
                "is_open": True,
            }
        ],
    }
    closed_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 48.5,
                "commission": -0.5,
                "swap": -0.1,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 99999999,
                "trade_note": "closed",
                "is_open": False,
            }
        ],
    }

    open_response = client.post(
        "/api/internal/mt5/sync",
        json=open_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    close_response = client.post(
        "/api/internal/mt5/sync",
        json=closed_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    trade = Trade.query.filter_by(
        trade_account_id=trade_account.id,
        mt5_position="99999999",
    ).one()

    assert open_response.status_code == 200
    assert open_response.get_json() == {"saved": 1, "updated": 0, "skipped": 0, "errors": 0}
    assert close_response.status_code == 200
    assert close_response.get_json() == {"saved": 0, "updated": 1, "skipped": 0, "errors": 0}
    assert trade.exit_price == pytest.approx(1.09)
    assert trade.pnl == pytest.approx(48.5)
    assert trade.commission == pytest.approx(-0.5)
    assert trade.swap == pytest.approx(-0.1)
    assert trade.closed_at is not None
    assert trade.trade_note is None
    assert trade.system_trade_note == "closed"


def test_internal_mt5_sync_invalidates_dashboard_caches_when_trade_changes(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-cache-user",
        email="mt5-cache@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="45454545",
    )

    invalidation_calls = []
    monkeypatch.setattr(
        "routes.mt5_internal.invalidate",
        lambda user_id, trade_account_id=None: invalidation_calls.append((user_id, trade_account_id)),
    )

    open_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": None,
                "lot_size": 0.01,
                "pnl": None,
                "commission": -0.25,
                "swap": 0.0,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": None,
                "mt5_position": 78787878,
                "trade_note": "open",
                "is_open": True,
            }
        ],
    }
    closed_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 48.5,
                "commission": -0.5,
                "swap": -0.1,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 78787878,
                "trade_note": "closed",
                "is_open": False,
            }
        ],
    }

    open_response = client.post(
        "/api/internal/mt5/sync",
        json=open_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )
    close_response = client.post(
        "/api/internal/mt5/sync",
        json=closed_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert open_response.status_code == 200
    assert close_response.status_code == 200
    assert invalidation_calls == [
        (user.id, trade_account.id),
        (user.id, trade_account.id),
    ]


def test_internal_mt5_sync_updates_existing_open_trade_from_close_only_row(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-close-only-user",
        email="mt5-close-only@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="34343434",
    )

    open_trade = Trade(
        pubkey="closeonlytrade01",
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=None,
        lot_size=0.01,
        pnl=None,
        commission=-0.25,
        swap=0.0,
        opened_at=datetime(2026, 3, 10, 8, 0, 0),
        closed_at=None,
        mt5_position="55550000",
    )
    db.session.add(open_trade)
    db.session.commit()

    close_only_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "",
                "entry_price": None,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 48.5,
                "commission": -0.5,
                "swap": -0.1,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": None,
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 55550000,
                "trade_note": "close only",
                "is_open": False,
            }
        ],
    }

    close_response = client.post(
        "/api/internal/mt5/sync",
        json=close_only_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    trade = Trade.query.filter_by(
        trade_account_id=trade_account.id,
        mt5_position="55550000",
    ).one()

    assert close_response.status_code == 200
    assert close_response.get_json() == {"saved": 0, "updated": 1, "skipped": 0, "errors": 0}
    assert trade.exit_price == pytest.approx(1.09)
    assert trade.pnl == pytest.approx(48.5)
    assert trade.commission == pytest.approx(-0.5)
    assert trade.swap == pytest.approx(-0.1)
    assert trade.closed_at is not None


def test_internal_mt5_sync_prefers_open_trade_when_duplicate_mt5_positions_exist(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-duplicate-position-user",
        email="mt5-duplicate-position@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="56565656",
    )

    open_trade = Trade(
        pubkey="duplicatetrade001",
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=None,
        lot_size=0.01,
        pnl=None,
        commission=-0.25,
        swap=0.0,
        opened_at=datetime(2026, 3, 21, 8, 0, 0),
        closed_at=None,
        mt5_position="66660000",
    )
    stale_closed_duplicate = Trade(
        pubkey="duplicatetrade002",
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=1.089,
        lot_size=0.01,
        pnl=40.0,
        commission=-0.4,
        swap=-0.05,
        opened_at=datetime(2026, 3, 20, 8, 0, 0),
        closed_at=datetime(2026, 3, 20, 10, 0, 0),
        mt5_position="66660000",
    )
    db.session.add_all([open_trade, stale_closed_duplicate])
    db.session.commit()

    closed_payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.085,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 48.5,
                "commission": -0.5,
                "swap": -0.1,
                "stop_loss": None,
                "take_profit": None,
                "opened_at": "2026-03-21T08:00:00+00:00",
                "closed_at": "2026-03-21T10:00:00+00:00",
                "mt5_position": 66660000,
                "trade_note": "closed",
                "is_open": False,
            }
        ],
    }

    close_response = client.post(
        "/api/internal/mt5/sync",
        json=closed_payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    db.session.expire_all()
    refreshed_open_trade = db.session.get(Trade, open_trade.id)
    refreshed_closed_duplicate = db.session.get(Trade, stale_closed_duplicate.id)

    assert close_response.status_code == 200
    assert close_response.get_json() == {"saved": 0, "updated": 1, "skipped": 0, "errors": 0}
    assert refreshed_open_trade.exit_price == pytest.approx(1.09)
    assert refreshed_open_trade.pnl == pytest.approx(48.5)
    assert refreshed_open_trade.commission == pytest.approx(-0.5)
    assert refreshed_open_trade.swap == pytest.approx(-0.1)
    assert refreshed_open_trade.closed_at == datetime(2026, 3, 21, 10, 0, 0)
    assert refreshed_closed_duplicate.exit_price == pytest.approx(1.089)
    assert refreshed_closed_duplicate.closed_at == datetime(2026, 3, 20, 10, 0, 0)


def test_admin_mt5_create_list_setup_and_trigger_sync(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(client)
    account_number = f"33{uuid.uuid4().int % 10_000_000:07d}"

    create_captured = {}

    def _fake_create_apply_async(*, args, queue, kwargs=None):
        create_captured["args"] = args
        create_captured["queue"] = queue

    import celery_workers.mt5_setup_tasks as mt5_setup_module

    monkeypatch.setattr(mt5_setup_module.setup_mt5_terminal, "apply_async", _fake_create_apply_async)

    create_response = client.post(
        "/dashboard/admin/access/mt5/create",
        data={
            "user_id": str(root_user.id),
            "trade_account_id": str(trade_account.id),
            "account_number": account_number,
            "investor_password": "investor-pass",
            "server": "Broker-Server",
        },
        follow_redirects=False,
    )

    mt5_account = MT5Account.query.filter_by(account_number=account_number).one()
    list_response = client.get("/dashboard/admin/access/mt5")

    setup_captured = {}

    def _fake_setup_apply_async(*, args, queue, kwargs=None):
        setup_captured["args"] = args
        setup_captured["queue"] = queue

    monkeypatch.setattr(mt5_setup_module.setup_mt5_terminal, "apply_async", _fake_setup_apply_async)

    setup_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/setup",
        data={},
        follow_redirects=False,
    )

    sync_captured = {}

    def _fake_sync_apply_async(*, args, queue, kwargs=None):
        sync_captured["args"] = args
        sync_captured["queue"] = queue

    import celery_workers.mt5_sync_tasks as mt5_sync_module
    monkeypatch.setattr(mt5_sync_module.sync_mt5_account, "apply_async", _fake_sync_apply_async)

    trigger_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/sync",
        data={},
        follow_redirects=False,
    )

    mt5_account = db.session.get(MT5Account, mt5_account.id)

    assert create_response.status_code == 302
    assert mt5_account.investor_password_encrypted != "investor-pass"
    assert decrypt_password(mt5_account.investor_password_encrypted) == "investor-pass"
    assert mt5_account.is_active is False
    assert mt5_account.terminal_path is None
    assert mt5_account.appdata_hash is None
    assert create_captured["queue"] == "mt5_setup"
    assert create_captured["args"] == [mt5_account.id]
    assert list_response.status_code == 200
    assert account_number.encode("ascii") in list_response.data
    assert b"Setup Terminal" in list_response.data
    assert b"Terminal not set up yet" in list_response.data
    assert b"AppData hash pending" in list_response.data
    assert setup_response.status_code == 302
    assert setup_captured["queue"] == "mt5_setup"
    assert setup_captured["args"] == [mt5_account.id]
    assert trigger_response.status_code == 302
    assert sync_captured == {}


def test_admin_mt5_manual_trigger_sync_queues_full_history_for_active_account(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(
        client,
        email="root-admin-manual-sync@example.com",
        username="root-admin-manual-sync",
    )
    mt5_account = _create_mt5_account(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        account_number="31313131",
    )
    mt5_account.is_active = True
    db.session.commit()

    sync_captured = {}

    def _fake_sync_apply_async(*, args, kwargs=None, queue):
        sync_captured["args"] = args
        sync_captured["kwargs"] = kwargs
        sync_captured["queue"] = queue

    import celery_workers.mt5_sync_tasks as mt5_sync_module

    monkeypatch.setattr(mt5_sync_module.sync_mt5_account, "apply_async", _fake_sync_apply_async)

    trigger_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/sync",
        data={},
        follow_redirects=False,
    )

    assert trigger_response.status_code == 302
    assert sync_captured["queue"] == "mt5_sync"
    assert sync_captured["args"] == [mt5_account.id]
    assert sync_captured["kwargs"] == {"full_history": True, "trigger_source": "manual"}


def test_admin_clear_all_trade_bars_removes_rows_keeps_trades(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(
        client,
        email="root-clear-bars@example.com",
        username="root-clear-bars",
    )
    trade = Trade(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.101,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position="clear-bars-pos",
    )
    db.session.add(trade)
    db.session.commit()
    db.session.add(
        TradeBars(
            trade_id=trade.id,
            timeframe="M5",
            bar_time=1_700_000_000,
            open=1.1,
            high=1.11,
            low=1.09,
            close=1.105,
            tick_volume=10,
        )
    )
    db.session.commit()
    assert TradeBars.query.count() >= 1

    response = client.post(
        "/dashboard/admin/access/mt5/clear-all-trade-bars",
        data={},
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.expire_all()
    assert TradeBars.query.count() == 0
    assert db.session.get(Trade, trade.id) is not None


def test_admin_mt5_backfill_bars_queues_only_missing_m5_timeframes(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(
        client,
        email="root-backfill-bars@example.com",
        username="root-backfill-bars",
    )
    mt5_account = _create_mt5_account(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        account_number="73737373",
    )
    first_trade = Trade(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.101,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position="backfill-pos-1",
    )
    second_trade = Trade(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.3,
        exit_price=1.299,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 11, 9, 0, 0),
        closed_at=datetime(2026, 4, 11, 10, 0, 0),
        mt5_position="backfill-pos-2",
    )
    db.session.add_all([first_trade, second_trade])
    db.session.commit()
    db.session.add(
        TradeBars(
            trade_id=first_trade.id,
            timeframe="M5",
            bar_time=1_700_000_000,
            open=1.1,
            high=1.11,
            low=1.09,
            close=1.105,
            tick_volume=10,
        )
    )
    db.session.commit()

    queued = []

    import celery_workers.mt5_sync_tasks as mt5_sync_module

    def _fake_apply_async(*, args, queue):
        queued.append({"args": args, "queue": queue})

    monkeypatch.setattr(mt5_sync_module.fetch_trade_bars, "apply_async", _fake_apply_async)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/backfill-bars",
        data={},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert queued == [{"args": [mt5_account.id, second_trade.id], "queue": "mt5_sync"}]


def test_admin_mt5_create_persists_inactive_account_when_setup_queue_fails(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(
        client,
        email="root-admin-queue-fail@example.com",
        username="root-admin-queue-fail",
    )

    import celery_workers.mt5_setup_tasks as mt5_setup_module

    def _failing_apply_async(*, args, queue):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(mt5_setup_module.setup_mt5_terminal, "apply_async", _failing_apply_async)

    response = client.post(
        "/dashboard/admin/access/mt5/create",
        data={
            "user_id": str(root_user.id),
            "trade_account_id": str(trade_account.id),
            "account_number": "66666666",
            "investor_password": "investor-pass",
            "server": "Broker-Server",
        },
        follow_redirects=True,
    )

    mt5_account = MT5Account.query.filter_by(account_number="66666666").one()

    assert response.status_code == 200
    assert b"Account created but setup could not be queued. Click Setup Terminal to retry." in response.data
    assert mt5_account.is_active is False
    assert mt5_account.terminal_path is None
    assert mt5_account.appdata_hash is None


def test_mt5_account_becomes_orphaned_with_trade_account_delete(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    user, trade_account = _create_user_with_account(
        username="mt5-owner",
        email="mt5-owner@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="44444444",
    )
    mt5_account_id = mt5_account.id

    db.session.delete(trade_account)
    db.session.commit()
    db.session.expire_all()

    orphaned_account = db.session.get(MT5Account, mt5_account_id)

    assert orphaned_account is not None
    assert orphaned_account.user_id is None
    assert orphaned_account.trade_account_id is None
    assert orphaned_account.is_orphaned is True
    assert orphaned_account.is_cleanup_only is True
    assert orphaned_account.account_number == "cleanup-4444"
    assert orphaned_account.investor_password_encrypted is None
    assert orphaned_account.is_active is False
    assert orphaned_account.cleanup_marked_at is not None
    assert db.session.get(User, user.id) is not None


def test_mt5_account_becomes_orphaned_with_user_delete(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)

    user, trade_account = _create_user_with_account(
        username="mt5-user-delete",
        email="mt5-user-delete@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="55555555",
    )
    mt5_account_id = mt5_account.id

    deleted_count = delete_users_with_related_data([user.id])
    db.session.commit()
    db.session.expire_all()

    assert deleted_count == 1
    assert db.session.get(User, user.id) is None
    orphaned_account = db.session.get(MT5Account, mt5_account_id)
    assert orphaned_account is not None
    assert orphaned_account.user_id is None
    assert orphaned_account.trade_account_id is None
    assert orphaned_account.is_orphaned is True
    assert orphaned_account.is_cleanup_only is True
    assert orphaned_account.account_number == "cleanup-5555"
    assert orphaned_account.investor_password_encrypted is None
    assert orphaned_account.is_active is False
    assert orphaned_account.cleanup_marked_at is not None


def test_celery_includes_mt5_modules():
    includes = set(celery.conf.include or [])
    assert "celery_workers.mt5_sync_tasks" in includes
    assert "celery_workers.mt5_setup_tasks" in includes
