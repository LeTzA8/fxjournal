import os
import secrets
from datetime import datetime, timezone
from functools import wraps

from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from ai_service import get_latest_trade_week_period
from models import (
    AllowedSignupEmailDomain,
    SignupCode,
    TradeAccount,
    User,
    db,
)
from helpers.core import sanitize_error_message
from helpers.utils import env_bool as _env_bool, env_int as _env_int, utcnow_naive

TOKEN_PURPOSE_PENDING_REGISTRATION = "pending_registration"
TOKEN_PURPOSE_EMAIL_CHANGE = "email_change"
PENDING_REGISTRATIONS = {}
SIGNUP_STATUS_PENDING = "pending"
SIGNUP_STATUS_APPROVED = "approved"
SIGNUP_STATUS_REJECTED = "rejected"
SIGNUP_STATUS_SUSPENDED = "suspended"
VALID_SIGNUP_STATUSES = {
    SIGNUP_STATUS_PENDING,
    SIGNUP_STATUS_APPROVED,
    SIGNUP_STATUS_REJECTED,
    SIGNUP_STATUS_SUSPENDED,
}
SIGNUP_CODE_MODE_OFF = "off"
SIGNUP_CODE_MODE_OPTIONAL = "optional"
SIGNUP_CODE_MODE_REQUIRED = "required"
VALID_SIGNUP_CODE_MODES = {
    SIGNUP_CODE_MODE_OFF,
    SIGNUP_CODE_MODE_OPTIONAL,
    SIGNUP_CODE_MODE_REQUIRED,
}


def get_registration_paused():
    return _env_bool("REGISTRATION_PAUSED", False)


def get_auto_approve_new_users():
    return _env_bool("AUTO_APPROVE_NEW_USERS", True)


def get_signup_code_mode():
    raw = os.getenv("SIGNUP_CODE_MODE", SIGNUP_CODE_MODE_OFF).strip().lower()
    return raw if raw in VALID_SIGNUP_CODE_MODES else SIGNUP_CODE_MODE_OFF


def get_signup_code_query_param():
    return (os.getenv("REFERRAL_LINK_QUERY_PARAM", "ref").strip().lower() or "ref")


def get_admin_user_emails():
    raw = os.getenv("ADMIN_USER_EMAILS", "").strip()
    return {part.strip().lower() for part in raw.split(",") if part and part.strip()}


def is_root_admin_email(email):
    candidate = (email or "").strip().lower()
    return bool(candidate and candidate in get_admin_user_emails())


def is_admin_email(email):
    return is_root_admin_email(email)


def normalize_signup_status(value, default=SIGNUP_STATUS_APPROVED):
    candidate = str(value or "").strip().lower()
    return candidate if candidate in VALID_SIGNUP_STATUSES else default


def normalize_signup_code(value):
    cleaned = "".join(ch for ch in str(value or "").strip().upper() if ch.isalnum())
    return cleaned[:32]


def generate_signup_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def send_error_log_email(*, subject, body):
    error_to_email = os.getenv("ERROR_LOG_TO_EMAIL", "").strip().lower()
    if not error_to_email:
        return
    try:
        email_result = send_email_placeholder(error_to_email, subject, body)
        if not (email_result or {}).get("sent"):
            current_app.logger.warning(
                "Error notification email was not sent: to=%s subject=%s mode=%s",
                error_to_email,
                subject,
                (email_result or {}).get("mode", "unknown"),
            )
    except Exception as notify_exc:
        current_app.logger.warning("Error notification email failed: %s", notify_exc)


def build_unique_signup_code():
    while True:
        candidate = generate_signup_code()
        existing = SignupCode.query.filter_by(code=candidate).first()
        if existing is None:
            return candidate


def get_signup_code_validation_message(mode):
    if mode == SIGNUP_CODE_MODE_REQUIRED:
        return "A valid referral code is required to register right now."
    return "Referral code is invalid or no longer active."


def find_signup_code(code_value):
    normalized = normalize_signup_code(code_value)
    if not normalized:
        return None
    return SignupCode.query.filter_by(code=normalized).first()


def is_signup_code_usable(code_row):
    if not code_row or not code_row.is_active:
        return False
    if code_row.expires_at and code_row.expires_at <= utcnow_naive():
        return False
    if code_row.max_uses is not None and code_row.used_count >= code_row.max_uses:
        return False
    return True


def get_initial_signup_status():
    return SIGNUP_STATUS_APPROVED if get_auto_approve_new_users() else SIGNUP_STATUS_PENDING


def user_has_admin_access(user):
    if not user:
        return False
    if not getattr(user, "email_verified", False):
        return False
    if normalize_signup_status(getattr(user, "signup_status", None)) != SIGNUP_STATUS_APPROVED:
        return False
    return bool(getattr(user, "is_admin", False) or is_root_admin_email(getattr(user, "email", "")))


def user_has_root_admin_access(user):
    if not user:
        return False
    if not getattr(user, "email_verified", False):
        return False
    if normalize_signup_status(getattr(user, "signup_status", None)) != SIGNUP_STATUS_APPROVED:
        return False
    return is_root_admin_email(getattr(user, "email", ""))


def get_public_base_url():
    configured_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured_base:
        return configured_base
    return request.host_url.rstrip("/")


def build_external_url(path_or_url):
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return f"{get_public_base_url()}{path_or_url}"


