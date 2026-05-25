import html
import os

from cryptography.fernet import Fernet
import pytest

import routes.dashboard as dashboard_routes
import routes.trade_accounts as trade_accounts_module
from extensions import limiter
from helpers.legal import LEGAL_LAST_UPDATED
from helpers.utils import decrypt_password, encrypt_password, utcnow_naive
from models import MT5AccessRequest, MT5Account, MT5SyncBatch, Trade, TradeAccount, User, db


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
    def _fake_dispatch(task, mt5_account_id, **options):
        if should_raise:
            raise RuntimeError("queue unavailable")
        if captured is not None:
            captured.append(
                {
                    "args": [mt5_account_id],
                    "kwargs": {
                        key: value
                        for key, value in options.items()
                        if key not in {"label", "extra", "log"}
                    },
                    "queue": "mt5_setup",
                }
            )
        return {"id": "test-mt5-setup-task"}

    monkeypatch.setattr("helpers.mt5_dispatch.dispatch_mt5_setup", _fake_dispatch)


def _stub_mt5_cleanup_queue(monkeypatch, captured=None, *, should_raise=False):
    def _fake_dispatch(task, terminal_path, appdata_hash, **options):
        if should_raise:
            raise RuntimeError("queue unavailable")
        if captured is not None:
            captured.append(
                {
                    "args": [terminal_path, appdata_hash],
                    "kwargs": dict(options.get("kwargs") or {}),
                    "account_vm_id": options.get("account_vm_id"),
                    "queue": "mt5_setup",
                }
            )
        return {"id": "test-mt5-cleanup-task"}

    monkeypatch.setattr("helpers.mt5_dispatch.dispatch_mt5_cleanup", _fake_dispatch)


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
    assert queued_jobs[0]["args"] == [mt5_account.id]
    assert queued_jobs[0]["queue"] == "mt5_setup"
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
    assert b"Start MT5 Sync" in response.data or b"data-mt5-setup-wizard" in response.data
    assert b"Requestable CFD" in response.data
    assert (
        b"Connect MT5 below. We handle terminal setup and email you when sync is live." in response.data
        or b"Stored encrypted; used only to pull your trade history." in response.data
    )
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
    assert b"Import a report while setup runs" in response.data
    assert b'id="trade-journal"' not in response.data
    assert b"Your weekly AI review" in response.data or b"Weekly AI Review" in response.data
    assert b"Session Performance" not in response.data


def test_dashboard_home_shows_archived_mt5_state_with_reactivation_prompt(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-archived-dashboard",
        email="mt5-archived-dashboard@example.com",
        account_name="Archived Account",
    )
    _log_in_user(client, user, trade_account)

    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="70018882",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Live",
            is_active=False,
            archived_at=utcnow_naive(),
            archive_reason=MT5Account.ARCHIVE_REASON_INACTIVITY,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-current-stage="4"' in response.data
    assert b"Sync Inactive" in response.data
    assert b"inactive due to inactivity" in response.data
    assert b"Reactivate MT5 sync" in response.data


def test_dashboard_home_shows_paused_mt5_state_not_connected(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-paused-dashboard",
        email="mt5-paused-dashboard@example.com",
        account_name="Paused Account",
    )
    _log_in_user(client, user, trade_account)

    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="70018883",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Live",
            is_active=True,
            sync_paused_at=utcnow_naive(),
            sync_pause_reason="trial_expired",
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b'data-current-stage="4"' in response.data
    assert b"Sync Paused" in response.data
    assert b"MT5 sync is paused for this account. Your trade history is safe." in response.data
    assert b"Connected Account" not in response.data


