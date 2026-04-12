import html
import os

from cryptography.fernet import Fernet
import pytest

import routes.dashboard as dashboard_routes
import routes.trade_accounts as trade_accounts_module
from extensions import limiter
from helpers.legal import LEGAL_LAST_UPDATED
from helpers.utils import decrypt_password, encrypt_password, utcnow_naive
from models import MT5AccessRequest, MT5Account, MT5SyncBatch, TradeAccount, User, db


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


def _single_step_mt5_payload(trade_account, **overrides):
    payload = {
        "trade_account_pubkey": trade_account.pubkey,
        "account_number": "70010001",
        "server": "Broker-Live",
        "investor_password": "investor-pass",
        "mt5_sync_consent": "on",
    }
    payload.update(overrides)
    return payload


def _stub_mt5_setup_queue(monkeypatch, captured=None, *, should_raise=False):
    def _fake_apply_async(args=None, kwargs=None, queue=None):
        if should_raise:
            raise RuntimeError("queue unavailable")
        if captured is not None:
            captured.append(
                {
                    "args": list(args or []),
                    "kwargs": dict(kwargs or {}),
                    "queue": queue,
                }
            )
        return {"id": "test-mt5-setup-task"}

    monkeypatch.setattr(
        "celery_workers.mt5_setup_tasks.setup_mt5_terminal.apply_async",
        _fake_apply_async,
    )


def _create_mt5_batch(
    *,
    name="Beta MT5 Batch",
    capacity_total=5,
    total_slots_claimed=0,
    is_open=True,
):
    if is_open:
        for batch in MT5SyncBatch.query.filter_by(is_open=True).all():
            batch.is_open = False
            batch.closed_at = utcnow_naive()
        db.session.flush()
    batch = MT5SyncBatch(
        name=name,
        capacity_total=capacity_total,
        total_slots_claimed=total_slots_claimed,
        is_open=is_open,
        opened_at=utcnow_naive(),
    )
    db.session.add(batch)
    db.session.commit()
    return batch


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


def test_user_can_submit_mt5_sync_request_and_send_confirmation_emails(app_ctx, client, monkeypatch):
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "support@example.com")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    queued_jobs = []

    user, trade_account = _create_user_with_account(
        username="mt5-request-user",
        email="mt5-request-user@example.com",
        account_name="Request Account",
    )
    _log_in_user(client, user, trade_account)

    captured = []

    def _fake_send_email(to_email, subject, text_body, html_body=None):
        captured.append(
            {
                "to_email": to_email,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
            }
        )
        return {"sent": True, "mode": "test"}

    monkeypatch.setattr(trade_accounts_module, "send_email_placeholder", _fake_send_email)
    _stub_mt5_setup_queue(monkeypatch, queued_jobs)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(
            trade_account,
            account_number="70018881",
            server="ICMarketsSC-Demo",
        ),
        follow_redirects=True,
    )

    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()

    assert response.status_code == 200
    assert request_row.user_id == user.id
    assert request_row.trade_account_id == trade_account.id
    assert request_row.status == MT5AccessRequest.STATUS_PENDING
    assert request_row.request_note is None
    assert mt5_account.account_number == "70018881"
    assert mt5_account.server == "ICMarketsSC-Demo"
    assert mt5_account.is_active is False
    assert request_row.batch_id is None
    assert decrypt_password(mt5_account.investor_password_encrypted) == "investor-pass"
    assert mt5_account.mt5_consent_accepted_at is not None
    assert mt5_account.mt5_consent_version == LEGAL_LAST_UPDATED
    assert queued_jobs == [{"args": [mt5_account.id], "kwargs": {}, "queue": "mt5_setup"}]
    assert {email["to_email"] for email in captured} == {"support@example.com", user.email}
    admin_email = next(email for email in captured if email["to_email"] == "support@example.com")
    user_email = next(email for email in captured if email["to_email"] == user.email)
    assert "Request Account" in admin_email["text_body"]
    assert "70018881" in admin_email["text_body"]
    assert "Setup Queued: yes" in admin_email["text_body"]
    assert "Your MT5 sync setup has started" == user_email["subject"]
    assert user_email["html_body"] is not None
    assert "MT5 setup started" in user_email["html_body"]
    assert "Request Account" in user_email["html_body"]
    response_text = html.unescape(response.get_data(as_text=True))
    assert "MT5 setup started right away. We'll email you when your sync is ready." in response_text


