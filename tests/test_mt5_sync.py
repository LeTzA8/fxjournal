import os
import uuid
from datetime import datetime, timedelta, timezone
import logging
import sys
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from celery_workers.mt5_sync_tasks import (
    _adjust_mt5_unix_epoch,
    _resolve_mt5_server_offset_minutes,
    aggregate_deals_to_trades,
    fetch_trade_bars,
    fetch_trade_bars_batch,
    _positions_to_open_trades,
    sync_mt5_account,
)
from celery_app import celery
from helpers.app_settings import MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY, set_bool_app_setting
from helpers.core import delete_users_with_related_data
from helpers.utils import decrypt_password, encrypt_password
from models import MT5Account, MT5BrokerServerOffset, Trade, TradeAccount, TradeBars, User, db


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
        terminal_path=r"C:\MT5Terminals\test\terminal64.exe",
        is_active=True,
    )
    db.session.add(mt5_account)
    db.session.commit()
    return mt5_account


def _patch_terminal_ready(monkeypatch):
    """Stub process check and pretend ``terminal64.exe`` exists on disk.

    ``ensure_mt5_terminal_ready`` no longer launches the exe; it still requires
    ``os.path.isfile(terminal_path)`` before ``mt5.initialize``.  The fake
    MetaTrader5 module provides initialize/login/account_info stubs.
    """
    monkeypatch.setattr(
        "celery_workers.mt5_setup_tasks._is_terminal_process_running",
        lambda path: (True, 9999),
        raising=False,
    )
    _real_isfile = os.path.isfile

    def _isfile_stub(path):
        if str(path).lower().endswith("terminal64.exe"):
            return True
        return _real_isfile(path)

    monkeypatch.setattr(
        "celery_workers.mt5_setup_tasks.os.path.isfile",
        _isfile_stub,
    )


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


