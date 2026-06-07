import csv
import re
from datetime import datetime
from io import StringIO
from itertools import count

import pytest

import app as app_module
from helpers.core import SUPPORT_VIEW_ADMIN_USER_SESSION_KEY, SUPPORT_VIEW_TARGET_USER_SESSION_KEY
from models import UpgradeWaitlistEntry, User, db
from helpers.app_settings import (
    MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY,
    MT5_BROKER_DISCOVERY_REFRESH_ENABLED_KEY,
    get_bool_app_setting,
)


ALL_ADMIN_ROUTES = [
    ("get", "/dashboard/admin/access"),
    ("get", "/dashboard/admin/access/users"),
    ("get", "/dashboard/admin/access/users/export"),
    ("get", "/dashboard/admin/access/waitlist"),
    ("get", "/dashboard/admin/access/waitlist/export"),
    ("get", "/dashboard/admin/access/test-accounts"),
    ("post", "/dashboard/admin/access/test-accounts/1/notes"),
    ("post", "/dashboard/admin/access/test-accounts/1/re-seed"),
    ("post", "/dashboard/admin/access/test-accounts/1/delete"),
    ("get", "/dashboard/admin/access/codes"),
    ("get", "/dashboard/admin/access/send-email"),
    ("get", "/dashboard/admin/access/send-email/recipients"),
    ("post", "/dashboard/admin/access/send-email/send-one"),
    ("get", "/dashboard/admin/access/send-email/signatures"),
    ("post", "/dashboard/admin/access/send-email/signatures/save"),
    ("post", "/dashboard/admin/access/send-email/signatures/delete"),
    ("get", "/dashboard/admin/access/mt5"),
    ("get", "/dashboard/admin/access/cfd-symbols"),
    ("post", "/dashboard/admin/access/cfd-symbols/1/aliases"),
    ("get", "/dashboard/admin/access/weekly-report"),
    ("get", "/dashboard/admin/access/weekly-report/1?trade_account_id=1"),
    ("get", "/dashboard/admin/users/1/view-dashboard"),
    ("get", "/dashboard/admin/support-view/exit"),
    ("post", "/dashboard/admin/access/users/1/regenerate-ai-advice"),
    ("post", "/dashboard/admin/access/users/1/backfill-bundles"),
    ("post", "/dashboard/admin/access/users/1/unbundle-trades"),
    ("post", "/dashboard/admin/access/users/1/approve"),
    ("post", "/dashboard/admin/access/users/1/reject"),
    ("post", "/dashboard/admin/access/users/1/suspend"),
    ("post", "/dashboard/admin/access/users/1/delete"),
    ("post", "/dashboard/admin/access/users/1/extend-trial"),
    ("post", "/dashboard/admin/access/users/1/admin-toggle"),
    ("post", "/dashboard/admin/access/mt5/create"),
    ("post", "/dashboard/admin/access/mt5/batches/create"),
    ("post", "/dashboard/admin/access/mt5/batches/1/add-slots"),
    ("post", "/dashboard/admin/access/mt5/batches/1/close"),
    ("post", "/dashboard/admin/access/mt5/1/setup"),
    ("post", "/dashboard/admin/access/mt5/1/sync"),
    ("post", "/dashboard/admin/access/mt5/recalibrate-trade-times"),
    ("post", "/dashboard/admin/access/mt5/clear-all-trade-bars"),
    ("post", "/dashboard/admin/access/mt5/auto-bar-sync"),
    ("post", "/dashboard/admin/access/mt5/1/recalibrate-trade-times"),
    ("post", "/dashboard/admin/access/mt5/requests/1/approve"),
    ("post", "/dashboard/admin/access/mt5/requests/1/reject"),
    ("post", "/dashboard/admin/access/mt5/1/delete"),
    ("post", "/dashboard/admin/access/mt5/1/delete-vm-files"),
    ("post", "/dashboard/admin/access/mt5/1/archive"),
    ("post", "/dashboard/admin/access/mt5/1/reactivate"),
    ("post", "/dashboard/admin/access/mt5/broker-discovery-refresh"),
    ("post", "/dashboard/admin/access/mt5/broker-discovery-refresh/feature"),
    ("get", "/dashboard/admin/access/mt5/broker-discovery-refresh/test-job"),
    ("post", "/dashboard/admin/access/mt5/vm-delete-files"),
    ("post", "/dashboard/admin/access/codes/create"),
    ("post", "/dashboard/admin/access/codes/1/toggle"),
    ("get", "/admin/journal"),
    ("post", "/admin/journal/sessions"),
    ("get", "/admin/journal/sessions/1"),
    ("post", "/admin/journal/sessions/1/chat"),
    ("post", "/admin/journal/sessions/1/tags"),
    ("post", "/admin/journal/messages/1/feedback"),
    ("post", "/admin/journal/sessions/1/end"),
]

