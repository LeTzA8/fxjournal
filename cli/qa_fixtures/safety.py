"""Safety guards for waitlist CTA QA fixture CLI operations."""

from __future__ import annotations

import os
import re
import sys

from models import QaTestAccount, User, db

DUMMY_EMAIL_RE = re.compile(
    r"^dummy-cta-[a-z0-9_-]+@myfxjournal\.test$",
    re.IGNORECASE,
)
FIXTURE_NOTES = "CTA_TEST_FIXTURE_DO_NOT_EMAIL_OR_CONTACT"
KNOWN_FIXTURE_PASSWORD = "TestPassword123!"


def is_production_environment() -> bool:
    app_env = os.getenv("APP_ENV", "").strip().lower()
    flask_env = os.getenv("FLASK_ENV", "").strip().lower()
    public_base = os.getenv("PUBLIC_BASE_URL", "").strip().lower()
    if app_env in {"production", "prod"}:
        return True
    if flask_env == "production":
        return True
    if "myfxjournal.com" in public_base:
        return True
    return False


def assert_dummy_email(email: str) -> None:
    normalized = (email or "").strip()
    if not DUMMY_EMAIL_RE.match(normalized):
        raise ValueError(
            f"Refusing fixture operation: email {normalized!r} does not match "
            f"{DUMMY_EMAIL_RE.pattern}"
        )


def assert_fixture_user(user: User) -> QaTestAccount:
    assert_dummy_email(user.email)
    sidecar = QaTestAccount.query.filter_by(user_id=user.id).one_or_none()
    if sidecar is None:
        raise ValueError(
            f"Refusing fixture operation: user {user.id} ({user.email}) has no qa_test_accounts row"
        )
    return sidecar


def assert_not_admin(user: User) -> None:
    if getattr(user, "is_admin", False):
        raise ValueError(f"Refusing fixture operation: user {user.id} is an admin account")


def require_production_ack(*, allow_production: bool, yes: bool) -> None:
    if not is_production_environment():
        return
    if not allow_production:
        app_env = os.getenv("APP_ENV", "").strip() or "(unset)"
        raise SystemExit(
            "Refusing to run in production without --allow-production.\n"
            f"APP_ENV={app_env}. Re-run with --allow-production to acknowledge."
        )
    if not sys.stdin.isatty() and not yes:
        raise SystemExit(
            "Refusing production fixture operation in non-TTY context. "
            "Re-run with --yes in addition to --allow-production."
        )


def production_confirmation_prompt(fixture_emails: list[str]) -> None:
    lines = [
        "",
        "================================================================",
        " PRODUCTION QA FIXTURE OPERATION",
        " The following dummy users will be created / updated / deleted:",
    ]
    for email in sorted(fixture_emails):
        lines.append(f"   - {email}")
    lines.extend(
        [
            " Real users will NOT be touched. Continue? Type \"I UNDERSTAND\": ",
            "================================================================",
            "",
        ]
    )
    print("\n".join(lines), end="")
    answer = input().strip()
    if answer != "I UNDERSTAND":
        raise SystemExit("Aborted: production confirmation not accepted.")
