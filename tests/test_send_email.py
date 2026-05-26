def test_send_email_placeholder_sets_reply_to_on_resend_payload(app_ctx, monkeypatch):
    import auth_account

    captured = {}

    class FakeResendEmails:
        @staticmethod
        def send(payload):
            captured["payload"] = payload
            return {"id": "email_123"}

    monkeypatch.setenv("EMAIL_SEND_ENABLED", "1")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "noreply@myfxjournal.com")
    monkeypatch.setenv("EMAIL_REPLY_TO", "support@myfxjournal.com")
    monkeypatch.setitem(
        __import__("sys").modules,
        "resend",
        type("FakeResendModule", (), {"Emails": FakeResendEmails, "api_key": None})(),
    )

    result = auth_account.send_email_placeholder(
        "user@example.com",
        "Test subject",
        "Plain body",
        html_body="<p>HTML body</p>",
    )

    assert result["sent"] is True
    assert captured["payload"]["from"] == "MyFXJournal <noreply@myfxjournal.com>"
    assert captured["payload"]["reply_to"] == "support@myfxjournal.com"


def test_send_email_placeholder_custom_from_header(app_ctx, monkeypatch):
    import auth_account

    captured = {}

    class FakeResendEmails:
        @staticmethod
        def send(payload):
            captured["payload"] = payload
            return {"id": "email_456"}

    monkeypatch.setenv("EMAIL_SEND_ENABLED", "1")
    monkeypatch.setenv("EMAIL_PROVIDER", "resend")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setitem(
        __import__("sys").modules,
        "resend",
        type("FakeResendModule", (), {"Emails": FakeResendEmails, "api_key": None})(),
    )

    result = auth_account.send_email_placeholder(
        "user@example.com",
        "Subject",
        "Body",
        from_header="MyFXJournal <admin@myfxjournal.com>",
    )

    assert result["sent"] is True
    assert captured["payload"]["from"] == "MyFXJournal <admin@myfxjournal.com>"


def test_resolve_email_reply_to_falls_back_to_feedback_to_email(app_ctx, monkeypatch):
    import auth_account

    monkeypatch.delenv("EMAIL_REPLY_TO", raising=False)
    monkeypatch.setenv("FEEDBACK_TO_EMAIL", "support@example.com")

    assert auth_account._resolve_email_reply_to() == "support@example.com"
