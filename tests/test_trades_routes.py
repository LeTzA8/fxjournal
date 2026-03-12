from models import Trade, TradeAccount, User, db


def _create_logged_in_user(client, username, email):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
    )
    db.session.add(user)
    db.session.flush()

    trade_account = TradeAccount(
        user_id=user.id,
        name="Main Account",
        account_type="CFD",
        is_default=True,
    )
    db.session.add(trade_account)
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = trade_account.id

    return user, trade_account


def test_manual_trade_detail_shows_rr_and_split_fees(app_ctx, client):
    user, _trade_account = _create_logged_in_user(
        client,
        username="trade-route-user",
        email="trade-route@example.com",
    )

    response = client.post(
        "/dashboard/trades/new",
        data={
            "symbol": "EURUSD",
            "side": "BUY",
            "entry_price": "1.10000",
            "exit_price": "1.10140",
            "lot_size": "1.00",
            "stop_loss": "1.09900",
            "take_profit": "1.10200",
            "pnl": "",
            "commission": "-3.50",
            "swap": "-0.80",
            "status": "Closed",
            "opened_at": "2026-03-10T09:00",
            "closed_at": "2026-03-10T10:24",
            "trade_note": "Test trade.",
            "trade_profile_pubkey": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302

    trade = Trade.query.filter_by(user_id=user.id).one()
    assert trade.swap == -0.8
    assert trade.commission == -3.5

    detail_response = client.get(f"/dashboard/trades/{trade.pubkey}")

    assert detail_response.status_code == 200
    assert b'<label for="planned_rr">Planned RR</label>' in detail_response.data
    assert b'value="2.0R"' in detail_response.data
    assert b'<label for="actual_rr">Actual RR</label>' in detail_response.data
    assert b'value="1.4R"' in detail_response.data
    assert b'<label for="commission">Commission</label>' in detail_response.data
    assert b'value="-3.50"' in detail_response.data
    assert b'<label for="swap">Swap</label>' in detail_response.data
    assert b'value="-0.80"' in detail_response.data
    assert b'<label for="net_pnl">Net PnL</label>' in detail_response.data
    assert b'value="135.70"' in detail_response.data


def test_analytics_page_shows_planned_vs_real_rr_panel(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="analytics-rr-user",
        email="analytics-rr@example.com",
    )

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=100.0,
            exit_price=112.0,
            stop_loss=90.0,
            take_profit=120.0,
            lot_size=1.0,
            pnl=120.0,
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=100.0,
            exit_price=108.0,
            stop_loss=90.0,
            take_profit=115.0,
            lot_size=1.0,
            pnl=80.0,
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=100.0,
            exit_price=109.0,
            stop_loss=95.0,
            take_profit=110.0,
            lot_size=1.0,
            pnl=90.0,
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    response = client.get("/dashboard/analytics")

    assert response.status_code == 200
    assert b"Planned vs Real RR" in response.data
    assert b"Avg Planned RR" in response.data
    assert b"1.83R" in response.data
    assert b"1.27R" in response.data
    assert b"69%" in response.data
    assert b"Based on 3 trades with SL &amp; TP set." in response.data
    assert b"close to your planned RR but leaving some on the table" in response.data


def test_analytics_page_shows_rr_empty_state_below_three_trades(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="analytics-rr-empty-user",
        email="analytics-rr-empty@example.com",
    )

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=100.0,
            exit_price=110.0,
            stop_loss=95.0,
            take_profit=115.0,
            lot_size=1.0,
            pnl=100.0,
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=100.0,
            exit_price=96.0,
            stop_loss=105.0,
            take_profit=90.0,
            lot_size=1.0,
            pnl=80.0,
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    response = client.get("/dashboard/analytics")

    assert response.status_code == 200
    assert b"Set stop loss and take profit on your trades to unlock RR analysis." in response.data
    assert b"2 trades so far - need 3 minimum." in response.data
