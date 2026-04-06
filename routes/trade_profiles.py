from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError

from helpers.core import (
    get_active_trade_account_for_user,
    get_user_trade_profiles,
    get_user_trade_profile_by_pubkey,
    get_trade_profile_version_snapshot,
    create_trade_profile,
    update_trade_profile,
)
from models import db
from helpers.utils import login_required, utcnow_naive

bp = Blueprint("trade_profiles", __name__)


@bp.route("/dashboard/strategies", methods=["GET", "POST"])
@bp.route("/dashboard/trade-profiles", methods=["GET", "POST"])
@login_required
def strategies():
    user_id = session["user_id"]
    username = session.get("username", "User")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        short_description = request.form.get("short_description", "").strip()
        try:
            create_trade_profile(user_id, name, short_description)
            db.session.commit()
            flash("Strategy created successfully.", "success")
            return redirect(url_for("trade_profiles.strategies"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("trade_profiles.strategies"))
        except (OperationalError, IntegrityError):
            db.session.rollback()
            flash("Could not create the strategy right now. Please try again.", "error")
            return redirect(url_for("trade_profiles.strategies"))

    profiles = get_user_trade_profiles(user_id)
    edit_pubkey = request.args.get("edit", "").strip()
    edit_target = next((profile for profile in profiles if profile.pubkey == edit_pubkey), None)
    profile_versions = {}
    for profile in profiles:
        profile_versions[profile.id] = get_trade_profile_version_snapshot(profile)

    return render_template(
        "trade_profiles.html",
        title="MyFXJournal | Strategies",
        username=username,
        trade_profiles=profiles,
        profile_versions=profile_versions,
        edit_target=edit_target,
    )


@bp.route("/dashboard/strategies/<string:profile_pubkey>/edit", methods=["POST"])
@bp.route("/dashboard/trade-profiles/<string:profile_pubkey>/edit", methods=["POST"])
@login_required
def edit_strategy(profile_pubkey):
    user_id = session["user_id"]
    profile = get_user_trade_profile_by_pubkey(user_id, profile_pubkey)
    if profile is None:
        flash("Strategy not found.", "error")
        return redirect(url_for("trade_profiles.strategies"))

    name = request.form.get("name", "").strip()
    short_description = request.form.get("short_description", "").strip()
    try:
        update_trade_profile(profile, name, short_description)
        db.session.commit()
        flash("Strategy updated successfully and saved as a new version.", "success")
        return redirect(url_for("trade_profiles.strategies"))
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("trade_profiles.strategies", edit=profile.pubkey))
    except (OperationalError, IntegrityError):
        db.session.rollback()
        flash("Could not update the strategy right now. Please try again.", "error")
        return redirect(url_for("trade_profiles.strategies", edit=profile.pubkey))


@bp.route("/dashboard/strategies/<string:profile_pubkey>/set-default", methods=["POST"])
@bp.route("/dashboard/trade-profiles/<string:profile_pubkey>/set-default", methods=["POST"])
@login_required
def set_default_strategy_for_active_account(profile_pubkey):
    user_id = session["user_id"]
    profile = get_user_trade_profile_by_pubkey(user_id, profile_pubkey)
    if profile is None:
        flash("Strategy not found.", "error")
        return redirect(url_for("trade_profiles.strategies"))

    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        flash("No active trade account is selected.", "error")
        return redirect(url_for("trade_profiles.strategies"))

    active_trade_account.default_trade_profile_id = profile.id
    db.session.commit()
    flash(
        f"Default strategy for '{active_trade_account.name}' set to '{profile.name}'.",
        "success",
    )
    return redirect(url_for("trade_profiles.strategies"))


@bp.route("/dashboard/strategies/<string:profile_pubkey>/archive", methods=["POST"])
@bp.route("/dashboard/trade-profiles/<string:profile_pubkey>/archive", methods=["POST"])
@login_required
def archive_strategy(profile_pubkey):
    user_id = session["user_id"]
    profile = get_user_trade_profile_by_pubkey(user_id, profile_pubkey)
    if profile is None:
        flash("Strategy not found.", "error")
        return redirect(url_for("trade_profiles.strategies"))

    profile.is_archived = True
    profile.updated_at = utcnow_naive()
    db.session.commit()
    flash("Strategy archived successfully.", "success")
    return redirect(url_for("trade_profiles.strategies"))