def get_allowed_signup_email_domains():
    raw = os.getenv("ALLOWED_SIGNUP_EMAIL_DOMAINS", "").strip()
    if not raw:
        try:
            rows = (
                AllowedSignupEmailDomain.query.filter_by(is_active=True)
                .order_by(AllowedSignupEmailDomain.domain.asc())
                .all()
            )
        except (OperationalError, ProgrammingError):
            current_app.logger.warning(
                "Allowed signup domain table is unavailable. Falling back to env-only allowlist."
            )
            return set()
        return {row.domain.strip().lower() for row in rows if row.domain and row.domain.strip()}
    return {part.strip().lower() for part in raw.split(",") if part and part.strip()}


def is_allowed_signup_email_domain(email):
    candidate = (email or "").strip().lower()
    if "@" not in candidate:
        return False
    domain = candidate.rsplit("@", 1)[1].strip()
    if not domain:
        return False
    return domain in get_allowed_signup_email_domains()


def get_token_serializer():
    token_salt = os.getenv("TOKEN_SALT", "fxjournal-token-salt")
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=token_salt)


def generate_auth_token(email, purpose):
    serializer = get_token_serializer()
    return serializer.dumps({"email": (email or "").strip().lower(), "purpose": purpose})


def generate_email_change_token(*, user_id, current_email, new_email, channel):
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in {"current", "new"}:
        raise ValueError("Email change token channel must be 'current' or 'new'.")
    serializer = get_token_serializer()
    return serializer.dumps(
        {
            "purpose": TOKEN_PURPOSE_EMAIL_CHANGE,
            "user_id": int(user_id),
            "current_email": (current_email or "").strip().lower(),
            "new_email": (new_email or "").strip().lower(),
            "channel": normalized_channel,
        }
    )


def generate_pending_registration_token(registration_id, email):
    serializer = get_token_serializer()
    return serializer.dumps(
        {
            "registration_id": str(registration_id or "").strip(),
            "email": (email or "").strip().lower(),
            "purpose": TOKEN_PURPOSE_PENDING_REGISTRATION,
        }
    )


def verify_auth_token(token, purpose, max_age_seconds):
    serializer = get_token_serializer()
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("purpose") != purpose:
        return None

    email = str(payload.get("email", "")).strip().lower()
    return email or None


def verify_email_change_token(token, max_age_seconds):
    serializer = get_token_serializer()
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("purpose") != TOKEN_PURPOSE_EMAIL_CHANGE:
        return None

    try:
        user_id = int(payload.get("user_id"))
    except (TypeError, ValueError):
        return None

    current_email = str(payload.get("current_email", "")).strip().lower()
    new_email = str(payload.get("new_email", "")).strip().lower()
    channel = str(payload.get("channel", "")).strip().lower()
    if not current_email or not new_email or channel not in {"current", "new"}:
        return None

    return {
        "user_id": user_id,
        "current_email": current_email,
        "new_email": new_email,
        "channel": channel,
    }


