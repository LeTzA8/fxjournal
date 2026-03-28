from datetime import datetime

import routes.trades as trades_routes
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


def test_trade_detail_treats_closed_timestamp_trade_as_closed(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-detail-closed-user",
        email="trade-detail-closed@example.com",
    )

    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=None,
        lot_size=1.00,
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    db.session.add(trade)
    db.session.commit()

    detail_response = client.get(f"/dashboard/trades/{trade.pubkey}")

    assert detail_response.status_code == 200
    assert b'<input id="status" type="text" value="Closed" readonly>' in detail_response.data
    assert b'<input id="exit_price" type="text" value="-" readonly>' in detail_response.data


def test_trade_detail_shows_import_note_separately(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-import-note-user",
        email="trade-import-note@example.com",
    )

    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10120,
        lot_size=1.00,
        pnl=120.0,
        trade_note="User note stays here.",
        system_trade_note="Imported from MT5 Positions",
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    db.session.add(trade)
    db.session.commit()

    detail_response = client.get(f"/dashboard/trades/{trade.pubkey}")

    assert detail_response.status_code == 200
    assert b'<label for="trade_note">Trade Note</label>' in detail_response.data
    assert b"User note stays here." in detail_response.data
    assert b'<label for="system_trade_note">Import Note</label>' in detail_response.data
    assert b"Imported from MT5 Positions" in detail_response.data