def test_dashboard_home_shows_grandfathered_mt5_beta_access(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _stub_weekly_ai_state(monkeypatch)

    user, trade_account = _create_user_with_account(
        username="mt5-grandfathered-dashboard",
        email="mt5-grandfathered-dashboard@example.com",
        account_name="Grandfathered Account",
    )
    user.plan_grandfathered = True
    _log_in_user(client, user, trade_account)

    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=trade_account.id,
            account_number="70018884",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Live",
            is_active=True,
        )
    )
    db.session.commit()

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Connected Account" in response.data
    assert b"Beta access: this account is grandfathered for premium workflow features during the beta." in response.data


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
    assert (
        b"Submit Details" in response.data
        or b"data-mt5-setup-wizard" in response.data
    )
    assert (
        b"Complete the form below to resume MT5 setup." in response.data
        or b"Stored encrypted; used only to pull your trade history." in response.data
    )
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
    active_linked_account = TradeAccount(
        user_id=user.id,
        name="Active Linked CFD",
        account_type="CFD",
        is_default=False,
    )
    paused_account = TradeAccount(
        user_id=user.id,
        name="Paused CFD",
        account_type="CFD",
        is_default=False,
    )
    archived_account = TradeAccount(
        user_id=user.id,
        name="Archived CFD",
        account_type="CFD",
        is_default=False,
    )
    failed_account = TradeAccount(
        user_id=user.id,
        name="Failed CFD",
        account_type="CFD",
        is_default=False,
    )
    user.plan_grandfathered = True
    db.session.add_all(
        [
            pending_account,
            approved_account,
            linked_account,
            active_linked_account,
            paused_account,
            archived_account,
            failed_account,
        ]
    )
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
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=active_linked_account.id,
            account_number="99110013",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Active",
            is_active=True,
            last_synced_at=utcnow_naive(),
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=paused_account.id,
            account_number="99110014",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Paused",
            is_active=True,
            sync_paused_at=utcnow_naive(),
            sync_pause_reason="trial_expired",
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=archived_account.id,
            account_number="99110004",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Archived",
            is_active=False,
            archived_at=utcnow_naive(),
            archive_reason=MT5Account.ARCHIVE_REASON_INACTIVITY,
        )
    )
    db.session.add(
        MT5Account(
            user_id=user.id,
            trade_account_id=failed_account.id,
            account_number="99110015",
            investor_password_encrypted=encrypt_password("investor-pass"),
            server="Broker-Failed",
            is_active=False,
            connection_status=MT5Account.CONNECTION_STATUS_FAILED,
            connection_error_message="Invalid investor password.",
        )
    )
    db.session.add(
        Trade(
            user_id=user.id,
            trade_account_id=active_linked_account.id,
            symbol="EURUSD",
            side="BUY",
            entry_price=1.1,
            exit_price=1.11,
            lot_size=0.1,
            pnl=10.0,
        )
    )
    db.session.commit()

    _log_in_user(client, user, requestable_account)

    response = client.get("/dashboard/trade-accounts")

    assert response.status_code == 200
    assert b"Trade accounts" in response.data
    assert b"Needs attention" in response.data
    assert b"Recent activity" in response.data
    assert b"All accounts OK" not in response.data
    assert b"MT5 Needs Details" in response.data
    assert b"MT5 Linked" in response.data
    assert b"MT5 Sync Paused" in response.data
    assert b"MT5 Setup Queued" in response.data
    assert b"MT5 Sync Inactive" in response.data
    assert b"MT5 Connection Failed" in response.data
    assert b"Trades" in response.data
    assert b"AI reviews" in response.data
    assert b"Last sync" in response.data
    assert b"Manage MT5" in response.data
    assert b"Fix on Dashboard" in response.data
    assert b"Invalid investor password." in response.data
    assert b"Manage MT5 sync from the dashboard card instead of per-account forms." not in response.data
    assert b"Open Dashboard MT5 Access" not in response.data
    assert b"Finish the full MT5 sync form from the dashboard card" not in response.data
    assert b"Open the dashboard card to finish the one-step MT5 setup form." not in response.data
    assert b"Beta access:" not in response.data
    assert b"Reactivate MT5 sync" in response.data
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


