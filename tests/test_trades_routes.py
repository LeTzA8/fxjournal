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
