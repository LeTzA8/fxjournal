import logging

import app as app_module


def test_send_error_notification_email_logs_when_email_not_sent(app_ctx, monkeypatch, caplog):
    monkeypatch.setenv("ERROR_LOG_TO_EMAIL", "alerts@example.com")

    def fake_send_email(to_email, subject, text_body, html_body=None):
        assert to_email == "alerts@example.com"
        assert "RuntimeError" in subject
        assert "Unhandled application error" in text_body
        return {"sent": False, "mode": "resend_error"}

    monkeypatch.setattr(app_module, "send_email_placeholder", fake_send_email)

    with app_module.app.test_request_context("/boom", method="POST"):
        with caplog.at_level(logging.WARNING):
            app_module.send_error_notification_email(RuntimeError("boom"))

    assert "Error notification email was not sent" in caplog.text
    assert "mode=resend_error" in caplog.text