def test_admin_mt5_page_shows_requested_setting_up_active_inactive_and_archived_statuses(app_ctx, client):
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
    archived_account = TradeAccount(
        user_id=status_user.id,
        name="Archived Account",
        account_type="CFD",
        is_default=False,
    )
    db.session.add_all([setting_up_account, active_account, inactive_account, archived_account])
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
            MT5Account(
                user_id=status_user.id,
                trade_account_id=archived_account.id,
                account_number="88110005",
                investor_password_encrypted=encrypt_password("investor-pass"),
                server="Broker-Archived",
                is_active=False,
                archived_at=utcnow_naive(),
                archive_reason=MT5Account.ARCHIVE_REASON_INACTIVITY,
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
    assert b"Archived Account" in response.data
    assert b"requested-chip" in response.data
    assert b"warning-chip" in response.data
    assert b"success-chip" in response.data
    assert b"danger-chip" in response.data
    assert b"Setup Queued" in response.data
    assert b"Setting Up" in response.data
    assert b"Active" in response.data
    assert b"Inactive" in response.data
    assert b"Archived" in response.data


def test_admin_mt5_delete_actions_render_submit_ready_buttons(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-render-root@example.com",
        username="mt5-delete-render-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-render-user",
        email="mt5-delete-render-user@example.com",
        account_name="Delete Render Target",
    )
    cleanup_trade_account = TradeAccount(
        user_id=user.id,
        name="Cleanup Render Target",
        account_type="CFD",
        is_default=False,
    )
    active_trade_account = TradeAccount(
        user_id=user.id,
        name="Active Render Target",
        account_type="CFD",
        is_default=False,
    )
    db.session.add_all([cleanup_trade_account, active_trade_account])
    db.session.commit()
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70110050",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Render",
        terminal_path=r"C:\MT5 User Terminals\render\terminal64.exe",
        appdata_hash="RENDERHASH123",
        is_active=False,
    )
    cleanup_only = MT5Account(
        user_id=user.id,
        trade_account_id=cleanup_trade_account.id,
        account_number="70110051",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Cleanup",
        is_active=False,
    )
    active_account = MT5Account(
        user_id=user.id,
        trade_account_id=active_trade_account.id,
        account_number="70110052",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Active-Render",
        terminal_path=r"C:\MT5 User Terminals\active-render\terminal64.exe",
        appdata_hash="ACTIVERENDERHASH123",
        is_active=True,
        connection_status=MT5Account.CONNECTION_STATUS_CONNECTED,
    )
    db.session.add_all([mt5_account, cleanup_only, active_account])
    db.session.commit()
    cleanup_only_id = cleanup_only.id
    active_account_id = active_account.id
    cleanup_only.mark_for_cleanup()
    db.session.commit()

    response = client.get("/dashboard/admin/access/mt5")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'return confirm("Delete this account\\u0027s MT5 terminal folder' in html
    assert "return confirm('Delete this account" not in html

    delete_action = f'action="/dashboard/admin/access/mt5/{cleanup_only_id}/delete"'
    start = html.index(delete_action)
    delete_form = html[start : html.index("</form>", start)]
    assert "Cleanup-only records: delete removes the DB row only." in delete_form
    assert "disabled" not in delete_form
    assert "aria-disabled" not in delete_form

    delete_vm_files_action = (
        f'action="/dashboard/admin/access/mt5/{active_account_id}/delete-vm-files"'
    )
    start = html.index(delete_vm_files_action)
    delete_vm_files_form = html[start : html.index("</form>", start)]
    assert "Archive this account before deleting VM files while it is still actively syncing." in delete_vm_files_form
    assert "disabled" not in delete_vm_files_form
    assert "aria-disabled" not in delete_vm_files_form


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
    assert queued_jobs[0]["args"] == [mt5_account.id]
    assert queued_jobs[0]["queue"] == "mt5_setup"
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
    assert b"Import a report now" in response.data
    assert b"Notify me when MT5 setup opens" in response.data
    assert b"MT5 setup capacity is currently closed. Import trades now and connect MT5 when setup capacity opens." in response.data


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


def test_user_can_reactivate_archived_mt5_sync(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    queued_jobs = []
    _stub_mt5_setup_queue(monkeypatch, queued_jobs)

    user, trade_account = _create_user_with_account(
        username="mt5-reactivate-user",
        email="mt5-reactivate-user@example.com",
    )
    _log_in_user(client, user, trade_account)

    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119991",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Archived",
        is_active=False,
        archived_at=utcnow_naive(),
        archive_reason=MT5Account.ARCHIVE_REASON_INACTIVITY,
    )
    db.session.add(mt5_account)
    db.session.commit()

    response = client.post(
        "/dashboard/trade-accounts/mt5/reactivate",
        data={"trade_account_pubkey": trade_account.pubkey},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)

    assert response.status_code == 200
    assert refreshed.archived_at is None
    assert refreshed.archive_reason is None
    assert refreshed.is_active is False
    assert queued_jobs[0]["args"] == [mt5_account.id]
    assert queued_jobs[0]["queue"] == "mt5_setup"
    assert b"MT5 reactivation started. We&#39;ll email you when your sync is ready again." in response.data