def test_mt5_server_offset_probe_seeds_crypto_alias_when_initial_probe_has_no_tick(monkeypatch):
    monkeypatch.setattr(
        "celery_workers.cache._client",
        lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    broker_offset_minutes = 120
    selected_symbols = []

    def _symbol_select(symbol, select):
        selected_symbols.append(symbol)
        return symbol == "ETHUSD.m"

    def _symbol_info_tick(symbol):
        if symbol == "ETHUSD.m" and symbol in selected_symbols:
            return SimpleNamespace(
                time=int(datetime.now(timezone.utc).timestamp()) + (broker_offset_minutes * 60)
            )
        return None

    fake_mt5 = SimpleNamespace(
        symbol_select=_symbol_select,
        symbol_info_tick=_symbol_info_tick,
    )

    offset_minutes = _resolve_mt5_server_offset_minutes(fake_mt5, 123)

    assert offset_minutes == broker_offset_minutes
    assert selected_symbols[0:3] == ["BTCUSD", "BTCUSD.m", "BTCUSD.r"]
    assert "ETHUSD.m" in selected_symbols


def test_mt5_server_offset_probe_stores_live_offset_by_server_name(app_ctx, monkeypatch):
    monkeypatch.setattr(
        "celery_workers.cache._client",
        lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    broker_offset_minutes = 180
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: SimpleNamespace(
            time=int(datetime.now(timezone.utc).timestamp()) + (broker_offset_minutes * 60)
        ),
        symbol_select=lambda *_args, **_kwargs: False,
    )

    offset_minutes = _resolve_mt5_server_offset_minutes(
        fake_mt5,
        123,
        preferred_symbol="BTCUSD",
        server_name="Broker-Live",
    )

    stored = MT5BrokerServerOffset.query.filter_by(server_key="broker-live").one()
    assert offset_minutes == broker_offset_minutes
    assert stored.server_name == "Broker-Live"
    assert stored.offset_minutes == broker_offset_minutes
    assert stored.probe_symbol == "BTCUSD"
    assert stored.probed_at is not None


def test_mt5_server_offset_probe_snaps_fresh_tick_to_nearest_hour(app_ctx, monkeypatch):
    monkeypatch.setattr(
        "celery_workers.cache._client",
        lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: SimpleNamespace(
            time=int(datetime.now(timezone.utc).timestamp()) + (120 * 60) - 45
        ),
        symbol_select=lambda *_args, **_kwargs: False,
    )

    offset_minutes = _resolve_mt5_server_offset_minutes(
        fake_mt5,
        123,
        preferred_symbol="BTCUSD",
        server_name="Broker-Slightly-Old-Tick",
    )

    assert offset_minutes == 120


def test_mt5_server_offset_probe_rejects_non_hour_delta_and_uses_db_fallback(app_ctx, monkeypatch):
    monkeypatch.setattr(
        "celery_workers.cache._client",
        lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    db.session.add(
        MT5BrokerServerOffset(
            server_name="Broker-Minute-Drift",
            server_key="broker-minute-drift",
            offset_minutes=180,
            probe_symbol="BTCUSD.m",
            probed_at=datetime(2026, 4, 25, 12, 0, 0),
        )
    )
    db.session.commit()
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: SimpleNamespace(
            time=int(datetime.now(timezone.utc).timestamp()) + (90 * 60)
        ),
        symbol_select=lambda *_args, **_kwargs: False,
    )

    offset_minutes = _resolve_mt5_server_offset_minutes(
        fake_mt5,
        123,
        preferred_symbol="BTCUSD",
        server_name="Broker-Minute-Drift",
    )

    assert offset_minutes == 180


def test_mt5_server_offset_probe_rejects_stale_minute_mismatch_and_uses_db_fallback(app_ctx, monkeypatch):
    monkeypatch.setattr(
        "celery_workers.cache._client",
        lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    db.session.add(
        MT5BrokerServerOffset(
            server_name="Broker-Minute-Mismatch",
            server_key="broker-minute-mismatch",
            offset_minutes=120,
            probe_symbol="BTCUSD.m",
            probed_at=datetime(2026, 4, 25, 12, 0, 0),
        )
    )
    db.session.commit()
    now_utc = int(datetime.now(timezone.utc).timestamp())
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: SimpleNamespace(
            time=now_utc + (120 * 60) - (20 * 60)
        ),
        symbol_select=lambda *_args, **_kwargs: False,
    )

    offset_minutes = _resolve_mt5_server_offset_minutes(
        fake_mt5,
        123,
        preferred_symbol="BTCUSD",
        server_name="Broker-Minute-Mismatch",
    )

    assert offset_minutes == 120


def test_mt5_server_offset_uses_db_offset_when_live_probe_unavailable(app_ctx, monkeypatch):
    monkeypatch.setattr(
        "celery_workers.cache._client",
        lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )
    db.session.add(
        MT5BrokerServerOffset(
            server_name="Broker-Down",
            server_key="broker-down",
            offset_minutes=120,
            probe_symbol="BTCUSD.m",
            probed_at=datetime(2026, 4, 25, 12, 0, 0),
        )
    )
    db.session.commit()

    selected_symbols = []
    fake_mt5 = SimpleNamespace(
        symbol_info_tick=lambda symbol: None,
        symbol_select=lambda symbol, select: selected_symbols.append(symbol) or False,
    )

    offset_minutes = _resolve_mt5_server_offset_minutes(
        fake_mt5,
        123,
        preferred_symbol="BTCUSD",
        server_name="broker-down",
    )

    assert offset_minutes == 120
    assert selected_symbols[0:3] == ["BTCUSD", "BTCUSD.m", "BTCUSD.r"]


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
    monkeypatch.setattr(
        "celery_workers.cache.acquire_mt5_global_lock",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: False)

    result = sync_mt5_account.run(123)

    assert result == {"skipped": "sync already running"}


def test_sync_mt5_account_skips_when_global_mt5_lock_busy(monkeypatch):
    monkeypatch.setattr(
        "celery_workers.cache.acquire_mt5_global_lock",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "celery_workers.cache.peek_mt5_global_lock_holder",
        lambda: "setup:task-xyz:42",
    )

    result = sync_mt5_account.run(123)

    assert result == {"skipped": "global MT5 lock busy"}


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
    _patch_terminal_ready(monkeypatch)

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


def test_sync_mt5_account_completion_stamps_last_synced_on_successful_worker_run(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-worker-stamp-user",
        email="mt5-worker-stamp@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="46464646",
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
        symbol_info_tick=lambda symbol: None,
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 0, "updated": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    sync_mt5_account.run(mt5_account.id)

    db.session.expire_all()
    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert refreshed.last_synced_at is not None
    assert refreshed.last_synced_at > datetime(2024, 6, 1, 12, 0, 0)
    # last_full_history_sync_at starts None → full-history path → must also be stamped
    assert refreshed.last_full_history_sync_at is not None
    assert refreshed.last_full_history_sync_at >= refreshed.last_synced_at


def test_sync_mt5_account_rolling_run_does_not_stamp_last_full_history_sync_at(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-rolling-stamp-user",
        email="mt5-rolling-stamp@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="46464647",
    )
    prior_full = datetime(2025, 1, 1, 0, 0, 0)
    mt5_account.last_synced_at = datetime(2024, 6, 1, 12, 0, 0)
    mt5_account.last_full_history_sync_at = prior_full
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
        symbol_info_tick=lambda symbol: None,
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 0, "updated": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    sync_mt5_account.run(mt5_account.id)

    db.session.expire_all()
    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert refreshed.last_synced_at > datetime(2024, 6, 1, 12, 0, 0)
    assert refreshed.last_full_history_sync_at == prior_full


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
        time=1_710_000_100,
        comment="",
    )

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
        ],
        positions_get=lambda: [open_position],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

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
        time=1_710_000_100,
        comment="",
    )

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
        ],
        positions_get=lambda: [open_position],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

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
    assert "latest_deal_utc=2024-03-09T16:00:00+00:00" in caplog.text
    assert "latest_deal_position=4004" in caplog.text
    assert "open_positions=1" in caplog.text
    assert "open_position_ids=9001" in caplog.text
    assert "Skip Reasons" not in caplog.text


