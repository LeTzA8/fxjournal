import auth_account
from werkzeug.security import generate_password_hash

import auth_account
from auth_account import (
    generate_auth_token,
    generate_email_change_token,
    generate_password_reset_token,
    rotate_password_reset_nonce,
    verify_auth_token,
    verify_email_change_token,
    verify_password_reset_token,
)
from extensions import limiter
from models import User, db


class _FakeOAuth:
    def __init__(self, client=None):
        self._client = client if client is not None else object()

    def create_client(self, name):
        return self._client

    def register(self, *args, **kwargs):
        return None


class _FakeGoogleClient:
    def __init__(self, *, token=None):
        self._token = token or {}

    def authorize_redirect(self, redirect_uri, **kwargs):
        raise AssertionError("authorize_redirect should not run in this test")

    def authorize_access_token(self):
        return self._token


def _enable_google_auth(monkeypatch, client=None):
    monkeypatch.setattr(auth_account, "oauth", _FakeOAuth(client))


def test_generate_and_verify_auth_token(app_ctx):
    """
    Fixed input:
      Email: trader@example.com
      Purpose: verify_email

    Expected result:
      Token verification returns the normalized email address.
    """
    token = generate_auth_token("trader@example.com", "verify_email")
    assert verify_auth_token(token, "verify_email", 3600) == "trader@example.com"


def test_verify_auth_token_rejects_wrong_purpose(app_ctx):
    token = generate_auth_token("trader@example.com", "verify_email")
    assert verify_auth_token(token, "password_reset", 3600) is None


def test_generate_and_verify_password_reset_token(app_ctx):
    reset_token = generate_password_reset_token(
        "trader@example.com",
        "password_reset",
        "nonce-123",
    )
    assert verify_password_reset_token(reset_token, "password_reset", 3600) == {
        "email": "trader@example.com",
        "reset_nonce": "nonce-123",
    }


def test_new_password_reset_token_invalidates_previous_link(app_ctx, client):
    user = User(
        username="resettester",
        email="reset@example.com",
        password=generate_password_hash("password123"),
        email_verified=True,
    )
    db.session.add(user)
    db.session.commit()

    first_nonce = rotate_password_reset_nonce(user)
    db.session.commit()
    first_token = generate_password_reset_token(user.email, "password_reset", first_nonce)

    second_nonce = rotate_password_reset_nonce(user)
    db.session.commit()
    second_token = generate_password_reset_token(user.email, "password_reset", second_nonce)

    stale_response = client.get(f"/password/reset/{first_token}")
    assert stale_response.status_code == 200
    assert b"invalid or has expired" in stale_response.data

    fresh_response = client.get(f"/password/reset/{second_token}")
    assert fresh_response.status_code == 200
    assert b"token_valid" not in fresh_response.data
    assert b"invalid or has expired" not in fresh_response.data
    assert b"Reset Password" in fresh_response.data


def test_register_post_is_rate_limited(app_ctx, client):
    limiter.reset()
    try:
        for _ in range(5):
            response = client.post("/register", data={})
            assert response.status_code == 200

        blocked_response = client.post("/register", data={})

        assert blocked_response.status_code == 429
        assert b"Too many requests" in blocked_response.data
    finally:
        limiter.reset()


def test_generate_and_verify_email_change_token(app_ctx):
    """
    Fixed input:
      user_id: 7
      current_email: old@example.com
      new_email: new@example.com
      channel: current

    Expected result:
      Verification returns the same normalized payload fields.
    """
    token = generate_email_change_token(
        user_id=7,
        current_email="old@example.com",
        new_email="new@example.com",
        channel="current",
    )

    payload = verify_email_change_token(token, 3600)

    assert payload == {
        "user_id": 7,
        "current_email": "old@example.com",
        "new_email": "new@example.com",
        "channel": "current",
    }