def test_root_admin_can_archive_mt5_account_and_keep_reactivation_path(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-archive-root@example.com",
        username="mt5-archive-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-archive-user",
        email="mt5-archive-user@example.com",
        account_name="Archive Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119992",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Archive",
        terminal_path=r"C:\MT5 User Terminals\archive\terminal64.exe",
        appdata_hash="ARCHIVEHASH123",
        is_active=True,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/archive",
        data={},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)

    assert response.status_code == 200
    assert refreshed.is_active is False
    assert refreshed.archived_at is not None
    assert refreshed.archive_reason == MT5Account.ARCHIVE_REASON_INACTIVITY
    assert refreshed.terminal_path == r"C:\MT5 User Terminals\archive\terminal64.exe"
    assert refreshed.appdata_hash == "ARCHIVEHASH123"
    assert cleanup_calls == []
    assert b"Archived MT5 account 70119992." in response.data
    assert b"Archived" in response.data


def test_root_admin_can_delete_vm_files_for_mt5_account_with_terminal_path(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-root@example.com",
        username="mt5-delete-files-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-user",
        email="mt5-delete-files-user@example.com",
        account_name="Delete Files Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119993",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Reset",
        terminal_path=r"C:\MT5 User Terminals\reset\terminal64.exe",
        appdata_hash=None,
        vm_id="MYFXJOURNAL-SG",
        is_active=False,
        connection_status=MT5Account.CONNECTION_STATUS_FAILED,
        connection_error_message="Old setup failed",
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)

    assert response.status_code == 200
    assert refreshed.terminal_path is None
    assert refreshed.appdata_hash is None
    assert refreshed.vm_id == "MYFXJOURNAL-SG"
    assert refreshed.is_active is False
    assert refreshed.cleanup_marked_at is None
    assert refreshed.connection_status == MT5Account.CONNECTION_STATUS_FAILED
    assert refreshed.connection_error_message == "Old setup failed"
    assert cleanup_calls == [
        {
            "args": [r"C:\MT5 User Terminals\reset\terminal64.exe", ""],
            "kwargs": {
                "mt5_account_id": mt5_account.id,
                "delete_account_row": False,
                "clear_cleanup_mark": False,
                "target_vm_id": "MYFXJOURNAL-SG",
            },
            "account_vm_id": "MYFXJOURNAL-SG",
            "queue": "mt5_setup",
        }
    ]
    assert b"VM terminal file cleanup queued" in response.data
    assert b"Wait for cleanup to finish on the VM" in response.data


