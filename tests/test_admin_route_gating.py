from itertools import count

import pytest

from models import User, db
from helpers.app_settings import MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY, get_bool_app_setting


ALL_ADMIN_ROUTES = [
    ("get", "/dashboard/admin/access"),
    ("get", "/dashboard/admin/access/users"),
    ("get", "/dashboard/admin/access/codes"),
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
    ("post", "/dashboard/admin/access/codes/create"),
    ("post", "/dashboard/admin/access/codes/1/toggle"),
]

ROOT_ONLY_ADMIN_ROUTES = [
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
