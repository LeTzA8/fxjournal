import json
from datetime import timedelta

from helpers.utils import utcnow_naive
from itertools import count

import auth_account
from models import User, db


_UNIQUE_COUNTER = count(1)


def _unique_suffix():
    return next(_UNIQUE_COUNTER)


def _create_user(*, username, email, last_login_at=None):
    user = User(
        username=username,
        email=email,
        password="hashed-password",
        email_verified=True,
        signup_status="approved",
        last_login_at=last_login_at,
    )
    db.session.add(user)
    db.session.flush()
    return user


def _login_as(client, user):
    with client.session_transaction() as session_state:
        session_state["user_id"] = user.id
        session_state["username"] = user.username


def test_apply_admin_email_placeholders_replaces_name():
    assert auth_account.apply_admin_email_placeholders(
        "Hi {{name}}, welcome.",
        recipient_name="Alex",
    ) == "Hi Alex, welcome."


def test_html_to_plain_email_text_strips_tags():
    assert auth_account.html_to_plain_email_text("<p>Hello <strong>there</strong></p>") == "Hello there"


def test_sanitize_admin_broadcast_html_keeps_logo_img():
    logo_url = "https://myfxjournal.com/static/site-logo.png"
    raw = f'<img src="{logo_url}" alt="MyFXJournal" height="36" onerror="alert(1)" />'
    cleaned = auth_account.sanitize_admin_broadcast_html(raw)
    assert cleaned == f'<img src="{logo_url}" alt="MyFXJournal" height="36" />'
    assert "onerror" not in cleaned


def test_sanitize_admin_broadcast_html_strips_script_tags():
    raw = '<script>alert(1)</script><p>Hello</p>'
    cleaned = auth_account.sanitize_admin_broadcast_html(raw)
    assert "script" not in cleaned
    assert "Hello" in cleaned


def test_admin_broadcast_message_contains_html_detects_img():
    assert auth_account.admin_broadcast_message_contains_html("Hi\n<img src=\"x\" />")
    assert not auth_account.admin_broadcast_message_contains_html("Hi there")


def test_admin_send_email_send_one_accepts_plain_text_before_img_html_body(
    app_ctx, client, monkeypatch
):
    """Client buildHtmlBody sends escaped plain lines plus raw img tags on later lines."""
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-mixed@example.com")
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "1")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("ADMIN_EMAIL_FROM", "admin@myfxjournal.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://myfxjournal.com")

    captured = {}

    class FakeResendEmails:
        @staticmethod
        def send(payload):
            captured["payload"] = payload
            return {"id": "email_mixed"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "resend",
        type("FakeResendModule", (), {"Emails": FakeResendEmails, "api_key": None})(),
    )

    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-mixed-admin-{suffix}",
        email="send-email-mixed@example.com",
    )
    recipient = _create_user(
        username=f"mixed-recipient-{suffix}",
        email=f"mixed-recipient-{suffix}@example.com",
    )
    db.session.commit()
    _login_as(client, admin)

    logo_url = "https://myfxjournal.com/static/site-logo.png"
    html_body = (
        f"Hi {recipient.username}<br>\n"
        f'<img src="{logo_url}" alt="MyFXJournal" height="36" />'
    )

    response = client.post(
        "/dashboard/admin/access/send-email/send-one",
        data=json.dumps(
            {
                "user_id": recipient.id,
                "subject": "Mixed body",
                "html_body": html_body,
                "text_body": f"Hi {recipient.username}\n[logo line]",
            }
        ),
        content_type="application/json",
        headers={"X-CSRFToken": _csrf_token(client)},
    )

    assert response.status_code == 200
    assert f"Hi {recipient.username}" in captured["payload"]["html"]
    assert logo_url in captured["payload"]["html"]