def test_root_admin_delete_vm_files_rejects_active_account(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-active-root@example.com",
        username="mt5-delete-files-active-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-active-user",
        email="mt5-delete-files-active-user@example.com",
        account_name="Delete Files Active Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119997",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Active",
        terminal_path=r"C:\MT5 User Terminals\active\terminal64.exe",
        vm_id="MYFXJOURNAL-SG",
        is_active=True,
        connection_status=MT5Account.CONNECTION_STATUS_CONNECTED,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert response.status_code == 200
    assert cleanup_calls == []
    assert refreshed.is_active is True
    assert refreshed.terminal_path is not None
    assert b"Archive it first before deleting its terminal files" in response.data


def test_root_admin_can_delete_vm_files_for_active_failed_connection(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-failed-active-root@example.com",
        username="mt5-delete-files-failed-active-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-failed-active-user",
        email="mt5-delete-files-failed-active-user@example.com",
        account_name="Delete Files Failed Active Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119992",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Failed-Active",
        terminal_path=r"C:\MT5 User Terminals\failed-active\terminal64.exe",
        vm_id="MYFXJOURNAL-SG",
        is_active=True,
        connection_status=MT5Account.CONNECTION_STATUS_FAILED,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert cleanup_calls
    assert b"VM terminal file cleanup queued" in response.data


def test_root_admin_can_delete_vm_files_when_account_vm_id_uses_celery_prefix(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "MYFXJOURNAL-SG,VM-OTHER")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-celery-root@example.com",
        username="mt5-delete-files-celery-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-celery-user",
        email="mt5-delete-files-celery-user@example.com",
        account_name="Delete Files Celery Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119995",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Celery",
        terminal_path=r"C:\MT5 User Terminals\celery\terminal64.exe",
        vm_id="mt5-sync@MYFXJOURNAL-SG",
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={"target_vm_id": "MYFXJOURNAL-SG"},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert response.status_code == 200
    assert refreshed.terminal_path is None
    assert cleanup_calls
    assert cleanup_calls[0]["account_vm_id"] == "MYFXJOURNAL-SG"
    assert b"VM terminal file cleanup queued" in response.data


def test_root_admin_delete_vm_files_rejects_cleanup_pending_with_artifacts(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-pending-root@example.com",
        username="mt5-delete-files-pending-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-pending-user",
        email="mt5-delete-files-pending-user@example.com",
        account_name="Delete Files Pending Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119998",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Pending",
        terminal_path=r"C:\MT5 User Terminals\pending\terminal64.exe",
        is_active=False,
        cleanup_marked_at=utcnow_naive(),
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert cleanup_calls == []
    assert b"already waiting on account cleanup" in response.data


def test_root_admin_delete_vm_files_rejects_target_vm_mismatch(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "MYFXJOURNAL-SG,VM-OTHER")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-mismatch-root@example.com",
        username="mt5-delete-files-mismatch-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-mismatch-user",
        email="mt5-delete-files-mismatch-user@example.com",
        account_name="Delete Files Mismatch Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119999",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Mismatch",
        terminal_path=r"C:\MT5 User Terminals\mismatch\terminal64.exe",
        vm_id="MYFXJOURNAL-SG",
        is_active=False,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={"target_vm_id": "VM-OTHER"},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert response.status_code == 200
    assert cleanup_calls == []
    assert refreshed.terminal_path is not None
    assert b"does not match" in response.data


def test_root_admin_can_setup_after_delete_vm_files(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-then-setup-root@example.com",
        username="mt5-delete-then-setup-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-then-setup-user",
        email="mt5-delete-then-setup-user@example.com",
        account_name="Delete Then Setup Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70120000",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Delete-Setup",
        terminal_path=r"C:\MT5 User Terminals\delete-setup\terminal64.exe",
        vm_id="MYFXJOURNAL-SG",
        is_active=False,
        connection_status=MT5Account.CONNECTION_STATUS_FAILED,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)
    setup_calls = []

    def _fake_admin_setup_dispatch(task, mt5_account_id, **options):
        setup_calls.append(
            {
                "args": [mt5_account_id],
                "kwargs": {
                    key: value
                    for key, value in options.items()
                    if key not in {"label", "extra", "log"}
                },
            }
        )
        return type("Result", (), {"id": "test-setup-task"})()

    monkeypatch.setattr("auth_account.dispatch_mt5_setup", _fake_admin_setup_dispatch)

    delete_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert cleanup_calls

    setup_response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/setup",
        data={},
        follow_redirects=True,
    )

    assert setup_response.status_code == 200
    assert setup_calls
    assert b"MT5 terminal setup queued" in setup_response.data


def test_root_admin_can_delete_vm_files_for_mt5_account_with_only_appdata_hash(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-hash-root@example.com",
        username="mt5-delete-files-hash-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-hash-user",
        email="mt5-delete-files-hash-user@example.com",
        account_name="Delete Files Hash Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119994",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Reset-Hash",
        terminal_path=None,
        appdata_hash="A" * 32,
        is_active=False,
        connection_status=MT5Account.CONNECTION_STATUS_FAILED,
        connection_error_message="Old setup failed",
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)

    assert response.status_code == 200
    assert refreshed.terminal_path is None
    assert refreshed.appdata_hash is None
    assert refreshed.is_active is False
    assert refreshed.cleanup_marked_at is None
    assert refreshed.connection_status == MT5Account.CONNECTION_STATUS_FAILED
    assert cleanup_calls == [
        {
            "args": ["", "A" * 32],
            "kwargs": {
                "mt5_account_id": mt5_account.id,
                "delete_account_row": False,
                "clear_cleanup_mark": False,
            },
            "account_vm_id": "",
            "queue": "mt5_setup",
        }
    ]
    assert b"VM terminal file cleanup queued" in response.data


def test_root_admin_cannot_setup_mt5_account_while_account_cleanup_is_pending(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-reset-pending-root@example.com",
        username="mt5-reset-pending-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-reset-pending-user",
        email="mt5-reset-pending-user@example.com",
        account_name="Reset Pending Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119995",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Reset-Pending",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
        cleanup_marked_at=utcnow_naive(),
        connection_status=MT5Account.CONNECTION_STATUS_PENDING,
    )
    db.session.add(mt5_account)
    db.session.commit()

    setup_calls = []

    def _fake_setup_apply_async(args=None, kwargs=None, queue=None):
        setup_calls.append({"args": list(args or []), "kwargs": dict(kwargs or {}), "queue": queue})
        return {"id": "setup-task"}

    monkeypatch.setattr(
        "celery_workers.mt5_setup_tasks.setup_mt5_terminal.apply_async",
        _fake_setup_apply_async,
    )

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/setup",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert setup_calls == []
    assert (
        b"That MT5 account is still waiting for VM cleanup to finish. Try Setup Terminal again after cleanup completes."
        in response.data
    )


def test_root_admin_delete_vm_files_noop_when_no_artifacts(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-empty-root@example.com",
        username="mt5-delete-files-empty-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-empty-user",
        email="mt5-delete-files-empty-user@example.com",
        account_name="Delete Files Empty Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70119996",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Empty",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
        connection_status=MT5Account.CONNECTION_STATUS_PENDING,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert cleanup_calls == []
    assert b"nothing to delete on the VM" in response.data


def test_root_admin_delete_vm_files_rejects_cleanup_pending_without_artifacts(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-files-pending-empty-root@example.com",
        username="mt5-delete-files-pending-empty-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-files-pending-empty-user",
        email="mt5-delete-files-pending-empty-user@example.com",
        account_name="Delete Files Pending Empty Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70120001",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Pending-Empty",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
        cleanup_marked_at=utcnow_naive(),
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete-vm-files",
        data={},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert cleanup_calls == []
    assert b"already waiting on account cleanup" in response.data


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
        cleanup_calls.append({"args": list(args or []), "kwargs": dict(kwargs or {}), "queue": queue})
        return {"id": "cleanup-task"}

    monkeypatch.setattr(
        "celery_workers.mt5_setup_tasks.cleanup_mt5_terminal.apply_async",
        _fake_cleanup_apply_async,
    )

    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account_id = mt5_account.id
    mt5_account.terminal_path = r"C:\fake\terminal"
    mt5_account.appdata_hash = "abc123hash"
    db.session.commit()

    response = client.post(
        "/dashboard/trade-accounts/mt5/unlink",
        data={"trade_account_pubkey": trade_account.pubkey},
        follow_redirects=True,
    )

    assert response.status_code == 200
    # Row should still exist as cleanup-only orphan (not deleted yet).
    assert MT5Account.query.filter_by(trade_account_id=trade_account.id).count() == 0
    orphan = db.session.get(MT5Account, mt5_account_id)
    assert orphan is not None
    assert orphan.is_orphaned
    assert orphan.cleanup_marked_at is not None
    assert MT5AccessRequest.query.filter_by(trade_account_id=trade_account.id).count() == 0
    db.session.refresh(batch)
    assert batch.total_slots_claimed == 0
    assert cleanup_calls == [
        {
            "args": [r"C:\fake\terminal", "abc123hash"],
            "kwargs": {
                "mt5_account_id": mt5_account_id,
                "delete_account_row": True,
                "clear_cleanup_mark": False,
            },
            "queue": "mt5_setup",
        },
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

    mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).one()
    mt5_account_id = mt5_account.id

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
    # Row should still exist as cleanup-only orphan.
    orphan = db.session.get(MT5Account, mt5_account_id)
    assert orphan is not None
    assert orphan.is_orphaned
    assert orphan.cleanup_marked_at is not None


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


def test_root_admin_delete_mt5_fails_when_cleanup_cannot_queue(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-skip-root@example.com",
        username="mt5-delete-skip-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-skip-user",
        email="mt5-delete-skip-user@example.com",
        account_name="Delete Skip Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70129991",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Delete",
        terminal_path=r"C:\MT5 User Terminals\delete\terminal64.exe",
        appdata_hash="DELETEHASH123",
        is_active=True,
        vm_id=None,
    )
    db.session.add(mt5_account)
    db.session.commit()
    mt5_account_id = mt5_account.id

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account_id}/delete",
        data={},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account_id)
    assert response.status_code == 200
    assert refreshed is not None
    assert refreshed.user_id == user.id
    assert b"could not be queued" in response.data


def test_root_admin_delete_mt5_without_vm_files_does_not_require_target_vm(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-no-files-root@example.com",
        username="mt5-delete-no-files-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-no-files-user",
        email="mt5-delete-no-files-user@example.com",
        account_name="Delete No Files Target",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70129990",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Delete-No-Files",
        terminal_path=None,
        appdata_hash=None,
        is_active=False,
        vm_id=None,
    )
    db.session.add(mt5_account)
    db.session.commit()
    mt5_account_id = mt5_account.id

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account_id}/delete",
        data={},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account_id)
    assert response.status_code == 200
    assert refreshed is not None
    assert refreshed.is_cleanup_only is True
    assert b"marked for cleanup" in response.data
    assert b"Choose a target VM" not in response.data