def test_login_page_shows_google_button_when_enabled(app_ctx, client, monkeypatch):
    app_ctx.config["GOOGLE_CLIENT_ID"] = "google-client-id"
    app_ctx.config["GOOGLE_CLIENT_SECRET"] = "google-client-secret"
    _enable_google_auth(monkeypatch)

    response = client.get("/login")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Sign in with Google" in response_text
    assert "/auth/google" in response_text


def test_google_callback_links_existing_user_and_logs_in(app_ctx, client, monkeypatch):
    app_ctx.config["GOOGLE_CLIENT_ID"] = "google-client-id"
    app_ctx.config["GOOGLE_CLIENT_SECRET"] = "google-client-secret"

    user = User(
        username="google-existing-user",
        email="google-existing@example.com",
        password=generate_password_hash("password123"),
        email_verified=False,
        signup_status="approved",
    )
    db.session.add(user)
    db.session.commit()

    fake_client = _FakeGoogleClient(
        token={
            "userinfo": {
                "sub": "google-sub-123",
                "email": "google-existing@example.com",
                "email_verified": True,
                "name": "Google Existing",
            }
        }
    )
    _enable_google_auth(monkeypatch, fake_client)

    with client.session_transaction() as session_state:
        session_state["google_auth_intent"] = "login"

    response = client.get("/auth/google/callback", follow_redirects=False)

    db.session.refresh(user)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding")
    assert user.google_sub == "google-sub-123"
    assert user.email_verified is True
    assert user.last_login_at is not None

    with client.session_transaction() as session_state:
        assert session_state["user_id"] == user.id
        assert session_state["username"] == user.username
        assert session_state["active_trade_account_id"]


def test_google_register_start_requires_legal_consent(app_ctx, client, monkeypatch):
    app_ctx.config["GOOGLE_CLIENT_ID"] = "google-client-id"
    app_ctx.config["GOOGLE_CLIENT_SECRET"] = "google-client-secret"
    _enable_google_auth(monkeypatch)

    response = client.post(
        "/auth/google/register",
        data={"signup_code": "", "username": "", "email": ""},
    )
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "You must agree to the Terms and acknowledge the Privacy Policy." in response_text


def test_register_page_shows_google_consent_dialog(app_ctx, client, monkeypatch):
    app_ctx.config["GOOGLE_CLIENT_ID"] = "google-client-id"
    app_ctx.config["GOOGLE_CLIENT_SECRET"] = "google-client-secret"
    _enable_google_auth(monkeypatch)

    response = client.get("/register")
    response_text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="googleRegisterDialog"' in response_text
    assert "Review the account agreement" in response_text
    assert "Continue with Google" in response_text


def test_google_callback_creates_new_user_from_register_flow(app_ctx, client, monkeypatch):
    app_ctx.config["GOOGLE_CLIENT_ID"] = "google-client-id"
    app_ctx.config["GOOGLE_CLIENT_SECRET"] = "google-client-secret"
    monkeypatch.setenv("ALLOWED_SIGNUP_EMAIL_DOMAINS", "example.com")
    monkeypatch.setenv("REGISTRATION_PAUSED", "0")
    monkeypatch.setenv("AUTO_APPROVE_NEW_USERS", "1")

    fake_client = _FakeGoogleClient(
        token={
            "userinfo": {
                "sub": "google-sub-new-456",
                "email": "brandnew@example.com",
                "email_verified": True,
                "name": "Brand New Trader",
            }
        }
    )
    _enable_google_auth(monkeypatch, fake_client)

    with client.session_transaction() as session_state:
        session_state["google_auth_intent"] = "register"
        session_state["google_auth_signup_code"] = ""

    response = client.get("/auth/google/callback", follow_redirects=False)
    user = User.query.filter_by(email="brandnew@example.com").first()

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding")
    assert user is not None
    assert user.google_sub == "google-sub-new-456"
    assert user.email_verified is True
    assert user.signup_status == "approved"

    with client.session_transaction() as session_state:
        assert session_state["user_id"] == user.id