def test_sync_mt5_account_warns_when_history_is_stale_but_db_still_has_open_mt5_trade(app_ctx, monkeypatch, caplog):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-history-stale-user",
        email="mt5-history-stale@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="31313131",
    )
    mt5_account.last_synced_at = datetime(2024, 6, 1, 12, 0, 0)
    db.session.add(mt5_account)
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=None,
            lot_size=0.1,
            opened_at=datetime(2026, 4, 14, 12, 0, 0),
            closed_at=None,
            mt5_position="9001",
        )
    )
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
        history_deals_get=lambda *args, **kwargs: [
            _deal(position_id=4004, price=1.25, time=1_710_000_000, symbol="EURUSD"),
        ],
        positions_get=lambda: [],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "saved": 0,
                "updated": 0,
                "skipped": 11,
                "errors": 0,
                "skip_reasons": {
                    "close_only_without_existing_open": 0,
                    "existing_already_closed_or_no_state_change": 11,
                    "incoming_close_validation_failed": 0,
                    "batch_duplicate_mt5_position": 0,
                    "batch_validation_skipped": 0,
                    "batch_symbol_validation_failed": 0,
                },
            }

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    caplog.set_level(logging.WARNING, logger="celery_workers.mt5_sync_tasks")

    sync_mt5_account.run(mt5_account.id)

    assert "MT5 sync noop mt5_account_id=" in caplog.text
    assert "open_positions=0" in caplog.text
    assert "latest_deal_lag_min=" in caplog.text
    assert "db_open_mt5=1" in caplog.text
    assert "history_stale=1" in caplog.text
    assert "MT5 sync soft reconnect" in caplog.text
    assert "soft_reconnect=1" in caplog.text


def test_sync_mt5_account_soft_reconnect_refetches_history_once(app_ctx, monkeypatch, caplog):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-soft-reconnect-user",
        email="mt5-soft-reconnect@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="42424242",
    )
    mt5_account.last_synced_at = datetime(2024, 6, 1, 12, 0, 0)
    db.session.add(mt5_account)
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=None,
            lot_size=0.1,
            opened_at=datetime(2026, 4, 14, 12, 0, 0),
            closed_at=None,
            mt5_position="7777",
        )
    )
    db.session.commit()

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    init_calls = []

    def _initialize(**kwargs):
        init_calls.append(1)
        return True

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=_initialize,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=int(mt5_account.account_number)),
        history_deals_get=lambda *args, **kwargs: [
            _deal(position_id=4004, price=1.25, time=1_710_000_000, symbol="EURUSD"),
        ],
        positions_get=lambda: [],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "saved": 0,
                "updated": 0,
                "skipped": 0,
                "errors": 0,
                "skip_reasons": {},
            }

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    caplog.set_level(logging.WARNING, logger="celery_workers.mt5_sync_tasks")

    sync_mt5_account.run(mt5_account.id)

    assert len(init_calls) == 2
    assert "MT5 sync soft reconnect" in caplog.text


def test_sync_mt5_account_soft_reconnect_disabled_by_env(app_ctx, monkeypatch, caplog):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")
    monkeypatch.setenv("FXJ_MT5_SOFT_RECONNECT_ON_STALE", "0")

    user, trade_account = _create_user_with_account(
        username="mt5-no-soft-reconnect-user",
        email="mt5-no-soft-reconnect@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="43434343",
    )
    mt5_account.last_synced_at = datetime(2024, 6, 1, 12, 0, 0)
    db.session.add(mt5_account)
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=None,
            lot_size=0.1,
            opened_at=datetime(2026, 4, 14, 12, 0, 0),
            closed_at=None,
            mt5_position="8888",
        )
    )
    db.session.commit()

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    init_calls = []

    def _initialize(**kwargs):
        init_calls.append(1)
        return True

    fake_mt5 = SimpleNamespace(
        DEAL_ENTRY_IN=0,
        DEAL_ENTRY_OUT=1,
        DEAL_ENTRY_INOUT=2,
        DEAL_ENTRY_OUT_BY=3,
        DEAL_TYPE_BUY=0,
        initialize=_initialize,
        login=lambda *args, **kwargs: True,
        account_info=lambda: SimpleNamespace(login=int(mt5_account.account_number)),
        history_deals_get=lambda *args, **kwargs: [
            _deal(position_id=4004, price=1.25, time=1_710_000_000, symbol="EURUSD"),
        ],
        positions_get=lambda: [],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "saved": 0,
                "updated": 0,
                "skipped": 11,
                "errors": 0,
                "skip_reasons": {
                    "existing_already_closed_or_no_state_change": 11,
                },
            }

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", lambda *args, **kwargs: DummyResponse())

    caplog.set_level(logging.WARNING, logger="celery_workers.mt5_sync_tasks")

    sync_mt5_account.run(mt5_account.id)

    assert len(init_calls) == 1
    assert "MT5 sync soft reconnect" not in caplog.text
    assert "soft_reconnect=0" in caplog.text


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


