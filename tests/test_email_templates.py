import pytest


@pytest.mark.parametrize(
    ("template_name", "context", "expected_snippets"),
    [
        (
            "emails/verify-email.html",
            {
                "name": "Template Tester",
                "verify_url": "https://example.com/verify",
            },
            ["Verify your email", "Verify Email"],
        ),
        (
            "emails/password-reset.html",
            {
                "name": "Template Tester",
                "reset_url": "https://example.com/reset",
            },
            ["Reset your password", "Reset Password"],
        ),
        (
            "emails/confirm-email-change.html",
            {
                "title": "Confirm your email",
                "badge_label": "Email change",
                "heading": "Confirm your email",
                "intro": "Please confirm this email change request.",
                "confirm_url": "https://example.com/confirm",
                "button_label": "Confirm Email",
                "detail": "Both addresses must be confirmed.",
                "footer_note": "Ignore this email if you did not request the change.",
            },
            ["Confirm your email", "Confirm Email"],
        ),
        (
            "emails/mt5-request-received.html",
            {
                "name": "Template Tester",
                "account_name": "Request Account",
                "dashboard_url": "https://example.com/dashboard",
                "setup_queued": True,
            },
            ["MT5 setup started", "Request Account"],
        ),
        (
            "emails/mt5-ready.html",
            {
                "name": "Template Tester",
                "account_name": "Ready Account",
                "account_number": "77112233",
                "dashboard_url": "https://example.com/dashboard",
            },
            ["Your MT5 sync is ready.", "77112233"],
        ),
        (
            "emails/welcome.html",
            {
                "name": "Template Tester",
                "dashboard_url": "https://example.com/dashboard",
                "unsubscribe_url": "",
            },
            ["welcome to MyFXJournal", "Go to Dashboard"],
        ),
        (
            "emails/weekly-review.html",
            {
                "name": "Template Tester",
                "week_label": "07 April 2026",
                "ai_preview": "You stayed patient and cut weaker setups faster.",
                "total_trades": 8,
                "win_rate": "62.5",
                "net_pnl": "+123.45",
                "pnl_color": "#1fc66a",
                "unsubscribe_url": "",
                "dashboard_url": "https://example.com/dashboard",
            },
            ["your review is ready", "View Full Review"],
        ),
    ],
)
def test_user_email_templates_render_with_shared_shell(app_ctx, template_name, context, expected_snippets):
    html = app_ctx.jinja_env.get_template(template_name).render(
        logo_url="https://example.com/static/site-logo.png",
        **context,
    )

    assert 'class="email-bg email-shell"' in html
    assert 'alt="MyFXJournal"' in html
    assert "https://example.com/static/site-logo.png" in html
    assert "myfxjournal.com" in html
    for snippet in expected_snippets:
        assert snippet in html
