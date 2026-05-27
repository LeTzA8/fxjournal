"""View-model helpers for Trade Accounts and Strategies workbench pages."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func

from helpers.utils import utcnow_naive
from models import Trade, db
from trading import normalize_account_type


def _format_short_datetime(value):
    if value is None:
        return None
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _resolve_account_mt5_state(
    trade_account,
    *,
    pending_request,
    approved_request,
    mt5_account,
    is_linked,
    is_active_linked,
    mt5_trial_state,
):
    is_cfd = normalize_account_type(trade_account.account_type) == "CFD"
    if not is_cfd:
        return {
            "is_cfd": False,
            "chip_label": None,
            "chip_tone": None,
            "status_key": None,
            "status_one_liner": None,
            "needs_attention": False,
            "show_mt5_block": False,
            "dashboard_cta_label": None,
            "dashboard_cta_href": None,
            "last_sync_label": None,
            "last_sync_at": None,
            "show_disconnect_form": False,
            "disconnect_action": None,
            "show_reactivate_form": False,
            "waitlist_lock": None,
        }

    is_archived = bool(mt5_account and mt5_account.is_archived)
    is_failed = bool(
        mt5_account
        and mt5_account.is_connection_failed
        and not is_active_linked
        and not is_archived
    )
    has_setup_artifacts = bool(
        mt5_account
        and not is_failed
        and (
            str(getattr(mt5_account, "terminal_path", "") or "").strip()
            or str(getattr(mt5_account, "appdata_hash", "") or "").strip()
        )
    )
    is_paused = bool(mt5_trial_state and mt5_trial_state.get("state") == "paused")
    if mt5_account is not None and not is_paused:
        from helpers.entitlements import is_mt5_sync_paused

        is_paused = is_mt5_sync_paused(mt5_account)

    waitlist_lock = None
    if mt5_trial_state and mt5_trial_state.get("show_trial_ui"):
        trial_state = mt5_trial_state.get("state")
        if trial_state == "expired":
            waitlist_lock = "expired"
        elif trial_state == "paused" or is_paused:
            waitlist_lock = "paused"

    last_sync_at = getattr(mt5_account, "last_synced_at", None) if mt5_account else None
    last_sync_label = _format_short_datetime(last_sync_at)

    chip_label = None
    chip_tone = "default"
    status_key = None
    status_one_liner = None
    needs_attention = False
    show_mt5_block = False
    dashboard_cta_label = None
    dashboard_cta_href = "dashboard.home#mt5-access"

    if is_paused:
        chip_label = "MT5 Sync Paused"
        chip_tone = "warn"
        status_key = "paused"
        status_one_liner = "MT5 sync is paused. Your trade history is safe."
        needs_attention = True
        show_mt5_block = True
        dashboard_cta_label = "Manage MT5"
    elif is_active_linked:
        chip_label = "MT5 Linked"
        chip_tone = "ok"
        status_key = "linked"
    elif is_archived:
        chip_label = "MT5 Sync Inactive"
        chip_tone = "default"
        status_key = "archived"
        status_one_liner = "MT5 sync is inactive due to inactivity."
        needs_attention = True
        show_mt5_block = True
    elif is_failed:
        chip_label = "MT5 Connection Failed"
        chip_tone = "error"
        status_key = "failed"
        status_one_liner = (
            getattr(mt5_account, "connection_error_message", None)
            or "MT5 connection failed. Fix your details and retry."
        )
        needs_attention = True
        show_mt5_block = True
        dashboard_cta_label = "Fix on Dashboard"
    elif is_linked and has_setup_artifacts:
        chip_label = "MT5 Setting Up"
        chip_tone = "default"
        status_key = "setting_up"
        status_one_liner = "Terminal setup is in progress. We'll email you when sync is ready."
        needs_attention = True
        show_mt5_block = True
        dashboard_cta_label = "Manage MT5"
    elif is_linked:
        chip_label = "MT5 Setup Queued"
        chip_tone = "default"
        status_key = "queued"
        status_one_liner = "MT5 setup is queued from the dashboard flow."
        needs_attention = True
        show_mt5_block = True
        dashboard_cta_label = "Manage MT5"
    elif pending_request or approved_request:
        chip_label = "MT5 Needs Details"
        chip_tone = "default"
        status_key = "needs_details"
        status_one_liner = "Finish MT5 setup from the dashboard card."
        needs_attention = True
        show_mt5_block = True
        dashboard_cta_label = "Manage MT5"
    else:
        chip_label = None
        status_key = "disconnected"
        status_one_liner = "Connect MT5 from the dashboard for automatic sync."
        needs_attention = True
        show_mt5_block = True
        dashboard_cta_label = "Manage MT5"

    show_disconnect_form = bool(mt5_account)
    disconnect_action = "reactivate" if is_archived else "unlink"
    show_reactivate_form = bool(mt5_account and is_archived)

    if is_active_linked and last_sync_label:
        show_mt5_block = False

    return {
        "is_cfd": True,
        "chip_label": chip_label,
        "chip_tone": chip_tone,
        "status_key": status_key,
        "status_one_liner": status_one_liner,
        "needs_attention": needs_attention,
        "show_mt5_block": show_mt5_block,
        "dashboard_cta_label": dashboard_cta_label,
        "dashboard_cta_href": dashboard_cta_href,
        "last_sync_label": last_sync_label,
        "last_sync_at": last_sync_at,
        "show_disconnect_form": show_disconnect_form,
        "disconnect_action": disconnect_action,
        "show_reactivate_form": show_reactivate_form,
        "is_archived": is_archived,
        "is_failed": is_failed,
        "is_paused": is_paused,
        "is_active_linked": is_active_linked,
        "waitlist_lock": waitlist_lock,
    }


def build_trade_account_card_views(
    account_rows,
    *,
    active_trade_account,
    account_trade_counts,
    account_review_counts,
    mt5_access_state,
    mt5_trial_states_by_trade_account,
):
    pending_requests = mt5_access_state["pending_requests_by_trade_account"]
    approved_requests = mt5_access_state["approved_requests_by_trade_account"]
    mt5_accounts = mt5_access_state["mt5_accounts_by_trade_account"]
    linked_ids = mt5_access_state["linked_mt5_trade_account_ids"]
    active_linked_ids = mt5_access_state["active_mt5_trade_account_ids"]

    cards = []
    for account in account_rows:
        mt5_account = mt5_accounts.get(account.id)
        mt5_state = _resolve_account_mt5_state(
            account,
            pending_request=pending_requests.get(account.id),
            approved_request=approved_requests.get(account.id),
            mt5_account=mt5_account,
            is_linked=account.id in linked_ids,
            is_active_linked=account.id in active_linked_ids,
            mt5_trial_state=mt5_trial_states_by_trade_account.get(account.id),
        )
        default_profile = getattr(account, "default_trade_profile", None)
        account_size_label = None
        if account.account_size is not None:
            account_size_label = f"${account.account_size:,.2f}"

        cards.append(
            {
                "account": account,
                "is_active": bool(
                    active_trade_account and account.id == active_trade_account.id
                ),
                "is_default": bool(account.is_default),
                "trade_count": int(account_trade_counts.get(account.id, 0)),
                "review_count": int(account_review_counts.get(account.id, 0)),
                "default_strategy_name": default_profile.name if default_profile else None,
                "account_size_label": account_size_label,
                "external_account_id": account.external_account_id or None,
                "account_type_label": (
                    "Futures" if account.account_type == "FUTURES" else "CFD"
                ),
                "mt5": mt5_state,
            }
        )
    return cards


def build_trade_accounts_sidebar(account_cards):
    attention_items = []
    activity_items = []

    for card in account_cards:
        account = card["account"]
        mt5 = card["mt5"]
        if mt5.get("needs_attention"):
            attention_items.append(
                {
                    "account_name": account.name,
                    "account_pubkey": account.pubkey,
                    "status_key": mt5.get("status_key"),
                    "message": mt5.get("status_one_liner") or "Needs attention.",
                    "cta_label": mt5.get("dashboard_cta_label"),
                    "cta_href": mt5.get("dashboard_cta_href"),
                }
            )

        if mt5.get("last_sync_at") is not None:
            activity_items.append(
                {
                    "account_name": account.name,
                    "message": f"Last sync {mt5.get('last_sync_label')}",
                    "sort_at": mt5.get("last_sync_at"),
                }
            )
        elif card["trade_count"] > 0:
            activity_items.append(
                {
                    "account_name": account.name,
                    "message": f"{card['trade_count']} trade{'s' if card['trade_count'] != 1 else ''} logged",
                    "sort_at": None,
                }
            )

    activity_items.sort(
        key=lambda row: row["sort_at"] or datetime.min,
        reverse=True,
    )

    return {
        "attention_items": attention_items,
        "activity_items": activity_items[:6],
        "all_ok": not attention_items,
    }


def build_strategy_usage_counts(user_id, *, trade_account_id=None):
    base_filters = [
        Trade.user_id == user_id,
        Trade.trade_profile_id.isnot(None),
    ]
    if trade_account_id is not None:
        base_filters.append(Trade.trade_account_id == trade_account_id)

    total_rows = (
        db.session.query(Trade.trade_profile_id, func.count(Trade.id))
        .filter(*base_filters)
        .group_by(Trade.trade_profile_id)
        .all()
    )
    total_by_profile = {profile_id: int(count) for profile_id, count in total_rows}

    seven_days_ago = utcnow_naive() - timedelta(days=7)
    recent_rows = (
        db.session.query(Trade.trade_profile_id, func.count(Trade.id))
        .filter(
            *base_filters,
            func.coalesce(Trade.closed_at, Trade.opened_at) >= seven_days_ago,
        )
        .group_by(Trade.trade_profile_id)
        .all()
    )
    recent_by_profile = {profile_id: int(count) for profile_id, count in recent_rows}

    return total_by_profile, recent_by_profile


def build_strategy_card_views(
    profiles,
    *,
    profile_versions,
    active_trade_account,
    total_usage_by_profile,
    recent_usage_by_profile,
):
    cards = []
    for profile in profiles:
        version = profile_versions.get(profile.id)
        description = ""
        if version and version.short_description:
            description = version.short_description
        is_default_for_active = bool(
            active_trade_account
            and active_trade_account.default_trade_profile_id == profile.id
        )
        cards.append(
            {
                "profile": profile,
                "version_number": profile.current_version_number,
                "description": description,
                "last_edited_label": _format_short_datetime(profile.updated_at),
                "usage_total": int(total_usage_by_profile.get(profile.id, 0)),
                "usage_last_7_days": int(recent_usage_by_profile.get(profile.id, 0)),
                "is_default_for_active": is_default_for_active,
            }
        )
    return cards
