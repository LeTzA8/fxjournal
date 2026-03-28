import html
import os

from cryptography.fernet import Fernet
import pytest

import routes.dashboard as dashboard_routes
import routes.trade_accounts as trade_accounts_module
from extensions import limiter
from helpers.legal import LEGAL_LAST_UPDATED
from helpers.utils import decrypt_password, encrypt_password
from models import MT5AccessRequest, MT5Account, TradeAccount, User, db


def _create_user_with_account(
    *,
    username,
    email,
    account_name="Main Account",
    account_type="CFD",
    is_default=True,
):
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
        account_type=account_type,
        is_default=is_default,
    )
    db.session.add(trade_account)
    db.session.commit()
    return user, trade_account


def _log_in_user(client, user, trade_account):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username
        session_state["display_timezone"] = "UTC"
        session_state["active_trade_account_id"] = trade_account.id


def _log_in_root_admin(client, *, email, username):
    os.environ["ADMIN_USER_EMAILS"] = email
    user, trade_account = _create_user_with_account(
        username=username,
        email=email,
    )
    user.is_admin = True
    db.session.commit()
    _log_in_user(client, user, trade_account)
    return user, trade_account


def _stub_weekly_ai_state(monkeypatch):
    monkeypatch.setattr(
        dashboard_routes,
        "_get_weekly_ai_state",
        lambda *args, **kwargs: {
            "weekly_ai_review": None,
            "weekly_ai_generated_at_label": "",
            "weekly_ai_period_label": "",
            "weekly_ai_empty_message": "No trades this week. Add closed trades to generate your AI review.",
            "weekly_ai_is_generating": False,
        },
    )


@pytest.fixture(autouse=True)
def disable_mt5_request_rate_limits(test_app):
    previous_value = test_app.config.get("RATELIMIT_ENABLED")
    test_app.config["RATELIMIT_ENABLED"] = False
    limiter.reset()
    yield
    limiter.reset()
    if previous_value is None:
        test_app.config.pop("RATELIMIT_ENABLED", None)
    else:
        test_app.config["RATELIMIT_ENABLED"] = previous_value


def test_user_can_submit_mt5_access_request_and_send_email(app_ctx, client, monkeypatch):
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "support@example.com")

    user, trade_account = _create_user_with_account(
        username="mt5-request-user",
        email="mt5-request-user@example.com",
        account_name="Request Account",
    )
    _log_in_user(client, user, trade_account)

    captured = {}

    def _fake_send_email(to_email, subject, text_body, html_body=None):
        captured["to_email"] = to_email
        captured["subject"] = subject
        captured["text_body"] = text_body
        captured["html_body"] = html_body
        return {"sent": True, "mode": "test"}

    monkeypatch.setattr(trade_accounts_module, "send_email_placeholder", _fake_send_email)

    response = client.post(
        "/dashboard/mt5/request-access",
        data={
            "trade_account_pubkey": trade_account.pubkey,
            "request_note": "Please enable MT5 sync for this account.",
        },
        follow_redirects=True,
    )

    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()

    assert response.status_code == 200
    assert request_row.user_id == user.id
    assert request_row.trade_account_id == trade_account.id
    assert request_row.status == MT5AccessRequest.STATUS_PENDING
    assert request_row.request_note == "Please enable MT5 sync for this account."
    assert captured["to_email"] == "support@example.com"
    assert "Request Account" in captured["text_body"]
    assert "Please enable MT5 sync for this account." in captured["text_body"]
    response_text = html.unescape(response.get_data(as_text=True))
    assert "MT5 sync access request submitted. I'll review it soon." in response_text


def test_mt5_access_request_is_kept_when_email_notification_is_unavailable(app_ctx, client, monkeypatch):
    monkeypatch.delenv("FEEDBACK_TO_EMAIL", raising=False)

    user, trade_account = _create_user_with_account(
        username="mt5-request-no-email",
        email="mt5-request-no-email@example.com",
        account_name="No Email Account",
    )
    _log_in_user(client, user, trade_account)

    response = client.post(
        "/dashboard/mt5/request-access",
        data={
            "trade_account_pubkey": trade_account.pubkey,
            "request_note": "No email configured.",
        },
        follow_redirects=True,
    )

    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()

    assert response.status_code == 200
    assert request_row.status == MT5AccessRequest.STATUS_PENDING
    assert request_row.request_note == "No email configured."
    assert (
        b"MT5 sync access request submitted and queued for review, but email notification could not be delivered."
        in response.data
    )


