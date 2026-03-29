import json
from datetime import datetime
from itertools import count

from ai_service import WEEKLY_DASHBOARD_KIND
from models import AIGeneratedResponse, AIPromptHistory, TradeAccount, User, db


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


def _build_payload(*, net_pnl, win_rate, emotional_score):
    return json.dumps(
        {
            "notes_coverage": 0.5,
            "notes_confidence": "medium",
            "notes_with_content": 2,
            "notes_missing": 2,
            "account_age_days": 45,
            "summary": {
                "closed_trades": 4,
                "win_rate": win_rate,
                "net_pnl": net_pnl,
                "max_drawdown": 42.5,
            },
            "emotional_index": {
                "score": emotional_score,
                "label": "moderate",
                "self_report_mismatch": False,
                "signals": {
                    "bundle_count": 1,
                    "revenge_trade_count": 1,
                    "reactive_trade_count": 1,
                    "corrective_trade_count": 0,
                    "total_closed_trades": 4,
                },
            },
            "historical_context": {
                "comparison_scope": "history_before_review_period_only",
                "window_days": 90,
                "summary": {
                    "closed_trades": 12,
                    "win_rate": 58.0,
                    "net_pnl": 320.0,
                    "max_drawdown": 88.0,
                },
                "top_pairs": [
                    {"symbol": "EURUSD", "count": 5, "win_rate": 60.0, "net_pnl": 140.0},
                ],
                "top_sessions": [
                    {"name": "London", "count": 6, "win_rate": 50.0, "net_pnl": 90.0},
                ],
                "top_weekdays": [
                    {"name": "Tuesday", "count": 3, "win_rate": 66.7, "net_pnl": 120.0},
                ],
            },
        }
    )


def _create_ai_response(
    *,
    user_id,
    trade_account_id,
    prompt_history_id,
    period_start_utc,
    period_end_utc,
    generated_at,
    response_text,
    payload_json,
):
    row = AIGeneratedResponse(
        user_id=user_id,
        trade_account_id=trade_account_id,
        prompt_history_id=prompt_history_id,
        kind=WEEKLY_DASHBOARD_KIND,
        model="gpt-5-mini",
        response_text=response_text,
        payload_json=payload_json,
        payload_hash=response_text,
        trade_count_used=4,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        generated_at=generated_at,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _login_as(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


def test_logged_out_weekly_report_returns_404(app_ctx, client):
    response = client.get("/dashboard/admin/access/weekly-report", follow_redirects=False)

    assert response.status_code == 404


def test_non_root_admin_weekly_report_returns_404(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    monkeypatch.setenv("ADMIN_USER_EMAILS", f"weekly-root{suffix}@example.com")
    admin_user = _create_user(
        username=f"weekly-dbadmin{suffix}",
        email=f"weekly-dbadmin{suffix}@example.com",
        is_admin=True,
    )
    db.session.commit()

    _login_as(client, admin_user)

    response = client.get("/dashboard/admin/access/weekly-report", follow_redirects=False)

    assert response.status_code == 404


def test_root_admin_users_page_shows_ai_audit_link(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"weekly-root{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"weekly-rootadmin{suffix}", email=root_email)
    target_user = _create_user(
        username=f"weekly-targetuser{suffix}",
        email=f"weekly-target{suffix}@example.com",
    )
    _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    db.session.commit()

    _login_as(client, root_admin)

    response = client.get("/dashboard/admin/access/users?status=all")

    assert response.status_code == 200
    assert b"Weekly AI" in response.data
    assert b"AI Audit" in response.data
    assert f"/dashboard/admin/access/weekly-report/{target_user.id}".encode() in response.data


def test_weekly_report_keeps_latest_generation_per_week(app_ctx, client, monkeypatch):
    suffix = _unique_suffix()
    root_email = f"weekly-root{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", root_email)
    root_admin = _create_user(username=f"weekly-rootadmin{suffix}", email=root_email)
    target_user = _create_user(
        username=f"weekly-targetuser{suffix}",
        email=f"weekly-target{suffix}@example.com",
    )
    account = _create_trade_account(user_id=target_user.id, name="Primary FX", account_type="CFD", is_default=True)
    prompt_history = _create_prompt_history()

    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 17, 0, 0, 0),
        generated_at=datetime(2026, 3, 17, 12, 0, 0),
        response_text="Older duplicated output",
        payload_json=_build_payload(net_pnl=110.0, win_rate=50.0, emotional_score=2.2),
    )
    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=datetime(2026, 3, 10, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 17, 0, 0, 0),
        generated_at=datetime(2026, 3, 17, 12, 30, 0),
        response_text="Latest duplicated output",
        payload_json=_build_payload(net_pnl=125.0, win_rate=75.0, emotional_score=1.8),
    )
    _create_ai_response(
        user_id=target_user.id,
        trade_account_id=account.id,
        prompt_history_id=prompt_history.id,
        period_start_utc=datetime(2026, 3, 3, 0, 0, 0),
        period_end_utc=datetime(2026, 3, 10, 0, 0, 0),
        generated_at=datetime(2026, 3, 10, 12, 0, 0),
        response_text="Previous week output",
        payload_json=_build_payload(net_pnl=-40.0, win_rate=25.0, emotional_score=3.1),
    )
    db.session.commit()

    _login_as(client, root_admin)

    response = client.get(
        f"/dashboard/admin/access/weekly-report/{target_user.id}?trade_account_id={account.id}"
    )

    assert response.status_code == 200
    assert b"Latest duplicated output" in response.data
    assert b"Previous week output" in response.data
    assert b"Older duplicated output" not in response.data
    assert b"Saved Prompt Text" in response.data
    assert b"Prompt text" in response.data
    assert b"125.0" in response.data
    assert b"110.0" not in response.data