def test_sync_mt5_account_uses_utc_history_window_without_server_shift(app_ctx, monkeypatch):
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
        symbol_info_tick=lambda symbol: None,
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

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

    assert captured_to_date > utc_now - timedelta(minutes=5)
    assert captured_to_date < utc_now + timedelta(minutes=5)
    assert captured_from_date < captured_to_date


def test_sync_mt5_account_posts_history_scope_full_until_full_history_stamped(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-history-scope-user",
        email="mt5-history-scope@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="61616161",
    )

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    def _empty_history(from_date, to_date):
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
        history_deals_get=_empty_history,
        positions_get=lambda: [],
        symbol_info_tick=lambda symbol: None,
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 0, "updated": 0, "skipped": 0, "errors": 0}

    captured = {}

    def capture_post(url, json=None, **kwargs):
        captured["json"] = json
        return DummyResponse()

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", capture_post)

    sync_mt5_account.run(mt5_account.id)
    assert captured["json"].get("history_scope") == "full"

    mt5_account.last_full_history_sync_at = datetime(2026, 1, 1, 12, 0, 0)
    db.session.commit()

    sync_mt5_account.run(mt5_account.id)
    assert captured["json"].get("history_scope") == "rolling"


def test_sync_mt5_account_shifts_history_window_into_broker_time(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-window-offset-user",
        email="mt5-window-offset@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="52525252",
    )

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    history_call = {}
    broker_offset_minutes = 120

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
        positions_get=lambda: [SimpleNamespace(symbol="XAUUSD")],
        symbol_info_tick=lambda symbol: SimpleNamespace(
            time=int(datetime.now(timezone.utc).timestamp()) + (broker_offset_minutes * 60)
        ),
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

    captured_from_date = history_call["from_date"]
    captured_to_date = history_call["to_date"]
    utc_now = datetime.now(timezone.utc)
    expected_from_date = datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=broker_offset_minutes)

    assert captured_from_date == expected_from_date
    assert captured_to_date > utc_now + timedelta(minutes=broker_offset_minutes - 5)
    assert captured_to_date < utc_now + timedelta(minutes=broker_offset_minutes + 5)


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
    _patch_terminal_ready(monkeypatch)

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


