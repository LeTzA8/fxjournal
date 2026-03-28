from datetime import datetime
from itertools import count

import auth_account
import celery_workers.tasks as celery_tasks
from ai_service import AIConfigError, AIRequestError, WEEKLY_DASHBOARD_KIND
from models import AIGeneratedResponse, AIPromptHistory, Trade, TradeAccount, User, db


_UNIQUE_COUNTER = count(1)


def _unique_suffix():
    return next(_UNIQUE_COUNTER)


def _create_user(
    *,
    username,
    email,
    email_verified=True,
    signup_status="approved",
    is_admin=False,
):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=email_verified,
        signup_status=signup_status,
        is_admin=is_admin,
        approved_at=datetime(2026, 3, 12, 9, 0, 0) if signup_status == "approved" else None,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _create_trade_account(*, user_id, name, account_type="CFD", is_default=False):
    account = TradeAccount(
        user_id=user_id,
        name=name,
        account_type=account_type,
        is_default=is_default,
    )
    db.session.add(account)
    db.session.flush()
    return account


def _create_prompt_history(prompt_id="dashboard_advice"):
    suffix = _unique_suffix()
    prompt_history = AIPromptHistory(
        prompt_id=prompt_id,
        prompt_sha256=f"{prompt_id}-sha256-{suffix}",
        prompt_text="Prompt text",
        source_path="prompts/dashboard_advice.txt",
    )
    db.session.add(prompt_history)
    db.session.flush()
    return prompt_history


def _create_ai_response(
    *,
    user_id,
    trade_account_id,
    prompt_history_id,
    period_start_utc,
    response_text,
):
    row = AIGeneratedResponse(
        user_id=user_id,
        trade_account_id=trade_account_id,
        prompt_history_id=prompt_history_id,
        kind=WEEKLY_DASHBOARD_KIND,
        model="gpt-5-mini",
        response_text=response_text,
        payload_hash=response_text,
        trade_count_used=3,
        period_start_utc=period_start_utc,
        period_end_utc=datetime(2026, 3, 14, 21, 30, 0),
    )
    db.session.add(row)
    db.session.flush()
    return row


