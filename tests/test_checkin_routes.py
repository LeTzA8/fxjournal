from datetime import datetime

import routes.checkin as checkin_routes
import routes.dashboard as dashboard_routes
from models import Trade, TradeAccount, User, WeeklyCheckin, db


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


def test_dashboard_checkin_banner_respects_active_account_only(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, active_account = _create_logged_in_user(
        client,
        username="checkin-active-account-user",
        email="checkin-active-account@example.com",
    )
    other_account = TradeAccount(
        user_id=user.id,
        name="Second Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(other_account)
    db.session.flush()
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=other_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            lot_size=1.0,
            pnl=55.0,
            opened_at=datetime(2026, 3, 17, 10, 0, 0),
            closed_at=datetime(2026, 3, 17, 11, 0, 0),
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Open Check-In" not in response.data
    assert active_account.id != other_account.id


def test_checkin_route_saves_answers_for_current_week(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-save-user",
        email="checkin-save@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.2700,
            exit_price=1.2690,
            lot_size=1.0,
            pnl=45.0,
            opened_at=datetime(2026, 3, 17, 12, 0, 0),
            closed_at=datetime(2026, 3, 17, 13, 0, 0),
        )
    )
    db.session.commit()

    get_response = client.get("/checkin")
    post_response = client.post(
        "/checkin",
        data={
            "emotional_state": "stressed",
            "plan_adherence": "impulsive",
            "execution_quality": "poor",
            "additional_context": "Felt reactive after the first loss.",
        },
        follow_redirects=False,
    )

    record = WeeklyCheckin.query.filter_by(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ).first()

    assert get_response.status_code == 200
    assert b"Give the AI a little context" in get_response.data
    assert post_response.status_code == 302
    assert record is not None
    assert record.emotional_state == "stressed"
    assert record.plan_adherence == "impulsive"
    assert record.execution_quality == "poor"
    assert record.additional_context == "Felt reactive after the first loss."


def test_checkin_skip_creates_placeholder_row(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-skip-user",
        email="checkin-skip@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="USDJPY",
            side="BUY",
            entry_price=149.20,
            exit_price=149.55,
            lot_size=1.0,
            pnl=32.0,
            opened_at=datetime(2026, 3, 17, 8, 0, 0),
            closed_at=datetime(2026, 3, 17, 8, 30, 0),
        )
    )
    db.session.commit()

    response = client.post("/checkin/skip", follow_redirects=False)
    record = WeeklyCheckin.query.filter_by(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ).first()

    assert response.status_code == 302
    assert record is not None
    assert record.emotional_state is None
    assert record.plan_adherence is None
    assert record.execution_quality is None
    assert record.additional_context is None


def test_checkin_skip_still_allows_returning_to_form(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-return-user",
        email="checkin-return@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="XAUUSD",
            side="BUY",
            entry_price=3000.0,
            exit_price=3012.0,
            lot_size=1.0,
            pnl=84.0,
            opened_at=datetime(2026, 3, 17, 8, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 0, 0),
        )
    )
    db.session.commit()

    skip_response = client.post("/checkin/skip", follow_redirects=False)
    reopen_response = client.get("/checkin")

    assert skip_response.status_code == 302
    assert reopen_response.status_code == 200
    assert b"Give the AI a little context" in reopen_response.data


def test_dashboard_shows_finish_checkin_after_skip(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-banner-return-user",
        email="checkin-banner-return@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="SELL",
            entry_price=1.1000,
            exit_price=1.0980,
            lot_size=1.0,
            pnl=50.0,
            opened_at=datetime(2026, 3, 17, 12, 0, 0),
            closed_at=datetime(2026, 3, 17, 13, 0, 0),
        )
    )
    db.session.commit()

    client.post("/checkin/skip", follow_redirects=False)
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Finish Check-In" in response.data
    assert b"You skipped it earlier" in response.data


