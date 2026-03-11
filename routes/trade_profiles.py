from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError

from helpers import (
    get_user_trade_profiles,
    get_user_trade_profile_by_pubkey,
    get_trade_profile_version_snapshot,
    create_trade_profile,
    update_trade_profile,
)
from models import db
from utils import login_required, utcnow_naive

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
            flash("Trade profile created successfully.", "success")
            return redirect(url_for("trade_profiles.strategies"))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("trade_profiles.strategies"))
        except (OperationalError, IntegrityError):
            db.session.rollback()
            flash("Could not create the trade profile right now. Please try again.", "error")
            return redirect(url_for("trade_profiles.strategies"))

    profiles = get_user_trade_profiles(user_id)
    edit_pubkey = request.args.get("edit", "").strip()
    edit_target = next((profile for profile in profiles if profile.pubkey == edit_pubkey), None)
    profile_versions = {}
    for profile in profiles:
        profile_versions[profile.id] = get_trade_profile_version_snapshot(profile)

    return render_template(
        "trade_profiles.html",
        title="Strategies | FX Journal",
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
        flash("Trade profile not found.", "error")
        return redirect(url_for("trade_profiles.strategies"))

    name = request.form.get("name", "").strip()
    short_description = request.form.get("short_description", "").strip()
    try:
        update_trade_profile(profile, name, short_description)
        db.session.commit()
        flash("Trade profile updated successfully and saved as a new version.", "success")
        return redirect(url_for("trade_profiles.strategies"))
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("trade_profiles.strategies", edit=profile.pubkey))
    except (OperationalError, IntegrityError):
        db.session.rollback()
        flash("Could not update the trade profile right now. Please try again.", "error")
        return redirect(url_for("trade_profiles.strategies", edit=profile.pubkey))


@bp.route("/dashboard/strategies/<string:profile_pubkey>/archive", methods=["POST"])
@bp.route("/dashboard/trade-profiles/<string:profile_pubkey>/archive", methods=["POST"])
@login_required
def archive_strategy(profile_pubkey):
    user_id = session["user_id"]
    profile = get_user_trade_profile_by_pubkey(user_id, profile_pubkey)
    if profile is None:
        flash("Trade profile not found.", "error")
        return redirect(url_for("trade_profiles.strategies"))

    profile.is_archived = True
    profile.updated_at = utcnow_naive()
    db.session.commit()
    flash("Trade profile archived successfully.", "success")
    return redirect(url_for("trade_profiles.strategies"))