def _login_as(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


def test_logged_out_admin_users_page_redirects_to_login(app_ctx, client):
    response = client.get("/dashboard/admin/access/users", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_logged_in_non_admin_users_page_returns_404(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"root{suffix}@example.com")
    user = _create_user(username=f"plainuser{suffix}", email=f"plain{suffix}@example.com")
    db.session.commit()

    _login_as(client, user)

    response = client.get("/dashboard/admin/access/users", follow_redirects=False)

    assert response.status_code == 404


def test_root_admin_users_page_shows_regen_ai_with_account_options(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    _create_trade_account(user_id=target_user.id, name="Index Futures", account_type="FUTURES")
    db.session.commit()

    _login_as(client, root_admin)

    response = client.get("/dashboard/admin/access/users?status=all")

    assert response.status_code == 200
    assert b"Regen AI" in response.data
    assert b"Unbundle All" in response.data
    assert b"Review Bundles" in response.data
    assert b"Primary FX (CFD)" in response.data
    assert b"Index Futures (Futures)" in response.data
    assert f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice".encode() in response.data
    assert f"/dashboard/admin/access/users/{target_user.id}/unbundle-trades".encode() in response.data
    assert f"/dashboard/admin/access/users/{target_user.id}/backfill-bundles".encode() in response.data


def test_non_root_admin_users_page_hides_regen_ai(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"root{suffix}@example.com")
    admin_user = _create_user(
        username=f"dbadmin{suffix}",
        email=f"dbadmin{suffix}@example.com",
        is_admin=True,
    )
    target_user = _create_user(username=f"targetuser{suffix}", email=f"target{suffix}@example.com")
    _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    db.session.commit()

    _login_as(client, admin_user)

    response = client.get("/dashboard/admin/access/users?status=all")

    assert response.status_code == 200
    assert b"Regen AI" not in response.data
    assert b"Unbundle All" not in response.data
    assert b"Review Bundles" not in response.data


def test_non_root_admin_regenerate_ai_route_returns_404(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"root{suffix}@example.com")
    admin_user = _create_user(
        username=f"dbadmin{suffix}",
        email=f"dbadmin{suffix}@example.com",
        is_admin=True,
    )
    target_user = _create_user(username=f"targetuser{suffix}", email=f"target{suffix}@example.com")
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    db.session.commit()

    _login_as(client, admin_user)

    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice",
        data={"trade_account_id": str(account.id)},
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_non_root_admin_bundle_backfill_route_returns_404(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"root{suffix}@example.com")
    admin_user = _create_user(
        username=f"dbadmin{suffix}",
        email=f"dbadmin{suffix}@example.com",
        is_admin=True,
    )
    target_user = _create_user(username=f"targetuser{suffix}", email=f"target{suffix}@example.com")
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    db.session.commit()

    _login_as(client, admin_user)

    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/backfill-bundles",
        data={"trade_account_id": str(account.id)},
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_non_root_admin_unbundle_route_returns_404(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"root{suffix}@example.com")
    admin_user = _create_user(
        username=f"dbadmin{suffix}",
        email=f"dbadmin{suffix}@example.com",
        is_admin=True,
    )
    target_user = _create_user(username=f"targetuser{suffix}", email=f"target{suffix}@example.com")
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    db.session.commit()

    _login_as(client, admin_user)

    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/unbundle-trades",
        data={"trade_account_id": str(account.id)},
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_admin_backfill_trade_bundles_marks_account_pending_review_without_applying(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    trades = [
        Trade(
            user_id=target_user.id,
            trade_account_id=account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0990,
            take_profit=1.1040,
            lot_size=1.0,
            pnl=-45.0,
            opened_at=datetime(2026, 3, 10, 9, 50, 0),
            closed_at=datetime(2026, 3, 10, 10, 15, 0),
        ),
        Trade(
            user_id=target_user.id,
            trade_account_id=account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0995,
            exit_price=1.1002,
            take_profit=1.1030,
            lot_size=0.5,
            pnl=22.0,
            opened_at=datetime(2026, 3, 10, 10, 20, 0),
            closed_at=datetime(2026, 3, 10, 10, 40, 0),
        ),
        Trade(
            user_id=target_user.id,
            trade_account_id=account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0998,
            exit_price=1.1004,
            take_profit=1.1030,
            lot_size=0.5,
            pnl=18.0,
            opened_at=datetime(2026, 3, 10, 10, 25, 0),
            closed_at=datetime(2026, 3, 10, 10, 42, 0),
        ),
    ]
    db.session.add_all(trades)
    db.session.commit()

    _login_as(client, root_admin)

    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/backfill-bundles",
        data={"trade_account_id": str(account.id)},
        follow_redirects=False,
    )

    db.session.refresh(account)
    for trade in trades:
        db.session.refresh(trade)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard/admin/access/users")
    assert account.bundle_review_requested_at is not None
    assert account.bundle_review_completed_at is None
    assert trades[1].bundle_pubkey is None
    assert trades[2].bundle_pubkey is None
    assert trades[1].is_reactive is False
    assert trades[2].is_reactive is False


def test_admin_unbundle_trade_account_clears_bundle_links_but_keeps_flags(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    trades = [
        Trade(
            user_id=target_user.id,
            trade_account_id=account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1000,
            exit_price=1.0990,
            lot_size=1.0,
            pnl=-45.0,
            opened_at=datetime(2026, 3, 10, 9, 50, 0),
            closed_at=datetime(2026, 3, 10, 10, 15, 0),
            bundle_pubkey="bundle-123",
            is_reactive=True,
        ),
        Trade(
            user_id=target_user.id,
            trade_account_id=account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.0995,
            exit_price=1.1002,
            lot_size=0.5,
            pnl=22.0,
            opened_at=datetime(2026, 3, 10, 10, 20, 0),
            closed_at=datetime(2026, 3, 10, 10, 40, 0),
            bundle_pubkey="bundle-123",
            is_corrective=True,
        ),
    ]
    db.session.add_all(trades)
    account.bundle_review_requested_at = datetime(2026, 3, 10, 11, 0, 0)
    account.bundle_review_completed_at = datetime(2026, 3, 10, 11, 30, 0)
    db.session.commit()

    _login_as(client, root_admin)

    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/unbundle-trades",
        data={"trade_account_id": str(account.id)},
        follow_redirects=False,
    )

    db.session.refresh(account)
    for trade in trades:
        db.session.refresh(trade)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard/admin/access/users")
    assert trades[0].bundle_pubkey is None
    assert trades[1].bundle_pubkey is None
    assert trades[0].is_reactive is True
    assert trades[1].is_corrective is True
    assert account.bundle_review_requested_at is None
    assert account.bundle_review_completed_at is None




def test_admin_regenerate_ai_advice_success_appends_new_row_and_keeps_history(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    prompt_history = _create_prompt_history(prompt_id="dashboard_advice")
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=period["period_start_utc"],
        response_text="Old advice 1",
    )
    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=period["period_start_utc"],
        response_text="Old advice 2",
    )
    db.session.commit()

    monkeypatch.setattr(auth_account, "get_latest_trade_week_period", lambda **kwargs: period)

    call_state = {"count": 0}

    def fake_delay(*args, **kwargs):
        call_state["count"] += 1
        assert args[0] == target_user.id
        assert args[1] == account.id
        assert args[2] == "dashboard_advice.txt"
        assert args[3] == period["period_start_utc"].isoformat()
        assert kwargs["force_regenerate"] is True
        assert kwargs["send_weekly_email"] is False

    monkeypatch.setattr(celery_tasks.generate_weekly_ai_task, "delay", fake_delay)

    _login_as(client, root_admin)
    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice",
        data={"trade_account_id": str(account.id)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert call_state["count"] == 1
    assert f"AI review generation queued for {target_user.email} / Primary FX.".encode() in response.data

    rows = AIGeneratedResponse.query.filter_by(
        user_id=target_user.id,
        trade_account_id=account.id,
        kind=WEEKLY_DASHBOARD_KIND,
        period_start_utc=period["period_start_utc"],
    ).order_by(AIGeneratedResponse.id.asc()).all()
    assert len(rows) == 2
    assert [row.response_text for row in rows] == ["Old advice 1", "Old advice 2"]


def test_admin_regenerate_ai_advice_requires_account_selection(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    db.session.commit()

    call_state = {"count": 0}

    def fake_delay(*args, **kwargs):
        call_state["count"] += 1

    monkeypatch.setattr(celery_tasks.generate_weekly_ai_task, "delay", fake_delay)

    _login_as(client, root_admin)
    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert call_state["count"] == 0
    assert b"No trade account selected." in response.data


def test_admin_regenerate_ai_advice_404s_for_account_outside_user(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=f"target{suffix}@example.com")
    other_user = _create_user(username=f"otheruser{suffix}", email=f"other{suffix}@example.com")
    other_account = _create_trade_account(user_id=other_user.id, name="Other Account", account_type="CFD", is_default=True)
    db.session.commit()

    _login_as(client, root_admin)
    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice",
        data={"trade_account_id": str(other_account.id)},
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_admin_regenerate_ai_advice_warns_when_no_period_exists(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    db.session.commit()

    monkeypatch.setattr(auth_account, "get_latest_trade_week_period", lambda **kwargs: None)
    call_state = {"count": 0}

    def fake_delay(*args, **kwargs):
        call_state["count"] += 1

    monkeypatch.setattr(celery_tasks.generate_weekly_ai_task, "delay", fake_delay)

    _login_as(client, root_admin)
    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice",
        data={"trade_account_id": str(account.id)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert call_state["count"] == 0
    assert f"No active trading period found for {target_user.email} / Primary FX.".encode() in response.data


def test_admin_regenerate_ai_advice_rolls_back_and_preserves_existing_rows_on_failure(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    prompt_history = _create_prompt_history(prompt_id="dashboard_advice-failure")
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=period["period_start_utc"],
        response_text="Old advice 1",
    )
    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=period["period_start_utc"],
        response_text="Old advice 2",
    )
    db.session.commit()

    monkeypatch.setattr(auth_account, "get_latest_trade_week_period", lambda **kwargs: period)
    monkeypatch.setenv("ERROR_LOG_TO_EMAIL", "alerts@example.com")

    email_calls = []

    def fake_send_email(to_email, subject, text_body, html_body=None):
        email_calls.append(
            {
                "to_email": to_email,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
            }
        )
        return {"sent": True, "mode": "resend"}

    monkeypatch.setattr(auth_account, "send_email_placeholder", fake_send_email)

    def fake_delay(*args, **kwargs):
        raise AIRequestError("temporary failure")

    monkeypatch.setattr(celery_tasks.generate_weekly_ai_task, "delay", fake_delay)

    _login_as(client, root_admin)
    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice",
        data={"trade_account_id": str(account.id)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"AI advice regeneration is temporarily unavailable. Please try again in a little while." in response.data

    rows = (
        AIGeneratedResponse.query.filter_by(
            user_id=target_user.id,
            trade_account_id=account.id,
            kind=WEEKLY_DASHBOARD_KIND,
            period_start_utc=period["period_start_utc"],
    )
        .order_by(AIGeneratedResponse.id.asc())
        .all()
    )
    assert len(rows) == 2
    assert [row.response_text for row in rows] == ["Old advice 1", "Old advice 2"]
    assert len(email_calls) == 1
    assert email_calls[0]["to_email"] == "alerts@example.com"
    assert "AIRequestError" in email_calls[0]["subject"]
    assert "Handled admin AI regeneration failure" in email_calls[0]["text_body"]
    assert f"Trade account ID: {account.id}" in email_calls[0]["text_body"]


def test_admin_regenerate_ai_advice_rolls_back_on_config_error(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"root{suffix}@example.com"
    target_email = f"target{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"rootadmin{suffix}", email=root_email)
    target_user = _create_user(username=f"targetuser{suffix}", email=target_email)
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    prompt_history = _create_prompt_history(prompt_id="dashboard_advice-config")
    period = {
        "period_start_utc": datetime(2026, 3, 7, 21, 30, 0),
        "period_end_utc": datetime(2026, 3, 14, 21, 30, 0),
    }
    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=period["period_start_utc"],
        response_text="Existing advice",
    )
    db.session.commit()

    monkeypatch.setattr(auth_account, "get_latest_trade_week_period", lambda **kwargs: period)

    def fake_delay(*args, **kwargs):
        raise AIConfigError("missing api key")

    monkeypatch.setattr(celery_tasks.generate_weekly_ai_task, "delay", fake_delay)

    _login_as(client, root_admin)
    response = client.post(
        f"/dashboard/admin/access/users/{target_user.id}/regenerate-ai-advice",
        data={"trade_account_id": str(account.id)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"AI advice regeneration is temporarily unavailable. Please try again in a little while." in response.data

    rows = AIGeneratedResponse.query.filter_by(
        user_id=target_user.id,
        trade_account_id=account.id,
        kind=WEEKLY_DASHBOARD_KIND,
        period_start_utc=period["period_start_utc"],
    ).all()
    assert len(rows) == 1
    assert rows[0].response_text == "Existing advice"
