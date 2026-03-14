import os
import re

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy.exc import IntegrityError, OperationalError

from auth_account import send_email_placeholder
from extensions import limiter
from helpers.core import is_local_dev_environment
from models import ContactSubmission, User, db
from helpers.utils import utcnow_naive

bp = Blueprint("contact", __name__)

CONTACT_CATEGORY_CHOICES = (
    "Feedback",
    "Bug Report",
    "Privacy / Data Issue",
    "Account / Access Issue",
    "General Enquiry",
    "Other",
)
CONTACT_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.route("/contact", methods=["GET", "POST"])
@limiter.limit(
    "3 per minute;20 per day",
    methods=["POST"],
    error_message="Too many contact submissions. Please wait and try again later.",
)
def contact():
    def persist_contact_submission(*, actor_user_id, actor_email, category, subject, body, email_result):
        submission = ContactSubmission(
            user_id=actor_user_id,
            contact_email=actor_email,
            category=category,
            subject=subject,
            body=body,
            delivery_sent=bool(email_result.get("sent")),
            delivery_mode=(email_result.get("mode") or "unknown")[:32],
        )
        try:
            db.session.add(submission)
            db.session.commit()
            return True
        except (IntegrityError, OperationalError):
            db.session.rollback()
            current_app.logger.exception(
                "Contact submission fallback persistence failed for category=%s subject=%s",
                category,
                subject,
            )
            return False

    user = None
    if session.get("user_id"):
        user = User.query.filter_by(id=session["user_id"]).first()

    contact_subject = ""
    contact_category = CONTACT_CATEGORY_CHOICES[0]
    contact_body = ""
    contact_email = user.email if user else ""

    def render_contact(code=200):
        return (
            render_template(
                "contact.html",
                title="Contact | FX Journal",
                username=session.get("username", "User"),
                contact_subject=contact_subject,
                contact_category=contact_category,
                contact_body=contact_body,
                contact_email=contact_email,
                contact_category_choices=CONTACT_CATEGORY_CHOICES,
                account_user=user,
            ),
            code,
        )

    if request.method == "POST":
        contact_subject = request.form.get("subject", "").strip()
        contact_category = request.form.get("category", "").strip()
        contact_body = request.form.get("message", "").strip()
        contact_email = (request.form.get("contact_email", "").strip().lower() if not user else user.email)

        if contact_category not in CONTACT_CATEGORY_CHOICES:
            flash("Please choose a valid contact category.", "error")
            return render_contact(400)

        if not contact_subject or not contact_body or (not user and not contact_email):
            flash(
                "Email, subject, and message are required."
                if not user
                else "Subject and message are required.",
                "error",
            )
            return render_contact(400)

        if len(contact_subject) > 120:
            flash("Subject must be 120 characters or less.", "error")
            return render_contact(400)

        if len(contact_body) > 5000:
            flash("Message must be 5000 characters or less.", "error")
            return render_contact(400)

        if not user and (
            len(contact_email) > 120 or not CONTACT_EMAIL_RE.match(contact_email)
        ):
            flash("Enter a valid contact email address.", "error")
            return render_contact(400)

        contact_to_email = os.getenv("FEEDBACK_TO_EMAIL", "").strip().lower()
        if not contact_to_email:
            current_app.logger.warning(
                "Contact submit blocked: FEEDBACK_TO_EMAIL is not configured."
            )
            flash("Contact email destination is not configured yet.", "error")
            return render_contact(500)

        submitted_at = utcnow_naive().strftime("%Y-%m-%d %H:%M:%S UTC")
        requester_ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr
            or "-"
        )
        submitted_user_id = request.form.get("contact_user_id", "").strip()
        submitted_user_email = request.form.get("contact_user_email", "").strip().lower()
        if user:
            actor_name = user.username
            actor_user_id = user.id
            actor_user_id_display = str(user.id)
            actor_email = user.email
            actor_kind = "authenticated"
        else:
            actor_name = "Guest"
            actor_user_id = None
            actor_user_id_display = submitted_user_id or "-"
            actor_email = contact_email
            actor_kind = "guest"
        email_subject = f"[FX Journal Contact] [{contact_category}] {contact_subject}"
        email_body = (
            "New contact submission\n\n"
            f"Submitted at: {submitted_at}\n"
            f"Contact type: {actor_kind}\n"
            f"Name: {actor_name}\n"
            f"User ID: {actor_user_id_display}\n"
            f"Contact email: {actor_email}\n"
            f"Submitted hidden user email: {submitted_user_email or '-'}\n"
            f"Client IP: {requester_ip}\n"
            f"Category: {contact_category}\n"
            f"Subject: {contact_subject}\n\n"
            "Message:\n"
            f"{contact_body}\n"
        )
        email_result = send_email_placeholder(
            contact_to_email,
            email_subject,
            email_body,
        )

        if email_result.get("sent"):
            flash("Your message has been sent. I will review it as soon as possible.", "success")
            return redirect(url_for("contact.contact"))

        persisted = persist_contact_submission(
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            category=contact_category,
            subject=contact_subject,
            body=contact_body,
            email_result=email_result,
        )
        if not persisted and not is_local_dev_environment():
            flash(
                "Your message could not be delivered or saved right now. Please try again later.",
                "error",
            )
            return render_contact(503)

        if is_local_dev_environment():
            flash("Message captured in server logs. Email delivery is disabled in this environment.", "info")
            return redirect(url_for("contact.contact"))

        flash("Your message was received, but email delivery is currently unavailable.", "info")
        return redirect(url_for("contact.contact"))

    return render_template(
        "contact.html",
        title="Contact | FX Journal",
        username=session.get("username", "User"),
        contact_subject=contact_subject,
        contact_category=contact_category,
        contact_body=contact_body,
        contact_email=contact_email,
        contact_category_choices=CONTACT_CATEGORY_CHOICES,
        account_user=user,
    )