def verify_pending_registration_token(token, max_age_seconds):
    serializer = get_token_serializer()
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("purpose") != TOKEN_PURPOSE_PENDING_REGISTRATION:
        return None

    registration_id = str(payload.get("registration_id", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    if not registration_id or not email:
        return None
    return {"registration_id": registration_id, "email": email}


def cleanup_pending_registrations(max_age_seconds):
    now = utcnow_naive()
    expired = []
    for registration_id, item in PENDING_REGISTRATIONS.items():
        created_at = item.get("created_at")
        if not isinstance(created_at, datetime):
            expired.append(registration_id)
            continue
        age = (now - created_at).total_seconds()
        if age > max_age_seconds:
            expired.append(registration_id)
    for registration_id in expired:
        PENDING_REGISTRATIONS.pop(registration_id, None)


def create_pending_registration(username, email, password_hash):
    expiry_seconds = _env_int("EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS", 86400)
    cleanup_pending_registrations(expiry_seconds)

    registration_id = secrets.token_urlsafe(24)
    PENDING_REGISTRATIONS[registration_id] = {
        "username": username,
        "email": email,
        "password_hash": password_hash,
        "created_at": utcnow_naive(),
    }
    return registration_id


def get_pending_registration(registration_id):
    if not registration_id:
        return None
    return PENDING_REGISTRATIONS.get(registration_id)


def pop_pending_registration(registration_id):
    if not registration_id:
        return None
    return PENDING_REGISTRATIONS.pop(registration_id, None)


def _should_log_email_bodies():
    app_env = os.getenv("APP_ENV", "").strip().lower()
    flask_env = os.getenv("FLASK_ENV", "").strip().lower()
    return (
        _env_bool("FLASK_DEBUG", False)
        or app_env in {"local", "development", "dev"}
        or flask_env in {"local", "development", "dev"}
    )


def send_email_placeholder(to_email, subject, text_body, html_body=None):
    provider = os.getenv("EMAIL_PROVIDER", "placeholder").strip().lower()
    sender = os.getenv("EMAIL_FROM", "noreply@example.com").strip()
    send_enabled = os.getenv("EMAIL_SEND_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    api_key = os.getenv("RESEND_API_KEY", "").strip() or os.getenv("EMAIL_API_KEY", "").strip()
    log_email_bodies = _should_log_email_bodies()

    if provider in {"console", "placeholder"} or not send_enabled:
        current_app.logger.info(
            "Email placeholder (console/disabled) -> to=%s from=%s subject=%s",
            to_email,
            sender,
            subject,
        )
        if log_email_bodies:
            current_app.logger.info("Email body:\n%s", text_body)
        return {"sent": False, "mode": "placeholder"}

    if not api_key:
        current_app.logger.warning(
            "EMAIL_SEND_ENABLED is true but RESEND_API_KEY/EMAIL_API_KEY is missing. Using placeholder mode."
        )
        if log_email_bodies:
            current_app.logger.info("Email body:\n%s", text_body)
        return {"sent": False, "mode": "missing_api_key"}

    if provider == "resend":
        try:
            import resend
        except ImportError:
            current_app.logger.warning("Resend SDK is not installed. Falling back to placeholder logging.")
            if log_email_bodies:
                current_app.logger.info("Email body:\n%s", text_body)
            return {"sent": False, "mode": "missing_resend_sdk"}

        html_payload = html_body
        if not html_payload:
            safe_text = (
                text_body.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            html_payload = f"<div>{safe_text}</div>"

        try:
            resend.api_key = api_key
            payload = {
                "from": sender,
                "to": [to_email],
                "subject": subject,
                "html": html_payload,
            }
            response = resend.Emails.send(payload)
            return {"sent": True, "mode": "resend", "response": response}
        except Exception as exc:
            current_app.logger.warning("Resend send failed: %s", exc)
            if log_email_bodies:
                current_app.logger.info("Email body:\n%s", text_body)
            return {"sent": False, "mode": "resend_error"}

    current_app.logger.warning(
        "Email provider '%s' is configured but integration is not implemented.", provider
    )
    if log_email_bodies:
        current_app.logger.info("Email body:\n%s", text_body)
    return {"sent": False, "mode": "not_implemented"}


def register_public_auth_routes(
    app,
    limiter,
    *,
    legal_last_updated,
    token_purpose_verify_email,
    token_purpose_password_reset,
    env_int,
    is_local_dev_environment,
    resolve_active_trade_account,
):
    def get_current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None
        return User.query.filter_by(id=user_id).first()

    def get_current_admin_user():
        user = get_current_user()
        if not user_has_admin_access(user):
            return None
        return user

    def get_current_root_admin_user():
        user = get_current_user()
        if not user_has_root_admin_access(user):
            return None
        return user

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            admin_user = get_current_admin_user()
            if admin_user is None:
                if session.get("user_id"):
                    abort(404)
                return redirect(url_for("login"))
            return f(*args, **kwargs)

        return decorated

    def root_admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            root_admin_user = get_current_root_admin_user()
            if root_admin_user is None:
                if session.get("user_id"):
                    abort(404)
                return redirect(url_for("login"))
            return f(*args, **kwargs)

        return decorated

    def require_admin_user():
        admin_user = get_current_admin_user()
        if admin_user is None:
            if session.get("user_id"):
                abort(404)
            return None
        return admin_user

    def require_root_admin_user():
        root_admin_user = get_current_root_admin_user()
        if root_admin_user is None:
            if session.get("user_id"):
                abort(404)
            return None
        return root_admin_user

    def render_login_page(*, error=None, success=None, info=None, email=""):
        return render_template(
            "login.html",
            title="Login | FX Journal",
            body_class="auth-layout",
            error=error,
            success=success,
            info=info,
            email_value=email,
        )

    def render_register_page(
        *,
        error=None,
        success=None,
        info=None,
        username="",
        email="",
        signup_code="",
    ):
        signup_code_mode = get_signup_code_mode()
        signup_code_query_param = get_signup_code_query_param()
        show_signup_code_input = signup_code_mode != SIGNUP_CODE_MODE_OFF
        return render_template(
            "register.html",
            title="Register | FX Journal",
            body_class="auth-layout",
            error=error,
            success=success,
            info=info,
            username_value=username,
            email_value=email,
            signup_code_value=signup_code,
            signup_code_mode=signup_code_mode,
            signup_code_query_param=signup_code_query_param,
            show_signup_code_input=show_signup_code_input,
            registrations_paused=get_registration_paused(),
        )

    def build_admin_redirect(section="users", message="", status="info"):
        endpoint = "admin_signup_codes" if section == "codes" else "admin_signup_users"
        if message:
            flash(message, status)
        return redirect(url_for(endpoint))

    def build_admin_overview():
        return {
            "total_users": User.query.count(),
            "pending_users": User.query.filter_by(signup_status=SIGNUP_STATUS_PENDING).count(),
            "approved_users": User.query.filter_by(signup_status=SIGNUP_STATUS_APPROVED).count(),
            "rejected_users": User.query.filter_by(signup_status=SIGNUP_STATUS_REJECTED).count(),
            "suspended_users": User.query.filter_by(signup_status=SIGNUP_STATUS_SUSPENDED).count(),
            "admin_users": User.query.filter_by(is_admin=True).count(),
            "signup_codes": SignupCode.query.count(),
        }

    def render_admin_page(*, admin_user, section, **extra_context):
        return render_template(
            "admin_signup_access.html",
            title="Access Control | FX Journal",
            username=admin_user.username,
            section=section,
            is_root_admin=user_has_root_admin_access(admin_user),
            root_admin_emails=get_admin_user_emails(),
            overview=build_admin_overview(),
            registration_paused=get_registration_paused(),
            auto_approve_new_users=get_auto_approve_new_users(),
            signup_code_mode=get_signup_code_mode(),
            signup_code_query_param=get_signup_code_query_param(),
            public_register_url=build_external_url(url_for("register")),
            **extra_context,
        )

    @app.route("/")
    def landing():
        return render_template(
            "landing.html",
            title="FX Journal",
            body_class="landing-layout",
            user_logged_in=bool(session.get("user_id")),
        )

    @app.route("/privacy")
    @app.route("/privacy-policy")
    def privacy_policy():
        return render_template(
            "privacy_policy.html",
            title="Privacy Policy | FX Journal",
            last_updated=legal_last_updated,
        )

    @app.route("/terms")
    @app.route("/terms-and-conditions")
    def terms_and_conditions():
        return render_template(
            "terms_and_conditions.html",
            title="Terms and Conditions | FX Journal",
            last_updated=legal_last_updated,
        )

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit(
        "8 per minute;40 per hour",
        methods=["POST"],
        error_message="Too many sign-in attempts. Please wait and try again.",
    )
    def login():
        if session.get("user_id"):
            return redirect(url_for("dashboard.home"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password, password):
                if os.getenv("REQUIRE_EMAIL_VERIFICATION", "true").strip().lower() in {"1", "true", "yes", "on"} and not user.email_verified:
                    session["pending_verify_email"] = user.email
                    return render_login_page(
                        error="Please verify your email address before signing in.",
                        email=email,
                    )
                signup_status = normalize_signup_status(user.signup_status)
                if signup_status == SIGNUP_STATUS_PENDING:
                    return render_login_page(
                        info="Your email has been verified. Your account is now waiting for approval.",
                        email=email,
                    )
                if signup_status == SIGNUP_STATUS_REJECTED:
                    return render_login_page(
                        error="Your registration was not approved. Contact support if you believe this is a mistake.",
                        email=email,
                    )
                if signup_status == SIGNUP_STATUS_SUSPENDED:
                    return render_login_page(
                        error="This account is currently suspended. Contact support if you need help.",
                        email=email,
                    )
                user.last_login_at = utcnow_naive()
                session["user_id"] = user.id
                session["username"] = user.username
                active_account, _accounts = resolve_active_trade_account(user.id)
                session["active_trade_account_id"] = active_account.id
                db.session.commit()
                return redirect(url_for("dashboard.home"))

            return render_login_page(
                error="Invalid email or password.",
                email=email,
            )
        return render_login_page()

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if session.get("user_id"):
            return redirect(url_for("dashboard.home"))
        query_param = get_signup_code_query_param()
        signup_code_prefill = normalize_signup_code(request.args.get(query_param, ""))

        if get_registration_paused():
            return render_register_page(
                info="New registrations are temporarily paused.",
                signup_code=signup_code_prefill,
            )

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            accepted_legal = request.form.get("accept_legal") == "on"
            signup_code_value = normalize_signup_code(
                request.form.get("signup_code") or request.args.get(query_param, "")
            )
            signup_code_mode = get_signup_code_mode()
            signup_code_row = None

            if signup_code_mode != SIGNUP_CODE_MODE_OFF and signup_code_value:
                signup_code_row = find_signup_code(signup_code_value)
                if not is_signup_code_usable(signup_code_row):
                    return render_register_page(
                        error=get_signup_code_validation_message(signup_code_mode),
                        username=username,
                        email=email,
                        signup_code=signup_code_value,
                    )
            elif signup_code_mode == SIGNUP_CODE_MODE_REQUIRED:
                return render_register_page(
                    error="A valid referral code is required to create an account right now.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if not username or not email or not password:
                return render_register_page(
                    error="All fields are required.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if len(password) < 8:
                return render_register_page(
                    error="Password must be at least 8 characters.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if not is_allowed_signup_email_domain(email):
                return render_register_page(
                    error=(
                        "Please use a common email provider "
                        "(for example Gmail, Outlook, Yahoo, iCloud, or Proton)."
                    ),
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if not accepted_legal:
                return render_register_page(
                    error="You must accept the Terms and Conditions and Privacy Policy.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            existing_user = User.query.filter(
                or_(User.username == username, User.email == email)
            ).first()
            if existing_user:
                existing_status = normalize_signup_status(existing_user.signup_status)
                if existing_user.email == email and existing_status == SIGNUP_STATUS_REJECTED:
                    return render_register_page(
                        error="This email is linked to a rejected registration. Contact support if you need help.",
                        username=username,
                        email=email,
                        signup_code=signup_code_value,
                    )
                if existing_user.email == email and existing_status == SIGNUP_STATUS_SUSPENDED:
                    return render_register_page(
                        error="This email belongs to a suspended account.",
                        username=username,
                        email=email,
                        signup_code=signup_code_value,
                    )
                if existing_user.email == email and not existing_user.email_verified:
                    session["pending_verify_email"] = existing_user.email
                    session.pop("pending_registration_id", None)
                    verify_token = generate_auth_token(
                        existing_user.email,
                        token_purpose_verify_email,
                    )
                    verify_link = build_external_url(
                        url_for("verify_email_token", token=verify_token)
                    )
                    email_subject = "Verify your FX Journal email"
                    email_body = (
                        f"Hi {existing_user.username},\n\n"
                        "You requested a new verification link.\n"
                        f"{verify_link}\n\n"
                        "If you did not request this, you can ignore this email."
                    )
                    email_result = send_email_placeholder(
                        existing_user.email,
                        email_subject,
                        email_body,
                    )
                    flash("Verification email sent. Please check your inbox.", "success")
                    verify_kwargs = {}
                    if is_local_dev_environment() and not email_result.get("sent"):
                        verify_kwargs["verify_link"] = verify_link
                    return redirect(url_for("verify_email_pending", **verify_kwargs))
                return render_register_page(
                    error="Username or email already exists.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            hashed_password = generate_password_hash(password)
            initial_signup_status = get_initial_signup_status()
            user = User(
                username=username,
                email=email,
                password=hashed_password,
                email_verified=False,
                signup_status=initial_signup_status,
                signup_code_used=signup_code_row.code if signup_code_row else None,
                approved_at=utcnow_naive() if initial_signup_status == SIGNUP_STATUS_APPROVED else None,
                verification_sent_at=utcnow_naive(),
            )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return render_register_page(
                    error="Username or email already exists.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            session["pending_verify_email"] = user.email
            session.pop("pending_registration_id", None)
            verify_token = generate_auth_token(
                user.email,
                token_purpose_verify_email,
            )
            verify_link = build_external_url(
                url_for("verify_email_token", token=verify_token)
            )
            email_subject = "Verify your FX Journal email"
            email_body = (
                f"Hi {user.username},\n\n"
                "Thanks for registering.\n"
                "Verify your email by opening this link:\n"
                f"{verify_link}\n\n"
                "If you did not create this account, you can ignore this email."
            )
            email_result = send_email_placeholder(user.email, email_subject, email_body)

            flash("Verification email sent. Please check your inbox.", "success")
            verify_kwargs = {}
            if is_local_dev_environment() and not email_result.get("sent"):
                verify_kwargs["verify_link"] = verify_link
            return redirect(url_for("verify_email_pending", **verify_kwargs))

        return render_register_page(
            signup_code=signup_code_prefill,
        )

    @app.route("/verify-email/pending", methods=["GET", "POST"])
    @limiter.limit(
        "5 per minute;20 per hour",
        methods=["POST"],
        error_message="Too many attempts. Please wait and try again.",
    )
    def verify_email_pending():
        pending_email = session.get("pending_verify_email", "").strip().lower()
        pending_username = ""
        pending_id = ""
        pending = None
        using_legacy_pending = False
        awaiting_approval = False

        if pending_email:
            user = User.query.filter_by(email=pending_email).first()
            if not user:
                session.pop("pending_verify_email", None)
                pending_email = ""
            elif user.email_verified:
                session.pop("pending_verify_email", None)
                if normalize_signup_status(user.signup_status) == SIGNUP_STATUS_APPROVED:
                    flash("Email verified. You can now log in.", "success")
                    return redirect(url_for("login"))
                flash("Email verified. You can now log in.", "success")
                flash(
                    "Your email has been verified. Your account is now waiting for approval.",
                    "info",
                )
                return redirect(url_for("login"))
            else:
                pending_username = user.username
                awaiting_approval = normalize_signup_status(user.signup_status) == SIGNUP_STATUS_PENDING

        if not pending_email:
            pending_id = session.get("pending_registration_id", "").strip()
            pending = get_pending_registration(pending_id)
            if not pending:
                flash("Your verification session has expired. Please register again.", "error")
                return redirect(url_for("register"))
            pending_email = pending["email"]
            pending_username = pending["username"]
            using_legacy_pending = True
            awaiting_approval = get_initial_signup_status() == SIGNUP_STATUS_PENDING

        success = ""
        error = ""
        debug_verify_link = request.args.get("verify_link", "").strip()

        if request.method == "POST":
            if using_legacy_pending:
                verify_token = generate_pending_registration_token(
                    registration_id=pending_id,
                    email=pending_email,
                )
            else:
                user = User.query.filter_by(email=pending_email).first()
                if not user:
                    session.pop("pending_verify_email", None)
                    flash("Your verification session has expired. Please register again.", "error")
                    return redirect(url_for("register"))
                if user.email_verified:
                    session.pop("pending_verify_email", None)
                    flash("Email verified. You can now log in.", "success")
                    return redirect(url_for("login"))
                verify_token = generate_auth_token(
                    pending_email,
                    token_purpose_verify_email,
                )
            verify_link = build_external_url(
                url_for("verify_email_token", token=verify_token)
            )
            email_subject = "Verify your FX Journal email"
            email_body = (
                f"Hi {pending_username},\n\n"
                "You requested a new verification link.\n"
                f"{verify_link}\n\n"
                "If you did not request this, you can ignore this email."
            )
            email_result = send_email_placeholder(
                pending_email,
                email_subject,
                email_body,
            )
            success = "Verification email sent. Please check your inbox."
            if is_local_dev_environment() and not email_result.get("sent"):
                debug_verify_link = verify_link

        return render_template(
            "resend_verification.html",
            title="Verify Email | FX Journal",
            body_class="auth-layout",
            pending_email=pending_email,
            success=success or None,
            error=error or None,
            debug_verify_link=debug_verify_link,
            awaiting_approval=awaiting_approval,
        )

    @app.route("/verify-email/resend", methods=["GET"])
    def resend_verification_email_alias():
        return redirect(url_for("verify_email_pending"))

    @app.route("/verify-email/<token>")
    def verify_email_token(token):
        max_age_seconds = env_int("EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS", 86400)
        pending_payload = verify_pending_registration_token(
            token=token,
            max_age_seconds=max_age_seconds,
        )
        if pending_payload:
            registration_id = pending_payload["registration_id"]
            pending = get_pending_registration(registration_id)
            if not pending:
                flash("This verification link is invalid or has expired. Please register again.", "error")
                return redirect(url_for("register"))

            if pending["email"] != pending_payload["email"]:
                pop_pending_registration(registration_id)
                if session.get("pending_registration_id") == registration_id:
                    session.pop("pending_registration_id", None)
                flash("We could not verify this registration request. Please register again.", "error")
                return redirect(url_for("register"))

            existing_user = User.query.filter(
                or_(User.username == pending["username"], User.email == pending["email"])
            ).first()
            if existing_user:
                pop_pending_registration(registration_id)
                if session.get("pending_registration_id") == registration_id:
                    session.pop("pending_registration_id", None)
                if (
                    existing_user.username == pending["username"]
                    and existing_user.email == pending["email"]
                    and existing_user.email_verified
                ):
                    flash("Email verified. You can now log in.", "success")
                    return redirect(url_for("login"))
                flash(
                    "That username or email is no longer available. Please register again.",
                    "error",
                )
                return redirect(url_for("register"))

            initial_signup_status = get_initial_signup_status()
            user = User(
                username=pending["username"],
                email=pending["email"],
                password=pending["password_hash"],
                email_verified=True,
                signup_status=initial_signup_status,
                approved_at=utcnow_naive() if initial_signup_status == SIGNUP_STATUS_APPROVED else None,
                verification_sent_at=utcnow_naive(),
            )
            db.session.add(user)
            db.session.commit()

            pop_pending_registration(registration_id)
            if session.get("pending_registration_id") == registration_id:
                session.pop("pending_registration_id", None)

            if normalize_signup_status(user.signup_status) == SIGNUP_STATUS_APPROVED:
                flash("Email verified. You can now log in.", "success")
                return redirect(url_for("login"))
            flash("Email verified. You can now log in.", "success")
            flash(
                "Your email has been verified. Your account is now waiting for approval.",
                "info",
            )
            return redirect(url_for("login"))

        email = verify_auth_token(
            token=token,
            purpose=token_purpose_verify_email,
            max_age_seconds=max_age_seconds,
        )
        if not email:
            flash("This verification link is invalid or has expired. Please register again.", "error")
            return redirect(url_for("register"))

        user = User.query.filter_by(email=email).first()
        if not user:
            flash("This verification link is invalid or has expired. Please register again.", "error")
            return redirect(url_for("register"))

        if not user.email_verified:
            user.email_verified = True
            if user.signup_code_used:
                signup_code = find_signup_code(user.signup_code_used)
                if is_signup_code_usable(signup_code):
                    signup_code.used_count += 1
            db.session.commit()

        if session.get("pending_verify_email", "").strip().lower() == email:
            session.pop("pending_verify_email", None)

        if normalize_signup_status(user.signup_status) == SIGNUP_STATUS_APPROVED:
            flash("Email verified. You can now log in.", "success")
            return redirect(url_for("login"))
        flash("Email verified. You can now log in.", "success")
        flash(
            "Your email has been verified. Your account is now waiting for approval.",
            "info",
        )
        return redirect(url_for("login"))

    @app.route("/dashboard/admin/access")
    @admin_required
    def admin_signup_access():
        return redirect(url_for("admin_signup_users"))

    @app.route("/dashboard/admin/access/users")
    @admin_required
    def admin_signup_users():
        admin_user = get_current_admin_user()

        status_filter = request.args.get("status", "pending").strip().lower()
        if status_filter not in {
            "pending",
            "approved",
            "rejected",
            "suspended",
            "admins",
            "all",
        }:
            status_filter = "pending"

        users_query = User.query
        if status_filter == "admins":
            root_admin_emails = sorted(get_admin_user_emails())
            if root_admin_emails:
                users_query = users_query.filter(
                    or_(User.is_admin.is_(True), User.email.in_(root_admin_emails))
                )
            else:
                users_query = users_query.filter(User.is_admin.is_(True))
        elif status_filter != "all":
            users_query = users_query.filter_by(signup_status=status_filter)

        users = (
            users_query.order_by(
                User.signup_status.asc(),
                User.email_verified.asc(),
                User.id.desc(),
            ).all()
        )
        pending_users = (
            User.query.filter_by(signup_status=SIGNUP_STATUS_PENDING)
            .order_by(User.email_verified.asc(), User.verification_sent_at.asc(), User.id.asc())
            .limit(6)
            .all()
        )
        user_ids = [user.id for user in users]
        accounts_by_user = {user.id: [] for user in users}
        if user_ids:
            account_rows = (
                TradeAccount.query.filter(TradeAccount.user_id.in_(user_ids))
                .order_by(
                    TradeAccount.user_id.asc(),
                    TradeAccount.is_default.desc(),
                    TradeAccount.id.asc(),
                )
                .all()
            )
            for account in account_rows:
                accounts_by_user.setdefault(account.user_id, []).append(account)

        return render_admin_page(
            admin_user=admin_user,
            section="users",
            users=users,
            status_filter=status_filter,
            pending_users=pending_users,
            accounts_by_user=accounts_by_user,
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/regenerate-ai-advice", methods=["POST"])
    @root_admin_required
    def admin_regenerate_ai_advice(user_id):
        admin_user = get_current_root_admin_user()

        target_user = User.query.filter_by(id=user_id).first_or_404()
        trade_account_id = request.form.get("trade_account_id", type=int)
        if not trade_account_id:
            return build_admin_redirect("users", "No trade account selected.", "info")

        account = TradeAccount.query.filter_by(id=trade_account_id, user_id=user_id).first_or_404()
        period = get_latest_trade_week_period(user_id=user_id, trade_account_id=account.id)
        if not period:
            return build_admin_redirect(
                "users",
                f"No active trading period found for {target_user.email} / {account.name}.",
                "info",
            )

        try:
            from celery_workers.cache import CacheUnavailableError, clear_ai_status
            from celery_workers.tasks import generate_weekly_ai_task

            try:
                clear_ai_status(
                    user_id,
                    trade_account_id=account.id,
                    period_start_utc=period["period_start_utc"],
                )
            except CacheUnavailableError:
                pass

            generate_weekly_ai_task.delay(
                user_id,
                account.id,
                "dashboard_advice.txt",
                period["period_start_utc"].isoformat(),
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "Admin AI regeneration unavailable: user_id=%s trade_account_id=%s error=%s",
                user_id,
                trade_account_id,
                exc,
            )
            occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            app_env = os.getenv("APP_ENV", "").strip() or os.getenv("FLASK_ENV", "").strip() or "unknown"
            subject = f"[FX Journal Error] {type(exc).__name__} on admin_regenerate_ai_advice"
            body = (
                "Handled admin AI regeneration failure\n\n"
                f"Occurred at: {occurred_at}\n"
                f"Environment: {app_env}\n"
                f"Method: {request.method}\n"
                f"Endpoint: {request.endpoint or '-'}\n"
                f"Route pattern: {request.url_rule.rule if request.url_rule else '-'}\n"
                f"Admin user ID: {getattr(admin_user, 'id', '-')}\n"
                f"Target user ID: {user_id}\n"
                f"Target user email: {target_user.email}\n"
                f"Trade account ID: {account.id}\n"
                f"Trade account name: {account.name}\n"
                f"Exception type: {type(exc).__name__}\n"
                f"Exception message: {sanitize_error_message(exc)}\n"
            )
            send_error_log_email(subject=subject, body=body)
            return build_admin_redirect(
                "users",
                "AI advice regeneration is temporarily unavailable. Please try again in a little while.",
                "error",
            )

        return build_admin_redirect(
            "users",
            f"AI review generation queued for {target_user.email} / {account.name}.",
            "success",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/approve", methods=["POST"])
    @root_admin_required
    def admin_signup_approve_user(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")

        current_status = normalize_signup_status(user.signup_status)
        if current_status == SIGNUP_STATUS_APPROVED:
            return build_admin_redirect("users", "That user is already approved.", "info")

        user.signup_status = SIGNUP_STATUS_APPROVED
        user.approved_at = utcnow_naive()
        user.approved_by_user_id = admin_user.id
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"{'Reactivated' if current_status in {SIGNUP_STATUS_REJECTED, SIGNUP_STATUS_SUSPENDED} else 'Approved'} {user.username}.",
            "success",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/reject", methods=["POST"])
    @root_admin_required
    def admin_signup_reject_user(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")

        if user.id == admin_user.id:
            return build_admin_redirect("users", "You cannot reject your own account.", "error")

        current_status = normalize_signup_status(user.signup_status)
        if current_status != SIGNUP_STATUS_PENDING:
            return build_admin_redirect(
                "users",
                "Only pending signups can be rejected. Approved accounts should be suspended instead.",
                "error",
            )

        user.signup_status = SIGNUP_STATUS_REJECTED
        user.approved_at = None
        user.approved_by_user_id = None
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"Rejected {user.username}.",
            "info",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/suspend", methods=["POST"])
    @root_admin_required
    def admin_signup_suspend_user(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")

        if user.id == admin_user.id:
            return build_admin_redirect("users", "You cannot suspend your own account.", "error")

        current_status = normalize_signup_status(user.signup_status)
        if current_status != SIGNUP_STATUS_APPROVED:
            return build_admin_redirect(
                "users",
                "Only approved accounts can be suspended.",
                "error",
            )

        user.signup_status = SIGNUP_STATUS_SUSPENDED
        user.approved_at = None
        user.approved_by_user_id = None
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"Suspended {user.username}.",
            "info",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/admin-toggle", methods=["POST"])
    @root_admin_required
    def admin_signup_toggle_user_admin(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")
        if user.id == admin_user.id:
            return build_admin_redirect(
                "users",
                "Use ADMIN_USER_EMAILS to control your own root access; this toggle is for other users.",
                "error",
            )
        if is_root_admin_email(user.email):
            return build_admin_redirect(
                "users",
                "Root admin emails keep access from environment settings and cannot be changed here.",
                "error",
            )
        if normalize_signup_status(user.signup_status) != SIGNUP_STATUS_APPROVED:
            return build_admin_redirect(
                "users",
                "Only approved users can be granted admin access.",
                "error",
            )

        user.is_admin = not user.is_admin
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"{'Granted' if user.is_admin else 'Removed'} admin access for {user.username}.",
            "success",
        )

    @app.route("/dashboard/admin/access/codes")
    @admin_required
    def admin_signup_codes():
        admin_user = get_current_admin_user()

        signup_codes = (
            SignupCode.query.order_by(SignupCode.created_at.desc(), SignupCode.id.desc()).all()
        )
        return render_admin_page(
            admin_user=admin_user,
            section="codes",
            signup_codes=signup_codes,
        )

    @app.route("/dashboard/admin/access/codes/create", methods=["POST"])
    @root_admin_required
    def admin_signup_create_code():
        admin_user = get_current_root_admin_user()

        requested_code = normalize_signup_code(request.form.get("code"))
        notes = (request.form.get("notes") or "").strip() or None
        max_uses_raw = (request.form.get("max_uses") or "").strip()
        expires_on_raw = (request.form.get("expires_on") or "").strip()

        if requested_code and len(requested_code) < 4:
            return build_admin_redirect("codes", "Manual codes must be at least 4 characters.", "error")

        if max_uses_raw:
            try:
                max_uses = int(max_uses_raw)
            except ValueError:
                return build_admin_redirect("codes", "Max uses must be a whole number.", "error")
            if max_uses <= 0:
                return build_admin_redirect("codes", "Max uses must be greater than zero.", "error")
        else:
            max_uses = None

        if expires_on_raw:
            try:
                expires_at = datetime.fromisoformat(f"{expires_on_raw}T23:59:59")
            except ValueError:
                return build_admin_redirect("codes", "Expiry date is invalid.", "error")
        else:
            expires_at = None

        code_value = requested_code or build_unique_signup_code()
        existing_code = SignupCode.query.filter_by(code=code_value).first()
        if existing_code:
            return build_admin_redirect("codes", "That signup code already exists.", "error")

        signup_code = SignupCode(
            code=code_value,
            created_by_user_id=admin_user.id,
            notes=notes,
            is_active=True,
            max_uses=max_uses,
            used_count=0,
            expires_at=expires_at,
        )
        db.session.add(signup_code)
        db.session.commit()
        return build_admin_redirect(
            "codes",
            f"Created signup code {signup_code.code}.",
            "success",
        )

    @app.route("/dashboard/admin/access/codes/<int:code_id>/toggle", methods=["POST"])
    @root_admin_required
    def admin_signup_toggle_code(code_id):
        signup_code = SignupCode.query.filter_by(id=code_id).first()
        if not signup_code:
            return build_admin_redirect("codes", "Signup code not found.", "error")

        signup_code.is_active = not signup_code.is_active
        db.session.commit()
        return build_admin_redirect(
            "codes",
            f"{'Activated' if signup_code.is_active else 'Deactivated'} signup code {signup_code.code}.",
            "success",
        )

    @app.route("/password/forgot", methods=["GET", "POST"])
    @limiter.limit(
        "5 per minute;20 per hour",
        methods=["POST"],
        error_message="Too many attempts. Please wait and try again.",
    )
    def forgot_password():
        success = ""
        error = ""
        debug_reset_link = ""

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            generic_success = (
                "Password reset request received. If the address matches an account, the email is on its way."
            )
            if not email:
                error = "Email is required."
            else:
                user = User.query.filter_by(email=email).first()
                if user:
                    reset_token = generate_auth_token(
                        email=user.email,
                        purpose=token_purpose_password_reset,
                    )
                    reset_link = build_external_url(
                        url_for("reset_password_token", token=reset_token)
                    )
                    email_subject = "Reset your FX Journal password"
                    email_body = (
                        f"Hi {user.username},\n\n"
                        "You requested a password reset.\n"
                        "Open this link to set a new password:\n"
                        f"{reset_link}\n\n"
                        "If you did not request this, you can ignore this email."
                    )
                    email_result = send_email_placeholder(user.email, email_subject, email_body)
                    if is_local_dev_environment() and not email_result.get("sent"):
                        debug_reset_link = reset_link
                success = generic_success

        return render_template(
            "forgot_password.html",
            title="Forgot Password | FX Journal",
            body_class="auth-layout",
            success=success or None,
            error=error or None,
            debug_reset_link=debug_reset_link,
        )

    @app.route("/password/reset/<token>", methods=["GET", "POST"])
    def reset_password_token(token):
        max_age_seconds = env_int("PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS", 3600)
        email = verify_auth_token(
            token=token,
            purpose=token_purpose_password_reset,
            max_age_seconds=max_age_seconds,
        )
        if not email:
            return render_template(
                "reset_password.html",
                title="Reset Password | FX Journal",
                body_class="auth-layout",
                error="This password reset link is invalid or has expired.",
                token_valid=False,
            )

        user = User.query.filter_by(email=email).first()
        if not user:
            return render_template(
                "reset_password.html",
                title="Reset Password | FX Journal",
                body_class="auth-layout",
                error="This password reset link is invalid or has expired.",
                token_valid=False,
            )

        if request.method == "POST":
            new_password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not new_password or not confirm_password:
                return render_template(
                    "reset_password.html",
                    title="Reset Password | FX Journal",
                    body_class="auth-layout",
                    error="Both password fields are required.",
                    token_valid=True,
                )
            if new_password != confirm_password:
                return render_template(
                    "reset_password.html",
                    title="Reset Password | FX Journal",
                    body_class="auth-layout",
                    error="Passwords do not match.",
                    token_valid=True,
                )
            if len(new_password) < 8:
                return render_template(
                    "reset_password.html",
                    title="Reset Password | FX Journal",
                    body_class="auth-layout",
                    error="Password must be at least 8 characters.",
                    token_valid=True,
                )

            user.password = generate_password_hash(new_password)
            db.session.commit()
            flash("Password reset successful. Please log in.", "success")
            return redirect(url_for("login"))

        return render_template(
            "reset_password.html",
            title="Reset Password | FX Journal",
            body_class="auth-layout",
            token_valid=True,
        )

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))