def test_trade_detail_shows_only_relevant_movement_metric_by_account_type(app_ctx, client):
    user, cfd_account = _create_logged_in_user(
        client,
        username="trade-detail-metric-user",
        email="trade-detail-metric@example.com",
    )
    futures_account = TradeAccount(
        user_id=user.id,
        name="Futures Account",
        account_type="FUTURES",
        is_default=False,
    )
    db.session.add(futures_account)
    db.session.flush()

    cfd_trade = Trade(
        user_id=user.id,
        trade_account_id=cfd_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10120,
        lot_size=1.00,
        pnl=120.0,
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    futures_trade = Trade(
        user_id=user.id,
        trade_account_id=futures_account.id,
        symbol="ES",
        contract_code="ESM26",
        side="BUY",
        entry_price=5200.00,
        exit_price=5201.00,
        lot_size=1.00,
        pnl=50.0,
        opened_at=datetime(2026, 3, 10, 11, 0, 0),
        closed_at=datetime(2026, 3, 10, 11, 20, 0),
    )
    db.session.add_all([cfd_trade, futures_trade])
    db.session.commit()

    cfd_response = client.get(f"/dashboard/trades/{cfd_trade.pubkey}")
    futures_response = client.get(f"/dashboard/trades/{futures_trade.pubkey}")

    assert cfd_response.status_code == 200
    assert b'<label for="pips">Pips</label>' in cfd_response.data
    assert b'<label for="ticks">Ticks</label>' not in cfd_response.data

    assert futures_response.status_code == 200
    assert b'<label for="ticks">Ticks</label>' in futures_response.data
    assert b'<label for="pips">Pips</label>' not in futures_response.data


def test_trade_list_and_detail_show_trade_flags(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-flags-user",
        email="trade-flags@example.com",
    )

    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10120,
        lot_size=1.00,
        pnl=120.0,
        is_revenge=True,
        is_corrective=True,
        is_reactive=True,
        bundle_pubkey="bundle-flag-test",
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    db.session.add(trade)
    db.session.commit()

    list_response = client.get("/dashboard/trades")
    detail_response = client.get(f"/dashboard/trades/{trade.pubkey}")

    assert list_response.status_code == 200
    assert b'title="Revenge Trade"' in list_response.data
    assert b'title="Reactive Trade"' in list_response.data
    assert b'title="Corrective Trade"' in list_response.data
    assert b'title="Bundled Entry"' in list_response.data
    assert b'data-bundle="bundle-flag-test"' in list_response.data
    assert b">Bundled</span>" in list_response.data
    assert detail_response.status_code == 200
    assert b'<label for="trade_flags">Trade Flags</label>' in detail_response.data
    assert b"Revenge" in detail_response.data
    assert b"Corrective" in detail_response.data
    assert b"Reactive" in detail_response.data
    assert b"Bundled Entry" in detail_response.data


def test_bundle_review_confirms_historical_bundle(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="bundle-review-user",
        email="bundle-review@example.com",
    )

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            take_profit=1.1040,
            lot_size=0.5,
            pnl=30.0,
            opened_at=datetime(2026, 3, 10, 10, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 20, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1002,
            exit_price=1.1012,
            take_profit=1.1040,
            lot_size=0.5,
            pnl=24.0,
            opened_at=datetime(2026, 3, 10, 10, 8, 0),
            closed_at=datetime(2026, 3, 10, 10, 26, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()
    trade_account.bundle_review_requested_at = datetime(2026, 3, 11, 9, 0, 0)
    db.session.commit()

    group_value = ",".join(sorted([trades[0].pubkey, trades[1].pubkey]))
    review_response = client.get("/dashboard/trades/bundle-review")
    confirm_response = client.post(
        "/dashboard/trades/bundle-confirm",
        data={
            "bundle_group": group_value,
        },
        follow_redirects=False,
    )

    db.session.refresh(trades[0])
    db.session.refresh(trades[1])
    db.session.refresh(trade_account)

    assert review_response.status_code == 200
    assert b"Historical Bundle Review" in review_response.data
    assert b"Confirm This Bundle" in review_response.data
    assert b"If bundling, classify as" not in review_response.data
    assert confirm_response.status_code == 302
    assert trades[0].bundle_pubkey is not None
    assert trades[0].bundle_pubkey == trades[1].bundle_pubkey
    assert trades[0].is_corrective is False
    assert trades[1].is_corrective is False
    assert trade_account.bundle_review_completed_at is not None


def test_bundle_review_complete_clears_pending_prompt_without_confirming_bundles(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="bundle-review-complete-user",
        email="bundle-review-complete@example.com",
    )

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            take_profit=1.1040,
            lot_size=0.5,
            pnl=30.0,
            opened_at=datetime(2026, 3, 10, 10, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 20, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1002,
            exit_price=1.1012,
            take_profit=1.1040,
            lot_size=0.5,
            pnl=24.0,
            opened_at=datetime(2026, 3, 10, 10, 8, 0),
            closed_at=datetime(2026, 3, 10, 10, 26, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()
    trade_account.bundle_review_requested_at = datetime(2026, 3, 11, 9, 0, 0)
    db.session.commit()

    response = client.post("/dashboard/trades/bundle-review/complete", follow_redirects=False)

    db.session.refresh(trade_account)
    db.session.refresh(trades[0])
    db.session.refresh(trades[1])

    assert response.status_code == 302
    assert trade_account.bundle_review_completed_at is not None
    assert trades[0].bundle_pubkey is None
    assert trades[1].bundle_pubkey is None


def test_user_cannot_view_edit_or_delete_another_users_trade(app_ctx, client):
    owner, owner_account = _create_logged_in_user(
        client,
        username="trade-owner-user",
        email="trade-owner@example.com",
    )
    intruder, intruder_account = _create_logged_in_user(
        client,
        username="trade-intruder-user",
        email="trade-intruder@example.com",
    )

    trade = Trade(
        user_id=owner.id,
        trade_account_id=owner_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10120,
        lot_size=1.00,
        pnl=120.0,
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    db.session.add(trade)
    db.session.commit()

    with client.session_transaction() as session_state:
        session_state["user_id"] = intruder.id
        session_state["username"] = intruder.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = intruder_account.id

    detail_response = client.get(f"/dashboard/trades/{trade.pubkey}")
    edit_response = client.get(f"/dashboard/trades/{trade.pubkey}/edit")
    delete_response = client.post(f"/dashboard/trades/{trade.pubkey}/delete")

    assert detail_response.status_code == 404
    assert edit_response.status_code == 404
    assert delete_response.status_code == 404
    assert db.session.get(Trade, trade.id) is not None


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
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
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
            closed_at=datetime(2026, 3, 11, 10, 0, 0),
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
            closed_at=datetime(2026, 3, 12, 10, 0, 0),
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
    assert b"Avg Planned RR" in response.data
    assert b"2.50R" in response.data
    assert b"1.40R" in response.data
    assert b"56%" in response.data
    assert b"Early RR read only. The numbers are live, but wait for at least 3 valid trades before trusting the pattern." in response.data
    assert b"Need 3 minimum for a reliable read." in response.data
