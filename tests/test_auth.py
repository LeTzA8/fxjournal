from werkzeug.security import generate_password_hash

from auth_account import (
    generate_auth_token,
    generate_email_change_token,
    generate_password_reset_token,
    rotate_password_reset_nonce,
    verify_auth_token,
    verify_email_change_token,
    verify_password_reset_token,
)
from models import User, db


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
