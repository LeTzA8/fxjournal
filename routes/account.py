from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, OperationalError

from auth_account import (
    ONBOARDING_EXPERIENCE_LEVEL_OPTIONS,
    ONBOARDING_INSTRUMENT_OPTIONS,
    ONBOARDING_TRADING_STYLE_OPTIONS,
    VALID_ONBOARDING_EXPERIENCE_LEVELS,
    VALID_ONBOARDING_INSTRUMENTS,
    VALID_ONBOARDING_TRADING_STYLES,
    build_external_url,
    generate_email_change_token,
    generate_password_reset_token,
    rotate_password_reset_nonce,
    send_email_placeholder,
    verify_email_change_token,
)
from extensions import limiter
from helpers.core import delete_users_with_related_data, is_local_dev_environment
from models import MT5Account, User, UserProfile, db
from helpers.utils import env_int, login_required, utcnow_naive

TOKEN_PURPOSE_VERIFY_EMAIL = "verify_email"
TOKEN_PURPOSE_PASSWORD_RESET = "password_reset"

TRADING_STYLE_LABELS = {
    "scalper": "Scalper",
    "intraday": "Intraday",
    "swing": "Swing",
    "position": "Position",
}
INSTRUMENT_LABELS = {
    "forex": "Forex only",
    "indices": "Indices only",
    "gold": "Gold / Commodities",
    "mixed": "Mixed",
}
EXPERIENCE_LEVEL_LABELS = {
    "beginner": "Just starting out",
    "intermediate": "Some experience",
    "experienced": "Experienced",
}
TRADING_STYLE_HINTS = {
    option["value"]: option.get("hint", "") for option in ONBOARDING_TRADING_STYLE_OPTIONS
}
INSTRUMENT_HINTS = {
    option["value"]: option.get("hint", "") for option in ONBOARDING_INSTRUMENT_OPTIONS
}
EXPERIENCE_LEVEL_HINTS = {
    option["value"]: option.get("hint", "") for option in ONBOARDING_EXPERIENCE_LEVEL_OPTIONS
}
PROFILE_FIELD_HELP = {
    "trading_style": "Sets the timeframe lens the AI uses when it talks about pacing, holding time, and execution rhythm.",
    "instruments": "Guides whether examples lean more FX-specific, index-focused, commodity-focused, or mixed across markets.",
    "experience_level": "Controls jargon depth and how much the AI should explain versus assume.",
}
PROFILE_EMPTY_HINTS = {
    "trading_style": "Pick the closest usual holding period.",
    "instruments": "Choose the markets you trade most often.",
    "experience_level": "Choose the level that matches your real comfort today.",
}