def test_dashboard_does_not_show_checkin_before_friday_close(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 18, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)
    monkeypatch.setattr(dashboard_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-before-cutoff-user",
        email="checkin-before-cutoff@example.com",
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="BUY",
            entry_price=1.2800,
            exit_price=1.2820,
            lot_size=1.0,
            pnl=40.0,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 10, 0, 0),
        )
    )
    db.session.commit()

    dashboard_response = client.get("/dashboard")
    checkin_response = client.get("/checkin", follow_redirects=False)

    assert dashboard_response.status_code == 200
    assert b"Open Check-In" not in dashboard_response.data
    assert b"Finish Check-In" not in dashboard_response.data
    assert checkin_response.status_code == 302


def test_checkin_shows_outlier_review_and_saves_bundle_confirmations(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-outlier-user",
        email="checkin-outlier@example.com",
    )
    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0990,
            take_profit=1.1040,
            lot_size=1.0,
            pnl=-45.0,
            opened_at=datetime(2026, 3, 17, 9, 50, 0),
            closed_at=datetime(2026, 3, 17, 10, 15, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0995,
            exit_price=1.1002,
            take_profit=1.1030,
            lot_size=0.5,
            pnl=22.0,
            opened_at=datetime(2026, 3, 17, 10, 20, 0),
            closed_at=datetime(2026, 3, 17, 10, 40, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0998,
            exit_price=1.1004,
            take_profit=1.1030,
            lot_size=0.5,
            pnl=18.0,
            opened_at=datetime(2026, 3, 17, 10, 25, 0),
            closed_at=datetime(2026, 3, 17, 10, 42, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    detected_outliers = checkin_routes.detect_outliers(trades)
    detected_candidate = detected_outliers["bundle_candidates"][0]
    candidate_pubkeys = [trade.pubkey for trade in detected_candidate["trades"]]
    group_value = checkin_routes._build_bundle_group_value(candidate_pubkeys)
    group_hash = checkin_routes._pubkey_group_hash(candidate_pubkeys)

    bundle_step_response = client.get("/checkin")
    classification_response = client.post(
        "/checkin",
        data={
            "stage": "bundle_review",
            "bundle_group": group_value,
        },
        follow_redirects=False,
    )
    weekly_step_response = client.post(
        "/checkin",
        data={
            "stage": "classification",
            "bundle_group": group_value,
            f"bundle_type_{group_hash}": "revenge",
        },
        follow_redirects=False,
    )
    post_response = client.post(
        "/checkin",
        data={
            "stage": "checkin",
            "bundle_group": group_value,
            f"bundle_type_{group_hash}": "revenge",
            "emotional_state": "slightly_off",
            "plan_adherence": "some_deviations",
            "execution_quality": "average",
            "additional_context": "Scaled in after the first loss.",
        },
        follow_redirects=False,
    )

    db.session.refresh(trades[1])
    db.session.refresh(trades[2])
    record = WeeklyCheckin.query.filter_by(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ).first()

    assert bundle_step_response.status_code == 200
    assert b"Step 1. Bundle Review" in bundle_step_response.data
    assert b"Confirm as bundle" in bundle_step_response.data
    assert b"Step 3. Weekly Check-In" not in bundle_step_response.data
    assert classification_response.status_code == 200
    assert b"Step 2. Behaviour Review" in classification_response.data
    assert b"Revenge" in classification_response.data
    assert b"Clean" in classification_response.data
    assert b"Not sure" in classification_response.data
    assert b"Step 3. Weekly Check-In" not in classification_response.data
    assert weekly_step_response.status_code == 200
    assert b"Step 3. Weekly Check-In" in weekly_step_response.data
    assert post_response.status_code == 302
    assert trades[1].bundle_pubkey is not None
    assert trades[1].bundle_pubkey == trades[2].bundle_pubkey
    assert trades[1].is_revenge is True
    assert trades[2].is_revenge is True
    assert record is not None
    assert record.additional_context == "Scaled in after the first loss."


def test_checkin_invalid_submission_does_not_mutate_trade_flags_or_bundles(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-invalid-outlier-user",
        email="checkin-invalid-outlier@example.com",
    )
    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0990,
            take_profit=1.1040,
            lot_size=1.0,
            pnl=-45.0,
            opened_at=datetime(2026, 3, 17, 9, 50, 0),
            closed_at=datetime(2026, 3, 17, 10, 15, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0995,
            exit_price=1.1002,
            take_profit=1.1030,
            lot_size=0.5,
            pnl=22.0,
            opened_at=datetime(2026, 3, 17, 10, 20, 0),
            closed_at=datetime(2026, 3, 17, 10, 40, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0998,
            exit_price=1.1004,
            take_profit=1.1030,
            lot_size=0.5,
            pnl=18.0,
            opened_at=datetime(2026, 3, 17, 10, 25, 0),
            closed_at=datetime(2026, 3, 17, 10, 42, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    detected_outliers = checkin_routes.detect_outliers(trades)
    detected_candidate = detected_outliers["bundle_candidates"][0]
    candidate_pubkeys = [trade.pubkey for trade in detected_candidate["trades"]]
    group_value = checkin_routes._build_bundle_group_value(candidate_pubkeys)
    group_hash = checkin_routes._pubkey_group_hash(candidate_pubkeys)

    response = client.post(
        "/checkin",
        data={
            "stage": "checkin",
            "bundle_group": group_value,
            f"bundle_type_{group_hash}": "revenge",
            "emotional_state": "stressed",
            "plan_adherence": "",
            "execution_quality": "poor",
        },
        follow_redirects=False,
    )

    db.session.refresh(trades[1])
    db.session.refresh(trades[2])

    assert response.status_code == 200
    assert b"Please answer the three multiple-choice questions before saving." in response.data
    assert b"Step 3. Weekly Check-In" in response.data
    assert trades[1].bundle_pubkey is None
    assert trades[2].bundle_pubkey is None
    assert trades[1].is_revenge is False
    assert trades[2].is_revenge is False
    assert trades[1].is_reactive is False
    assert trades[2].is_reactive is False


def test_checkin_unsure_classification_keeps_standalone_trade_unflagged(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-unsure-classification-user",
        email="checkin-unsure-classification@example.com",
    )
    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0990,
            lot_size=1.0,
            pnl=-45.0,
            opened_at=datetime(2026, 3, 17, 9, 50, 0),
            closed_at=datetime(2026, 3, 17, 10, 15, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0995,
            exit_price=1.1002,
            lot_size=0.5,
            pnl=22.0,
            opened_at=datetime(2026, 3, 17, 10, 20, 0),
            closed_at=datetime(2026, 3, 17, 10, 40, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    reactive_trade_pubkey = trades[1].pubkey

    classification_step = client.get("/checkin")
    weekly_step = client.post(
        "/checkin",
        data={
            "stage": "classification",
            f"trade_type_{reactive_trade_pubkey}": "unsure",
        },
        follow_redirects=False,
    )
    response = client.post(
        "/checkin",
        data={
            "stage": "checkin",
            f"trade_type_{reactive_trade_pubkey}": "unsure",
            "emotional_state": "slightly_off",
            "plan_adherence": "some_deviations",
            "execution_quality": "average",
        },
        follow_redirects=False,
    )

    db.session.refresh(trades[1])
    record = WeeklyCheckin.query.filter_by(
        user_id=user.id,
        trade_account_id=trade_account.id,
    ).first()

    assert classification_step.status_code == 200
    assert b"Step 2. Behaviour Review" in classification_step.data
    assert b"Not sure" in classification_step.data
    assert weekly_step.status_code == 200
    assert b"Step 3. Weekly Check-In" in weekly_step.data
    assert response.status_code == 302
    assert trades[1].is_revenge is False
    assert trades[1].is_reactive is False
    assert trades[1].is_corrective is False
    assert record is not None


def test_detect_outliers_does_not_chain_same_pair_bundle_candidates(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-bundle-chain-user",
        email="checkin-bundle-chain@example.com",
    )
    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.1010,
            take_profit=1.1050,
            stop_loss=1.0975,
            lot_size=0.5,
            pnl=20.0,
            opened_at=datetime(2026, 3, 17, 9, 0, 0),
            closed_at=datetime(2026, 3, 17, 9, 20, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1002,
            exit_price=1.1012,
            take_profit=1.1050,
            stop_loss=1.0974,
            lot_size=0.5,
            pnl=18.0,
            opened_at=datetime(2026, 3, 17, 9, 15, 0),
            closed_at=datetime(2026, 3, 17, 9, 40, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1010,
            exit_price=1.1020,
            take_profit=1.1050,
            stop_loss=1.0973,
            lot_size=0.5,
            pnl=24.0,
            opened_at=datetime(2026, 3, 17, 13, 10, 0),
            closed_at=datetime(2026, 3, 17, 13, 35, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1012,
            exit_price=1.1022,
            take_profit=1.1050,
            stop_loss=1.0972,
            lot_size=0.5,
            pnl=21.0,
            opened_at=datetime(2026, 3, 17, 13, 25, 0),
            closed_at=datetime(2026, 3, 17, 13, 50, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    detected_outliers = checkin_routes.detect_outliers(trades)

    bundle_candidates = detected_outliers["bundle_candidates"]
    bundled_pubkey_groups = sorted(
        sorted(trade.pubkey for trade in candidate["trades"])
        for candidate in bundle_candidates
    )

    assert len(bundle_candidates) == 2
    assert all(len(candidate["trades"]) == 2 for candidate in bundle_candidates)
    assert bundled_pubkey_groups == sorted([
        sorted([trades[0].pubkey, trades[1].pubkey]),
        sorted([trades[2].pubkey, trades[3].pubkey]),
    ])


def test_detect_outliers_does_not_bundle_same_pair_across_multiple_days(app_ctx, client, monkeypatch):
    fixed_now = datetime(2026, 3, 21, 12, 0, 0)
    monkeypatch.setattr(checkin_routes, "utcnow_naive", lambda: fixed_now)

    user, trade_account = _create_logged_in_user(
        client,
        username="checkin-bundle-multiday-user",
        email="checkin-bundle-multiday@example.com",
    )
    trades = [
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.3400,
            exit_price=1.3390,
            take_profit=1.3360,
            stop_loss=1.3420,
            lot_size=0.2,
            pnl=8.0,
            opened_at=datetime(2026, 3, 2, 12, 55, 0),
            closed_at=datetime(2026, 3, 2, 13, 15, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.3410,
            exit_price=1.3398,
            take_profit=1.3360,
            stop_loss=1.3420,
            lot_size=0.1,
            pnl=-12.0,
            opened_at=datetime(2026, 3, 3, 13, 38, 0),
            closed_at=datetime(2026, 3, 3, 14, 5, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.3420,
            exit_price=1.3405,
            take_profit=1.3360,
            stop_loss=1.3420,
            lot_size=0.13,
            pnl=-11.0,
            opened_at=datetime(2026, 3, 4, 4, 9, 0),
            closed_at=datetime(2026, 3, 4, 4, 30, 0),
        ),
        Trade(
            user_id=user.id,
            trade_account_id=trade_account.id,
            symbol="GBPUSD",
            side="SELL",
            entry_price=1.3430,
            exit_price=1.3415,
            take_profit=1.3360,
            stop_loss=1.3420,
            lot_size=0.03,
            pnl=6.0,
            opened_at=datetime(2026, 3, 6, 10, 20, 0),
            closed_at=datetime(2026, 3, 6, 10, 50, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    detected_outliers = checkin_routes.detect_outliers(trades)

    assert detected_outliers["bundle_candidates"] == []
