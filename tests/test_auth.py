from auth_account import (
    generate_auth_token,
    generate_email_change_token,
    verify_auth_token,
    verify_email_change_token,
)


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
