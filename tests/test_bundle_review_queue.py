"""queue_bundle_review_if_split_candidates after bulk ingest."""

from datetime import datetime

from helpers.core import delete_users_with_related_data, queue_bundle_review_if_split_candidates
from models import Trade, TradeAccount, User, db


def _seed_user_account():
    user = User(
        username="bundle-queue-user",
        email="bundle-queue@example.com",
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(
        user_id=user.id,
        name="Main",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()
    return user, trade_account


def test_queue_bundle_review_false_without_bundle_candidates(app_ctx):
    user, trade_account = _seed_user_account()
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.085,
        exit_price=1.09,
        lot_size=0.01,
        pnl=5.0,
        stop_loss=1.08,
        take_profit=1.095,
        opened_at=datetime(2026, 3, 20, 10, 0, 0),
        closed_at=datetime(2026, 3, 20, 12, 0, 0),
        mt5_position="10001",
    )
    db.session.add(trade)
    db.session.commit()

    assert queue_bundle_review_if_split_candidates(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ) is False
    db.session.refresh(trade_account)
    assert trade_account.bundle_review_requested_at is None

    delete_users_with_related_data([user.id])
    db.session.commit()


def test_queue_bundle_review_true_when_split_like_pair_exists(app_ctx):
    user, trade_account = _seed_user_account()
    # Overlapping EURUSD BUY legs with identical TP/SL (heuristic bundle pair).
    t1 = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.08,
        exit_price=1.09,
        lot_size=0.01,
        pnl=10.0,
        stop_loss=1.07,
        take_profit=1.1,
        opened_at=datetime(2026, 3, 20, 10, 0, 0),
        closed_at=datetime(2026, 3, 20, 12, 0, 0),
        mt5_position="20001",
    )
    t2 = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.081,
        exit_price=1.089,
        lot_size=0.01,
        pnl=8.0,
        stop_loss=1.07,
        take_profit=1.1,
        opened_at=datetime(2026, 3, 20, 11, 0, 0),
        closed_at=datetime(2026, 3, 20, 11, 45, 0),
        mt5_position="20002",
    )
    db.session.add_all([t1, t2])
    db.session.commit()

    assert queue_bundle_review_if_split_candidates(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ) is True
    db.session.refresh(trade_account)
    assert trade_account.bundle_review_requested_at is not None

    delete_users_with_related_data([user.id])
    db.session.commit()


def test_internal_mt5_sync_queues_bundle_review_when_splits_detected(app_ctx, client, monkeypatch):
    from cryptography.fernet import Fernet

    from helpers.utils import encrypt_password
    from models import MT5Account

    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    monkeypatch.setenv("MT5_SYNC_SECRET", "sync-secret")

    user = User(
        username="mt5-bundle-queue-user",
        email="mt5-bundle-queue@example.com",
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.flush()
    trade_account = TradeAccount(
        user_id=user.id,
        name="CFD Main",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.flush()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="55667788",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Server",
        is_active=True,
    )
    db.session.add(mt5_account)
    db.session.commit()

    payload = {
        "mt5_account_id": mt5_account.id,
        "trades": [
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.08,
                "exit_price": 1.09,
                "lot_size": 0.01,
                "pnl": 10.0,
                "commission": -0.2,
                "swap": 0.0,
                "stop_loss": 1.07,
                "take_profit": 1.1,
                "opened_at": "2026-03-20T10:00:00+00:00",
                "closed_at": "2026-03-20T12:00:00+00:00",
                "mt5_position": 30001,
                "trade_note": "a",
                "is_open": False,
            },
            {
                "symbol": "EURUSD",
                "side": "buy",
                "entry_price": 1.081,
                "exit_price": 1.089,
                "lot_size": 0.01,
                "pnl": 8.0,
                "commission": -0.2,
                "swap": 0.0,
                "stop_loss": 1.07,
                "take_profit": 1.1,
                "opened_at": "2026-03-20T11:00:00+00:00",
                "closed_at": "2026-03-20T11:45:00+00:00",
                "mt5_position": 30002,
                "trade_note": "b",
                "is_open": False,
            },
        ],
    }

    response = client.post(
        "/api/internal/mt5/sync",
        json=payload,
        headers={"X-Sync-Secret": "sync-secret"},
    )

    assert response.status_code == 200
    assert response.get_json()["saved"] == 2

    db.session.refresh(trade_account)
    assert trade_account.bundle_review_requested_at is not None

    delete_users_with_related_data([user.id])
    db.session.commit()
