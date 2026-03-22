import os

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError

from auth_account import send_email_placeholder
from extensions import limiter
from helpers.core import (
    build_mt5_access_state,
    build_unique_trade_account_pubkey,
    get_active_trade_account_for_user,
    get_safe_internal_next,
    get_user_trade_account_by_pubkey,
    get_user_trade_accounts,
    normalize_trade_account_name,
    parse_trade_account_size,
    resolve_active_trade_account,
)
from helpers.legal import LEGAL_LAST_UPDATED
from models import AIGeneratedResponse, MT5AccessRequest, MT5Account, Trade, TradeAccount, db
from trading import get_account_type_choices, normalize_account_type
from helpers.utils import TRUE_VALUES, encrypt_password, login_required, utcnow_naive

bp = Blueprint("trade_accounts", __name__)


def _get_mt5_access_redirect_target():
    return get_safe_internal_next("dashboard.home")


@bp.route("/dashboard/trade-accounts/switch", methods=["POST"])
@login_required
def switch_trade_account():
    requested_account_id = request.form.get("trade_account_id", "").strip()
    requested_account_pubkey = request.form.get("trade_account_pubkey", "").strip()
    resolve_active_trade_account(
        session["user_id"],
        requested_account_id=requested_account_id,
        requested_account_pubkey=requested_account_pubkey,
    )
    next_path = request.form.get("next", "").strip()
    if next_path.startswith("/") and not next_path.startswith("//"):
        return redirect(next_path)
    return redirect(request.referrer or url_for("dashboard.home"))


