from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import limiter
from helpers import (
    build_unique_trade_account_pubkey,
    get_active_trade_account_for_user,
    get_safe_internal_next,
    get_user_trade_account_by_pubkey,
    get_user_trade_accounts,
    normalize_trade_account_name,
    parse_trade_account_size,
    resolve_active_trade_account,
)
from models import AIGeneratedResponse, Trade, TradeAccount, db
from trading import get_account_type_choices, normalize_account_type
from utils import TRUE_VALUES, login_required

bp = Blueprint("trade_accounts", __name__)


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
    deleted_ai_review_count = AIGeneratedResponse.query.filter_by(
        user_id=user_id,
        trade_account_id=account.id,
    ).count()
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

    review_msg = (
        f" and {deleted_ai_review_count} linked AI review"
        f"{'' if deleted_ai_review_count == 1 else 's'}"
        if deleted_ai_review_count
        else ""
    )
    replacement_msg = " A fresh Main Account was created." if replacement_created else ""
    success_message = (
        f"Deleted trade account '{deleted_name}', {deleted_trade_count} linked trade"
        f"{'' if deleted_trade_count == 1 else 's'}{review_msg}."
        f"{replacement_msg}"
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
    ai_review_count = AIGeneratedResponse.query.filter_by(user_id=user_id).count()
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
        f"Deleted {account_count} trade accounts, {trade_count} linked trades, and "
        f"{ai_review_count} linked AI review{'s' if ai_review_count != 1 else ''}. "
        "A fresh Main Account was created.",
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
    )
