"""Admin QA test accounts page tests (landing 3)."""

from __future__ import annotations

from itertools import count

import pytest

from cli.qa_fixtures.registry import SCENARIO_REGISTRY, all_scenario_keys
from cli.qa_fixtures.scenarios import reset_fixture_users, seed_all_fixture_scenarios, seed_scenario
from models import QaTestAccount, User, db


_ADMIN_COUNTER = count(1)


def _create_admin(client):
    suffix = next(_ADMIN_COUNTER)
    admin = User(
        username=f"qa-admin-{suffix}",
        email=f"qa-admin-{suffix}@example.com",
        password="hashed",
        email_verified=True,
        signup_status="approved",
        is_admin=True,
    )
    db.session.add(admin)
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = admin.id
        session_state["username"] = admin.username
    return admin


def _create_root_admin(client, monkeypatch):
    suffix = next(_ADMIN_COUNTER)
    email = f"qa-root-admin-{suffix}@example.com"
    monkeypatch.setenv("ADMIN_USER_EMAILS", email)
    admin = User(
        username=f"qa-root-admin-{suffix}",
        email=email,
        password="hashed",
        email_verified=True,
        signup_status="approved",
        is_admin=True,
    )
    db.session.add(admin)
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = admin.id
        session_state["username"] = admin.username
    return admin


def _seed_all_fixtures():
    seed_all_fixture_scenarios()


def test_test_accounts_page_requires_admin(app_ctx, client):
    user = User(
        username="plain-qa-user",
        email="plain-qa-user@example.com",
        password="hashed",
        email_verified=True,
        signup_status="approved",
        is_admin=False,
    )
    db.session.add(user)
    db.session.commit()
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username

    response = client.get("/dashboard/admin/access/test-accounts")
    assert response.status_code == 404


def test_test_accounts_page_lists_all_six_fixtures(app_ctx, client):
    _seed_all_fixtures()
    _create_admin(client)

    response = client.get("/dashboard/admin/access/test-accounts")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    for key in all_scenario_keys():
        assert SCENARIO_REGISTRY[key]["label"] in html
    assert "dummy-cta-" in html
    assert "@myfxjournal.test" in html


def test_test_accounts_page_renders_expected_cta_json(app_ctx, client):
    _seed_all_fixtures()
    _create_admin(client)

    response = client.get("/dashboard/admin/access/test-accounts")
    assert response.status_code == 200
    html = response.data.decode("utf-8")
    assert "feature_interest" in html
    assert "advanced_replay" in html
    assert "No CTA expected" in html


def test_test_accounts_reseed_rejects_non_fixture_email(app_ctx, client, monkeypatch):
    reset_fixture_users()
    seed_scenario("zero_data")
    db.session.commit()
    _create_root_admin(client, monkeypatch)
    sidecar = QaTestAccount.query.filter_by(scenario_key="zero_data").one()
    sidecar.user.email = "tampered-not-dummy@example.com"
    db.session.commit()

    response = client.post(
        f"/dashboard/admin/access/test-accounts/{sidecar.id}/re-seed",
        follow_redirects=False,
    )
    assert response.status_code == 400
    db.session.rollback()
    db.session.delete(sidecar.user)
    db.session.delete(sidecar)
    db.session.commit()


def test_test_accounts_delete_scoped_to_fixture(app_ctx, client, monkeypatch):
    seed_all_fixture_scenarios()
    _create_root_admin(client, monkeypatch)

    bystander = User(
        username="bystander-user",
        email="bystander-user@example.com",
        password="hashed",
        email_verified=True,
        signup_status="approved",
    )
    db.session.add(bystander)
    db.session.commit()
    bystander_id = bystander.id

    sidecar = QaTestAccount.query.filter_by(scenario_key="replay_lock").one()
    response = client.post(
        f"/dashboard/admin/access/test-accounts/{sidecar.id}/delete",
        follow_redirects=False,
    )
    assert response.status_code in {302, 303}

    assert User.query.filter_by(email=SCENARIO_REGISTRY["replay_lock"]["email"]).first() is None
    assert db.session.get(User, bystander_id) is not None
    assert QaTestAccount.query.filter_by(scenario_key="replay_lock").first() is None
    assert QaTestAccount.query.filter(
        QaTestAccount.scenario_key.in_(all_scenario_keys())
    ).count() == 5