bp = Blueprint("account", __name__)


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    user = User.query.filter_by(id=session["user_id"]).first_or_404()
    profile = user.user_profile

    if request.method == "POST":
        form_name = request.form.get("form_name", "account_details").strip().lower()

        if form_name == "trading_profile":
            trading_style = request.form.get("trading_style", "").strip().lower()
            instruments = request.form.get("instruments", "").strip().lower()
            experience_level = request.form.get("experience_level", "").strip().lower()

            if (
                trading_style not in VALID_ONBOARDING_TRADING_STYLES
                or instruments not in VALID_ONBOARDING_INSTRUMENTS
                or experience_level not in VALID_ONBOARDING_EXPERIENCE_LEVELS
            ):
                flash("Choose one option for trading style, markets, and experience level.", "error")
                return redirect(url_for("account.account"))

            if profile is None:
                profile = UserProfile(user_id=user.id)
                db.session.add(profile)

            profile.trading_style = trading_style
            profile.instruments = instruments
            profile.experience_level = experience_level
            profile.completed_at = profile.completed_at or utcnow_naive()
            profile.skipped = False

            try:
                db.session.commit()
            except OperationalError:
                db.session.rollback()
                flash("Could not update your trading profile right now. Please try again.", "error")
                return redirect(url_for("account.account"))

            flash("Trading profile updated successfully.", "success")
            return redirect(url_for("account.account"))

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not username or not email:
            flash("Username and email are required.", "error")
            return redirect(url_for("account.account"))

        conflicting_filters = [User.username == username]
        if email != user.email:
            conflicting_filters.extend(
                [
                    User.email == email,
                    User.pending_email == email,
                ]
            )
        conflicting_user = User.query.filter(
            User.id != user.id,
            or_(*conflicting_filters),
        ).first()
        if conflicting_user:
            flash("Username or email is already in use.", "error")
            return redirect(url_for("account.account"))

        username_changed = user.username != username
        email_changed = user.email != email
        user.username = username

        debug_email_change_current_link = ""
        debug_email_change_new_link = ""

        if email_changed:
            if not user.email_verified:
                flash("Please verify your current email address before requesting an email change.", "error")
                return redirect(url_for("account.account"))

            user.pending_email = email
            user.pending_email_change_requested_at = utcnow_naive()
            user.pending_email_change_current_verified_at = None
            user.pending_email_change_new_verified_at = None

        try:
            db.session.commit()
        except OperationalError:
            db.session.rollback()
            flash("Could not save account changes. Please try again.", "error")
            return redirect(url_for("account.account"))

        if username_changed:
            session["username"] = username

        if email_changed:
            current_email_token = generate_email_change_token(
                user_id=user.id,
                current_email=user.email,
                new_email=email,
                channel="current",
            )
            new_email_token = generate_email_change_token(
                user_id=user.id,
                current_email=user.email,
                new_email=email,
                channel="new",
            )
            current_email_link = build_external_url(
                url_for("account.account_confirm_email_change", token=current_email_token)
            )
            new_email_link = build_external_url(
                url_for("account.account_confirm_email_change", token=new_email_token)
            )

            current_email_result = send_email_placeholder(
                user.email,
                "Confirm your current FX Journal email",
                (
                    f"Hi {user.username},\n\n"
                    "You requested an email change for your FX Journal account.\n"
                    "Confirm from your current email address using this link:\n"
                    f"{current_email_link}\n\n"
                    "Your new email will not be applied until both addresses are verified.\n"
                    "If you did not request this, you can ignore this email."
                ),
                html_body=render_template(
                    "emails/confirm-email-change.html",
                    title="Confirm your current email",
                    badge_label="Email change",
                    heading="Confirm your current email",
                    intro=(
                        f"Hi {user.username}, confirm this request from your current "
                        "email address before we can apply the new one."
                    ),
                    confirm_url=current_email_link,
                    button_label="Confirm Current Email",
                    detail="Your new email will not be applied until both addresses are verified.",
                    footer_note="If you did not request this change, you can safely ignore this email.",
                    logo_url=build_external_url("/static/site-logo.png"),
                ),
            )
            new_email_result = send_email_placeholder(
                email,
                "Confirm your new FX Journal email",
                (
                    f"Hi {user.username},\n\n"
                    "You requested to use this email for your FX Journal account.\n"
                    "Confirm your new email address using this link:\n"
                    f"{new_email_link}\n\n"
                    "Your new email will not be applied until both addresses are verified.\n"
                    "If you did not request this, you can ignore this email."
                ),
                html_body=render_template(
                    "emails/confirm-email-change.html",
                    title="Confirm your new email",
                    badge_label="Email change",
                    heading="Confirm your new email",
                    intro=(
                        f"Hi {user.username}, confirm that you want to use this new "
                        "email address for your FX Journal account."
                    ),
                    confirm_url=new_email_link,
                    button_label="Confirm New Email",
                    detail="Your new email will not be applied until both addresses are verified.",
                    footer_note="If you did not request this change, you can safely ignore this email.",
                    logo_url=build_external_url("/static/site-logo.png"),
                ),
            )

            flash("Email change requested. Please confirm from both your current email and your new email to complete the update.", "info")
            kwargs = {}
            if is_local_dev_environment():
                if not current_email_result.get("sent"):
                    kwargs["debug_email_change_current_link"] = current_email_link
                if not new_email_result.get("sent"):
                    kwargs["debug_email_change_new_link"] = new_email_link
            return redirect(url_for("account.account", **kwargs))

        flash("Account details updated successfully.", "success")
        return redirect(url_for("account.account"))
    debug_reset_link = request.args.get("debug_reset_link", "").strip()
    debug_email_change_current_link = request.args.get("debug_email_change_current_link", "").strip()
    debug_email_change_new_link = request.args.get("debug_email_change_new_link", "").strip()
    if not (
        debug_reset_link.startswith("http://")
        or debug_reset_link.startswith("https://")
    ):
        debug_reset_link = ""
    if not (
        debug_email_change_current_link.startswith("http://")
        or debug_email_change_current_link.startswith("https://")
    ):
        debug_email_change_current_link = ""
    if not (
        debug_email_change_new_link.startswith("http://")
        or debug_email_change_new_link.startswith("https://")
    ):
        debug_email_change_new_link = ""

    onboarding_completed = profile is not None and getattr(profile, "completed_at", None) is not None
    onboarding_was_skipped = bool(
        profile is not None and getattr(profile, "skipped", False) and not onboarding_completed
    )
    profile_values = {
        "trading_style": str(getattr(profile, "trading_style", "") or "").strip().lower(),
        "instruments": str(getattr(profile, "instruments", "") or "").strip().lower(),
        "experience_level": str(getattr(profile, "experience_level", "") or "").strip().lower(),
    }
    trading_profile = {
        "trading_style": TRADING_STYLE_LABELS.get(profile_values["trading_style"], "Not set"),
        "instruments": INSTRUMENT_LABELS.get(profile_values["instruments"], "Not set"),
        "experience_level": EXPERIENCE_LEVEL_LABELS.get(profile_values["experience_level"], "Not set"),
    }
    trading_profile_cards = (
        {
            "label": "Trading style",
            "value": trading_profile["trading_style"],
            "value_hint": TRADING_STYLE_HINTS.get(
                profile_values["trading_style"],
                PROFILE_EMPTY_HINTS["trading_style"],
            ),
            "help": PROFILE_FIELD_HELP["trading_style"],
        },
        {
            "label": "Markets",
            "value": trading_profile["instruments"],
            "value_hint": INSTRUMENT_HINTS.get(
                profile_values["instruments"],
                PROFILE_EMPTY_HINTS["instruments"],
            ),
            "help": PROFILE_FIELD_HELP["instruments"],
        },
        {
            "label": "Experience",
            "value": trading_profile["experience_level"],
            "value_hint": EXPERIENCE_LEVEL_HINTS.get(
                profile_values["experience_level"],
                PROFILE_EMPTY_HINTS["experience_level"],
            ),
            "help": PROFILE_FIELD_HELP["experience_level"],
        },
    )

    return render_template(
        "account.html",
        title="MyFXJournal | My Account",
        username=session.get("username", "User"),
        account_user=user,
        email_verified=bool(user.email_verified),
        debug_reset_link=debug_reset_link or None,
        debug_email_change_current_link=debug_email_change_current_link or None,
        debug_email_change_new_link=debug_email_change_new_link or None,
        pending_email=user.pending_email,
        pending_email_change_requested_at=user.pending_email_change_requested_at,
        pending_email_change_current_verified=bool(user.pending_email_change_current_verified_at),
        pending_email_change_new_verified=bool(user.pending_email_change_new_verified_at),
        onboarding_completed=onboarding_completed,
        onboarding_was_skipped=onboarding_was_skipped,
        trading_profile=trading_profile,
        trading_profile_cards=trading_profile_cards,
        profile_values=profile_values,
        trading_style_options=ONBOARDING_TRADING_STYLE_OPTIONS,
        instrument_options=ONBOARDING_INSTRUMENT_OPTIONS,
        experience_level_options=ONBOARDING_EXPERIENCE_LEVEL_OPTIONS,
        user_trade_accounts=getattr(g, "user_trade_accounts", []),
        active_trade_account=getattr(g, "active_trade_account", None),
    )