def test_admin_send_email_page_requires_admin(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-admin@example.com")
    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-admin-{suffix}",
        email="send-email-admin@example.com",
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/send-email")
    assert response.status_code == 200
    assert b"Send to selected" in response.data
    assert b"adminSendEmailMessage" in response.data
    assert b"Insert logo" in response.data
    assert b"Name personalization" in response.data
    assert b"quill" not in response.data.lower()


def test_admin_send_email_recipients_inactive_filter(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-recipients@example.com")
    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-recipients-admin-{suffix}",
        email="send-email-recipients@example.com",
    )
    now = utcnow_naive()
    active = _create_user(
        username=f"active-user-{suffix}",
        email=f"active-{suffix}@example.com",
        last_login_at=now - timedelta(days=1),
    )
    inactive = _create_user(
        username=f"inactive-user-{suffix}",
        email=f"inactive-{suffix}@example.com",
        last_login_at=now - timedelta(days=45),
    )
    never = _create_user(
        username=f"never-user-{suffix}",
        email=f"never-{suffix}@example.com",
        last_login_at=None,
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get(
        "/dashboard/admin/access/send-email/recipients"
        "?inactive_days=30&inactive_only=1"
    )
    assert response.status_code == 200
    payload = response.get_json()
    ids = {row["id"] for row in payload["recipients"]}
    assert active.id not in ids
    assert inactive.id in ids
    assert never.id in ids


def test_admin_send_email_send_one_uses_admin_from_and_placeholder(
    app_ctx, client, monkeypatch
):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-send@example.com")
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "1")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("ADMIN_EMAIL_FROM", "admin@myfxjournal.com")

    captured = {}

    class FakeResendEmails:
        @staticmethod
        def send(payload):
            captured["payload"] = payload
            return {"id": "email_123"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "resend",
        type("FakeResendModule", (), {"Emails": FakeResendEmails, "api_key": None})(),
    )

    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-send-admin-{suffix}",
        email="send-email-send@example.com",
    )
    recipient = _create_user(
        username=f"recipient-{suffix}",
        email=f"recipient-{suffix}@example.com",
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.post(
        "/dashboard/admin/access/send-email/send-one",
        data=json.dumps(
            {
                "user_id": recipient.id,
                "subject": "Hello {{name}}",
                "html_body": "<p>Hi {{name}}</p>",
            }
        ),
        content_type="application/json",
        headers={"X-CSRFToken": _csrf_token(client)},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["sent"] is True
    assert captured["payload"]["from"] == "MyFXJournal <admin@myfxjournal.com>"
    assert captured["payload"]["to"] == [recipient.email]
    assert captured["payload"]["subject"] == f"Hello {recipient.username}"
    assert f"Hi {recipient.username}" in captured["payload"]["html"]


def test_admin_send_email_send_one_accepts_plain_text_body(
    app_ctx, client, monkeypatch
):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-text@example.com")
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "1")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("ADMIN_EMAIL_FROM", "admin@myfxjournal.com")

    captured = {}

    class FakeResendEmails:
        @staticmethod
        def send(payload):
            captured["payload"] = payload
            return {"id": "email_456"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "resend",
        type("FakeResendModule", (), {"Emails": FakeResendEmails, "api_key": None})(),
    )

    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-text-admin-{suffix}",
        email="send-email-text@example.com",
    )
    recipient = _create_user(
        username=f"text-recipient-{suffix}",
        email=f"text-recipient-{suffix}@example.com",
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.post(
        "/dashboard/admin/access/send-email/send-one",
        data=json.dumps(
            {
                "user_id": recipient.id,
                "subject": "Hello {{name}}",
                "text_body": "Hi {{name}}\nSecond line",
            }
        ),
        content_type="application/json",
        headers={"X-CSRFToken": _csrf_token(client)},
    )

    assert response.status_code == 200
    assert captured["payload"]["subject"] == f"Hello {recipient.username}"
    assert f"Hi {recipient.username}<br>Second line" in captured["payload"]["html"]
    assert "{{name}}" not in captured["payload"]["html"]


def test_admin_send_email_send_one_accepts_html_body_with_logo(
    app_ctx, client, monkeypatch
):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-logo@example.com")
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "1")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("ADMIN_EMAIL_FROM", "admin@myfxjournal.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://myfxjournal.com")

    captured = {}

    class FakeResendEmails:
        @staticmethod
        def send(payload):
            captured["payload"] = payload
            return {"id": "email_789"}

    monkeypatch.setitem(
        __import__("sys").modules,
        "resend",
        type("FakeResendModule", (), {"Emails": FakeResendEmails, "api_key": None})(),
    )

    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-logo-admin-{suffix}",
        email="send-email-logo@example.com",
    )
    recipient = _create_user(
        username=f"logo-recipient-{suffix}",
        email=f"logo-recipient-{suffix}@example.com",
    )
    db.session.commit()
    _login_as(client, admin)

    logo_url = "https://myfxjournal.com/static/site-logo.png"
    html_body = (
        'Hi {{name}}<br>\n'
        f'<img src="{logo_url}" alt="MyFXJournal" height="36" />'
    )

    response = client.post(
        "/dashboard/admin/access/send-email/send-one",
        data=json.dumps(
            {
                "user_id": recipient.id,
                "subject": "Logo test",
                "html_body": html_body,
                "text_body": f"Hi {{name}}\n[logo]",
            }
        ),
        content_type="application/json",
        headers={"X-CSRFToken": _csrf_token(client)},
    )

    assert response.status_code == 200
    assert f"Hi {recipient.username}" in captured["payload"]["html"]
    assert logo_url in captured["payload"]["html"]
    assert 'alt="MyFXJournal"' in captured["payload"]["html"]