@bp.route("/dashboard/trade-accounts", methods=["POST"])
@limiter.limit(
    "6 per minute;30 per hour",
    methods=["POST"],
    error_message="Too many trade account actions. Please wait and try again.",
)
@login_required
def create_trade_account():
    user_id = session["user_id"]
    account_name = normalize_trade_account_name(request.form.get("trade_account_name"))
    account_type = normalize_account_type(request.form.get("account_type"))
    external_account_id = normalize_trade_account_name(
        request.form.get("external_account_id")
    )
    try:
        account_size = parse_trade_account_size(request.form.get("account_size"))
    except ValueError:
        flash("Account size must be a positive number or left blank.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    if not account_name:
        flash("Trade account name is required.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))
    if len(account_name) > 80:
        flash("Trade account name must be 80 characters or less.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))
    if external_account_id and len(external_account_id) > 80:
        flash("External account ID must be 80 characters or less.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    existing_accounts = get_user_trade_accounts(user_id)
    lowered_name = account_name.lower()
    if any(
        normalize_trade_account_name(account.name).lower() == lowered_name
        for account in existing_accounts
    ):
        flash("A trade account with that name already exists.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    lowered_external = external_account_id.lower()
    if external_account_id and any(
        normalize_trade_account_name(account.external_account_id).lower()
        == lowered_external
        for account in existing_accounts
        if account.external_account_id
    ):
        flash("That external account ID is already linked.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    wants_default = (
        request.form.get("set_as_default", "").strip().lower() in TRUE_VALUES
    )
    is_default = wants_default or not existing_accounts
    if is_default:
        for account in existing_accounts:
            account.is_default = False

    new_account = TradeAccount(
        pubkey=build_unique_trade_account_pubkey(),
        user_id=user_id,
        name=account_name,
        external_account_id=external_account_id or None,
        account_size=account_size,
        account_type=account_type,
        is_default=is_default,
    )
    db.session.add(new_account)
    db.session.commit()

    session["active_trade_account_id"] = new_account.id
    flash(f"Trade account '{new_account.name}' created successfully.", "success")
    return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))


@bp.route("/dashboard/trade-accounts/<string:trade_account_pubkey>/default", methods=["POST"])
@login_required
def set_default_trade_account(trade_account_pubkey):
    user_id = session["user_id"]
    selected = get_user_trade_account_by_pubkey(user_id, trade_account_pubkey)
    if not selected:
        flash("Trade account not found.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    TradeAccount.query.filter_by(user_id=user_id).update(
        {"is_default": False},
        synchronize_session=False,
    )
    selected.is_default = True
    db.session.commit()
    session["active_trade_account_id"] = selected.id

    flash(f"Default trade account updated to '{selected.name}'.", "info")
    return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))


@bp.route("/dashboard/trade-accounts/<string:trade_account_pubkey>/update", methods=["POST"])
@login_required
def update_trade_account(trade_account_pubkey):
    user_id = session["user_id"]
    account = get_user_trade_account_by_pubkey(user_id, trade_account_pubkey)
    if not account:
        flash("Trade account not found.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    account_name = normalize_trade_account_name(request.form.get("trade_account_name"))
    account_type = normalize_account_type(request.form.get("account_type"))
    external_account_id = normalize_trade_account_name(
        request.form.get("external_account_id")
    )
    try:
        account_size = parse_trade_account_size(request.form.get("account_size"))
    except ValueError:
        flash("Account size must be a positive number or left blank.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    if not account_name:
        flash("Trade account name is required.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))
    if len(account_name) > 80:
        flash("Trade account name must be 80 characters or less.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))
    if external_account_id and len(external_account_id) > 80:
        flash("External account ID must be 80 characters or less.", "error")
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    existing_accounts = get_user_trade_accounts(user_id)
    lowered_name = account_name.lower()
    for existing in existing_accounts:
        if existing.id == account.id:
            continue
        if normalize_trade_account_name(existing.name).lower() == lowered_name:
            flash("A trade account with that name already exists.", "error")
            return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    lowered_external = external_account_id.lower()
    if external_account_id:
        for existing in existing_accounts:
            if existing.id == account.id or not existing.external_account_id:
                continue
            if (
                normalize_trade_account_name(existing.external_account_id).lower()
                == lowered_external
            ):
                flash("That external account ID is already linked.", "error")
                return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    account.name = account_name
    account.external_account_id = external_account_id or None
    account.account_size = account_size
    account.account_type = account_type
    db.session.commit()
    flash(f"Trade account '{account.name}' updated successfully.", "success")
    return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))


@bp.route("/dashboard/mt5/request-access", methods=["POST"])
@bp.route("/dashboard/trade-accounts/<string:trade_account_pubkey>/request-mt5-access", methods=["POST"])
@limiter.limit(
    "3 per minute;20 per day",
    methods=["POST"],
    error_message="Too many MT5 access requests. Please wait and try again later.",
)
@login_required
def request_mt5_access(trade_account_pubkey=None):
    user_id = session["user_id"]
    selected_pubkey = (trade_account_pubkey or request.form.get("trade_account_pubkey") or "").strip()
    if not selected_pubkey:
        flash("Choose a CFD trade account first.", "error")
        return redirect(_get_mt5_access_redirect_target())

    account = get_user_trade_account_by_pubkey(user_id, selected_pubkey)
    if not account:
        flash("Trade account not found.", "error")
        return redirect(_get_mt5_access_redirect_target())

    if normalize_account_type(account.account_type) != "CFD":
        flash("MT5 sync access can only be requested for CFD trade accounts.", "error")
        return redirect(_get_mt5_access_redirect_target())

    mt5_access_state = build_mt5_access_state(user_id, [account])
    if account.id in mt5_access_state["linked_mt5_trade_account_ids"]:
        flash("This trade account already has MT5 sync access configured.", "error")
        return redirect(_get_mt5_access_redirect_target())

    if account.id in mt5_access_state["pending_requests_by_trade_account"]:
        flash("An MT5 sync access request is already pending for this trade account.", "error")
        return redirect(_get_mt5_access_redirect_target())
    if account.id in mt5_access_state["approved_requests_by_trade_account"]:
        flash(
            "This trade account is already approved for MT5 sync. Submit your MT5 read-only account details below.",
            "info",
        )
        return redirect(_get_mt5_access_redirect_target())

    request_note = (request.form.get("request_note") or "").strip()
    if len(request_note) > 500:
        flash("MT5 request note must be 500 characters or less.", "error")
        return redirect(_get_mt5_access_redirect_target())

    request_row = MT5AccessRequest(
        user_id=user_id,
        trade_account_id=account.id,
        status=MT5AccessRequest.STATUS_PENDING,
        request_note=request_note or None,
    )

    try:
        db.session.add(request_row)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("An MT5 sync access request is already pending for this trade account.", "error")
        return redirect(_get_mt5_access_redirect_target())
    except OperationalError:
        db.session.rollback()
        flash("Could not submit that MT5 sync access request right now. Please try again.", "error")
        return redirect(_get_mt5_access_redirect_target())

    feedback_to_email = os.getenv("FEEDBACK_TO_EMAIL", "").strip().lower()
    email_sent = False
    if feedback_to_email:
        email_subject = f"[FX Journal MT5 Request] {session.get('username', 'User')} requested access"
        email_body = (
            "New MT5 sync access request\n\n"
            f"Request ID: {request_row.id}\n"
            f"Submitted at: {request_row.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"User ID: {user_id}\n"
            f"Username: {session.get('username', 'User')}\n"
            f"Trade Account ID: {account.id}\n"
            f"Trade Account Name: {account.name}\n"
            f"Trade Account Type: {account.account_type}\n"
            f"Account Pubkey: {account.pubkey}\n"
            f"Note: {request_row.request_note or '-'}\n"
        )
        try:
            email_result = send_email_placeholder(
                feedback_to_email,
                email_subject,
                email_body,
            )
            email_sent = bool((email_result or {}).get("sent"))
        except Exception as exc:
            current_app.logger.warning(
                "MT5 access request email failed for request_id=%s: %s",
                request_row.id,
                exc,
            )
    else:
        current_app.logger.warning(
            "MT5 access request submitted without FEEDBACK_TO_EMAIL configured: request_id=%s",
            request_row.id,
        )

    if email_sent:
        flash("MT5 sync access request submitted. I'll review it soon.", "success")
    else:
        flash(
            "MT5 sync access request submitted and queued for review, but email notification could not be delivered.",
            "info",
        )
    return redirect(_get_mt5_access_redirect_target())


@bp.route("/dashboard/mt5/submit-details", methods=["POST"])
@limiter.limit(
    "3 per minute;20 per day",
    methods=["POST"],
    error_message="Too many MT5 detail submissions. Please wait and try again later.",
)
@login_required
def submit_mt5_details():
    user_id = session["user_id"]
    selected_pubkey = (request.form.get("trade_account_pubkey") or "").strip()
    if not selected_pubkey:
        flash("Choose an approved CFD trade account first.", "error")
        return redirect(_get_mt5_access_redirect_target())

    account = get_user_trade_account_by_pubkey(user_id, selected_pubkey)
    if not account:
        flash("Trade account not found.", "error")
        return redirect(_get_mt5_access_redirect_target())

    if normalize_account_type(account.account_type) != "CFD":
        flash("MT5 sync currently supports CFD trade accounts only.", "error")
        return redirect(_get_mt5_access_redirect_target())

    mt5_access_state = build_mt5_access_state(user_id, [account])
    if account.id in mt5_access_state["linked_mt5_trade_account_ids"]:
        flash("This trade account already has MT5 sync access configured.", "error")
        return redirect(_get_mt5_access_redirect_target())

    approved_request = mt5_access_state["approved_requests_by_trade_account"].get(account.id)
    if approved_request is None:
        if account.id in mt5_access_state["pending_requests_by_trade_account"]:
            flash("This MT5 sync request is still pending review.", "error")
        else:
            flash("This trade account has not been approved for MT5 sync yet.", "error")
        return redirect(_get_mt5_access_redirect_target())

    account_number = (request.form.get("account_number") or "").strip()
    investor_password = request.form.get("investor_password") or ""
    server = (request.form.get("server") or "").strip()
    mt5_sync_consent = (request.form.get("mt5_sync_consent") or "").strip().lower()

    if not account_number or not investor_password or not server:
        flash("Account number, investor password, and server are required.", "error")
        return redirect(_get_mt5_access_redirect_target())
    if mt5_sync_consent not in TRUE_VALUES:
        flash(
            "Confirm that you are submitting MT5 investor/read-only credentials and accept the Terms and Privacy Policy for MT5 sync.",
            "error",
        )
        return redirect(_get_mt5_access_redirect_target())
    if len(account_number) > 50:
        flash("MT5 account number must be 50 characters or less.", "error")
        return redirect(_get_mt5_access_redirect_target())
    if len(server) > 100:
        flash("MT5 server must be 100 characters or less.", "error")
        return redirect(_get_mt5_access_redirect_target())

    try:
        mt5_account = MT5Account(
            user_id=user_id,
            trade_account_id=account.id,
            account_number=account_number,
            investor_password_encrypted=encrypt_password(investor_password),
            server=server,
            terminal_path=None,
            appdata_hash=None,
            is_active=False,
            mt5_consent_accepted_at=utcnow_naive(),
            mt5_consent_version=LEGAL_LAST_UPDATED,
        )
        db.session.add(mt5_account)
        db.session.commit()
    except (RuntimeError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(_get_mt5_access_redirect_target())
    except IntegrityError:
        db.session.rollback()
        flash("This trade account already has MT5 sync access configured.", "error")
        return redirect(_get_mt5_access_redirect_target())
    except OperationalError:
        db.session.rollback()
        flash("Could not save your MT5 account details right now. Please try again.", "error")
        return redirect(_get_mt5_access_redirect_target())

    flash(
        f"MT5 account details saved for {account.name}. I'll finish the onboarding from admin.",
        "success",
    )
    return redirect(_get_mt5_access_redirect_target())


@bp.route("/dashboard/trade-accounts/<string:trade_account_pubkey>/delete", methods=["POST"])
@limiter.limit(
    "3 per minute;10 per hour",
    methods=["POST"],
    error_message="Too many trade account deletion attempts. Please wait and try again.",
)
@login_required
def delete_trade_account(trade_account_pubkey):
    wants_json_response = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    def respond_with_status(status, message, status_code=400):
        redirect_url = get_safe_internal_next("trade_accounts.trade_accounts")
        if wants_json_response:
            payload = {
                "ok": status == "success",
                "message": message,
                "redirect_url": redirect_url,
            }
            return jsonify(payload), status_code
        flash(message, status)
        return redirect(redirect_url)

    user_id = session["user_id"]
    account = get_user_trade_account_by_pubkey(user_id, trade_account_pubkey)
    if not account:
        return respond_with_status("error", "Trade account not found.", 404)

    account_rows = get_user_trade_accounts(user_id)
    remaining_accounts = [row for row in account_rows if row.id != account.id]
    deleted_name = account.name
    deleted_trade_count = Trade.query.filter_by(
        user_id=user_id,
        trade_account_id=account.id,
    ).count()
    linked_mt5_count = MT5Account.query.filter_by(trade_account_id=account.id).count()
    active_trade_account_id = session.get("active_trade_account_id")
    confirmation_text = request.form.get(
        "delete_trade_account_confirmation", ""
    ).strip().upper()
    acknowledged = request.form.get("delete_trade_account_acknowledge") == "on"
    if confirmation_text != "DELETE" or not acknowledged:
        return respond_with_status(
            "error",
            f"To delete '{account.name}', type DELETE and check the confirmation box.",
        )

    try:
        next_active_account = None
        replacement_created = False
        if remaining_accounts:
            if account.is_default:
                for row in remaining_accounts:
                    row.is_default = False
                remaining_accounts[0].is_default = True
            next_active_account = next(
                (row for row in remaining_accounts if row.id == active_trade_account_id),
                None,
            ) or next(
                (row for row in remaining_accounts if row.is_default),
                remaining_accounts[0],
            )

        db.session.delete(account)

        if not remaining_accounts:
            next_active_account = TradeAccount(
                pubkey=build_unique_trade_account_pubkey(),
                user_id=user_id,
                name="Main Account",
                account_type="CFD",
                is_default=True,
            )
            db.session.add(next_active_account)
            db.session.flush()
            replacement_created = True

        db.session.commit()
    except (OperationalError, IntegrityError):
        db.session.rollback()
        return respond_with_status(
            "error",
            "Could not delete that trade account right now. Please try again.",
            500,
        )

    if next_active_account is not None:
        session["active_trade_account_id"] = next_active_account.id
    else:
        session.pop("active_trade_account_id", None)

    replacement_msg = " A fresh Main Account was created." if replacement_created else ""
    cleanup_msg = (
        " Linked MT5 terminal cleanup records were kept temporarily until terminal cleanup is completed."
        if linked_mt5_count
        else ""
    )
    success_message = (
        f"Deleted trade account '{deleted_name}', {deleted_trade_count} linked trade"
        f"{'' if deleted_trade_count == 1 else 's'}."
        f"{replacement_msg}"
        f"{cleanup_msg}"
    )
    if wants_json_response:
        default_trade_account = (
            next_active_account
            if replacement_created
            else next((row for row in remaining_accounts if row.is_default), None)
        )
        redirect_url = get_safe_internal_next("trade_accounts.trade_accounts")
        return jsonify(
            {
                "ok": True,
                "message": success_message,
                "redirect_url": redirect_url,
                "deleted_pubkey": trade_account_pubkey,
                "remaining_account_count": len(remaining_accounts)
                + (1 if replacement_created else 0),
                "active_trade_account_pubkey": (
                    next_active_account.pubkey if next_active_account else None
                ),
                "default_trade_account_pubkey": (
                    default_trade_account.pubkey if default_trade_account else None
                ),
                "replacement_created": replacement_created,
                "requires_reload": replacement_created,
            }
        )
    flash(success_message, "success")
    return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))


@bp.route("/dashboard/trade-accounts/delete-all", methods=["POST"])
@limiter.limit(
    "2 per hour",
    methods=["POST"],
    error_message="Too many destructive account actions. Please wait and try again.",
)
@login_required
def delete_all_trade_accounts():
    user_id = session["user_id"]
    confirmation_text = request.form.get(
        "delete_all_trade_accounts_confirmation", ""
    ).strip().upper()
    acknowledged = request.form.get("delete_all_trade_accounts_acknowledge") == "on"
    if confirmation_text != "DELETE" or not acknowledged:
        flash(
            "Type DELETE and check the confirmation box to remove all trade accounts.",
            "error",
        )
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    trade_count = Trade.query.filter_by(user_id=user_id).count()
    account_count = TradeAccount.query.filter_by(user_id=user_id).count()
    account_ids = [account_id for account_id, in db.session.query(TradeAccount.id).filter_by(user_id=user_id).all()]
    linked_mt5_count = 0
    if account_ids:
        linked_mt5_count = (
            MT5Account.query.filter(MT5Account.trade_account_id.in_(account_ids)).count()
        )
    try:
        orphan_ai_reviews = AIGeneratedResponse.query.filter_by(
            user_id=user_id,
            trade_account_id=None,
        ).all()
        orphan_trades = Trade.query.filter_by(
            user_id=user_id,
            trade_account_id=None,
        ).all()
        account_rows = TradeAccount.query.filter_by(user_id=user_id).all()

        for ai_review in orphan_ai_reviews:
            db.session.delete(ai_review)
        for trade in orphan_trades:
            db.session.delete(trade)
        for account in account_rows:
            db.session.delete(account)

        replacement_account = TradeAccount(
            pubkey=build_unique_trade_account_pubkey(),
            user_id=user_id,
            name="Main Account",
            account_type="CFD",
            is_default=True,
        )
        db.session.add(replacement_account)
        db.session.commit()
    except (OperationalError, IntegrityError):
        db.session.rollback()
        flash(
            "Could not delete every trade account right now. Please try again.",
            "error",
        )
        return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))

    session["active_trade_account_id"] = replacement_account.id

    flash(
        f"Deleted {account_count} trade account{'s' if account_count != 1 else ''} and "
        f"{trade_count} linked trade{'s' if trade_count != 1 else ''}. "
        "A fresh Main Account was created."
        + (
            " Linked MT5 terminal cleanup records were kept temporarily until terminal cleanup is completed."
            if linked_mt5_count
            else ""
        ),
        "success",
    )
    return redirect(get_safe_internal_next("trade_accounts.trade_accounts"))