def test_mt5_sync_request_returns_json_without_redirect(app_ctx, client, monkeypatch):
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "support@example.com")
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-request-json",
        email="mt5-request-json@example.com",
        account_name="JSON Account",
    )
    _log_in_user(client, user, trade_account)
    monkeypatch.setattr(
        trade_accounts_module,
        "send_email_placeholder",
        lambda *_args, **_kwargs: {"sent": True, "mode": "test"},
    )
    _stub_mt5_setup_queue(monkeypatch)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(trade_account),
        headers={"X-Requested-With": "XMLHttpRequest"},
        follow_redirects=False,
    )

    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["status_label"] == "Setup Queued"
    assert payload["account_name"] == "JSON Account"
    assert payload["progress_stage"] == 2
    assert "MT5 setup started right away." in payload["message"]


def test_mt5_sync_request_is_kept_when_email_notification_is_unavailable(app_ctx, client, monkeypatch):
    monkeypatch.delenv("FEEDBACK_TO_EMAIL", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-request-no-email",
        email="mt5-request-no-email@example.com",
        account_name="No Email Account",
    )
    _log_in_user(client, user, trade_account)
    monkeypatch.setattr(
        trade_accounts_module,
        "send_email_placeholder",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("email unavailable")),
    )
    _stub_mt5_setup_queue(monkeypatch)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(trade_account),
        follow_redirects=True,
    )

    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()

    assert response.status_code == 200
    assert request_row.status == MT5AccessRequest.STATUS_PENDING
    assert request_row.request_note is None
    assert mt5_account.account_number == "70010001"
    assert b"MT5 setup started right away. We&#39;ll email you when your sync is ready." in response.data


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
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
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
        data=_single_step_mt5_payload(other_account),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Trade account not found." in response.data
    assert MT5AccessRequest.query.filter_by(trade_account_id=other_account.id).count() == 0


def test_mt5_access_request_rejects_non_cfd_trade_accounts(app_ctx, client):
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
    user, trade_account = _create_user_with_account(
        username="mt5-request-futures",
        email="mt5-request-futures@example.com",
        account_name="Futures Account",
        account_type="FUTURES",
    )
    _log_in_user(client, user, trade_account)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(trade_account),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert b"MT5 sync currently supports CFD trade accounts only." in response.data


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
        data=_single_step_mt5_payload(trade_account),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert b"This trade account already has MT5 sync setup in progress." in response.data


def test_existing_pending_request_can_be_completed_with_full_details(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "support@example.com")
    _stub_mt5_setup_queue(monkeypatch)
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
        data=_single_step_mt5_payload(
            trade_account,
            account_number="70015555",
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 1
    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()
    assert request_row.status == MT5AccessRequest.STATUS_PENDING
    assert request_row.request_note == "Already waiting."
    assert mt5_account.account_number == "70015555"
    assert b"MT5 setup started right away. We&#39;ll email you when your sync is ready." in response.data


def test_dashboard_home_uses_active_account_for_mt5_panel(app_ctx, client, monkeypatch):
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
    submitted_account = TradeAccount(
        user_id=user.id,
        name="Submitted CFD",
        account_type="CFD",
        is_default=False,
    )
    futures_account = TradeAccount(
        user_id=user.id,
        name="Futures Only",
        account_type="FUTURES",
        is_default=False,
    )
    db.session.add_all([pending_account, approved_account, linked_account, submitted_account, futures_account])
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
            is_active=True,
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=submitted_account.id,
            account_number="99110003",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Server",
            is_active=False,
        )
    )
    db.session.commit()

    _log_in_user(client, user, requestable_account)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Start MT5 Sync" in response.data
    assert b"Requestable CFD" in response.data
    assert b"Fill in and submit the form below to queue setup." in response.data
    assert b"Pending Review" not in response.data
    assert b"Finish Request" not in response.data
    assert b"Save MT5 Account Details" not in response.data
    assert b"Request MT5 Sync Access" not in response.data


