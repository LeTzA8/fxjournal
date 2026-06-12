import csv
from datetime import datetime, timedelta
from io import BytesIO, StringIO

import pytest
import routes.trades as trades_routes
from helpers.trade_interpretation import apply_interpretation
from models import FuturesSymbol, Trade, TradeAccount, TradeBars, TradeProfile, TradeProfileVersion, User, db
from trading import clear_cfd_symbol_cache


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


def test_topstep_duplicate_import_refreshes_existing_trade_costs(app_ctx, client, monkeypatch):
    user, trade_account = _create_logged_in_user(
        client,
        username="topstep-route-cost-refresh-user",
        email="topstep-route-cost-refresh@example.com",
    )
    trade_account.account_type = "FUTURES"
    if FuturesSymbol.query.filter_by(root_symbol="MES").first() is None:
        db.session.add(
            FuturesSymbol(
                root_symbol="MES",
                tick_size=0.25,
                tick_value=5.0,
                display_name="MES",
                exchange="CME",
                currency="USD",
                sort_order=1,
                is_active=True,
            )
        )
    db.session.flush()
    clear_cfd_symbol_cache()

    existing_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="MES",
        contract_code="MESM6",
        side="BUY",
        entry_price=5900.0,
        exit_price=5916.5,
        lot_size=4.0,
        pnl=330.0,
        commission=2.96,
        opened_at=datetime(2026, 6, 1, 1, 0, 0),
        closed_at=datetime(2026, 6, 1, 1, 5, 0),
    )
    db.session.add(existing_trade)
    db.session.commit()

    monkeypatch.setattr(
        trades_routes,
        "queue_bundle_review_if_split_candidates",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        trades_routes,
        "queue_weekly_ai_review_after_ingest",
        lambda **_kwargs: None,
    )

    csv_bytes = (
        "Id,ContractName,EnteredAt,ExitedAt,EntryPrice,ExitPrice,Fees,Commissions,PnL,Size,Type\n"
        "2665990315,MESM6,06/01/2026 09:00:00 +08:00,06/01/2026 09:05:00 +08:00,"
        "5900.00,5916.50,2.96,2.00,330.00,4,Long\n"
    ).encode("utf-8")

    response = client.post(
        "/dashboard/import",
        data={"mt5_file": (BytesIO(csv_bytes), "topstep.csv")},
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(existing_trade)
    assert existing_trade.commission == 4.96
    assert Trade.query.filter_by(user_id=user.id).count() == 1
    with client.session_transaction() as session_state:
        flash_messages = [message for _category, message in session_state.get("_flashes", [])]
    assert any(
        "Updated costs on 1 existing trade from Topstep CSV." in message
        for message in flash_messages
    )
    assert not any("Imported 0 trades" in message for message in flash_messages)


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
    assert b'for="planned_rr"' in detail_response.data
    assert b"Planned RR" in detail_response.data
    assert b'value="2R"' in detail_response.data
    assert b'for="actual_rr"' in detail_response.data
    assert b"Actual RR" in detail_response.data
    assert b'value="1.4R"' in detail_response.data
    assert b'for="commission"' in detail_response.data
    assert b"Commission" in detail_response.data
    assert b'value="-3.5"' in detail_response.data
    assert b'for="swap"' in detail_response.data
    assert b"Swap" in detail_response.data
    assert b'value="-0.8"' in detail_response.data
    assert b'for="net_pnl"' in detail_response.data
    assert b"Net PnL" in detail_response.data
    assert b'value="135.7"' in detail_response.data


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
    assert b'for="trade_note"' in detail_response.data
    assert b"Trade Note" in detail_response.data
    assert b"User note stays here." in detail_response.data
    assert b'for="system_trade_note"' in detail_response.data
    assert b"Import Note" in detail_response.data
    assert b"Imported from MT5 Positions" in detail_response.data


def test_trade_edit_uses_progressive_disclosure_sections(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-edit-collapsible-user",
        email="trade-edit-collapsible@example.com",
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
        trade_note="Edit note",
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    db.session.add(trade)
    db.session.commit()

    edit_response = client.get(f"/dashboard/trades/{trade.pubkey}/edit")

    assert edit_response.status_code == 200
    assert b"trade-summary-compact" in edit_response.data
    assert b'trade-form-section--instrument trade-form-collapsible"' in edit_response.data
    assert b'trade-form-section--timing trade-form-collapsible"' in edit_response.data
    assert b'trade-form-collapsible" open' not in edit_response.data
    assert b'name="entry_price"' in edit_response.data
    assert b'name="trade_note"' in edit_response.data
    assert b"Save changes" in edit_response.data


def test_trade_note_api_get_and_post(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-note-api-user",
        email="trade-note-api@example.com",
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
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    db.session.add(trade)
    db.session.commit()

    get_response = client.get(f"/api/trades/{trade.pubkey}/note")
    assert get_response.status_code == 200
    get_payload = get_response.get_json()
    assert get_payload["has_trade_note"] is False
    assert get_payload["trade_note"] == ""
    assert "EURUSD" in get_payload["trade_label"]

    post_response = client.post(
        f"/api/trades/{trade.pubkey}/note",
        json={"trade_note": "Quick dashboard note."},
    )
    assert post_response.status_code == 200
    post_payload = post_response.get_json()
    assert post_payload["ok"] is True
    assert post_payload["has_trade_note"] is True
    assert post_payload["trade_note"] == "Quick dashboard note."

    db.session.refresh(trade)
    assert trade.trade_note == "Quick dashboard note."


def test_trade_note_api_rejects_overlong_note(app_ctx, client):
    _user, trade_account = _create_logged_in_user(
        client,
        username="trade-note-length-user",
        email="trade-note-length@example.com",
    )

    trade = Trade(
        user_id=trade_account.user_id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.25000,
        exit_price=1.24800,
        lot_size=0.50,
        pnl=100.0,
        opened_at=datetime(2026, 3, 11, 9, 0, 0),
        closed_at=datetime(2026, 3, 11, 10, 0, 0),
    )
    db.session.add(trade)
    db.session.commit()

    response = client.post(
        f"/api/trades/{trade.pubkey}/note",
        json={"trade_note": "x" * (trades_routes.TRADE_NOTE_MAX_LENGTH + 1)},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "trade_note_too_long"


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
    assert b'for="pips"' in cfd_response.data
    assert b"Pips" in cfd_response.data
    assert b'for="ticks"' not in cfd_response.data

    assert futures_response.status_code == 200
    assert b'for="ticks"' in futures_response.data
    assert b"Ticks" in futures_response.data
    assert b'for="pips"' not in futures_response.data


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
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 24, 0),
    )
    db.session.add(trade)
    db.session.flush()
    apply_interpretation(
        trade,
        bundle_pubkey="bundle-flag-test",
        is_revenge=True,
        is_corrective=True,
        is_reactive=True,
        source="test",
        user_id=user.id,
    )
    db.session.commit()

    list_response = client.get("/dashboard/trades")
    detail_response = client.get(f"/dashboard/trades/{trade.pubkey}")

    assert list_response.status_code == 200
    assert f'data-trade-detail-url="/dashboard/trades/{trade.pubkey}"'.encode() in list_response.data
    assert b'title="Revenge Trade"' in list_response.data
    assert b'title="Reactive Trade"' in list_response.data
    assert b'title="Corrective Trade"' in list_response.data
    assert b'title="Bundled Entry"' in list_response.data
    assert b'data-bundle="bundle-flag-test"' in list_response.data
    assert b">Bundled</span>" in list_response.data
    assert b">Revenge</span>" in list_response.data
    assert b">Reactive</span>" in list_response.data
    assert b">Corrective</span>" in list_response.data
    assert detail_response.status_code == 200
    assert b'for="trade_flags"' in detail_response.data
    assert b"Trade Flags" in detail_response.data
    assert b"Revenge" in detail_response.data
    assert b"Corrective" in detail_response.data
    assert b"Reactive" in detail_response.data
    assert b"Bundled Entry" in detail_response.data


def test_trade_list_shows_possible_behavior_badges(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-possible-flags-user",
        email="trade-possible-flags@example.com",
    )

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0980,
            lot_size=1.0,
            pnl=-50.0,
            opened_at=datetime(2026, 3, 10, 8, 0, 0),
            closed_at=datetime(2026, 3, 10, 8, 20, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0985,
            exit_price=1.0975,
            lot_size=1.5,
            pnl=-20.0,
            opened_at=datetime(2026, 3, 10, 8, 35, 0),
            closed_at=datetime(2026, 3, 10, 8, 50, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.2500,
            exit_price=1.2520,
            stop_loss=1.2550,
            lot_size=0.4,
            pnl=-10.0,
            opened_at=datetime(2026, 3, 10, 10, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 5, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    response = client.get("/dashboard/trades")

    assert response.status_code == 200
    assert b">Possible Revenge</span>" in response.data
    assert b">Possible Reactive</span>" in response.data
    assert b">Possible Corrective</span>" in response.data


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


def test_bundle_review_renders_trade_times_in_display_timezone(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="bundle-review-timezone-user",
        email="bundle-review-timezone@example.com",
    )
    with client.session_transaction() as session_state:
        session_state["display_timezone"] = "Asia/Singapore"

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

    response = client.get("/dashboard/trades/bundle-review")

    assert response.status_code == 200
    assert b"10 Mar 2026 18:08" in response.data
    assert b"10 Mar 2026 10:08" not in response.data


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


def test_analytics_page_shows_behavior_signal_panels(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="analytics-behavior-user",
        email="analytics-behavior@example.com",
    )

    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0980,
            lot_size=1.0,
            pnl=-50.0,
            opened_at=datetime(2026, 3, 10, 8, 0, 0),
            closed_at=datetime(2026, 3, 10, 8, 20, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0985,
            exit_price=1.0975,
            lot_size=1.5,
            pnl=-20.0,
            opened_at=datetime(2026, 3, 10, 8, 35, 0),
            closed_at=datetime(2026, 3, 10, 8, 50, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.2500,
            exit_price=1.2520,
            stop_loss=1.2550,
            lot_size=0.4,
            pnl=-10.0,
            opened_at=datetime(2026, 3, 10, 10, 0, 0),
            closed_at=datetime(2026, 3, 10, 10, 5, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    response = client.get("/dashboard/analytics")

    assert response.status_code == 200
    assert b"Behavior Signals" in response.data
    assert b"High-Signal Trades" in response.data
    assert b"Top focus: Revenge" in response.data
    assert b">Possible Revenge</span>" in response.data
    assert b">Possible Reactive</span>" in response.data
    assert b">Possible Corrective</span>" in response.data


def test_analytics_shows_grouped_kpis_and_breakdown_disclosure(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="analytics-layout-user",
        email="analytics-layout@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=1.11,
            lot_size=1.0,
            pnl=10.0,
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard/analytics")
    assert response.status_code == 200
    assert b"Session, pair, and weekday breakdowns" in response.data
    assert b"At a glance" in response.data
    assert b"Net PnL (realized)" in response.data


def test_trades_list_accepts_journal_filter_query_param(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trades-query-user",
        email="trades-query@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=1.11,
            lot_size=1.0,
            pnl=10.0,
            closed_at=datetime(2026, 3, 10, 10, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard/trades?pair=EURUSD")
    assert response.status_code == 200


def test_trade_chart_data_returns_unavailable_for_non_mt5_or_open_trade(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-chart-unavailable-user",
        email="trade-chart-unavailable@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    db.session.commit()

    manual_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1010,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position=None,
    )
    db.session.add(manual_trade)
    db.session.commit()

    response = client.get(f"/api/trades/{manual_trade.pubkey}/chart-data")
    assert response.status_code == 200
    assert response.get_json() == {"status": "unavailable"}


def test_trade_chart_data_returns_pending_when_mt5_trade_has_no_bars(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-chart-pending-user",
        email="trade-chart-pending@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    db.session.commit()

    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1010,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position="7770001",
    )
    db.session.add(trade)
    db.session.commit()

    response = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    assert response.status_code == 200
    assert response.get_json() == {"status": "pending"}


def _create_closed_mt5_trade_with_m5_bars(user, trade_account, position_id="7770002"):
    trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1000,
        exit_price=1.1010,
        stop_loss=1.0980,
        take_profit=1.1040,
        lot_size=1.0,
        opened_at=datetime(2026, 4, 10, 9, 0, 0),
        closed_at=datetime(2026, 4, 10, 10, 0, 0),
        mt5_position=position_id,
    )
    db.session.add(trade)
    db.session.flush()
    db.session.add_all(
        [
            TradeBars(
                trade_id=trade.id,
                timeframe="M5",
                bar_time=1_700_000_000,
                open=1.0990,
                high=1.1010,
                low=1.0985,
                close=1.1005,
                tick_volume=50,
            ),
            TradeBars(
                trade_id=trade.id,
                timeframe="M5",
                bar_time=1_700_000_300,
                open=1.1005,
                high=1.1015,
                low=1.1000,
                close=1.1010,
                tick_volume=52,
            ),
        ]
    )
    db.session.commit()
    return trade


def test_trade_chart_data_returns_ready_payload_when_bars_exist(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-chart-ready-user",
        email="trade-chart-ready@example.com",
    )
    user.is_admin = True
    user.email_verified = True
    db.session.commit()

    trade = _create_closed_mt5_trade_with_m5_bars(user, trade_account)

    response = client.get(f"/api/trades/{trade.pubkey}/chart-data")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ready"
    assert payload["timeframe"] == "M5"
    assert payload["available_timeframes"] == ["M5", "M15"]
    assert len(payload["bars"]) == 2
    assert payload["bars"][0]["time"] == 1_700_000_000
    assert payload["markers"]["entry_price"] == pytest.approx(1.1)
    assert payload["markers"]["exit_price"] == pytest.approx(1.101)
    assert payload["markers"]["side"] == "BUY"
    assert payload.get("display_timezone")
    assert "prefetched_bars" in payload
    assert "M5" in payload["prefetched_bars"] and "M15" in payload["prefetched_bars"]
    assert payload["prefetched_bars_source"]["M15"] == "aggregated_from_m5"
    assert len(payload["prefetched_bars"]["M5"]) == 2

    m15_response = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M15")
    assert m15_response.status_code == 200
    m15_payload = m15_response.get_json()
    assert m15_payload["status"] == "ready"
    assert m15_payload["timeframe"] == "M15"
    assert m15_payload["available_timeframes"] == ["M5", "M15"]
    assert m15_payload["bars_source"] == "aggregated_from_m5"
    assert len(m15_payload["bars"]) == 2
    assert m15_payload["bars"][0]["open"] == pytest.approx(1.0990)
    assert m15_payload["bars"][0]["close"] == pytest.approx(1.1005)
    assert m15_payload["bars"][1]["open"] == pytest.approx(1.1005)
    assert m15_payload["bars"][1]["close"] == pytest.approx(1.1010)


def test_trade_chart_data_denies_expired_trial_user_requesting_m1(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-chart-free-m1-user",
        email="trade-chart-free-m1@example.com",
    )
    user.plan_tier = "free"
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=20)
    db.session.commit()
    trade = _create_closed_mt5_trade_with_m5_bars(user, trade_account, "7770003")

    response = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M1")

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "upgrade_required"
    assert payload["requested_timeframe"] == "M1"
    assert payload["available_timeframes"] == ["M5", "M15"]
    assert payload["required_tier"] == "trader"
    assert payload["upgrade_url"] == "/pricing"
    assert payload["cta"]["source"] == "replay_lock"
    assert payload["cta"]["feature_interest"] == "advanced_replay"
    assert payload["cta"]["cta_context"] == "trade_replay_1m"


def test_trade_chart_data_active_trial_user_reaches_m1_not_implemented(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-chart-active-trial-m1-user",
        email="trade-chart-active-trial-m1@example.com",
    )
    user.plan_tier = "free"
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=2)
    db.session.commit()
    trade = _create_closed_mt5_trade_with_m5_bars(user, trade_account, "7770003-trial")

    response = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M1")

    assert response.status_code == 501
    payload = response.get_json()
    assert payload["error"] == "timeframe_not_available"
    assert payload["requested_timeframe"] == "M1"
    assert payload["available_timeframes"] == ["M5", "M15"]


def test_trade_chart_data_allows_free_user_m5_and_m15(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-chart-free-allowed-user",
        email="trade-chart-free-allowed@example.com",
    )
    user.plan_tier = "free"
    user.premium_trial_started_at = datetime.utcnow() - timedelta(days=20)
    db.session.commit()
    trade = _create_closed_mt5_trade_with_m5_bars(user, trade_account, "7770004")

    m5_response = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M5")
    m15_response = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M15")

    assert m5_response.status_code == 200
    assert m5_response.get_json()["timeframe"] == "M5"
    assert m5_response.get_json()["available_timeframes"] == ["M5", "M15"]
    assert m15_response.status_code == 200
    assert m15_response.get_json()["timeframe"] == "M15"
    assert m15_response.get_json()["available_timeframes"] == ["M5", "M15"]


@pytest.mark.parametrize("plan_tier", ["trader", "pro"])
def test_trade_chart_data_returns_not_implemented_for_paid_tier_m1(app_ctx, client, plan_tier):
    user, trade_account = _create_logged_in_user(
        client,
        username=f"trade-chart-{plan_tier}-m1-user",
        email=f"trade-chart-{plan_tier}-m1@example.com",
    )
    user.plan_tier = plan_tier
    db.session.commit()
    trade = _create_closed_mt5_trade_with_m5_bars(user, trade_account, f"777000-{plan_tier}")

    response = client.get(f"/api/trades/{trade.pubkey}/chart-data?timeframe=M1")

    assert response.status_code == 501
    payload = response.get_json()
    assert payload["error"] == "timeframe_not_available"
    assert payload["requested_timeframe"] == "M1"
    assert payload["available_timeframes"] == ["M5", "M15"]
    assert "timeframe" not in payload
    assert payload["cta"]["source"] == "replay_lock"


def test_export_trades_returns_closed_trade_csv(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-export-user",
        email="trade-export@example.com",
    )
    profile = TradeProfile(
        user_id=user.id,
        name="London Breakout",
        current_version_number=1,
    )
    db.session.add(profile)
    db.session.flush()
    profile_version = TradeProfileVersion(
        trade_profile_id=profile.id,
        version_number=1,
        name="London Breakout v1",
        short_description="Enter on London open break of Asian range.",
    )
    db.session.add(profile_version)
    db.session.flush()

    closed_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10120,
        lot_size=0.50,
        pnl=60.0,
        commission=-1.20,
        swap=0.25,
        stop_loss=1.09900,
        take_profit=1.10200,
        trade_note="Held through pullback",
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 9, 15, 0),
        trade_profile_id=profile.id,
        trade_profile_version_id=profile_version.id,
        mt5_position="123456",
    )
    open_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.25000,
        lot_size=0.20,
        opened_at=datetime(2026, 3, 11, 9, 0, 0),
    )
    db.session.add_all([closed_trade, open_trade])
    db.session.flush()
    apply_interpretation(
        closed_trade,
        bundle_pubkey="bundle-export-test",
        is_revenge=True,
        source="test",
        user_id=user.id,
    )
    db.session.commit()

    response = client.get("/dashboard/trades/export?format=csv")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert 'attachment; filename="trades_Main-Account_' in response.headers.get(
        "Content-Disposition", ""
    )
    rows = list(csv.reader(StringIO(response.get_data(as_text=True))))
    assert rows[0] == ["# Strategies used in this export"]
    assert rows[1] == ["strategy_name", "description"]
    assert rows[2] == [
        "London Breakout",
        "Enter on London open break of Asian range.",
    ]
    assert rows[3] == []
    assert rows[4][0] == "opened_at_utc"
    assert "size_lots" in rows[4]
    assert "bundle_group" in rows[4]
    assert "bundle_trades" in rows[4]
    assert len(rows) == 6
    trade_row = rows[5]
    assert trade_row[0] == "2026-03-10 09:00:00 UTC"
    assert trade_row[1] == "2026-03-10 09:15:00 UTC"
    assert trade_row[2] == "15"
    assert trade_row[3] == "EURUSD"
    assert trade_row[4] == "BUY"
    assert trade_row[5] == "0.5"
    assert trade_row[13] == "London Breakout v1"
    assert trade_row[14] == "Held through pullback"
    assert trade_row[15] == "bundle-export-test"
    assert trade_row[16] == closed_trade.pubkey
    assert trade_row[17] == "TRUE"
    assert trade_row[20] == "mt5_sync"


def test_export_trades_respects_opened_at_date_range(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-export-range-user",
        email="trade-export-range@example.com",
    )
    early_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10100,
        lot_size=0.10,
        pnl=10.0,
        opened_at=datetime(2026, 3, 1, 9, 0, 0),
        closed_at=datetime(2026, 3, 1, 9, 30, 0),
    )
    late_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="GBPUSD",
        side="SELL",
        entry_price=1.25000,
        exit_price=1.24900,
        lot_size=0.10,
        pnl=10.0,
        opened_at=datetime(2026, 3, 20, 9, 0, 0),
        closed_at=datetime(2026, 3, 20, 9, 30, 0),
    )
    db.session.add_all([early_trade, late_trade])
    db.session.commit()

    response = client.get(
        "/dashboard/trades/export?format=csv&from=2026-03-15&to=2026-03-31"
    )

    assert response.status_code == 200
    rows = list(csv.reader(StringIO(response.get_data(as_text=True))))
    data_rows = [
        row
        for row in rows
        if row and row[0] != "opened_at_utc" and not row[0].startswith("#")
    ]
    assert len(data_rows) == 1
    assert data_rows[0][3] == "GBPUSD"


def test_export_trades_scoped_to_active_account(app_ctx, client):
    user, trade_account = _create_logged_in_user(
        client,
        username="trade-export-scope-user",
        email="trade-export-scope@example.com",
    )
    other_account = TradeAccount(
        user_id=user.id,
        name="Secondary",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(other_account)
    db.session.flush()

    active_trade = Trade(
        user_id=user.id,
        trade_account_id=trade_account.id,
        symbol="EURUSD",
        side="BUY",
        entry_price=1.10000,
        exit_price=1.10100,
        lot_size=0.10,
        pnl=10.0,
        opened_at=datetime(2026, 3, 10, 9, 0, 0),
        closed_at=datetime(2026, 3, 10, 9, 30, 0),
    )
    other_trade = Trade(
        user_id=user.id,
        trade_account_id=other_account.id,
        symbol="USDJPY",
        side="SELL",
        entry_price=150.0,
        exit_price=149.5,
        lot_size=0.10,
        pnl=10.0,
        opened_at=datetime(2026, 3, 10, 10, 0, 0),
        closed_at=datetime(2026, 3, 10, 10, 30, 0),
    )
    db.session.add_all([active_trade, other_trade])
    db.session.commit()

    response = client.get("/dashboard/trades/export?format=csv")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "EURUSD" in body
    assert "USDJPY" not in body


def test_trades_page_includes_export_link(app_ctx, client):
    _create_logged_in_user(
        client,
        username="trade-export-link-user",
        email="trade-export-link@example.com",
    )

    response = client.get("/dashboard/trades")

    assert response.status_code == 200
    assert b"/dashboard/trades/export?format=csv" in response.data
    assert b"Export CSV" in response.data