@bp.route("/dashboard/trade-accounts")
@login_required
def trade_accounts():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_rows = get_user_trade_accounts(user_id)
    account_trade_counts = dict(
        db.session.query(Trade.trade_account_id, func.count(Trade.id))
        .filter_by(user_id=user_id)
        .group_by(Trade.trade_account_id)
        .all()
    )
    account_review_counts = dict(
        db.session.query(
            AIGeneratedResponse.trade_account_id,
            func.count(AIGeneratedResponse.id),
        )
        .filter_by(user_id=user_id)
        .group_by(AIGeneratedResponse.trade_account_id)
        .all()
    )
    mt5_access_state = build_mt5_access_state(user_id, account_rows)
    edit_pubkey = request.args.get("edit", "").strip() or request.args.get(
        "edit_id", ""
    ).strip()
    delete_pubkey = request.args.get("delete", "").strip() or request.args.get(
        "delete_id", ""
    ).strip()
    edit_target = None
    delete_target = None
    delete_target_trade_count = 0
    delete_target_ai_review_count = 0
    if edit_pubkey:
        edit_target = next(
            (account for account in account_rows if account.pubkey == edit_pubkey),
            None,
        )
    if delete_pubkey:
        delete_target = next(
            (account for account in account_rows if account.pubkey == delete_pubkey),
            None,
        )
    if delete_target:
        delete_target_trade_count = account_trade_counts.get(delete_target.id, 0)
        delete_target_ai_review_count = account_review_counts.get(delete_target.id, 0)
    total_trade_count = Trade.query.filter_by(user_id=user_id).count()
    total_ai_review_count = AIGeneratedResponse.query.filter_by(user_id=user_id).count()

    return render_template(
        "trade_accounts.html",
        title="Trade Accounts | FX Journal",
        username=session.get("username", "User"),
        account_rows=account_rows,
        account_trade_counts=account_trade_counts,
        account_review_counts=account_review_counts,
        account_type_choices=get_account_type_choices(),
        edit_target=edit_target,
        delete_target=delete_target,
        delete_target_trade_count=delete_target_trade_count,
        delete_target_ai_review_count=delete_target_ai_review_count,
        total_trade_count=total_trade_count,
        total_ai_review_count=total_ai_review_count,
        active_trade_account=active_trade_account,
        pending_mt5_requests_by_trade_account=mt5_access_state["pending_requests_by_trade_account"],
        approved_mt5_requests_by_trade_account=mt5_access_state["approved_requests_by_trade_account"],
        linked_mt5_trade_account_ids=mt5_access_state["linked_mt5_trade_account_ids"],
    )