def test_root_admin_delete_mt5_passes_target_vm_id(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FXJ_MT5_MULTI_VM", "1")
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-TARGET,VM-OTHER")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-delete-target-root@example.com",
        username="mt5-delete-target-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-delete-target-user",
        email="mt5-delete-target-user@example.com",
        account_name="Delete Target VM",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70129992",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Delete",
        terminal_path=r"C:\MT5 User Terminals\delete2\terminal64.exe",
        appdata_hash="DELETEHASH456",
        is_active=True,
        vm_id=None,
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        f"/dashboard/admin/access/mt5/{mt5_account.id}/delete",
        data={"target_vm_id": "VM-TARGET"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert cleanup_calls
    assert cleanup_calls[0]["account_vm_id"] == "VM-TARGET"
    assert b"marked for cleanup" in response.data


def test_root_admin_vm_delete_files_clears_runtime_fields(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-BULK")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-bulk-root@example.com",
        username="mt5-bulk-root",
    )

    user, trade_account = _create_user_with_account(
        username="mt5-bulk-user",
        email="mt5-bulk-user@example.com",
        account_name="Bulk Cleanup",
    )
    mt5_account = MT5Account(
        user_id=user.id,
        trade_account_id=trade_account.id,
        account_number="70129993",
        investor_password_encrypted=encrypt_password("investor-pass"),
        server="Broker-Bulk",
        terminal_path=r"C:\MT5 User Terminals\bulk\terminal64.exe",
        appdata_hash="BULKHASH123",
        is_active=False,
        vm_id="VM-BULK",
    )
    db.session.add(mt5_account)
    db.session.commit()

    cleanup_calls = []
    _stub_mt5_cleanup_queue(monkeypatch, cleanup_calls)

    response = client.post(
        "/dashboard/admin/access/mt5/vm-delete-files",
        data={"vm_id": "VM-BULK"},
        follow_redirects=True,
    )

    refreshed = db.session.get(MT5Account, mt5_account.id)
    assert response.status_code == 200
    assert cleanup_calls
    assert refreshed.terminal_path is None
    assert refreshed.appdata_hash is None
    assert refreshed.is_active is False
    assert b"cleared their terminal runtime fields" in response.data


def test_root_admin_vm_delete_files_rejects_unknown_vm(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8"))
    monkeypatch.setenv("FXJ_MT5_SETUP_VM_IDS", "VM-BULK")
    _root_user, _ = _log_in_root_admin(
        client,
        email="mt5-bulk-invalid-root@example.com",
        username="mt5-bulk-invalid-root",
    )

    response = client.post(
        "/dashboard/admin/access/mt5/vm-delete-files",
        data={"vm_id": "VM-NOT-REAL"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert b"Unknown target VM" in response.data
