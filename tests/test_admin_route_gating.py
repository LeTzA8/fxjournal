from itertools import count

import pytest

from models import User, db


ALL_ADMIN_ROUTES = [
    ("get", "/dashboard/admin/access"),
    ("get", "/dashboard/admin/access/users"),
    ("get", "/dashboard/admin/access/codes"),
    ("get", "/dashboard/admin/access/mt5"),
    ("get", "/dashboard/admin/access/weekly-report"),
    ("get", "/dashboard/admin/access/weekly-report/1?trade_account_id=1"),
    ("post", "/dashboard/admin/access/users/1/regenerate-ai-advice"),
    ("post", "/dashboard/admin/access/users/1/backfill-bundles"),
    ("post", "/dashboard/admin/access/users/1/unbundle-trades"),
    ("post", "/dashboard/admin/access/users/1/approve"),
    ("post", "/dashboard/admin/access/users/1/reject"),
    ("post", "/dashboard/admin/access/users/1/suspend"),
    ("post", "/dashboard/admin/access/users/1/admin-toggle"),
    ("post", "/dashboard/admin/access/mt5/create"),
    ("post", "/dashboard/admin/access/mt5/batches/create"),
    ("post", "/dashboard/admin/access/mt5/batches/1/add-slots"),
    ("post", "/dashboard/admin/access/mt5/batches/1/close"),
    ("post", "/dashboard/admin/access/mt5/1/setup"),
    ("post", "/dashboard/admin/access/mt5/1/sync"),
    ("post", "/dashboard/admin/access/mt5/recalibrate-trade-times"),
    ("post", "/dashboard/admin/access/mt5/clear-all-trade-bars"),
    ("post", "/dashboard/admin/access/mt5/1/recalibrate-trade-times"),
    ("post", "/dashboard/admin/access/mt5/requests/1/approve"),
    ("post", "/dashboard/admin/access/mt5/requests/1/reject"),
    ("post", "/dashboard/admin/access/mt5/1/delete"),
    ("post", "/dashboard/admin/access/codes/create"),
    ("post", "/dashboard/admin/access/codes/1/toggle"),
]

ROOT_ONLY_ADMIN_ROUTES = [
    ("get", "/dashboard/admin/access/mt5"),
    ("get", "/dashboard/admin/access/weekly-report"),
    ("get", "/dashboard/admin/access/weekly-report/1?trade_account_id=1"),
    ("post", "/dashboard/admin/access/users/1/regenerate-ai-advice"),
    ("post", "/dashboard/admin/access/users/1/backfill-bundles"),
    ("post", "/dashboard/admin/access/users/1/unbundle-trades"),
    ("post", "/dashboard/admin/access/users/1/approve"),
    ("post", "/dashboard/admin/access/users/1/reject"),
    ("post", "/dashboard/admin/access/users/1/suspend"),
    ("post", "/dashboard/admin/access/users/1/admin-toggle"),
    ("post", "/dashboard/admin/access/mt5/create"),
    ("post", "/dashboard/admin/access/mt5/batches/create"),
    ("post", "/dashboard/admin/access/mt5/batches/1/add-slots"),
    ("post", "/dashboard/admin/access/mt5/batches/1/close"),
    ("post", "/dashboard/admin/access/mt5/1/setup"),
    ("post", "/dashboard/admin/access/mt5/1/sync"),
    ("post", "/dashboard/admin/access/mt5/recalibrate-trade-times"),
    ("post", "/dashboard/admin/access/mt5/clear-all-trade-bars"),
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