def test_user_cannot_switch_to_another_users_trade_account(app_ctx, client):
    user, own_account = _create_user_with_account(
        username="switch-own-user",
        email="switch-own@example.com",
        account_name="Own Account",
    )
    _other_user, other_account = _create_user_with_account(
        username="switch-other-user",
        email="switch-other@example.com",
        account_name="Other Account",
    )
    _log_in_user(client, user, own_account)

    response = client.post(
        "/dashboard/trade-accounts/switch",
        data={
            "trade_account_pubkey": other_account.pubkey,
            "next": "/dashboard/trades",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard/trades")

    with client.session_transaction() as session_state:
        assert session_state["active_trade_account_id"] == own_account.id

    resolved_account = trade_accounts_module.get_active_trade_account_for_user(user.id)
    assert resolved_account.id == own_account.id


def test_user_cannot_set_another_users_trade_account_as_default(app_ctx, client):
    user, own_account = _create_user_with_account(
        username="default-own-user",
        email="default-own@example.com",
        account_name="Own Account",
    )
    _other_user, other_account = _create_user_with_account(
        username="default-other-user",
        email="default-other@example.com",
        account_name="Other Account",
    )
    _log_in_user(client, user, own_account)

    response = client.post(
        f"/dashboard/trade-accounts/{other_account.pubkey}/default",
        data={"next": "/dashboard/trade-accounts"},
        follow_redirects=True,
    )

    db.session.refresh(own_account)
    db.session.refresh(other_account)

    assert response.status_code == 200
    assert b"Trade account not found." in response.data
    assert own_account.is_default is True
    assert other_account.is_default is True

    with client.session_transaction() as session_state:
        assert session_state["active_trade_account_id"] == own_account.id


def test_user_cannot_request_mt5_access_for_another_users_trade_account(app_ctx, client):
    user, own_account = _create_user_with_account(
        username="request-own-user",
        email="request-own@example.com",
        account_name="Own Account",
    )
    _other_user, other_account = _create_user_with_account(
        username="request-other-user",
        email="request-other@example.com",
        account_name="Other Account",
    )
    _log_in_user(client, user, own_account)

    response = client.post(
        "/dashboard/mt5/request-access",
        data={
            "trade_account_pubkey": other_account.pubkey,
            "request_note": "Please enable MT5 sync for this account.",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Trade account not found." in response.data
    assert MT5AccessRequest.query.filter_by(trade_account_id=other_account.id).count() == 0


def test_mt5_access_request_rejects_non_cfd_trade_accounts(app_ctx, client):
    user, trade_account = _create_user_with_account(
        username="mt5-request-futures",
        email="mt5-request-futures@example.com",
        account_name="Futures Account",
        account_type="FUTURES",
    )
    _log_in_user(client, user, trade_account)

    response = client.post(
        "/dashboard/mt5/request-access",
        data={"trade_account_pubkey": trade_account.pubkey},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert b"MT5 sync access can only be requested for CFD trade accounts." in response.data


def test_mt5_access_request_rejects_accounts_with_existing_mt5_link(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-request-linked",
        email="mt5-request-linked@example.com",
        account_name="Linked Account",
    )
    _log_in_user(client, user, trade_account)
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="99110001",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Server",
            is_active=False,
        )
    )
    db.session.commit()

    response = client.post(
        "/dashboard/mt5/request-access",
        data={"trade_account_pubkey": trade_account.pubkey},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert b"This trade account already has MT5 sync access configured." in response.data


def test_mt5_access_request_rejects_duplicate_pending_requests(app_ctx, client):
    user, trade_account = _create_user_with_account(
        username="mt5-request-pending",
        email="mt5-request-pending@example.com",
        account_name="Pending Account",
    )
    _log_in_user(client, user, trade_account)
    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=trade_account.id,
            status=MT5AccessRequest.STATUS_PENDING,
            request_note="Already waiting.",
        )
    )
    db.session.commit()

    response = client.post(
        "/dashboard/mt5/request-access",
        data={
            "trade_account_pubkey": trade_account.pubkey,
            "request_note": "Please submit again.",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 1
    assert b"An MT5 sync access request is already pending for this trade account." in response.data


def test_dashboard_home_shows_mt5_request_and_approval_states(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, requestable_account = _create_user_with_account(
        username="mt5-request-states",
        email="mt5-request-states@example.com",
        account_name="Requestable CFD",
    )
    pending_account = TradeAccount(
        user_id=user.id,
        name="Pending CFD",
        account_type="CFD",
        is_default=False,
    )
    approved_account = TradeAccount(
        user_id=user.id,
        name="Approved CFD",
        account_type="CFD",
        is_default=False,
    )
    linked_account = TradeAccount(
        user_id=user.id,
        name="Linked CFD",
        account_type="CFD",
        is_default=False,
    )
    futures_account = TradeAccount(
        user_id=user.id,
        name="Futures Only",
        account_type="FUTURES",
        is_default=False,
    )
    db.session.add_all([pending_account, approved_account, linked_account, futures_account])
    db.session.commit()

    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=pending_account.id,
            status=MT5AccessRequest.STATUS_PENDING,
            request_note="Still waiting.",
        )
    )
    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=approved_account.id,
            status=MT5AccessRequest.STATUS_APPROVED,
            request_note="Approved already.",
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=linked_account.id,
            account_number="99110002",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Server",
            is_active=False,
        )
    )
    db.session.commit()

    _log_in_user(client, user, requestable_account)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Request MT5 Sync Access" in response.data
    assert b"Submit MT5 Account Details" in response.data
    assert b"Pending Review" in response.data
    assert b"Approved" in response.data
    assert b"MT5 Linked" in response.data
    assert b"Requestable CFD" in response.data
    assert b"Approved CFD" in response.data
    assert b"Choose a CFD trade account" in response.data
    assert b"Choose an approved CFD trade account" in response.data
    assert b"read-only credentials only" in response.data
    assert b"Terms and Conditions" in response.data
    assert b"Privacy Policy" in response.data


def test_user_can_submit_mt5_details_after_approval(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-approved-details",
        email="mt5-approved-details@example.com",
        account_name="Approved Details Account",
    )
    _log_in_user(client, user, trade_account)
    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=trade_account.id,
            status=MT5AccessRequest.STATUS_APPROVED,
            request_note="Ready for details.",
        )
    )
    db.session.commit()

    response = client.post(
        "/dashboard/mt5/submit-details",
        data={
            "trade_account_pubkey": trade_account.pubkey,
            "account_number": "70010001",
            "server": "Broker-Live",
            "investor_password": "investor-pass",
            "mt5_sync_consent": "on",
        },
        follow_redirects=True,
    )

    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()

    assert response.status_code == 200
    assert mt5_account.user_id == user.id
    assert mt5_account.account_number == "70010001"
    assert mt5_account.server == "Broker-Live"
    assert mt5_account.is_active is False
    assert mt5_account.investor_password_encrypted != "investor-pass"
    assert decrypt_password(mt5_account.investor_password_encrypted) == "investor-pass"
    assert mt5_account.mt5_consent_accepted_at is not None
    assert mt5_account.mt5_consent_version == LEGAL_LAST_UPDATED
    assert b"MT5 account details saved for Approved Details Account." in response.data