def test_sync_mt5_account_overlays_positions_profit_onto_open_deal_row(app_ctx, monkeypatch):
    """Open rows from deal history use pnl=None; live profit from positions_get must be merged."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-overlay-user",
        email="mt5-overlay@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="55557777",
    )

    monkeypatch.setattr("celery_workers.cache.claim_lock", lambda *args, **kwargs: True)
    monkeypatch.setattr("celery_workers.cache.release_lock", lambda *args, **kwargs: True)

    entry_deal = _deal(
        position_id=9001,
        symbol="EURUSD",
        price=1.085,
        volume=0.1,
        time=1_710_000_000,
        entry=0,
        type=0,
        profit=0.0,
    )

    open_position = SimpleNamespace(
        identifier=9001,
        type=0,
        symbol="EURUSD",
        price_open=1.085,
        price_current=1.091,
        volume=0.1,
        commission=-0.5,
        swap=0.0,
        profit=42.5,
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
        history_deals_get=lambda *args, **kwargs: [entry_deal],
        positions_get=lambda: [open_position],
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)
    _patch_terminal_ready(monkeypatch)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 0, "updated": 0, "skipped": 0, "errors": 0}

    def capture_post(url, json=None, **kwargs):
        captured_payload.update(json or {})
        return DummyResponse()

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", capture_post)

    sync_mt5_account.run(mt5_account.id)

    trades_sent = captured_payload.get("trades", [])
    assert len(trades_sent) == 1
    assert trades_sent[0]["mt5_position"] == "9001"
    assert trades_sent[0]["is_open"] is True
    assert trades_sent[0]["pnl"] == pytest.approx(42.5)


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
    _patch_terminal_ready(monkeypatch)

    def _fake_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        raise AssertionError("requests.post should not run when the MT5 login is wrong")

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", _fake_post)
    retry_calls = []

    def _fake_retry(exc=None, **kwargs):
        retry_calls.append({"exc": exc, **kwargs})
        return exc

    monkeypatch.setattr(sync_mt5_account, "retry", _fake_retry)

    with pytest.raises(RuntimeError, match="Wrong account logged in"):
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


def test_internal_mt5_sync_empty_payload_stamps_last_synced_without_history_scope(app_ctx, client, monkeypatch):
    """Successful sync always stamps last_synced_at; full-history marker requires explicit history_scope."""
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
    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert refreshed.last_synced_at is not None
    assert refreshed.last_full_history_sync_at is None


def test_internal_mt5_sync_history_scope_full_stamps_full_history_marker(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-full-scope",
        email="sync-full-scope@example.com",
    )
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)

    resp = client.post(
        "/api/internal/mt5/sync",
        json={"mt5_account_id": mt5_account.id, "trades": [], "history_scope": "full"},
        headers={"X-Sync-Secret": "sync-secret"},
    )
    assert resp.status_code == 200
    db.session.expire_all()
    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert refreshed.last_synced_at is not None
    assert refreshed.last_full_history_sync_at is not None
    assert refreshed.last_full_history_sync_at == refreshed.last_synced_at


def test_internal_mt5_sync_updates_vm_id_on_success(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-vm-id",
        email="sync-vm-id@example.com",
    )
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)
    db.session.commit()

    response = client.post(
        "/api/internal/mt5/sync",
        json={
            "mt5_account_id": mt5_account.id,
            "vm_id": "vm-1",
            "trades": [
                {
                    "symbol": "XAUUSD",
                    "side": "BUY",
                    "entry_price": 3000.0,
                    "exit_price": 3012.0,
                    "lot_size": 0.1,
                    "pnl": 12.0,
                    "commission": -0.3,
                    "swap": 0.0,
                    "opened_at": "2026-04-10T08:00:00+00:00",
                    "closed_at": "2026-04-10T09:00:00+00:00",
                    "mt5_position": 99887766,
                }
            ],
        },
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert response.status_code == 200
    db.session.expire_all()
    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert refreshed.last_synced_at is not None
    assert refreshed.vm_id == "vm-1"
    assert refreshed.connection_status == MT5Account.CONNECTION_STATUS_CONNECTED
    assert refreshed.connection_error_message is None


def test_internal_mt5_sync_clears_failed_connection_status(app_ctx, client, monkeypatch):
    """A successful sync should reset connection_status from 'failed' back to 'connected'."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="sync-clear-failed",
        email="sync-clear-failed@example.com",
    )
    mt5_account = _create_mt5_account(user_id=user.id, trade_account_id=trade_account.id)
    mt5_account.connection_status = MT5Account.CONNECTION_STATUS_FAILED
    mt5_account.connection_error_message = "Previous setup error"
    mt5_account.last_synced_at = datetime(2024, 6, 1, 12, 0, 0)
    db.session.commit()

    response = client.post(
        "/api/internal/mt5/sync",
        json={
            "mt5_account_id": mt5_account.id,
            "vm_id": "vm-1",
            "trades": [],
        },
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert response.status_code == 200
    db.session.expire_all()
    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert refreshed.connection_status == MT5Account.CONNECTION_STATUS_CONNECTED
    assert refreshed.connection_error_message is None


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


def test_internal_mt5_trade_bars_batch_replaces_rows_for_multiple_trades(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="trade-bars-batch-ingest-user",
        email="trade-bars-batch-ingest@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="81818181",
    )
    first_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.101,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position="818181",
    )
    second_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.25,
        exit_price=1.245,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 11, 0, 0),
        closed_at=datetime(2026, 4, 10, 12, 0, 0),
        mt5_position="818182",
    )
    db.session.add_all([first_trade, second_trade])
    db.session.commit()

    response = client.post(
        "/api/internal/mt5/trade-bars/batch",
        json={
            "mt5_account_id": mt5_account.id,
            "timeframe": "M5",
            "items": [
                {
                    "trade_id": first_trade.id,
                    "bars": [
                        {"time": 1_700_000_000, "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 100}
                    ],
                },
                {
                    "trade_id": second_trade.id,
                    "bars": [
                        {"time": 1_700_000_300, "open": 1.25, "high": 1.251, "low": 1.249, "close": 1.2505, "tick_volume": 120},
                        {"time": 1_700_000_600, "open": 1.2505, "high": 1.252, "low": 1.25, "close": 1.2515, "tick_volume": 140},
                    ],
                },
            ],
        },
        headers={"X-Sync-Secret": "sync-secret"},
    )

    first_rows = TradeBars.query.filter_by(trade_id=first_trade.id, timeframe="M5").all()
    second_rows = TradeBars.query.filter_by(trade_id=second_trade.id, timeframe="M5").all()

    assert response.status_code == 200
    assert response.get_json()["saved"] == 3
    assert len(first_rows) == 1
    assert len(second_rows) == 2


def test_fetch_trade_bars_normalizes_server_epoch_with_stored_server_offset(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")
    monkeypatch.setattr(
        "celery_workers.cache._client",
        lambda: (_ for _ in ()).throw(RuntimeError("cache unavailable")),
    )

    user, trade_account = _create_user_with_account(
        username="trade-bars-stored-offset-user",
        email="trade-bars-stored-offset@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="84848484",
    )
    mt5_account.server = "Broker-Stored-Offset"
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
        mt5_position="848484",
    )
    db.session.add(
        MT5BrokerServerOffset(
            server_name="Broker-Stored-Offset",
            server_key="broker-stored-offset",
            offset_minutes=120,
            probe_symbol="BTCUSD.m",
            probed_at=datetime(2026, 4, 9, 12, 0, 0),
        )
    )
    db.session.add(trade)
    db.session.commit()

    broker_offset_minutes = 120
    bar_time_utc = int(datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc).timestamp())
    raw_bar_time = bar_time_utc + (broker_offset_minutes * 60)
    copy_calls = []
    posted_payload = {}

    def _copy_rates_range(symbol, timeframe, date_from, date_to):
        copy_calls.append((date_from, date_to))
        return [
            {
                "time": raw_bar_time,
                "open": 1.1,
                "high": 1.101,
                "low": 1.099,
                "close": 1.1005,
                "tick_volume": 100,
            }
        ]

    fake_mt5 = SimpleNamespace(
        TIMEFRAME_M5=5,
        TIMEFRAME_M15=15,
        TIMEFRAME_H1=60,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        copy_rates_range=_copy_rates_range,
        symbol_info_tick=lambda symbol: None,
        symbol_select=lambda symbol, select: False,
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 1, "timeframe": "M5"}

    def _fake_post(url, json, headers, timeout):
        posted_payload["json"] = json
        return DummyResponse()

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", _fake_post)

    result = fetch_trade_bars.run(mt5_account.id, trade.id)

    assert result == {"saved": 1, "timeframe": "M5"}
    assert len(copy_calls) == 1
    expected_start = datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc) - timedelta(hours=36)
    expected_end = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc) + timedelta(hours=12)
    assert copy_calls[0] == (
        expected_start + timedelta(minutes=broker_offset_minutes),
        expected_end + timedelta(minutes=broker_offset_minutes),
    )
    assert posted_payload["json"]["bars"][0]["time"] == bar_time_utc


