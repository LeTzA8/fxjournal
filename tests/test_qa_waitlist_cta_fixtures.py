"""Tests for waitlist CTA QA fixture CLI (landing 1: safety, login, no side effects)."""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from werkzeug.security import check_password_hash

from cli.qa_fixtures.registry import SCENARIO_REGISTRY, all_scenario_keys
from cli.qa_fixtures.safety import KNOWN_FIXTURE_PASSWORD
from cli.qa_fixtures.scenarios import reset_fixture_users, seed_all_fixture_scenarios, seed_scenario
from extensions import limiter
from models import QaTestAccount, User, db


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _cleanup_fixture_users(app_ctx):
    yield
    db.session.rollback()
    reset_fixture_users()


@pytest.fixture
def cli_runner(app_ctx):
    return CliRunner()


def test_seed_refuses_production_without_allow_flag(cli_runner, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    before_count = QaTestAccount.query.count()
    result = cli_runner.invoke(
        importlib.import_module("cli.qa_fixtures.commands").seed_waitlist_cta_test_data,
        [],
    )
    assert result.exit_code != 0
    assert "--allow-production" in result.output
    assert QaTestAccount.query.count() == before_count


def test_seed_idempotent(cli_runner, app_ctx):
    reset_fixture_users()
    mod = importlib.import_module("cli.qa_fixtures.commands")
    first = cli_runner.invoke(mod.seed_waitlist_cta_test_data, [])
    assert first.exit_code == 0, first.output
    second = cli_runner.invoke(mod.seed_waitlist_cta_test_data, [])
    assert second.exit_code == 0, second.output
    assert User.query.filter(User.email.like("dummy-cta-%@myfxjournal.test")).count() == 6
    assert QaTestAccount.query.count() == 6


def test_reset_rejects_non_dummy_email_targets(app_ctx):
    from cli.qa_fixtures.safety import assert_dummy_email, assert_fixture_user

    user = None
    try:
        user = User(
            username="real-shape-user",
            email="not-a-dummy@example.com",
            password="x",
            email_verified=True,
            signup_status="approved",
        )
        db.session.add(user)
        db.session.flush()
        sidecar = QaTestAccount(
            user_id=user.id,
            scenario_key="rogue_fixture",
            label="Rogue",
            fixture_version="0",
            notes="test",
            last_seeded_at=user.created_at,
            created_at=user.created_at,
            updated_at=user.created_at,
        )
        db.session.add(sidecar)
        db.session.commit()

        with pytest.raises(ValueError, match="does not match"):
            assert_dummy_email(user.email)
        with pytest.raises(ValueError, match="does not match"):
            assert_fixture_user(user)
    finally:
        db.session.rollback()
        if user is not None and user.id is not None:
            persisted_user = db.session.get(User, user.id)
            if persisted_user is not None:
                db.session.delete(persisted_user)
                db.session.commit()


@pytest.mark.parametrize("scenario_key", all_scenario_keys())
def test_each_fixture_user_can_authenticate(client, app_ctx, scenario_key):
    seed_all_fixture_scenarios()
    meta = SCENARIO_REGISTRY[scenario_key]
    response = client.post(
        "/login",
        data={"email": meta["email"], "password": KNOWN_FIXTURE_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/dashboard" in response.headers.get("Location", "")


@pytest.mark.parametrize("scenario_key", all_scenario_keys())
def test_fixture_users_are_email_verified_and_approved(app_ctx, scenario_key):
    seed_all_fixture_scenarios()
    user = User.query.filter_by(email=SCENARIO_REGISTRY[scenario_key]["email"]).one()
    assert user.email_verified is True
    assert user.signup_status == "approved"


@pytest.mark.parametrize("scenario_key", all_scenario_keys())
def test_fixture_users_are_not_admin(app_ctx, scenario_key):
    seed_all_fixture_scenarios()
    user = User.query.filter_by(email=SCENARIO_REGISTRY[scenario_key]["email"]).one()
    assert user.is_admin is False


def test_fixture_password_hash_matches_known_password(app_ctx):
    seed_all_fixture_scenarios()
    user = User.query.filter_by(email=SCENARIO_REGISTRY["zero_data"]["email"]).one()
    assert check_password_hash(user.password, KNOWN_FIXTURE_PASSWORD)


def test_seed_does_not_call_email_helper(app_ctx):
    import auth_account

    with patch.object(auth_account, "send_email_placeholder") as send_mock:
        seed_all_fixture_scenarios()
    assert send_mock.call_count == 0


def test_seed_does_not_publish_celery_tasks(app_ctx):
    celery_mod = importlib.import_module("celery_app")
    with patch.object(celery_mod.celery, "send_task") as send_task_mock:
        seed_all_fixture_scenarios()
    assert send_task_mock.call_count == 0


def test_seed_does_not_call_openai(app_ctx):
    ai_mod = importlib.import_module("ai_service")
    with patch.object(ai_mod, "request_openai_response") as openai_mock:
        seed_all_fixture_scenarios()
    assert openai_mock.call_count == 0
