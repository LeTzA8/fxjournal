import os
import secrets
from datetime import datetime

from flask import current_app, redirect, render_template, request, session, url_for
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from models import User, db

TOKEN_PURPOSE_PENDING_REGISTRATION = "pending_registration"
PENDING_REGISTRATIONS = {}
DEFAULT_ALLOWED_SIGNUP_EMAIL_DOMAINS = {
    "gmail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "yahoo.co.uk",
    "yahoo.ca",
    "ymail.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "pm.me",
    "gmx.com",
    "mail.com",
}


def _env_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


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
        return DEFAULT_ALLOWED_SIGNUP_EMAIL_DOMAINS
    parsed = {part.strip().lower() for part in raw.split(",") if part and part.strip()}
    return parsed or DEFAULT_ALLOWED_SIGNUP_EMAIL_DOMAINS


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
    now = datetime.utcnow()
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
        "created_at": datetime.utcnow(),
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


def send_email_placeholder(to_email, subject, text_body, html_body=None):
    provider = os.getenv("EMAIL_PROVIDER", "placeholder").strip().lower()
    sender = os.getenv("EMAIL_FROM", "noreply@example.com").strip()
    send_enabled = os.getenv("EMAIL_SEND_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    api_key = os.getenv("RESEND_API_KEY", "").strip() or os.getenv("EMAIL_API_KEY", "").strip()

    if provider in {"console", "placeholder"} or not send_enabled:
        current_app.logger.info(
            "Email placeholder (console/disabled) -> to=%s from=%s subject=%s",
            to_email,
            sender,
            subject,
        )
        current_app.logger.info("Email body:\n%s", text_body)
        return {"sent": False, "mode": "placeholder"}

    if not api_key:
        current_app.logger.warning(
            "EMAIL_SEND_ENABLED is true but RESEND_API_KEY/EMAIL_API_KEY is missing. Using placeholder mode."
        )
        current_app.logger.info("Email body:\n%s", text_body)
        return {"sent": False, "mode": "missing_api_key"}

    if provider == "resend":
        try:
            import resend
        except ImportError:
            current_app.logger.warning("Resend SDK is not installed. Falling back to placeholder logging.")
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
            current_app.logger.info("Email body:\n%s", text_body)
            return {"sent": False, "mode": "resend_error"}

    current_app.logger.warning(
        "Email provider '%s' is configured but integration is not implemented.", provider
    )
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
        error_message="Too many login attempts. Please wait and try again.",
    )
    def login():
        if session.get("user_id"):
            return redirect(url_for("home"))

        verified = request.args.get("verified") == "1"
        reset_done = request.args.get("reset") == "1"
        success_message = request.args.get("success", "").strip()

        if not success_message:
            if reset_done:
                success_message = "Password reset successful. Please log in."
            elif verified:
                success_message = "Email verified. You can now log in."

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password, password):
                if os.getenv("REQUIRE_EMAIL_VERIFICATION", "true").strip().lower() in {"1", "true", "yes", "on"} and not user.email_verified:
                    session["pending_verify_email"] = user.email
                    return render_template(
                        "login.html",
                        title="Login | FX Journal",
                        body_class="auth-layout",
                        error="Please verify your email before logging in.",
                        success=success_message,
                    )
                session["user_id"] = user.id
                session["username"] = user.username
                active_account, _accounts = resolve_active_trade_account(user.id)
                session["active_trade_account_id"] = active_account.id
                return redirect(url_for("home"))

            return render_template(
                "login.html",
                title="Login | FX Journal",
                body_class="auth-layout",
                error="Invalid email or password.",
                success=success_message,
            )
        return render_template(
            "login.html",
            title="Login | FX Journal",
            body_class="auth-layout",
            success=success_message,
        )

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if session.get("user_id"):
            return redirect(url_for("home"))

        error_message = request.args.get("error", "").strip()

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            accepted_legal = request.form.get("accept_legal") == "on"

            if not username or not email or not password:
                return render_template(
                    "register.html",
                    title="Register | FX Journal",
                    body_class="auth-layout",
                    error="All fields are required.",
                )

            if not is_allowed_signup_email_domain(email):
                return render_template(
                    "register.html",
                    title="Register | FX Journal",
                    body_class="auth-layout",
                    error=(
                        "Please use a common email provider "
                        "(for example Gmail, Outlook, Yahoo, iCloud, or Proton)."
                    ),
                )

            if not accepted_legal:
                return render_template(
                    "register.html",
                    title="Register | FX Journal",
                    body_class="auth-layout",
                    error="You must accept the Terms and Conditions and Privacy Policy.",
                )

            existing_user = User.query.filter(
                or_(User.username == username, User.email == email)
            ).first()
            if existing_user:
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
                    verify_kwargs = {"sent": "1"}
                    if is_local_dev_environment() and not email_result.get("sent"):
                        verify_kwargs["verify_link"] = verify_link
                    return redirect(url_for("verify_email_pending", **verify_kwargs))
                return render_template(
                    "register.html",
                    title="Register | FX Journal",
                    body_class="auth-layout",
                    error="Username or email already exists.",
                )

            hashed_password = generate_password_hash(password)
            user = User(
                username=username,
                email=email,
                password=hashed_password,
                email_verified=False,
                verification_sent_at=datetime.utcnow(),
            )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return render_template(
                    "register.html",
                    title="Register | FX Journal",
                    body_class="auth-layout",
                    error="Username or email already exists.",
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

            verify_kwargs = {"sent": "1"}
            if is_local_dev_environment() and not email_result.get("sent"):
                verify_kwargs["verify_link"] = verify_link
            return redirect(url_for("verify_email_pending", **verify_kwargs))

        return render_template(
            "register.html",
            title="Register | FX Journal",
            body_class="auth-layout",
            error=error_message or None,
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

        if pending_email:
            user = User.query.filter_by(email=pending_email).first()
            if not user:
                session.pop("pending_verify_email", None)
                pending_email = ""
            elif user.email_verified:
                session.pop("pending_verify_email", None)
                return redirect(url_for("login", verified="1"))
            else:
                pending_username = user.username

        if not pending_email:
            pending_id = session.get("pending_registration_id", "").strip()
            pending = get_pending_registration(pending_id)
            if not pending:
                return redirect(
                    url_for(
                        "register",
                        error="Verification session expired. Please register again.",
                    )
                )
            pending_email = pending["email"]
            pending_username = pending["username"]
            using_legacy_pending = True

        success = (
            "Verification email sent. Please check your inbox."
            if request.args.get("sent") == "1"
            else ""
        )
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
                    return redirect(
                        url_for(
                            "register",
                            error="Verification session expired. Please register again.",
                        )
                    )
                if user.email_verified:
                    session.pop("pending_verify_email", None)
                    return redirect(url_for("login", verified="1"))
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
                return redirect(
                    url_for(
                        "register",
                        error="Verification link is invalid or expired. Please register again.",
                    )
                )

            if pending["email"] != pending_payload["email"]:
                pop_pending_registration(registration_id)
                if session.get("pending_registration_id") == registration_id:
                    session.pop("pending_registration_id", None)
                return redirect(
                    url_for(
                        "register",
                        error="Verification payload mismatch. Please register again.",
                    )
                )

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
                    return redirect(url_for("login", verified="1"))
                return redirect(
                    url_for(
                        "register",
                        error="Username or email is no longer available. Please register again.",
                    )
                )

            user = User(
                username=pending["username"],
                email=pending["email"],
                password=pending["password_hash"],
                email_verified=True,
                verification_sent_at=datetime.utcnow(),
            )
            db.session.add(user)
            db.session.commit()

            pop_pending_registration(registration_id)
            if session.get("pending_registration_id") == registration_id:
                session.pop("pending_registration_id", None)

            return redirect(url_for("login", verified="1"))

        email = verify_auth_token(
            token=token,
            purpose=token_purpose_verify_email,
            max_age_seconds=max_age_seconds,
        )
        if not email:
            return redirect(
                url_for(
                    "register",
                    error="Verification link is invalid or expired. Please register again.",
                )
            )

        user = User.query.filter_by(email=email).first()
        if not user:
            return redirect(
                url_for(
                    "register",
                    error="Verification link is invalid or expired. Please register again.",
                )
            )

        if not user.email_verified:
            user.email_verified = True
            db.session.commit()

        if session.get("pending_verify_email", "").strip().lower() == email:
            session.pop("pending_verify_email", None)

        return redirect(url_for("login", verified="1"))

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
                "If this email exists, a password reset link has been sent."
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
                error="Reset link is invalid or expired.",
                token_valid=False,
            )

        user = User.query.filter_by(email=email).first()
        if not user:
            return render_template(
                "reset_password.html",
                title="Reset Password | FX Journal",
                body_class="auth-layout",
                error="Reset link is invalid or expired.",
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
            return redirect(url_for("login", reset="1"))

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