def test_fetch_trade_bars_does_not_query_utc_fallback_when_broker_window_has_no_rates(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="trade-bars-fallback-user",
        email="trade-bars-fallback@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="83838383",
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
        mt5_position="838383",
    )
    db.session.add(trade)
    db.session.commit()

    broker_offset_minutes = 120
    copy_calls = []
    post_calls = []

    def _copy_rates_range(symbol, timeframe, date_from, date_to):
        copy_calls.append((symbol, date_from, date_to))
        return []

    fake_mt5 = SimpleNamespace(
        TIMEFRAME_M5=5,
        TIMEFRAME_M15=15,
        TIMEFRAME_H1=60,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        copy_rates_range=_copy_rates_range,
        symbol_info_tick=lambda symbol: SimpleNamespace(
            time=int(datetime.now(timezone.utc).timestamp()) + (broker_offset_minutes * 60)
        ),
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 1, "timeframe": "M5"}

    def _fake_post(url, json, headers, timeout):
        post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return DummyResponse()

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", _fake_post)

    result = fetch_trade_bars.run(mt5_account.id, trade.id)

    assert result == {"saved": 0, "timeframe": "M5"}
    assert len(copy_calls) >= 1
    expected_start = datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc) - timedelta(hours=36)
    expected_end = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc) + timedelta(hours=12)
    for _symbol, date_from, date_to in copy_calls:
        assert (date_from, date_to) == (
            expected_start + timedelta(minutes=broker_offset_minutes),
            expected_end + timedelta(minutes=broker_offset_minutes),
    )
    assert post_calls == []


def test_fetch_trade_bars_batch_posts_one_bulk_payload_and_skips_existing(app_ctx, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")
    monkeypatch.setenv("FLASK_API_URL", "https://example.com")

    user, trade_account = _create_user_with_account(
        username="trade-bars-batch-user",
        email="trade-bars-batch@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="82828282",
    )
    first_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.101,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position="828281",
    )
    second_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.25,
        exit_price=1.245,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 11, 0, 0),
        closed_at=datetime(2026, 4, 10, 12, 0, 0),
        mt5_position="828282",
    )
    skipped_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="AUDUSD",
        side="BUY",
        entry_price=0.65,
        exit_price=0.651,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 13, 0, 0),
        closed_at=datetime(2026, 4, 10, 14, 0, 0),
        mt5_position="828283",
    )
    db.session.add_all([first_trade, second_trade, skipped_trade])
    db.session.flush()
    db.session.add_all(
        [
            TradeBars(
                trade_id=skipped_trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 4, 10, 13, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=0.65,
                high=0.652,
                low=0.649,
                close=0.651,
            ),
            TradeBars(
                trade_id=skipped_trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 4, 10, 14, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=0.651,
                high=0.653,
                low=0.650,
                close=0.652,
            ),
            TradeBars(
                trade_id=skipped_trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 4, 11, 2, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=0.652,
                high=0.653,
                low=0.650,
                close=0.651,
            ),
        ]
    )
    db.session.commit()

    copy_calls = []
    raw_bar_time = int(datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc).timestamp())

    def _copy_rates_range(symbol, timeframe, date_from, date_to):
        copy_calls.append(symbol)
        return [
            {
                "time": raw_bar_time + (len(copy_calls) * 300),
                "open": 1.1,
                "high": 1.101,
                "low": 1.099,
                "close": 1.1005,
                "tick_volume": 100,
            }
        ]

    fake_mt5 = SimpleNamespace(
        TIMEFRAME_M5=5,
        TIMEFRAME_M15=15,
        TIMEFRAME_H1=60,
        initialize=lambda **kwargs: True,
        login=lambda *args, **kwargs: True,
        copy_rates_range=_copy_rates_range,
        symbol_info_tick=lambda symbol: None,
        symbol_select=lambda symbol, select: True,
        shutdown=lambda: True,
        last_error=lambda: (0, "ok"),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5)

    post_calls = []

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"saved": 2, "items": []}

    def _fake_post(url, json, headers, timeout):
        post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return DummyResponse()

    monkeypatch.setattr("celery_workers.mt5_sync_tasks.requests.post", _fake_post)

    result = fetch_trade_bars_batch.run(
        mt5_account.id,
        [first_trade.id, second_trade.id, skipped_trade.id],
    )

    assert result == {"saved": 2, "items": []}
    assert len(post_calls) == 1
    assert post_calls[0]["url"] == "https://example.com/api/internal/mt5/trade-bars/batch"
    assert post_calls[0]["timeout"] == 60
    assert [item["trade_id"] for item in post_calls[0]["json"]["items"]] == [
        first_trade.id,
        second_trade.id,
    ]
    assert skipped_trade.id not in [item["trade_id"] for item in post_calls[0]["json"]["items"]]


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