def test_mt5_detail_submission_requires_approval(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-details-no-approval",
        email="mt5-details-no-approval@example.com",
        account_name="No Approval Account",
    )
    _log_in_user(client, user, trade_account)

    response = client.post(
        "/dashboard/mt5/submit-details",
        data={
            "trade_account_pubkey": trade_account.pubkey,
            "account_number": "70010002",
            "server": "Broker-Live",
            "investor_password": "investor-pass",
            "mt5_sync_consent": "on",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5Account.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert b"This trade account has not been approved for MT5 sync yet." in response.data


def test_mt5_detail_submission_requires_read_only_consent(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-details-no-consent",
        email="mt5-details-no-consent@example.com",
        account_name="No Consent Account",
    )
    _log_in_user(client, user, trade_account)
    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=trade_account.id,
            status=MT5AccessRequest.STATUS_APPROVED,
            request_note="Approved but awaiting consent.",
        )
    )
    db.session.commit()

    response = client.post(
        "/dashboard/mt5/submit-details",
        data={
            "trade_account_pubkey": trade_account.pubkey,
            "account_number": "70010003",
            "server": "Broker-Live",
            "investor_password": "investor-pass",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5Account.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert (
        b"Confirm that you are submitting MT5 investor/read-only credentials and accept the Terms and Privacy Policy for MT5 sync."
        in response.data
    )


def test_trade_accounts_page_shows_mt5_status_only(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, requestable_account = _create_user_with_account(
        username="mt5-request-status-only",
        email="mt5-request-status-only@example.com",
        account_name="Requestable CFD",
    )
    pending_account = TradeAccount(
        user_id=user.id,
        name="Pending CFD",
        account_type="CFD",
        is_default=False,
    )
    approved_account = TradeAccount(
        user_id=user.id,
        name="Approved CFD",
        account_type="CFD",
        is_default=False,
    )
    linked_account = TradeAccount(
        user_id=user.id,
        name="Linked CFD",
        account_type="CFD",
        is_default=False,
    )
    db.session.add_all([pending_account, approved_account, linked_account])
    db.session.commit()

    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=pending_account.id,
            status=MT5AccessRequest.STATUS_PENDING,
            request_note="Still waiting.",
        )
    )
    db.session.add(
        MT5AccessRequest(
            user_id=user.id,
            trade_account_id=approved_account.id,
            status=MT5AccessRequest.STATUS_APPROVED,
            request_note="Ready.",
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=linked_account.id,
            account_number="99110003",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Server",
            is_active=False,
        )
    )
    db.session.commit()

    _log_in_user(client, user, requestable_account)

    response = client.get("/dashboard/trade-accounts")

    assert response.status_code == 200
    assert b"MT5 Request Pending" in response.data
    assert b"MT5 Approved" in response.data
    assert b"MT5 Linked" in response.data
    assert b"Manage MT5 requests from the dashboard card instead of per-account forms." in response.data
    assert b"Open Dashboard MT5 Access" in response.data
    assert b"Submit your MT5 account number, read-only investor password, and server from the dashboard card." in response.data
    assert b"Request MT5 Sync Access" not in response.data


def test_admin_mt5_page_shows_pending_requests_and_supports_review(app_ctx, client):
    root_user, _ = _log_in_root_admin(
        client,
        email="mt5-request-root@example.com",
        username="mt5-request-root",
    )

    first_user, first_account = _create_user_with_account(
        username="mt5-review-first",
        email="mt5-review-first@example.com",
        account_name="First Review Account",
    )
    second_user, second_account = _create_user_with_account(
        username="mt5-review-second",
        email="mt5-review-second@example.com",
        account_name="Second Review Account",
    )
    first_request = MT5AccessRequest(
        user_id=first_user.id,
        trade_account_id=first_account.id,
        status=MT5AccessRequest.STATUS_PENDING,
        request_note="Please approve this one.",
    )
    second_request = MT5AccessRequest(
        user_id=second_user.id,
        trade_account_id=second_account.id,
        status=MT5AccessRequest.STATUS_PENDING,
        request_note="Please reject this one.",
    )
    db.session.add_all([first_request, second_request])
    db.session.commit()

    list_response = client.get("/dashboard/admin/access/mt5")
    approve_response = client.post(
        f"/dashboard/admin/access/mt5/requests/{first_request.id}/approve",
        data={},
        follow_redirects=True,
    )
    reject_response = client.post(
        f"/dashboard/admin/access/mt5/requests/{second_request.id}/reject",
        data={},
        follow_redirects=True,
    )
    db.session.expire_all()

    approved_request = db.session.get(MT5AccessRequest, first_request.id)
    rejected_request = db.session.get(MT5AccessRequest, second_request.id)

    assert list_response.status_code == 200
    assert b"Pending MT5 Requests" in list_response.data
    assert b"First Review Account" in list_response.data
    assert b"Second Review Account" in list_response.data
    assert approve_response.status_code == 200
    assert reject_response.status_code == 200
    assert approved_request.status == MT5AccessRequest.STATUS_APPROVED
    assert rejected_request.status == MT5AccessRequest.STATUS_REJECTED
    assert approved_request.reviewed_by_user_id == root_user.id
    assert rejected_request.reviewed_by_user_id == root_user.id
    assert approved_request.reviewed_at is not None
    assert rejected_request.reviewed_at is not None


def test_non_root_user_cannot_review_mt5_access_requests(app_ctx, client):
    user, trade_account = _create_user_with_account(
        username="mt5-review-blocked-user",
        email="mt5-review-blocked-user@example.com",
    )
    request_owner, request_account = _create_user_with_account(
        username="mt5-review-request-owner",
        email="mt5-review-request-owner@example.com",
        account_name="Blocked Review Account",
    )
    request_row = MT5AccessRequest(
        user_id=request_owner.id,
        trade_account_id=request_account.id,
        status=MT5AccessRequest.STATUS_PENDING,
    )
    db.session.add(request_row)
    db.session.commit()
    _log_in_user(client, user, trade_account)

    response = client.post(
        f"/dashboard/admin/access/mt5/requests/{request_row.id}/approve",
        data={},
        follow_redirects=False,
    )

    assert response.status_code == 404