@bp.route("/account/email-change/cancel", methods=["POST"])
@limiter.limit(
    "5 per minute;20 per hour",
    methods=["POST"],
    error_message="Too many attempts. Please wait and try again.",
)
@login_required
def account_cancel_email_change():
    user = User.query.filter_by(id=session["user_id"]).first_or_404()
    if not user.pending_email:
        flash("There is no pending email change to cancel.", "info")
        return redirect(url_for("account.account"))

    user.pending_email = None
    user.pending_email_change_requested_at = None
    user.pending_email_change_current_verified_at = None
    user.pending_email_change_new_verified_at = None
    db.session.commit()

    flash("Pending email change canceled.", "success")
    return redirect(url_for("account.account"))


@bp.route("/account/email-change/<token>")
def account_confirm_email_change(token):
    max_age_seconds = env_int("EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS", 86400)
    payload = verify_email_change_token(token, max_age_seconds=max_age_seconds)
    if not payload:
        flash("This email change link is invalid or has expired.", "error")
        return redirect(url_for("account.account"))

    user = User.query.filter_by(id=payload["user_id"]).first()
    if not user:
        flash("This email change link is invalid or has expired.", "error")
        return redirect(url_for("login"))

    if user.email != payload["current_email"] or user.pending_email != payload["new_email"]:
        if session.get("user_id") == user.id:
            flash("This email change request is no longer active.", "error")
            return redirect(url_for("account.account"))
        flash("This email change request is no longer active.", "error")
        return redirect(url_for("login"))

    now_utc = utcnow_naive()
    if payload["channel"] == "current":
        user.pending_email_change_current_verified_at = user.pending_email_change_current_verified_at or now_utc
    else:
        user.pending_email_change_new_verified_at = user.pending_email_change_new_verified_at or now_utc

    if user.pending_email_change_current_verified_at and user.pending_email_change_new_verified_at:
        conflicting_user = User.query.filter(
            User.id != user.id,
            or_(User.email == user.pending_email, User.pending_email == user.pending_email),
        ).first()
        if conflicting_user:
            user.pending_email = None
            user.pending_email_change_requested_at = None
            user.pending_email_change_current_verified_at = None
            user.pending_email_change_new_verified_at = None
            db.session.commit()
            flash("That new email address is no longer available. Please start the email change again.", "error")
            if session.get("user_id") == user.id:
                return redirect(url_for("account.account"))
            return redirect(url_for("login"))

        user.email = user.pending_email
        user.email_verified = True
        user.pending_email = None
        user.pending_email_change_requested_at = None
        user.pending_email_change_current_verified_at = None
        user.pending_email_change_new_verified_at = None
        db.session.commit()

        if session.get("user_id") == user.id:
            flash("Email address updated successfully.", "success")
            return redirect(url_for("account.account"))
        flash("Your email address has been updated. You can now sign in with the new email.", "success")
        return redirect(url_for("login"))

    db.session.commit()
    if session.get("user_id") == user.id:
        flash("One verification step is complete. Please confirm from the other email address to finish the change.", "info")
        return redirect(url_for("account.account"))
    flash("One verification step is complete. Please confirm from the other email address to finish the change.", "info")
    return redirect(url_for("login"))