def test_internal_mt5_sync_auto_queues_bars_for_public_user_when_enabled(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-auto-bars-public-user",
        email="mt5-auto-bars-public@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="34343434",
    )
    set_bool_app_setting(MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY, True)
    db.session.commit()

    queued = []

    def _fake_dispatch(task, *, args=None, kwargs=None, queue=None, log=None, label=None, extra=None):
        queued.append(
            {
                "task": getattr(task, "name", ""),
                "args": args,
                "queue": queue,
                "label": label,
                "extra": extra,
            }
        )
        return SimpleNamespace(id=f"queued-{len(queued)}")

    monkeypatch.setattr("routes.mt5_internal.dispatch_celery_task", _fake_dispatch)

    response = client.post(
        "/api/internal/mt5/sync",
        json={
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
                    "opened_at": "2026-03-22T08:00:00+00:00",
                    "closed_at": "2026-03-22T10:00:00+00:00",
                    "mt5_position": 34343434,
                    "trade_note": "closed",
                    "is_open": False,
                }
            ],
        },
        headers={"X-Sync-Secret": "sync-secret"},
    )

    trade = Trade.query.filter_by(
        trade_account_id=trade_account.id,
        mt5_position="34343434",
    ).one()

    assert response.status_code == 200
    assert response.get_json()["auto_bar_sync_queued"] == 1
    assert queued == [
        {
            "task": "celery_workers.mt5_sync_tasks.fetch_trade_bars_batch",
            "args": [mt5_account.id, [trade.id]],
            "queue": "mt5_priority",
            "label": "mt5_auto_bar_sync_batch_after_ingest",
            "extra": {
                "mt5_account_id": mt5_account.id,
                "trade_ids": [trade.id],
                "user_id": user.id,
                "trade_account_id": trade_account.id,
                "trade_count": 1,
            },
        }
    ]


