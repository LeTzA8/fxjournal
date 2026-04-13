"""Integration tests for Running P&L API + cash flow CRUD routes."""

import json
from datetime import datetime

import pytest

from models import AccountCashFlow, Trade, TradeAccount, User, db


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["display_timezone"] = "UTC"


def _create_user(username="testuser"):
    user = User(
        username=username,
        email=f"{username}@test.com",
        password="x",
        email_verified=True,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _create_account(user, name="Main"):
    account = TradeAccount(user_id=user.id, name=name, is_default=True)
    db.session.add(account)
    db.session.flush()
    return account


def _create_trade(user, account, *, pnl=100.0, closed_at=None):
    trade = Trade(
        user_id=user.id,
        trade_account_id=account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.105,
        lot_size=1.0,
        pnl=pnl,
        opened_at=closed_at or datetime(2026, 4, 1),
        closed_at=closed_at or datetime(2026, 4, 1, 12, 0),
    )
    db.session.add(trade)
    db.session.flush()
    return trade


def _create_cash_flow(user, account, *, flow_type="deposit", amount=1000.0, occurred_at=None):
    cf = AccountCashFlow(
        user_id=user.id,
        trade_account_id=account.id,
        flow_type=flow_type,
        amount=amount,
        occurred_at=occurred_at or datetime(2026, 4, 1, 8, 0),
    )
    db.session.add(cf)
    db.session.flush()
    return cf


@pytest.fixture(autouse=True)
def _clean_tables(app_ctx):
    yield
    db.session.rollback()
    for model in (AccountCashFlow, Trade, TradeAccount, User):
        model.query.delete()
    db.session.commit()


class TestRunningPnlApi:
    def test_unauthenticated_redirects(self, client):
        resp = client.get("/api/running-pnl")
        assert resp.status_code in (302, 401)

    def test_empty_account(self, client, app_ctx):
        user = _create_user()
        account = _create_account(user)
        db.session.commit()
        _login(client, user.id)

        resp = client.get("/api/running-pnl")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["events"] == []
        assert data["summary"]["event_count"] == 0

    def test_trades_and_cash_flows_appear(self, client, app_ctx):
        user = _create_user()
        account = _create_account(user)
        _create_trade(user, account, pnl=200.0, closed_at=datetime(2026, 4, 2))
        _create_cash_flow(user, account, flow_type="deposit", amount=5000, occurred_at=datetime(2026, 4, 1))
        db.session.commit()
        _login(client, user.id)

        resp = client.get("/api/running-pnl")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["events"]) == 2
        assert data["events"][0]["event_type"] == "deposit"
        assert data["events"][1]["event_type"] == "trade_close"
        assert data["summary"]["total_realized_pnl"] == pytest.approx(200.0)
        assert data["summary"]["total_cash_flow"] == pytest.approx(5000.0)

    def test_date_range_filter(self, client, app_ctx):
        user = _create_user()
        account = _create_account(user)
        _create_trade(user, account, pnl=100.0, closed_at=datetime(2026, 3, 15))
        _create_trade(user, account, pnl=200.0, closed_at=datetime(2026, 4, 5))
        db.session.commit()
        _login(client, user.id)

        resp = client.get("/api/running-pnl?from=2026-04-01T00:00:00&to=2026-04-10T00:00:00")
        data = resp.get_json()
        assert len(data["events"]) == 1
        assert data["events"][0]["amount"] == pytest.approx(200.0)


class TestCashFlowCrud:
    def test_add_deposit(self, client, app_ctx):
        user = _create_user()
        _create_account(user)
        db.session.commit()
        _login(client, user.id)

        resp = client.post(
            "/dashboard/trade-accounts/cash-flows",
            data=json.dumps({"flow_type": "deposit", "amount": 5000, "occurred_at": "2026-04-01"}),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        assert data["cash_flow"]["flow_type"] == "deposit"
        assert data["cash_flow"]["amount"] == 5000.0

    def test_add_withdrawal(self, client, app_ctx):
        user = _create_user()
        _create_account(user)
        db.session.commit()
        _login(client, user.id)

        resp = client.post(
            "/dashboard/trade-accounts/cash-flows",
            data=json.dumps({"flow_type": "withdrawal", "amount": 1000}),
            content_type="application/json",
        )
        assert resp.status_code == 201

    def test_invalid_flow_type_rejected(self, client, app_ctx):
        user = _create_user()
        _create_account(user)
        db.session.commit()
        _login(client, user.id)

        resp = client.post(
            "/dashboard/trade-accounts/cash-flows",
            data=json.dumps({"flow_type": "bonus", "amount": 100}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_zero_amount_rejected(self, client, app_ctx):
        user = _create_user()
        _create_account(user)
        db.session.commit()
        _login(client, user.id)

        resp = client.post(
            "/dashboard/trade-accounts/cash-flows",
            data=json.dumps({"flow_type": "deposit", "amount": 0}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_list_cash_flows(self, client, app_ctx):
        user = _create_user()
        account = _create_account(user)
        _create_cash_flow(user, account, flow_type="deposit", amount=3000)
        _create_cash_flow(user, account, flow_type="withdrawal", amount=500, occurred_at=datetime(2026, 4, 2))
        db.session.commit()
        _login(client, user.id)

        resp = client.get("/dashboard/trade-accounts/cash-flows")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["cash_flows"]) == 2

    def test_delete_cash_flow(self, client, app_ctx):
        user = _create_user()
        account = _create_account(user)
        cf = _create_cash_flow(user, account)
        db.session.commit()
        cf_id = cf.id
        _login(client, user.id)

        resp = client.delete(f"/dashboard/trade-accounts/cash-flows/{cf_id}")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

        assert db.session.get(AccountCashFlow, cf_id) is None

    def test_delete_other_users_cash_flow_returns_404(self, client, app_ctx):
        user1 = _create_user("user1")
        user2 = _create_user("user2")
        account1 = _create_account(user1, "A1")
        cf = _create_cash_flow(user1, account1)
        db.session.commit()
        cf_id = cf.id
        _login(client, user2.id)

        resp = client.delete(f"/dashboard/trade-accounts/cash-flows/{cf_id}")
        assert resp.status_code == 404