@bp.route("/account/password-reset-email", methods=["POST"])
@limiter.limit(
    "5 per minute;20 per hour",
    methods=["POST"],
    error_message="Too many attempts. Please wait and try again.",
)
@login_required
def account_password_reset_email():
    user = User.query.filter_by(id=session["user_id"]).first_or_404()
    reset_nonce = rotate_password_reset_nonce(user)
    try:
        db.session.commit()
    except OperationalError as exc:
        db.session.rollback()
        current_app.logger.warning(
            "Account password reset nonce persistence failed for user_id=%s: %s",
            user.id,
            exc,
        )
        flash("Password reset is temporarily unavailable. Please try again shortly.", "error")
        return redirect(url_for("account.account"))

    reset_token = generate_password_reset_token(
        user.email,
        TOKEN_PURPOSE_PASSWORD_RESET,
        reset_nonce,
    )
    reset_link = build_external_url(url_for("reset_password_token", token=reset_token))
    email_subject = "Reset your FX Journal password"
    email_body = (
        f"Hi {user.username},\n\n"
        "You requested a password reset.\n"
        "Open this link to set a new password:\n"
        f"{reset_link}\n\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = render_template(
        "emails/password-reset.html",
        name=user.username,
        reset_url=reset_link,
        logo_url=build_external_url("/static/site-logo.png"),
    )
    email_result = send_email_placeholder(
        user.email,
        email_subject,
        email_body,
        html_body=html_body,
    )

    if email_result.get("sent"):
        account_message = "Password reset email sent to your account email address."
    elif is_local_dev_environment():
        account_message = (
            "Password reset link captured in server logs. Email delivery is disabled in this environment."
        )
    else:
        account_message = "Password reset requested, but email delivery is currently unavailable."

    flash(account_message, "info")
    kwargs = {}
    if is_local_dev_environment() and not email_result.get("sent"):
        kwargs["debug_reset_link"] = reset_link
    return redirect(url_for("account.account", **kwargs))


@bp.route("/account/delete", methods=["POST"])
@limiter.limit(
    "2 per hour",
    methods=["POST"],
    error_message="Too many account deletion attempts. Please wait and try again.",
)
@login_required
def delete_account():
    user = User.query.filter_by(id=session["user_id"]).first()
    if not user:
        session.clear()
        return redirect(url_for("login"))

    confirmation_text = request.form.get("delete_confirmation", "").strip().upper()
    acknowledged = request.form.get("delete_acknowledge") == "on"
    if confirmation_text != "DELETE" or not acknowledged:
        flash("To delete your account, type DELETE and check the confirmation box.", "error")
        return redirect(url_for("account.account"))

    linked_mt5_count = MT5Account.query.filter_by(user_id=user.id).count()

    try:
        delete_users_with_related_data([user.id])
        db.session.commit()
    except (OperationalError, IntegrityError):
        db.session.rollback()
        flash("Could not delete your account right now. Please try again.", "error")
        return redirect(url_for("account.account"))

    session.clear()
    cleanup_note = (
        " Limited cleanup-only MT5 terminal records may remain temporarily until terminal cleanup is completed."
        if linked_mt5_count
        else ""
    )
    flash(
        "Your account and all related trades, trade accounts, and AI reviews have been deleted."
        f"{cleanup_note}",
        "success",
    )
    return redirect(url_for("login"))