ROOT_ONLY_ADMIN_ROUTES = [
    ("post", "/dashboard/admin/access/test-accounts/1/re-seed"),
    ("post", "/dashboard/admin/access/test-accounts/1/delete"),
    ("get", "/dashboard/admin/access/mt5"),
    ("get", "/dashboard/admin/access/cfd-symbols"),
    ("post", "/dashboard/admin/access/cfd-symbols/1/aliases"),
    ("get", "/dashboard/admin/access/weekly-report"),
    ("get", "/dashboard/admin/access/weekly-report/1?trade_account_id=1"),
    ("get", "/dashboard/admin/users/1/view-dashboard"),
    ("get", "/dashboard/admin/support-view/exit"),
    ("post", "/dashboard/admin/access/users/1/regenerate-ai-advice"),
    ("post", "/dashboard/admin/access/users/1/backfill-bundles"),
    ("post", "/dashboard/admin/access/users/1/unbundle-trades"),
    ("post", "/dashboard/admin/access/users/1/approve"),
    ("post", "/dashboard/admin/access/users/1/reject"),
    ("post", "/dashboard/admin/access/users/1/suspend"),
    ("post", "/dashboard/admin/access/users/1/delete"),
    ("post", "/dashboard/admin/access/users/1/extend-trial"),
    ("post", "/dashboard/admin/access/users/1/admin-toggle"),
    ("post", "/dashboard/admin/access/mt5/create"),
    ("post", "/dashboard/admin/access/mt5/batches/create"),
    ("post", "/dashboard/admin/access/mt5/batches/1/add-slots"),
    ("post", "/dashboard/admin/access/mt5/batches/1/close"),
    ("post", "/dashboard/admin/access/mt5/1/setup"),
    ("post", "/dashboard/admin/access/mt5/1/sync"),
    ("post", "/dashboard/admin/access/mt5/recalibrate-trade-times"),
    ("post", "/dashboard/admin/access/mt5/clear-all-trade-bars"),
    ("post", "/dashboard/admin/access/mt5/auto-bar-sync"),
    ("post", "/dashboard/admin/access/mt5/1/recalibrate-trade-times"),
    ("post", "/dashboard/admin/access/mt5/requests/1/approve"),
    ("post", "/dashboard/admin/access/mt5/requests/1/reject"),
    ("post", "/dashboard/admin/access/mt5/1/delete"),
    ("post", "/dashboard/admin/access/mt5/1/delete-vm-files"),
    ("post", "/dashboard/admin/access/mt5/1/archive"),
    ("post", "/dashboard/admin/access/mt5/1/reactivate"),
    ("post", "/dashboard/admin/access/mt5/broker-discovery-refresh"),
    ("post", "/dashboard/admin/access/mt5/broker-discovery-refresh/feature"),
    ("get", "/dashboard/admin/access/mt5/broker-discovery-refresh/test-job"),
    ("post", "/dashboard/admin/access/mt5/vm-delete-files"),
    ("post", "/dashboard/admin/access/codes/create"),
    ("post", "/dashboard/admin/access/codes/1/toggle"),
]


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
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login_as(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


@pytest.mark.parametrize(("method", "path"), ALL_ADMIN_ROUTES)
def test_admin_routes_return_404_for_anonymous_visitors(app_ctx, client, method, path):
    response = getattr(client, method)(path, follow_redirects=False)

    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path"), ALL_ADMIN_ROUTES)
def test_admin_routes_return_404_for_logged_in_non_admins(app_ctx, client, monkeypatch, method, path):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root-gating@example.com")
    suffix = _unique_suffix()
    user = _create_user(
        username=f"plain-gating-user-{suffix}",
        email=f"plain-gating-{suffix}@example.com",
    )
    db.session.commit()

    _login_as(client, user)

    response = getattr(client, method)(path, follow_redirects=False)

    assert response.status_code == 404


@pytest.mark.parametrize(("method", "path"), ROOT_ONLY_ADMIN_ROUTES)
def test_root_only_admin_routes_return_404_for_db_admins(app_ctx, client, monkeypatch, method, path):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root-only-gating@example.com")
    suffix = _unique_suffix()
    user = _create_user(
        username=f"db-admin-gating-user-{suffix}",
        email=f"db-admin-gating-{suffix}@example.com",
        is_admin=True,
    )
    db.session.commit()

    _login_as(client, user)

    response = getattr(client, method)(path, follow_redirects=False)

    assert response.status_code == 404


def test_admin_users_list_accepts_sort_query(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-users-sort@example.com")
    admin = _create_user(
        username="admin-users-sort",
        email="admin-users-sort@example.com",
        is_admin=True,
    )
    db.session.commit()
    _login_as(client, admin)
    response = client.get("/dashboard/admin/access/users?sort=created_desc")
    assert response.status_code == 200
    assert b"Sort" in response.data
    assert b"Created: newest first" in response.data


def test_admin_users_list_accepts_last_active_sort(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-users-active-sort@example.com")
    admin = _create_user(
        username="admin-users-active-sort",
        email="admin-users-active-sort@example.com",
        is_admin=True,
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/users?sort=active_desc")

    assert response.status_code == 200
    assert b"Last active: recent first" in response.data
    assert b"Last active:" in response.data


def test_support_view_request_does_not_stamp_last_active_at(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "support-active-admin@example.com")
    admin = _create_user(
        username="support-active-admin",
        email="support-active-admin@example.com",
        is_admin=True,
    )
    target = _create_user(
        username="support-active-target",
        email="support-active-target@example.com",
    )
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = admin.id
        session_state["username"] = admin.username
        session_state[SUPPORT_VIEW_ADMIN_USER_SESSION_KEY] = admin.id
        session_state[SUPPORT_VIEW_TARGET_USER_SESSION_KEY] = target.id

    monkeypatch.setattr(app_module, "utcnow_naive", lambda: datetime(2026, 5, 24, 8, 0, 0))

    response = client.get("/dashboard", follow_redirects=False)

    db.session.refresh(admin)
    db.session.refresh(target)
    assert response.status_code in {200, 302}
    assert admin.last_active_at is None
    assert target.last_active_at is None


def test_admin_users_list_shows_distinct_waitlist_people_count(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-waitlist-count@example.com")
    admin = _create_user(
        username="admin-waitlist-count",
        email="admin-waitlist-count@example.com",
        is_admin=True,
    )
    db.session.add_all(
        [
            UpgradeWaitlistEntry(
                email="one@example.com",
                tier_intent="trader",
                source="pricing_page",
                feature_interest="advanced_replay",
            ),
            UpgradeWaitlistEntry(
                email="two@example.com",
                tier_intent="pro",
                source="pricing_page",
                feature_interest="advanced_replay",
            ),
            UpgradeWaitlistEntry(
                email="one@example.com",
                tier_intent="pro",
                source="pricing_page",
                feature_interest="mt5_sync",
            ),
        ]
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/users")

    assert response.status_code == 200
    assert b"Waitlist" in response.data
    assert b"/dashboard/admin/access/waitlist" in response.data
    assert re.search(
        rb'admin-stat-label">Waitlist</span>\s*<span class="admin-stat-value">\d+</span>',
        response.data,
    )


def test_admin_mt5_list_accepts_sort_query(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root-mt5-sort@example.com")
    root = _create_user(
        username="root-mt5-sort",
        email="root-mt5-sort@example.com",
        is_admin=True,
    )
    db.session.commit()
    _login_as(client, root)
    response = client.get("/dashboard/admin/access/mt5?sort=sync_asc")
    assert response.status_code == 200
    assert b"Sort accounts" in response.data


def test_root_admin_can_toggle_mt5_auto_bar_sync(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root-auto-bars@example.com")
    root = _create_user(
        username="root-auto-bars",
        email="root-auto-bars@example.com",
        is_admin=True,
    )
    db.session.commit()
    _login_as(client, root)

    response = client.post(
        "/dashboard/admin/access/mt5/auto-bar-sync",
        data={"enabled": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert get_bool_app_setting(MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY, False) is True


def test_root_admin_can_toggle_mt5_broker_discovery_refresh_feature(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "root-broker-refresh@example.com")
    root = _create_user(
        username="root-broker-refresh",
        email="root-broker-refresh@example.com",
        is_admin=True,
    )
    db.session.commit()
    _login_as(client, root)

    response = client.post(
        "/dashboard/admin/access/mt5/broker-discovery-refresh/feature",
        data={"enabled": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert get_bool_app_setting(MT5_BROKER_DISCOVERY_REFRESH_ENABLED_KEY, False) is True


def test_admin_users_export_returns_csv_for_all_users(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-users-export@example.com")
    admin = _create_user(
        username="admin-users-export",
        email="admin-users-export@example.com",
        is_admin=True,
    )
    first = _create_user(
        username="export-alpha",
        email="export-alpha@example.com",
    )
    second = _create_user(
        username="export-beta",
        email="export-beta@example.com",
    )
    first.created_at = datetime(2026, 1, 10, 12, 0, 0)
    first.last_login_at = datetime(2026, 2, 1, 8, 30, 0)
    first.last_active_at = datetime(2026, 2, 5, 14, 15, 0)
    second.created_at = datetime(2026, 1, 15, 9, 0, 0)
    second.last_login_at = None
    second.last_active_at = None
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/users/export")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers.get("Content-Disposition", "")
    assert "myfxjournal-users-" in response.headers.get("Content-Disposition", "")

    rows = list(csv.reader(StringIO(response.get_data(as_text=True))))
    assert rows[0] == [
        "name",
        "email",
        "signup date",
        "last login date",
        "last active date",
    ]
    exported = {row[1]: row for row in rows[1:]}
    assert exported["export-alpha@example.com"] == [
        "export-alpha",
        "export-alpha@example.com",
        "2026-01-10 12:00 UTC",
        "2026-02-01 08:30 UTC",
        "2026-02-05 14:15 UTC",
    ]
    assert exported["export-beta@example.com"] == [
        "export-beta",
        "export-beta@example.com",
        "2026-01-15 09:00 UTC",
        "-",
        "-",
    ]
    assert "admin-users-export@example.com" in exported


def test_admin_users_page_includes_export_link(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "admin-users-export-link@example.com")
    admin = _create_user(
        username="admin-users-export-link",
        email="admin-users-export-link@example.com",
        is_admin=True,
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/users")

    assert response.status_code == 200
    assert b"/dashboard/admin/access/users/export" in response.data
    assert b"Export all users (CSV)" in response.data