def test_dashboard_home_treats_inactive_mt5_details_as_setup_pending_not_active_sync(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-submitted-dashboard",
        email="mt5-submitted-dashboard@example.com",
        account_name="Submitted Account",
    )
    _log_in_user(client, user, trade_account)

    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="70018881",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Live",
            is_active=False,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-dashboard-state="state-1"' in response.data
    assert b'data-current-stage="2"' in response.data
    assert b"Setup Queued" in response.data
    assert b"Submit Details" in response.data
    assert b"Setup Queued" in response.data
    assert b"Setting Up" in response.data
    assert b"Sync Active" in response.data
    assert b"setup started right away" in response.data
    assert b"Optional: automatic sync for this account after you have trades." in response.data
    assert b'id="trade-journal"' not in response.data
    assert b"Weekly AI Review" in response.data
    assert b"Session Performance" not in response.data


def test_dashboard_home_treats_legacy_approved_request_as_direct_submit_flow(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-legacy-approved",
        email="mt5-legacy-approved@example.com",
        account_name="Legacy Approved Account",
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

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Submit Details" in response.data
    assert b"Complete the form below to resume MT5 setup." in response.data
    assert b"Approval is already in place for this account." not in response.data
    assert b"APPROVED" not in response.data


def test_dashboard_home_prompts_switch_when_active_account_is_not_cfd(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, futures_account = _create_user_with_account(
        username="mt5-futures-active",
        email="mt5-futures-active@example.com",
        account_name="Futures Active",
        account_type="FUTURES",
    )
    cfd_account = TradeAccount(
        user_id=user.id,
        name="CFD Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add(cfd_account)
    db.session.commit()
    _log_in_user(client, user, futures_account)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Switch your active dashboard account to a CFD trade account" in response.data
    assert b"Start MT5 Sync" not in response.data


def test_legacy_approved_request_can_be_completed_via_direct_mt5_submission(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_mt5_setup_queue(monkeypatch)

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
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(
            trade_account,
            account_number="70010001",
            server="Broker-Live",
        ),
        follow_redirects=True,
    )

    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()

    assert response.status_code == 200
    assert request_row.status == MT5AccessRequest.STATUS_PENDING
    assert request_row.reviewed_at is None
    assert request_row.reviewed_by_user_id is None
    assert mt5_account.user_id == user.id
    assert mt5_account.account_number == "70010001"
    assert mt5_account.server == "Broker-Live"
    assert mt5_account.is_active is False
    assert mt5_account.investor_password_encrypted != "investor-pass"
    assert decrypt_password(mt5_account.investor_password_encrypted) == "investor-pass"
    assert mt5_account.mt5_consent_accepted_at is not None
    assert mt5_account.mt5_consent_version == LEGAL_LAST_UPDATED
    assert b"Setup Queued" in response.data
    assert b"MT5 setup started right away. We&#39;ll email you when your sync is ready." in response.data


def test_mt5_request_can_start_without_prior_approval(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_mt5_setup_queue(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-details-no-approval",
        email="mt5-details-no-approval@example.com",
        account_name="No Approval Account",
    )
    _log_in_user(client, user, trade_account)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(
            trade_account,
            account_number="70010002",
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()
    assert request_row.status == MT5AccessRequest.STATUS_PENDING
    assert request_row.request_note is None
    assert mt5_account.account_number == "70010002"
    assert b"MT5 setup started right away. We&#39;ll email you when your sync is ready." in response.data


def test_mt5_request_requires_read_only_consent(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))

    user, trade_account = _create_user_with_account(
        username="mt5-details-no-consent",
        email="mt5-details-no-consent@example.com",
        account_name="No Consent Account",
    )
    _log_in_user(client, user, trade_account)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(
            trade_account,
            mt5_sync_consent="",
        ),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5Account.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
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
    assert b"MT5 Needs Details" in response.data
    assert b"MT5 Setup Queued" in response.data
    assert b"Manage MT5 sync from the dashboard card instead of per-account forms." in response.data
    assert b"Open Dashboard MT5 Access" in response.data
    assert b"Finish the full MT5 sync form from the dashboard card" not in response.data
    assert b"Open the dashboard card to finish the one-step MT5 setup form." in response.data
    assert b"Request MT5 Sync Access" not in response.data


def test_admin_mt5_page_shows_submitted_accounts_without_legacy_request_panels(app_ctx, client):
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
    _root_user, _ = _log_in_root_admin(
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
    db.session.flush()
    db.session.add_all(
        [
            MT5Account(
                user_id=first_user.id,
                trade_account_id=first_account.id,
                account_number="77110001",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Server-One",
                is_active=False,
                mt5_consent_accepted_at=utcnow_naive(),
                mt5_consent_version=LEGAL_LAST_UPDATED,
            ),
            MT5Account(
                user_id=second_user.id,
                trade_account_id=second_account.id,
                account_number="77110002",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Server-Two",
                is_active=False,
                mt5_consent_accepted_at=utcnow_naive(),
                mt5_consent_version=LEGAL_LAST_UPDATED,
            ),
        ]
    )
    db.session.commit()

    list_response = client.get("/dashboard/admin/access/mt5")
    assert list_response.status_code == 200
    assert b"Pending MT5 Requests" not in list_response.data
    assert b"Add MT5 Account" not in list_response.data
    assert b"monitor queued accounts" in list_response.data
    assert b"Approve or reject MT5 requests here." not in list_response.data
    assert b"First Review Account" in list_response.data
    assert b"Second Review Account" in list_response.data
    assert b"77110001" in list_response.data
    assert b"Broker-Server-One" in list_response.data
    assert MT5Account.query.filter_by(trade_account_id=first_account.id).count() == 1
    assert MT5Account.query.filter_by(trade_account_id=second_account.id).count() == 1


def test_admin_mt5_page_shows_requested_setting_up_active_and_inactive_statuses(app_ctx, client):
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-status-root@example.com",
        username="mt5-status-root",
    )

    status_user, requested_account = _create_user_with_account(
        username="mt5-status-user",
        email="mt5-status-user@example.com",
        account_name="Requested Account",
    )
    setting_up_account = TradeAccount(
        user_id=status_user.id,
        name="Setting Up Account",
        account_type="CFD",
        is_default=False,
    )
    active_account = TradeAccount(
        user_id=status_user.id,
        name="Active Account",
        account_type="CFD",
        is_default=False,
    )
    inactive_account = TradeAccount(
        user_id=status_user.id,
        name="Inactive Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add_all([setting_up_account, active_account, inactive_account])
    db.session.commit()

    db.session.add(
        MT5AccessRequest(
            user_id=status_user.id,
            trade_account_id=requested_account.id,
            status=MT5AccessRequest.STATUS_PENDING,
        )
    )
    db.session.add(
        MT5AccessRequest(
            user_id=status_user.id,
            trade_account_id=setting_up_account.id,
            status=MT5AccessRequest.STATUS_APPROVED,
        )
    )
    db.session.add_all(
        [
            MT5Account(
                user_id=status_user.id,
                trade_account_id=requested_account.id,
                account_number="88110001",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Requested",
                is_active=False,
            ),
            MT5Account(
                user_id=status_user.id,
                trade_account_id=setting_up_account.id,
                account_number="88110002",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Setting-Up",
                is_active=False,
            ),
            MT5Account(
                user_id=status_user.id,
                trade_account_id=active_account.id,
                account_number="88110003",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Active",
                terminal_path=r"C:\MT5 User Terminals\active\terminal64.exe",
                appdata_hash="ACTIVEHASH123",
                is_active=True,
            ),
            MT5Account(
                user_id=status_user.id,
                trade_account_id=inactive_account.id,
                account_number="88110004",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Inactive",
                terminal_path=r"C:\MT5 User Terminals\inactive\terminal64.exe",
                appdata_hash="INACTIVEHASH456",
                is_active=False,
            ),
        ]
    )
    db.session.commit()

    response = client.get("/dashboard/admin/access/mt5")

    assert response.status_code == 200
    assert b"Requested Account" in response.data
    assert b"Setting Up Account" in response.data
    assert b"Active Account" in response.data
    assert b"Inactive Account" in response.data
    assert b"requested-chip" in response.data
    assert b"warning-chip" in response.data
    assert b"success-chip" in response.data
    assert b"danger-chip" in response.data
    assert b"Setup Queued" in response.data
    assert b"Setting Up" in response.data
    assert b"Active" in response.data
    assert b"Inactive" in response.data


def test_mt5_submission_claims_open_batch_slot_and_queues_setup(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _create_mt5_batch(name="April Batch", capacity_total=2)
    queued_jobs = []

    user, trade_account = _create_user_with_account(
        username="mt5-batch-user",
        email="mt5-batch-user@example.com",
        account_name="Batch Account",
    )
    _log_in_user(client, user, trade_account)
    monkeypatch.setattr(
        trade_accounts_module,
        "send_email_placeholder",
        lambda *_args, **_kwargs: {"sent": True, "mode": "test"},
    )
    _stub_mt5_setup_queue(monkeypatch, queued_jobs)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(trade_account, account_number="70110001"),
        follow_redirects=True,
    )

    request_row = MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()
    batch = MT5SyncBatch.query.filter_by(name="April Batch").one()

    assert response.status_code == 200
    assert request_row.batch_id == batch.id
    assert batch.total_slots_claimed == 1
    assert mt5_account.account_number == "70110001"
    assert queued_jobs == [{"args": [mt5_account.id], "kwargs": {}, "queue": "mt5_setup"}]
    assert b"MT5 setup started right away. We&#39;ll email you when your sync is ready." in response.data


def test_mt5_submission_blocks_when_open_batch_is_full(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _create_mt5_batch(name="Full Batch", capacity_total=1, total_slots_claimed=1)

    user, trade_account = _create_user_with_account(
        username="mt5-batch-full-user",
        email="mt5-batch-full-user@example.com",
        account_name="Blocked Batch Account",
    )
    _log_in_user(client, user, trade_account)

    response = client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(trade_account, account_number="70110002"),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert MT5Account.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert b"Full Batch is full right now. Start with file import and join the next MT5 sync batch." in response.data


def test_root_admin_can_create_expand_and_close_mt5_sync_batch(app_ctx, client):
    for batch in MT5SyncBatch.query.filter_by(is_open=True).all():
        batch.is_open = False
        batch.closed_at = utcnow_naive()
    db.session.commit()

    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-batch-admin@example.com",
        username="mt5-batch-admin",
    )

    create_response = client.post(
        "/dashboard/admin/access/mt5/batches/create",
        data={"name": "May Batch", "capacity_total": "3", "notes": "First wave"},
        follow_redirects=True,
    )

    batch = MT5SyncBatch.query.filter_by(name="May Batch").one()
    assert create_response.status_code == 200
    assert batch.capacity_total == 3
    assert batch.is_open is True
    assert b"Opened MT5 sync batch &#39;May Batch&#39; with 3 slots." in create_response.data

    add_slots_response = client.post(
        f"/dashboard/admin/access/mt5/batches/{batch.id}/add-slots",
        data={"additional_slots": "2"},
        follow_redirects=True,
    )

    db.session.refresh(batch)
    assert add_slots_response.status_code == 200
    assert batch.capacity_total == 5
    assert b"Added 2 MT5 sync slots to May Batch." in add_slots_response.data

    close_response = client.post(
        f"/dashboard/admin/access/mt5/batches/{batch.id}/close",
        data={},
        follow_redirects=True,
    )

    db.session.refresh(batch)
    assert close_response.status_code == 200
    assert batch.is_open is False
    assert batch.closed_at is not None
    assert b"Closed MT5 sync batch &#39;May Batch&#39;." in close_response.data


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


def test_user_unlink_mt5_clears_requests_and_decrements_batch(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    batch = _create_mt5_batch(name="Unlink Batch", capacity_total=5, total_slots_claimed=0)
    _stub_mt5_setup_queue(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-unlink-user",
        email="mt5-unlink-user@example.com",
    )
    _log_in_user(client, user, trade_account)

    client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(trade_account, account_number="70120001"),
        follow_redirects=True,
    )

    db.session.refresh(batch)
    assert batch.total_slots_claimed == 1
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 1

    cleanup_calls = []

    def _fake_cleanup_apply_async(args=None, kwargs=None, queue=None):
        cleanup_calls.append({"args": list(args or []), "queue": queue})
        return {"id": "cleanup-task"}

    monkeypatch.setattr(
        "celery_workers.mt5_setup_tasks.cleanup_mt5_terminal.apply_async",
        _fake_cleanup_apply_async,
    )

    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account.terminal_path = r"C:\fake\terminal"
    mt5_account.appdata_hash = "abc123hash"
    db.session.commit()

    response = client.post(
        "/dashboard/trade-accounts/mt5/unlink",
        data={"trade_account_pubkey": trade_account.pubkey},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5Account.query.filter_by(trade_account_id=trade_account.id).count() == 0
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
    db.session.refresh(batch)
    assert batch.total_slots_claimed == 0
    assert cleanup_calls == [
        {"args": [r"C:\fake\terminal", "abc123hash"], "queue": "mt5_setup"},
    ]
    assert b"MT5 sync disconnected for this trade account." in response.data


def test_user_unlink_mt5_without_terminal_skips_cleanup_queue(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_mt5_setup_queue(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-unlink-plain",
        email="mt5-unlink-plain@example.com",
    )
    _log_in_user(client, user, trade_account)

    client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(trade_account, account_number="70120002"),
        follow_redirects=True,
    )

    cleanup_calls = []

    def _fake_cleanup_apply_async(args=None, kwargs=None, queue=None):
        cleanup_calls.append(True)
        return {"id": "cleanup-task"}

    monkeypatch.setattr(
        "celery_workers.mt5_setup_tasks.cleanup_mt5_terminal.apply_async",
        _fake_cleanup_apply_async,
    )

    response = client.post(
        "/dashboard/trade-accounts/mt5/unlink",
        data={"trade_account_pubkey": trade_account.pubkey},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert cleanup_calls == []
    assert MT5Account.query.filter_by(trade_account_id=trade_account.id).count() == 0


def test_user_cannot_unlink_mt5_for_foreign_trade_account_pubkey(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_mt5_setup_queue(monkeypatch)

    owner, owner_account = _create_user_with_account(
        username="mt5-unlink-owner",
        email="mt5-unlink-owner@example.com",
        account_name="Owner CFD",
    )
    _log_in_user(client, owner, owner_account)
    client.post(
        "/dashboard/mt5/request-access",
        data=_single_step_mt5_payload(owner_account, account_number="70120003"),
        follow_redirects=True,
    )

    other, other_account = _create_user_with_account(
        username="mt5-unlink-other",
        email="mt5-unlink-other@example.com",
        account_name="Other CFD",
    )
    _log_in_user(client, other, other_account)

    response = client.post(
        "/dashboard/trade-accounts/mt5/unlink",
        data={"trade_account_pubkey": owner_account.pubkey},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert MT5Account.query.filter_by(trade_account_id=owner_account.id).count() == 1
    assert b"Trade account not found." in response.data