def test_admin_send_email_signatures_save_list_and_delete(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-signatures@example.com")
    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-signatures-admin-{suffix}",
        email="send-email-signatures@example.com",
    )
    db.session.commit()
    _login_as(client, admin)

    list_response = client.get("/dashboard/admin/access/send-email/signatures")
    assert list_response.status_code == 200
    assert list_response.get_json()["signatures"] == []

    save_response = client.post(
        "/dashboard/admin/access/send-email/signatures/save",
        data=json.dumps(
            {
                "name": "Standard sign-off",
                "body": "Best,\n{{name}}\nMyFXJournal",
            }
        ),
        content_type="application/json",
        headers={"X-CSRFToken": _csrf_token(client)},
    )
    assert save_response.status_code == 200
    save_payload = save_response.get_json()
    signature_id = save_payload["signature"]["id"]
    assert save_payload["signature"]["name"] == "Standard sign-off"
    assert len(save_payload["signatures"]) == 1

    update_response = client.post(
        "/dashboard/admin/access/send-email/signatures/save",
        data=json.dumps(
            {
                "id": signature_id,
                "name": "Updated sign-off",
                "body": "Thanks,\n{{name}}",
            }
        ),
        content_type="application/json",
        headers={"X-CSRFToken": _csrf_token(client)},
    )
    assert update_response.status_code == 200
    assert update_response.get_json()["signature"]["name"] == "Updated sign-off"

    delete_response = client.post(
        "/dashboard/admin/access/send-email/signatures/delete",
        data=json.dumps({"id": signature_id}),
        content_type="application/json",
        headers={"X-CSRFToken": _csrf_token(client)},
    )
    assert delete_response.status_code == 200
    assert delete_response.get_json()["signatures"] == []


def test_admin_send_email_page_includes_signature_controls(app_ctx, client, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_EMAILS", "send-email-signature-ui@example.com")
    suffix = _unique_suffix()
    admin = _create_user(
        username=f"send-email-signature-ui-admin-{suffix}",
        email="send-email-signature-ui@example.com",
    )
    db.session.commit()
    _login_as(client, admin)

    response = client.get("/dashboard/admin/access/send-email")

    assert response.status_code == 200
    assert b"adminSendEmailSignatureDialog" in response.data
    assert b"Save signature" in response.data
    assert b"data-signatures-save-url" in response.data


def _csrf_token(client):
    response = client.get("/dashboard/admin/access/send-email")
    assert response.status_code == 200
    import re

    match = re.search(
        rb'name="csrf_token" value="([^"]+)"',
        response.data,
    )
    assert match is not None
    return match.group(1).decode("utf-8")