def test_internal_mt5_sync_auto_queues_bars_for_existing_closed_trade_on_empty_beat(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user, trade_account = _create_user_with_account(
        username="mt5-auto-bars-sweep-user",
        email="mt5-auto-bars-sweep@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="56565656",
    )
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=1.09,
        lot_size=0.01,
        pnl=48.5,
        opened_at=datetime(2026, 3, 20, 8, 0, 0),
        closed_at=datetime(2026, 3, 20, 10, 0, 0),
        mt5_position="56565656",
    )
    db.session.add(trade)
    set_bool_app_setting(MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY, True)
    db.session.commit()
    db.session.add_all(
        [
            TradeBars(
                trade_id=trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 3, 20, 8, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=1.085,
                high=1.09,
                low=1.08,
                close=1.087,
            ),
            TradeBars(
                trade_id=trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 3, 20, 11, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=1.087,
                high=1.091,
                low=1.086,
                close=1.09,
            ),
        ]
    )
    db.session.commit()

    queued = []

    def _fake_dispatch(task, *, args=None, kwargs=None, queue=None, log=None, label=None, extra=None):
        queued.append({"args": args, "queue": queue, "label": label})
        return SimpleNamespace(id=f"queued-{len(queued)}")

    monkeypatch.setattr("routes.mt5_internal.dispatch_celery_task", _fake_dispatch)
    response = client.post(
        "/api/internal/mt5/sync",
        json={"mt5_account_id": mt5_account.id, "trades": []},
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert response.status_code == 200
    assert response.get_json()["auto_bar_sync_queued"] == 1
    assert queued == [
        {
            "args": [mt5_account.id, [trade.id]],
            "queue": "mt5_priority",
            "label": "mt5_auto_bar_sync_batch_after_ingest",
        }
    ]


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
    assert f"MT5 ID: {mt5_account.id}".encode("ascii") in list_response.data
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
    assert sync_captured["queue"] == "mt5_priority"
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
            bar_time=int(datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc).timestamp()),
            open=1.1,
            high=1.11,
            low=1.09,
            close=1.105,
            tick_volume=10,
        )
    )
    db.session.add(
        TradeBars(
            trade_id=first_trade.id,
            timeframe="M5",
            bar_time=int(datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc).timestamp()),
            open=1.105,
            high=1.11,
            low=1.1,
            close=1.101,
            tick_volume=12,
        )
    )
    db.session.add(
        TradeBars(
            trade_id=first_trade.id,
            timeframe="M5",
            bar_time=int(datetime(2026, 4, 10, 22, 0, 0, tzinfo=timezone.utc).timestamp()),
            open=1.101,
            high=1.11,
            low=1.1,
            close=1.102,
            tick_volume=12,
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
    assert queued == [{"args": [mt5_account.id, second_trade.id], "queue": "mt5_priority"}]


def test_admin_mt5_backfill_bars_requeues_trade_with_incomplete_recent_m5_coverage(app_ctx, client, monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    root_user, trade_account = _log_in_root_admin(
        client,
        email="root-backfill-bars-incomplete@example.com",
        username="root-backfill-bars-incomplete",
    )
    mt5_account = _create_mt5_account(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        account_number="74747474",
    )
    older_trade = Trade(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.101,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 1, 9, 0, 0),
        closed_at=datetime(2026, 4, 1, 10, 0, 0),
        mt5_position="backfill-complete-pos",
    )
    recent_trade = Trade(
        user_id=root_user.id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.3,
        exit_price=1.299,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 12, 9, 0, 0),
        closed_at=datetime(2026, 4, 12, 10, 0, 0),
        mt5_position="backfill-incomplete-pos",
    )
    db.session.add_all([older_trade, recent_trade])
    db.session.commit()
    db.session.add_all(
        [
            TradeBars(
                trade_id=older_trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=1.1,
                high=1.11,
                low=1.09,
                close=1.105,
                tick_volume=10,
            ),
            TradeBars(
                trade_id=older_trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=1.105,
                high=1.11,
                low=1.1,
                close=1.101,
                tick_volume=12,
            ),
            TradeBars(
                trade_id=older_trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 4, 1, 22, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=1.101,
                high=1.11,
                low=1.1,
                close=1.102,
                tick_volume=12,
            ),
            TradeBars(
                trade_id=recent_trade.id,
                timeframe="M5",
                bar_time=int(datetime(2026, 4, 12, 9, 0, 0, tzinfo=timezone.utc).timestamp()),
                open=1.3,
                high=1.301,
                low=1.299,
                close=1.3005,
                tick_volume=8,
            ),
        ]
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
    assert queued == [{"args": [mt5_account.id, recent_trade.id], "queue": "mt5_priority"}]


def test_sync_all_active_mt5_accounts_uses_sync_queue_with_short_expiry(app_ctx, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    for existing_account in MT5Account.query.all():
        existing_account.is_active = False
    db.session.commit()

    user, trade_account = _create_user_with_account(
        username="beat-queue-user",
        email="beat-queue-user@example.com",
    )
    mt5_account = _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="85858585",
    )

    captured = []

    def _fake_apply_async(*, args, kwargs=None, queue, expires=None):
        captured.append(
            {
                "args": args,
                "kwargs": kwargs,
                "queue": queue,
                "expires": expires,
            }
        )

    import celery_workers.mt5_sync_tasks as mt5_sync_module

    monkeypatch.setattr("celery_workers.cache.get_queue_depth", lambda queue_name: 0)
    monkeypatch.setattr(mt5_sync_module.sync_mt5_account, "apply_async", _fake_apply_async)

    mt5_sync_module.sync_all_active_mt5_accounts.run()

    assert captured == [
        {
            "args": [mt5_account.id],
            "kwargs": {"trigger_source": "beat"},
            "queue": "mt5_sync",
            "expires": 28,
        }
    ]


def test_sync_all_active_mt5_accounts_skips_when_sync_queues_are_backed_up(app_ctx, monkeypatch, caplog):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    for existing_account in MT5Account.query.all():
        existing_account.is_active = False
    db.session.commit()

    user, trade_account = _create_user_with_account(
        username="beat-backup-user",
        email="beat-backup-user@example.com",
    )
    _create_mt5_account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="86868686",
    )

    captured = []

    def _fake_apply_async(*, args, kwargs=None, queue, expires=None):
        captured.append(
            {
                "args": args,
                "kwargs": kwargs,
                "queue": queue,
                "expires": expires,
            }
        )

    def _fake_get_queue_depth(queue_name):
        if queue_name == "mt5_sync":
            return 140
        if queue_name == "mt5_priority":
            return 11
        return 0

    import celery_workers.mt5_sync_tasks as mt5_sync_module

    monkeypatch.setattr("celery_workers.cache.get_queue_depth", _fake_get_queue_depth)
    monkeypatch.setattr(mt5_sync_module.sync_mt5_account, "apply_async", _fake_apply_async)
    caplog.set_level(logging.WARNING, logger="celery_workers.mt5_sync_tasks")

    mt5_sync_module.sync_all_active_mt5_accounts.run()

    assert captured == []
    assert "MT5 beat skipped queue_depth_total=151" in caplog.text


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
