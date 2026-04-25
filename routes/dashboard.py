import json
import re
from datetime import datetime, timedelta

from flask import Blueprint, current_app, g, jsonify, render_template, request, session, url_for
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import load_only, selectinload

from ai_service import (
    AIRequestError,
    MIN_CLOSED_TRADES_FOR_ADVICE,
    WEEKLY_REVIEW_CHAT_PROMPT_VERSION,
    WEEKLY_DASHBOARD_KIND,
    build_dashboard_review_display,
    count_closed_trade_ideas_in_period,
    generate_weekly_review_chat_reply,
    get_latest_trade_week_period,
    get_latest_weekly_dashboard_advice,
    get_weekly_dashboard_period,
    normalize_dashboard_advice_text,
    should_generate_weekly_dashboard_advice,
    weekly_review_generation_past_market_week_cutoff,
    weekly_review_text_preserves_display_format,
)
from celery_workers.cache import (
    AI_STATUS_FAILED_TTL,
    AI_STATUS_QUEUED_TTL,
    ANALYTICS_TTL,
    CacheUnavailableError,
    claim_ai_status,
    get_ai_status,
    get_cached,
    set_ai_status,
    set_cached,
)
from helpers.behavior_labels import build_trade_behavior_analytics, build_trade_behavior_badge_map
from helpers.core import (
    build_mt5_access_state,
    get_effective_user_id,
    get_effective_username,
    get_active_trade_account_for_user,
    get_app_timezone_name,
    get_display_timezone_name,
    get_support_view_admin_username,
    get_user_trade_accounts,
    is_weekly_checkin_complete,
    is_support_view_active,
    is_support_view_session_active,
    is_trade_running,
    normalize_timezone_name,
)
from helpers.running_pnl import build_running_pnl_events, summarize_running_pnl
from helpers.trade_analysis import detect_outliers, get_trade_identity
from helpers.trends import trend_direction_ei_scores, trend_direction_expectancy_weeks, trend_direction_win_rate_weeks
from helpers.weekly_review_ref_rewrite import (
    build_weekly_review_citation_lookup as _build_weekly_review_citation_lookup,
    deserialize_datetime_iso as _deserialize_datetime,
    parse_json_blob as _parse_json_blob,
    rewrite_review_text_refs as _rewrite_review_text_refs,
)
from auth_account import build_external_url
from extensions import limiter
from helpers.utils import login_required, utcnow_naive
from models import AccountCashFlow, AIGeneratedResponse, Trade, UserProfile, WeeklyCheckin, WeeklyReviewChatMessage, db
from trading import (
    SMALL_SAMPLE_MIN_TRADES,
    build_rr_summary,
    build_trade_analytics,
    classify_trading_session,
    format_duration_minutes,
    format_trade_symbol,
    resolve_net_pnl,
    to_display_timezone,
)

bp = Blueprint("dashboard", __name__)

DEFAULT_WEEKLY_AI_EMPTY_MESSAGE = (
    f"Weekly review needs at least {MIN_CLOSED_TRADES_FOR_ADVICE} closed trade(s) on this account "
    "in the active review window."
)
WEEKLY_AI_GENERATING_MESSAGE = (
    "Generating your weekly AI review. Check back shortly."
)
WEEKLY_AI_NO_TRADES_MESSAGE = "No trades this week. Add closed trades to generate a review."
WEEKLY_AI_TOO_FEW_TRADES_MESSAGE = (
    "Limited trade data this week, so the review will stay cautious."
)
WEEKLY_AI_UNAVAILABLE_MESSAGE = (
    "Weekly AI review is temporarily unavailable. Please try again shortly."
)
WEEKLY_AI_WAIT_FOR_WEEK_CLOSE_MESSAGE = (
    "Your weekly AI review is scheduled for after Friday 5:30 PM New York time, "
    "once that trading week is complete."
)
WEEKLY_AI_PROMPT_FILENAME = "dashboard_advice.txt"
DASHBOARD_CACHE_PREFIX = "dashboard_v3"
ANALYTICS_CACHE_PREFIX = "analytics_v5"
RR_SUMMARY_CACHE_PREFIX = "rr_summary_v5"
WEEKLY_REVIEW_CHAT_MAX_CHARS = 800
WEEKLY_REVIEW_CHAT_HISTORY_LIMIT = 6


def _serialize_datetime(value):
    if value is None:
        return None
    return value.isoformat()


def _dedupe_review_citations(citations, seen_keys):
    deduped = []
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        citation_type = str(citation.get("type") or "").strip()
        if citation_type == "bundle":
            identity = f"bundle:{citation.get('bundle_key') or ''}"
        else:
            identity = f"trade:{citation.get('trade_id') or ''}"
        if not identity or identity in seen_keys:
            continue
        seen_keys.add(identity)
        deduped.append(citation)
    return deduped


def _augment_citations_from_mentions(text, citations, citation_lookup):
    normalized = str(text or "").strip()
    deduped = _dedupe_review_citations(citations, set())
    if not normalized or not citation_lookup:
        return deduped

    label_groups = {}
    for citation in citation_lookup.values():
        if not isinstance(citation, dict):
            continue
        label = str(citation.get("inline_label") or "").strip().lower()
        if not label:
            continue
        label_groups.setdefault(label, []).append(citation)

    seen_keys = set()
    for citation in deduped:
        citation_type = str(citation.get("type") or "").strip()
        identity = (
            f"bundle:{citation.get('bundle_key') or ''}"
            if citation_type == "bundle"
            else f"trade:{citation.get('trade_id') or ''}"
        )
        if identity:
            seen_keys.add(identity)

    for label, grouped_citations in label_groups.items():
        if len(grouped_citations) != 1:
            continue
        if re.search(rf"\b{re.escape(label)}\b", normalized, flags=re.IGNORECASE) is None:
            continue
        citation = grouped_citations[0]
        citation_type = str(citation.get("type") or "").strip()
        identity = (
            f"bundle:{citation.get('bundle_key') or ''}"
            if citation_type == "bundle"
            else f"trade:{citation.get('trade_id') or ''}"
        )
        if not identity or identity in seen_keys:
            continue
        deduped.append(citation)
        seen_keys.add(identity)

    return deduped


def _build_review_text_segments(text, citations):
    normalized = str(text or "").strip()
    deduped_citations = _dedupe_review_citations(citations, set())
    if not normalized:
        return []

    matches = []
    for citation in deduped_citations:
        full_label = str(citation.get("label") or "").strip()
        short_label = str(citation.get("inline_label") or "").strip()
        candidates = []
        if full_label:
            candidates.append(full_label)
        if short_label and short_label != full_label:
            candidates.append(short_label)
        if not candidates:
            continue

        best_match = None
        for candidate in candidates:
            found = re.search(re.escape(candidate), normalized, flags=re.IGNORECASE)
            if found is None:
                continue
            if best_match is None:
                best_match = found
                continue
            if found.start() < best_match.start():
                best_match = found
            elif found.start() == best_match.start() and found.end() > best_match.end():
                best_match = found

        if best_match is None:
            continue
        matches.append(
            {
                "start": best_match.start(),
                "end": best_match.end(),
                "length": best_match.end() - best_match.start(),
                "citation": citation,
            }
        )

    matches.sort(key=lambda item: (item["start"], -item["length"]))
    selected_matches = []
    last_end = -1
    matched_keys = set()
    for item in matches:
        start = item["start"]
        end = item["end"]
        citation = item["citation"]
        citation_type = str(citation.get("type") or "").strip()
        identity = (
            f"bundle:{citation.get('bundle_key') or ''}"
            if citation_type == "bundle"
            else f"trade:{citation.get('trade_id') or ''}"
        )
        if start < last_end or identity in matched_keys:
            continue
        selected_matches.append(item)
        last_end = end
        matched_keys.add(identity)

    segments = []
    cursor = 0
    for item in selected_matches:
        start = item["start"]
        end = item["end"]
        citation = item["citation"]
        if start > cursor:
            segments.append({"type": "text", "text": normalized[cursor:start]})
        segments.append(
            {
                "type": "citation",
                "label": str(citation.get("label") or citation.get("inline_label") or "").strip(),
                "citation_type": citation.get("type"),
                "trade_id": citation.get("trade_id"),
                "bundle_key": citation.get("bundle_key"),
                "tone": citation.get("tone") or "neutral",
            }
        )
        cursor = end

    if cursor < len(normalized):
        segments.append({"type": "text", "text": normalized[cursor:]})

    unmatched = []
    for citation in deduped_citations:
        citation_type = str(citation.get("type") or "").strip()
        identity = (
            f"bundle:{citation.get('bundle_key') or ''}"
            if citation_type == "bundle"
            else f"trade:{citation.get('trade_id') or ''}"
        )
        if identity and identity not in matched_keys:
            unmatched.append(citation)

    if unmatched:
        if segments:
            segments.append({"type": "text", "text": " "})
        for index, citation in enumerate(unmatched):
            segments.append(
                {
                    "type": "citation",
                    "label": str(citation.get("label") or citation.get("inline_label") or "").strip(),
                    "citation_type": citation.get("type"),
                    "trade_id": citation.get("trade_id"),
                    "bundle_key": citation.get("bundle_key"),
                    "tone": citation.get("tone") or "neutral",
                }
            )
            if index < len(unmatched) - 1:
                segments.append({"type": "text", "text": " "})

    return segments


def _resolve_weekly_review_citations(refs, citation_lookup):
    resolved = []
    seen = set()
    for raw_ref in refs or []:
        ref = str(raw_ref or "").strip().upper()
        if not ref or ref in seen:
            continue
        citation = citation_lookup.get(ref)
        if citation is None:
            continue
        resolved.append(citation)
        seen.add(ref)
    return resolved


def _build_weekly_ai_review_display(review_record, timezone_name):
    if review_record is None:
        return None

    pass_2_output = (
        getattr(review_record, "pass_2_output", None)
        if getattr(review_record, "prompt_version_pass_2", None)
        else None
    )
    if pass_2_output and not weekly_review_text_preserves_display_format(pass_2_output):
        pass_2_output = None
    display_text = pass_2_output or getattr(review_record, "pass_1_output", None) or review_record.response_text or ""
    display_meta_json = None if pass_2_output else getattr(review_record, "response_meta_json", None)
    display = build_dashboard_review_display(
        display_text,
        display_meta_json,
    )
    if pass_2_output:
        original_display = build_dashboard_review_display(
            getattr(review_record, "pass_1_output", None) or review_record.response_text or "",
            getattr(review_record, "response_meta_json", None),
        )
        if not (display.get("experiment") or {}).get("text"):
            display["experiment"] = (original_display.get("experiment") or {})
        if not (display.get("strength") or {}).get("text"):
            display["strength"] = (original_display.get("strength") or {})
    citation_lookup = _build_weekly_review_citation_lookup(
        getattr(review_record, "payload_json", None),
        timezone_name,
    )

    summary = dict(display.get("summary") or {})
    summary["text"] = _rewrite_review_text_refs(summary.get("text"), citation_lookup)
    summary["citations"] = _augment_citations_from_mentions(
        summary.get("text"),
        _resolve_weekly_review_citations(summary.get("refs"), citation_lookup),
        citation_lookup,
    )
    summary["segments"] = _build_review_text_segments(summary.get("text"), summary.get("citations"))

    takeaways = []
    for item in display.get("takeaways") or []:
        takeaway = dict(item or {})
        takeaway["text"] = _rewrite_review_text_refs(takeaway.get("text"), citation_lookup)
        takeaway["citations"] = _augment_citations_from_mentions(
            takeaway.get("text"),
            _resolve_weekly_review_citations(
                takeaway.get("refs"),
                citation_lookup,
            ),
            citation_lookup,
        )
        takeaway["segments"] = _build_review_text_segments(
            takeaway.get("text"),
            takeaway.get("citations"),
        )
        takeaways.append(takeaway)

    improvement = dict(display.get("improvement") or {})
    improvement["text"] = _rewrite_review_text_refs(improvement.get("text"), citation_lookup)
    improvement["citations"] = []
    improvement["segments"] = [{"type": "text", "text": improvement.get("text")}] if improvement.get("text") else []

    strength = dict(display.get("strength") or {})
    strength["text"] = _rewrite_review_text_refs(strength.get("text"), citation_lookup)
    strength["citations"] = []
    strength["segments"] = [{"type": "text", "text": strength.get("text")}] if strength.get("text") else []
    experiment = dict(display.get("experiment") or {})
    experiment["text"] = _rewrite_review_text_refs(experiment.get("text"), citation_lookup)
    experiment["citations"] = []
    experiment["segments"] = [{"type": "text", "text": experiment.get("text")}] if experiment.get("text") else []

    return {
        "summary": summary,
        "takeaways": takeaways,
        "improvement": improvement,
        "strength": strength,
        "experiment": experiment,
        "has_citations": bool(
            summary.get("citations")
            or any(item.get("citations") for item in takeaways)
        ),
    }


def _load_user_trades(user_id, active_trade_account):
    if active_trade_account is None:
        return []
    try:
        return (
            Trade.query.filter_by(
                user_id=user_id,
                trade_account_id=active_trade_account.id,
            )
            .options(
                selectinload(Trade.trade_account),
                selectinload(Trade.trade_profile),
                selectinload(Trade.trade_profile_version),
                selectinload(Trade.interpretation),
            )
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        return []


def _get_review_workflow_banner_state(user_id, active_trade_account, user_trades):
    account_id = getattr(active_trade_account, "id", None)
    if account_id is None:
        return {
            "show_workflow_banner": False,
            "workflow_stage": None,
            "title": "",
            "note": "",
            "button_label": "",
            "button_href": None,
            "show_skip": False,
            "closed_trade_count": 0,
        }

    if _has_bundle_candidates(user_id, active_trade_account):
        return {
            "show_workflow_banner": True,
            "workflow_stage": "bundle_review",
            "title": "Review bundle candidates before anything else.",
            "note": "A bundle review is waiting on your active account. Confirm the older split entries first so the rest of the review flow builds on the correct trade structure.",
            "button_label": "Review Bundles",
            "button_href": url_for("trades.bundle_review"),
            "show_skip": False,
            "closed_trade_count": 0,
        }

    period = get_weekly_dashboard_period(now_utc=utcnow_naive())
    closed_trades = [
        trade
        for trade in user_trades
        if getattr(trade, "closed_at", None) is not None
        and getattr(trade, "trade_account_id", None) == account_id
        and getattr(trade, "closed_at", None) >= period["period_start_utc"]
        and getattr(trade, "closed_at", None) < period["period_end_utc"]
    ]
    closed_trade_count = len(closed_trades)
    if closed_trade_count <= 0:
        return {
            "show_workflow_banner": False,
            "workflow_stage": None,
            "title": "",
            "note": "",
            "button_label": "",
            "button_href": None,
            "show_skip": False,
            "closed_trade_count": 0,
        }

    try:
        existing_checkin = WeeklyCheckin.query.filter_by(
            user_id=user_id,
            trade_account_id=account_id,
            week_start_utc=period["period_start_utc"],
        ).first()
    except OperationalError:
        db.session.rollback()
        existing_checkin = None

    if is_weekly_checkin_complete(existing_checkin):
        return {
            "show_workflow_banner": False,
            "workflow_stage": None,
            "title": "",
            "note": "",
            "button_label": "",
            "button_href": None,
            "show_skip": False,
            "closed_trade_count": closed_trade_count,
        }

    outliers = detect_outliers(closed_trades)
    if outliers["bundle_candidates"]:
        return {
            "show_workflow_banner": True,
            "workflow_stage": "bundle_review",
            "title": "Start with the bundle review for this trade week.",
            "note": f"{closed_trade_count} closed trade{'s' if closed_trade_count != 1 else ''} landed in the latest completed trade week. Confirm any split entries first, then the flow will move into revenge review and the weekly check-in.",
            "button_label": "Review Bundles",
            "button_href": url_for("checkin.checkin"),
            "show_skip": False,
            "closed_trade_count": closed_trade_count,
        }

    if outliers["standalone_candidates"]:
        return {
            "show_workflow_banner": True,
            "workflow_stage": "classification",
            "title": "Review possible revenge sequences next.",
            "note": "The detector found post-loss sequences that may have been revenge-driven. Confirm only the ones that genuinely felt like revenge before the weekly check-in.",
            "button_label": "Review Revenge Signals",
            "button_href": url_for("checkin.checkin"),
            "show_skip": False,
            "closed_trade_count": closed_trade_count,
        }

    weekly_checkin_was_skipped = existing_checkin is not None and not is_weekly_checkin_complete(existing_checkin)
    return {
        "show_workflow_banner": True,
        "workflow_stage": "weekly_checkin",
        "title": (
            "You can still add trader context for the latest completed trade week."
            if weekly_checkin_was_skipped
            else "Add a quick trader context check for the latest completed trade week."
        ),
        "note": (
            "You skipped it earlier, but the door is still open. Adding a short check-in gives the AI better behavior and execution context than trade data alone."
            if weekly_checkin_was_skipped
            else (
                f"{closed_trade_count} closed trade{'s' if closed_trade_count != 1 else ''} landed on your active account in the latest completed trade week. A short check-in helps the AI weight behaviour and execution more accurately."
            )
        ),
        "button_label": "Finish Check-In" if weekly_checkin_was_skipped else "Open Check-In",
        "button_href": url_for("checkin.checkin"),
        "show_skip": not weekly_checkin_was_skipped,
        "closed_trade_count": closed_trade_count,
    }


def _get_onboarding_banner_state(user_id):
    try:
        profile = UserProfile.query.filter_by(user_id=user_id).first()
    except OperationalError:
        db.session.rollback()
        profile = None

    is_complete = profile is not None and getattr(profile, "completed_at", None) is not None
    was_skipped = bool(profile is not None and getattr(profile, "skipped", False) and not is_complete)
    return {
        "show_onboarding_banner": not is_complete,
        "onboarding_was_skipped": was_skipped,
    }


def _has_bundle_candidates(user_id, active_trade_account):
    account_id = getattr(active_trade_account, "id", None)
    if account_id is None:
        return False
    requested_at = getattr(active_trade_account, "bundle_review_requested_at", None)
    completed_at = getattr(active_trade_account, "bundle_review_completed_at", None)
    return requested_at is not None and (completed_at is None or completed_at < requested_at)


def _serialize_dashboard_cache_payload(analytics):
    summary = analytics.get("summary") or {}
    return {
        "summary": {
            "win_rate": summary.get("win_rate"),
            "weekly_pnl": summary.get("weekly_pnl"),
            "net_pnl": summary.get("net_pnl"),
            "avg_win": summary.get("avg_win"),
            "avg_loss_abs": summary.get("avg_loss_abs"),
        },
        "session_stats": analytics.get("session_stats") or [],
        "chart_points": analytics.get("daily_equity_curve") or [],
        "closed_records": [
            {
                "realized_at_local": _serialize_datetime(
                    record.get("realized_at_local") or record.get("opened_at_local")
                ),
                "pnl": record.get("pnl"),
            }
            for record in (analytics.get("closed_records") or [])
        ],
    }


def _deserialize_dashboard_cache_payload(payload):
    payload = payload or {}
    return {
        "summary": payload.get("summary") or {},
        "session_stats": payload.get("session_stats") or [],
        "chart_points": payload.get("chart_points") or [],
        "closed_records": [
            {
                "realized_at_local": _deserialize_datetime(
                    record.get("realized_at_local") or record.get("opened_at_local")
                ),
                "pnl": record.get("pnl"),
            }
            for record in (payload.get("closed_records") or [])
        ],
    }


def _serialize_summary_trade(record):
    if not record:
        return None
    trade = record.get("trade")
    pubkey = (getattr(trade, "pubkey", None) or "").strip() if trade is not None else ""
    return {
        "pnl": record.get("pnl"),
        "symbol": record.get("symbol"),
        "opened_label": record.get("opened_label"),
        "trade_pubkey": pubkey,
    }


def _serialize_analytics_payload(analytics):
    summary = dict(analytics.get("summary") or {})
    summary["best_trade"] = _serialize_summary_trade(summary.get("best_trade"))
    summary["worst_trade"] = _serialize_summary_trade(summary.get("worst_trade"))
    return {
        "summary": summary,
        "streaks": analytics.get("streaks") or {},
        "equity_curve": analytics.get("equity_curve") or [],
        "daily_equity_curve": analytics.get("daily_equity_curve") or [],
        "weekday_stats": analytics.get("weekday_stats") or [],
        "weekday_has_reliable_pattern": analytics.get("weekday_has_reliable_pattern", False),
        "pair_stats": analytics.get("pair_stats") or [],
        "pair_has_reliable_pattern": analytics.get("pair_has_reliable_pattern", False),
        "session_stats": analytics.get("session_stats") or [],
        "session_has_reliable_pattern": analytics.get("session_has_reliable_pattern", False),
        "week_label": analytics.get("week_label") or "",
        "behavior": analytics.get("behavior") or {},
    }


def _get_cached_payload(prefix, user_id, trade_account_id):
    try:
        return get_cached(prefix, user_id, trade_account_id=trade_account_id)
    except CacheUnavailableError as exc:
        current_app.logger.warning("%s cache unavailable: %s", prefix, exc)
        return None


def _set_cached_payload(prefix, user_id, trade_account_id, payload):
    try:
        set_cached(
            prefix,
            user_id,
            payload,
            ANALYTICS_TTL,
            trade_account_id=trade_account_id,
        )
    except CacheUnavailableError as exc:
        current_app.logger.warning("%s cache unavailable: %s", prefix, exc)


def _load_dashboard_analytics(user_id, active_trade_account, user_trades, timezone_name):
    account_id = getattr(active_trade_account, "id", None)
    cached_payload = _get_cached_payload(DASHBOARD_CACHE_PREFIX, user_id, account_id)
    if cached_payload is not None:
        return _deserialize_dashboard_cache_payload(cached_payload)

    analytics = build_trade_analytics(
        user_trades,
        display_timezone_name=timezone_name,
        account_size=active_trade_account.account_size if active_trade_account else None,
    )
    payload = _serialize_dashboard_cache_payload(analytics)
    _set_cached_payload(DASHBOARD_CACHE_PREFIX, user_id, account_id, payload)
    return {
        "summary": payload["summary"],
        "session_stats": payload["session_stats"],
        "chart_points": payload["chart_points"],
        "closed_records": [
            {
                "realized_at_local": record.get("realized_at_local") or record.get("opened_at_local"),
                "pnl": record.get("pnl"),
            }
            for record in (analytics.get("closed_records") or [])
        ],
    }


def _build_cached_analytics_payload(user_id, active_trade_account, user_trades, timezone_name):
    analytics = build_trade_analytics(
        user_trades,
        display_timezone_name=timezone_name,
        account_size=active_trade_account.account_size if active_trade_account else None,
    )
    analytics["behavior"] = build_trade_behavior_analytics(
        user_trades,
        timezone_name=timezone_name,
    )
    payload = _serialize_analytics_payload(analytics)
    _set_cached_payload(ANALYTICS_CACHE_PREFIX, user_id, getattr(active_trade_account, "id", None), payload)
    return payload


def _build_cached_rr_summary(user_id, active_trade_account, user_trades):
    rr_summary = build_rr_summary(user_trades)
    _set_cached_payload(
        RR_SUMMARY_CACHE_PREFIX,
        user_id,
        getattr(active_trade_account, "id", None),
        rr_summary,
    )
    return rr_summary


def _summarize_week(records, start_local, end_local=None):
    weekly_records = []
    for record in records:
        realized_local = record.get("realized_at_local") or record.get("opened_at_local")
        if realized_local is None or realized_local < start_local:
            continue
        if end_local is not None and realized_local >= end_local:
            continue
        weekly_records.append(record)

    trade_count = len(weekly_records)
    wins = sum(1 for record in weekly_records if (record.get("pnl") or 0) > 0)
    net_pnl = sum((record.get("pnl") or 0) for record in weekly_records)
    return {
        "trade_count": trade_count,
        "wins": wins,
        "win_rate": (wins / trade_count * 100.0) if trade_count else None,
        "net_pnl": net_pnl,
        "week_sample_is_reliable": trade_count >= SMALL_SAMPLE_MIN_TRADES,
    }


def _build_performance_trends(closed_records, now_local, min_trades_per_week=3):
    """
    Win rate and expectancy trend over the last four completed weeks (Mon–Mon, local).
    Direction is None when there is not enough non-null weekly data.
    """
    week_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=now_local.weekday()
    )

    weeks = []
    for i in range(1, 5):
        start = week_start - timedelta(weeks=i)
        end = week_start - timedelta(weeks=i - 1)
        week_records = [
            r
            for r in closed_records
            if (r.get("realized_at_local") or r.get("opened_at_local")) is not None
            and start <= (r.get("realized_at_local") or r.get("opened_at_local")) < end
        ]
        count = len(week_records)
        if count < min_trades_per_week:
            weeks.append(None)
            continue
        wins = sum(1 for r in week_records if (r.get("pnl") or 0) > 0)
        net_pnl = sum((r.get("pnl") or 0) for r in week_records)
        weeks.append(
            {
                "win_rate": wins / count * 100.0,
                "expectancy": net_pnl / count,
                "count": count,
            }
        )

    win_rates = [w["win_rate"] if w else None for w in weeks]
    expectancies = [w["expectancy"] if w else None for w in weeks]
    usable_weeks = sum(1 for w in weeks if w is not None)

    return {
        "win_rate_trend": trend_direction_win_rate_weeks(win_rates),
        "expectancy_trend": trend_direction_expectancy_weeks(expectancies),
        "weeks_available": usable_weeks,
        "current_win_rate": weeks[0]["win_rate"] if weeks[0] else None,
        "current_expectancy": weeks[0]["expectancy"] if weeks[0] else None,
    }


def _build_ei_trend(user_id, trade_account_id):
    """Trend from stored weekly dashboard AI payloads (emotional_index.score); lower is better."""
    if user_id is None:
        return {"ei_trend": None, "current_ei_score": None}

    reviews = (
        AIGeneratedResponse.query.filter_by(
            user_id=user_id,
            trade_account_id=trade_account_id,
            kind=WEEKLY_DASHBOARD_KIND,
        )
        .options(load_only(AIGeneratedResponse.payload_json, AIGeneratedResponse.period_start_utc))
        .order_by(AIGeneratedResponse.period_start_utc.desc())
        .limit(4)
        .all()
    )

    scores = []
    for review in reviews:
        try:
            payload = json.loads(review.payload_json or "")
            score = payload.get("emotional_index", {}).get("score")
            if score is not None:
                scores.append(float(score))
            else:
                scores.append(None)
        except (TypeError, ValueError):
            scores.append(None)

    current_ei_score = None
    for entry in scores:
        if entry is not None:
            current_ei_score = entry
            break

    return {
        "ei_trend": trend_direction_ei_scores(scores),
        "current_ei_score": current_ei_score,
    }


def _build_week_on_week_insight(current_week_stats, previous_week_stats):
    if (
        current_week_stats["trade_count"]
        and previous_week_stats["trade_count"]
        and current_week_stats["week_sample_is_reliable"]
        and previous_week_stats["week_sample_is_reliable"]
    ):
        pnl_delta = current_week_stats["net_pnl"] - previous_week_stats["net_pnl"]
        win_rate_delta = (
            current_week_stats["win_rate"] - previous_week_stats["win_rate"]
            if current_week_stats["win_rate"] is not None
            and previous_week_stats["win_rate"] is not None
            else None
        )
        trade_delta = current_week_stats["trade_count"] - previous_week_stats["trade_count"]
        pnl_direction = "up" if pnl_delta > 0 else "down" if pnl_delta < 0 else "flat"
        win_rate_text = (
            f" Win rate {'improved' if win_rate_delta > 0 else 'fell' if win_rate_delta < 0 else 'held flat'} by {abs(win_rate_delta):.1f} pts."
            if win_rate_delta is not None
            else ""
        )
        volume_text = (
            f" Trade count {'increased' if trade_delta > 0 else 'decreased' if trade_delta < 0 else 'matched last week'}"
            + (f" by {abs(trade_delta)}." if trade_delta != 0 else ".")
        )
        return (
            f"Net PnL is {pnl_direction} by ${abs(pnl_delta):.2f} versus last week."
            f"{win_rate_text}{volume_text}"
        )
    if current_week_stats["trade_count"] and previous_week_stats["trade_count"]:
        return (
            f"One or both weeks have fewer than {SMALL_SAMPLE_MIN_TRADES} trades, "
            "so treat this comparison as early context rather than a reliable pattern."
        )
    if current_week_stats["trade_count"]:
        if not current_week_stats["week_sample_is_reliable"]:
            return (
                f"This week has fewer than {SMALL_SAMPLE_MIN_TRADES} trades, "
                "so use it as early context rather than a firm pattern."
            )
        return "This is your first completed trade week with comparable dashboard data."
    return "No prior completed trade week to compare yet."


def _get_weekly_ai_state(user_id, active_trade_account, timezone_name, user_trades, generate=True):
    account_id = getattr(active_trade_account, "id", None)
    weekly_ai_review = None
    weekly_ai_review_text = ""
    weekly_ai_review_display = None
    weekly_ai_generated_at_label = ""
    weekly_ai_period_label = ""
    weekly_ai_empty_message = DEFAULT_WEEKLY_AI_EMPTY_MESSAGE
    weekly_ai_is_generating = False
    displayed_review_period = None

    latest_trade_period = get_latest_trade_week_period(
        user_id=user_id,
        trade_account_id=account_id,
    )
    weekly_period = latest_trade_period or get_weekly_dashboard_period()

    if latest_trade_period is None:
        weekly_ai_empty_message = WEEKLY_AI_NO_TRADES_MESSAGE
    else:
        current_period_review = None
        fallback_review = None
        reviews_available = True
        try:
            current_period_review = get_latest_weekly_dashboard_advice(
                user_id=user_id,
                trade_account_id=account_id,
                period_start_utc=latest_trade_period["period_start_utc"],
            )
            if current_period_review is None:
                fallback_review = get_latest_weekly_dashboard_advice(
                    user_id=user_id,
                    trade_account_id=account_id,
                )
        except OperationalError as exc:
            db.session.rollback()
            reviews_available = False
            current_app.logger.warning("Weekly AI review query unavailable: %s", exc)
            weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE

        weekly_ai_review = current_period_review or fallback_review
        if reviews_available and weekly_ai_review is not None:
            weekly_ai_review_text = normalize_dashboard_advice_text(weekly_ai_review.response_text)
            weekly_ai_review_display = _build_weekly_ai_review_display(
                weekly_ai_review,
                timezone_name,
            )
            if (
                weekly_ai_review.period_start_utc is not None
                or weekly_ai_review.period_end_utc is not None
            ):
                displayed_review_period = {
                    "period_start_utc": weekly_ai_review.period_start_utc,
                    "period_end_utc": weekly_ai_review.period_end_utc,
                }

        ai_status = None
        try:
            ai_status = get_ai_status(
                user_id,
                trade_account_id=account_id,
                period_start_utc=latest_trade_period["period_start_utc"],
            )
        except CacheUnavailableError as exc:
            current_app.logger.warning("Weekly AI status unavailable: %s", exc)

        if reviews_available and current_period_review is None and not generate:
            weekly_ai_empty_message = DEFAULT_WEEKLY_AI_EMPTY_MESSAGE

        if reviews_available and current_period_review is None and generate:
            if ai_status in {"queued", "running"}:
                weekly_ai_is_generating = True
                weekly_ai_empty_message = WEEKLY_AI_GENERATING_MESSAGE
            elif ai_status == "failed":
                weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE
            elif not should_generate_weekly_dashboard_advice(
                user_id=user_id,
                trade_account_id=account_id,
                period_start_utc=latest_trade_period["period_start_utc"],
                period_end_utc=latest_trade_period["period_end_utc"],
            ):
                weekly_ai_empty_message = DEFAULT_WEEKLY_AI_EMPTY_MESSAGE
            elif not weekly_review_generation_past_market_week_cutoff(
                user_id=user_id,
                trade_account_id=account_id,
                period=latest_trade_period,
            ):
                weekly_ai_empty_message = WEEKLY_AI_WAIT_FOR_WEEK_CLOSE_MESSAGE
            else:
                closed_trade_idea_count = count_closed_trade_ideas_in_period(
                    user_id=user_id,
                    trade_account_id=account_id,
                    period_start_utc=latest_trade_period["period_start_utc"],
                    period_end_utc=latest_trade_period["period_end_utc"],
                )
                if closed_trade_idea_count <= 0:
                    weekly_ai_empty_message = WEEKLY_AI_NO_TRADES_MESSAGE
                elif closed_trade_idea_count < MIN_CLOSED_TRADES_FOR_ADVICE:
                    weekly_ai_empty_message = WEEKLY_AI_TOO_FEW_TRADES_MESSAGE
                else:
                    try:
                        claimed = claim_ai_status(
                            user_id,
                            trade_account_id=account_id,
                            period_start_utc=latest_trade_period["period_start_utc"],
                            status="queued",
                            ttl=AI_STATUS_QUEUED_TTL,
                        )
                    except CacheUnavailableError as exc:
                        current_app.logger.warning("Weekly AI queue unavailable: %s", exc)
                        claimed = False
                        weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE

                    if claimed:
                        try:
                            from celery_workers.weekly_tasks import generate_weekly_ai_task

                            generate_weekly_ai_task.delay(
                                user_id,
                                account_id,
                                WEEKLY_AI_PROMPT_FILENAME,
                                _serialize_datetime(latest_trade_period["period_start_utc"]),
                                send_weekly_email=True,
                            )
                        except Exception as exc:
                            try:
                                set_ai_status(
                                    user_id,
                                    trade_account_id=account_id,
                                    period_start_utc=latest_trade_period["period_start_utc"],
                                    status="failed",
                                    ttl=AI_STATUS_FAILED_TTL,
                                )
                            except CacheUnavailableError:
                                pass
                            current_app.logger.warning("Weekly AI review dispatch failed: %s", exc)
                            weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE
                        else:
                            weekly_ai_is_generating = True
                            weekly_ai_empty_message = WEEKLY_AI_GENERATING_MESSAGE
                    else:
                        try:
                            ai_status = get_ai_status(
                                user_id,
                                trade_account_id=account_id,
                                period_start_utc=latest_trade_period["period_start_utc"],
                            )
                        except CacheUnavailableError:
                            ai_status = None
                        if ai_status in {"queued", "running"}:
                            weekly_ai_is_generating = True
                            weekly_ai_empty_message = WEEKLY_AI_GENERATING_MESSAGE
                        elif ai_status == "failed":
                            weekly_ai_empty_message = WEEKLY_AI_UNAVAILABLE_MESSAGE

    if weekly_ai_review and weekly_ai_review.generated_at:
        generated_local = to_display_timezone(weekly_ai_review.generated_at, timezone_name)
        if generated_local is not None:
            weekly_ai_generated_at_label = generated_local.strftime("%d %b %Y %H:%M")

    period_source = displayed_review_period or weekly_period
    period_end_local = to_display_timezone(
        period_source.get("period_end_utc") if period_source else None,
        timezone_name,
    )
    if period_end_local is not None:
        weekly_ai_period_label = (
            f"{period_end_local.strftime('%a %d %b %Y %H:%M')} {timezone_name}"
        )

    return {
        "weekly_ai_review": weekly_ai_review,
        "weekly_ai_review_text": weekly_ai_review_text,
        "weekly_ai_review_display": weekly_ai_review_display,
        "weekly_ai_generated_at_label": weekly_ai_generated_at_label,
        "weekly_ai_period_label": weekly_ai_period_label,
        "weekly_ai_empty_message": weekly_ai_empty_message,
        "weekly_ai_is_generating": weekly_ai_is_generating,
    }


def _build_dashboard_mt5_sections(*, account_rows, active_trade_account, mt5_access_state):
    mt5_cfd_accounts = [
        account
        for account in account_rows
        if str(account.account_type or "").strip().upper() == "CFD"
    ]
    pending_requests_by_trade_account = mt5_access_state["pending_requests_by_trade_account"]
    approved_requests_by_trade_account = mt5_access_state["approved_requests_by_trade_account"]
    mt5_accounts_by_trade_account = mt5_access_state["mt5_accounts_by_trade_account"]
    active_mt5_trade_account_ids = mt5_access_state["active_mt5_trade_account_ids"]
    batch_state = mt5_access_state["batch_state"]

    status_rows = []
    for account in mt5_cfd_accounts:
        pending_request = pending_requests_by_trade_account.get(account.id)
        approved_request = approved_requests_by_trade_account.get(account.id)
        mt5_account = mt5_accounts_by_trade_account.get(account.id)
        is_linked = mt5_account is not None
        is_active_linked = account.id in active_mt5_trade_account_ids
        is_archived = bool(getattr(mt5_account, "is_archived", False))
        has_setup_artifacts = bool(
            str(getattr(mt5_account, "terminal_path", "") or "").strip()
            or str(getattr(mt5_account, "appdata_hash", "") or "").strip()
        )

        if is_active_linked:
            status = "linked"
            status_label = "MT5 Linked"
            note = "MT5 details are already on file for this account."
        elif is_archived:
            status = "archived"
            status_label = "Sync Inactive"
            archived_on = getattr(mt5_account, "archived_at", None)
            archive_date_label = (
                archived_on.strftime("%Y-%m-%d UTC")
                if archived_on is not None
                else "an earlier date"
            )
            note = (
                f"MT5 sync was archived due to inactivity on {archive_date_label}. "
                "Reactivate to rebuild the VM terminal using your saved read-only credentials."
            )
        elif is_linked and getattr(mt5_account, "connection_status", None) == "failed":
            status = "failed"
            status_label = "Connection Failed"
            note = (
                getattr(mt5_account, "connection_error_message", None)
                or "MT5 connection failed. Update your details and retry."
            )
        elif has_setup_artifacts:
            status = "setting_up"
            status_label = "Setting Up"
            note = "MT5 terminal setup is in progress. We'll email you when sync is ready."
        elif is_linked:
            status = "queued"
            status_label = "Setup Queued"
            note = (
                "MT5 details are saved and setup started right away. We'll email you when sync is ready."
            )
        elif pending_request is not None or approved_request is not None:
            status = "pending"
            status_label = "Submit Details"
            note = "Complete the form below to resume MT5 setup."
        elif batch_state["batches_enabled"] and not batch_state["can_accept_requests"]:
            status = "batch_unavailable"
            status_label = batch_state["status_label"]
            note = batch_state["request_blocked_message"] or "MT5 sync is not available right now."
        else:
            status = "requestable"
            status_label = "Ready to Start"
            note = "Fill in and submit the form below to queue setup."

        status_rows.append(
            {
                "account": account,
                "status": status,
                "status_label": status_label,
                "note": note,
                "mt5_account": mt5_account,
                "pending_request": pending_request,
                "approved_request": approved_request,
                "is_linked": is_linked,
                "is_active_linked": is_active_linked,
                "is_archived": is_archived,
                "has_setup_artifacts": has_setup_artifacts,
            }
        )

    active_account_id = getattr(active_trade_account, "id", None)
    selected_row = None
    if active_account_id is not None:
        selected_row = next(
            (row for row in status_rows if row["account"].id == active_account_id),
            None,
        )

    selected_account = selected_row["account"] if selected_row is not None else None
    selected_status = selected_row["status"] if selected_row is not None else None

    dashboard_next = url_for("dashboard.home", _anchor="mt5-access")

    return {
        "mt5_cfd_accounts": mt5_cfd_accounts,
        "mt5_selected_row": selected_row,
        "mt5_selected_account": selected_account,
        "mt5_selected_status": selected_status,
        "mt5_dashboard_next": dashboard_next,
        "mt5_batch_state": batch_state,
    }


@bp.route("/dashboard")
def home():
    if not session.get("user_id"):
        return render_template(
            "dashboard_public_gate.html",
            title="Trading dashboard | MyFXJournal weekly AI review and analytics",
            meta_description=(
                "Sign in to MyFXJournal to open your trading dashboard: account-scoped analytics, trade history, "
                "optional MT5 sync, and a concise weekly AI review built from your own executions."
            ),
            canonical_url=build_external_url("/dashboard"),
            body_class="auth-layout",
        )
    return _dashboard_home_authenticated()


def _dashboard_home_authenticated(target_user_id=None, admin_viewer_username=None):
    support_view_active = is_support_view_active()
    is_admin_view = (
        (target_user_id is not None and admin_viewer_username is not None)
        or support_view_active
    )
    if is_admin_view:
        if support_view_active and target_user_id is None:
            target_user = getattr(g, "support_view_target_user", None)
            admin_viewer_username = get_support_view_admin_username()
        else:
            from models import User as _User
            target_user = db.session.get(_User, target_user_id)
        if target_user is None:
            from flask import abort
            abort(404)
        username = target_user.username
        user_id = target_user.id
        timezone_name = normalize_timezone_name(target_user.timezone, get_app_timezone_name()) or "UTC"
    else:
        username = get_effective_username()
        user_id = get_effective_user_id()
        timezone_name = get_display_timezone_name()

    active_trade_account = get_active_trade_account_for_user(user_id)
    account_rows = get_user_trade_accounts(user_id)
    mt5_access_state = build_mt5_access_state(user_id, account_rows)
    mt5_sections = _build_dashboard_mt5_sections(
        account_rows=account_rows,
        active_trade_account=active_trade_account,
        mt5_access_state=mt5_access_state,
    )
    user_trades = _load_user_trades(user_id, active_trade_account)
    dashboard_analytics = _load_dashboard_analytics(
        user_id,
        active_trade_account,
        user_trades,
        timezone_name,
    )
    summary = dashboard_analytics["summary"]
    closed_records = dashboard_analytics["closed_records"]
    closed_trade_count = len(closed_records)
    session_stats = dashboard_analytics["session_stats"]
    chart_points = dashboard_analytics["chart_points"]
    behavior_badge_map = build_trade_behavior_badge_map(user_trades)

    now_local = to_display_timezone(utcnow_naive(), timezone_name)
    trades_this_month = sum(
        1
        for trade in user_trades
        if (opened_local := to_display_timezone(trade.opened_at, timezone_name))
        and opened_local.month == now_local.month
        and opened_local.year == now_local.year
    )

    now_utc = utcnow_naive()
    recent_trades = []
    for trade in user_trades:
        trade_is_running = is_trade_running(trade)
        pnl_value = resolve_net_pnl(trade)
        opened_local = to_display_timezone(trade.opened_at, timezone_name)
        trade_date = (
            f"{opened_local.strftime('%d %b %Y')} ({opened_local.strftime('%a')}) · {opened_local.strftime('%H:%M')}"
            if opened_local
            else "-"
        )
        trade_date_value = opened_local.strftime("%Y-%m-%d") if opened_local else ""
        opened_at_value = opened_local.isoformat() if opened_local else ""
        trade_profile = getattr(trade, "trade_profile", None)
        trade_profile_version = getattr(trade, "trade_profile_version", None)
        recent_trades.append(
            {
                "trade_id": getattr(trade, "id", None),
                "trade_pubkey": getattr(trade, "pubkey", None) or "",
                "date": trade_date,
                "date_value": trade_date_value,
                "opened_at_value": opened_at_value,
                "symbol": format_trade_symbol(trade),
                "trade_profile_label": (
                    trade_profile_version.name
                    if trade_profile_version is not None
                    else (trade_profile.name if trade_profile is not None else "-")
                ),
                "side": trade.side,
                "pnl": pnl_value,
                "running_pnl": pnl_value if trade_is_running else None,
                "running_duration_label": (
                    format_duration_minutes(
                        (now_utc - trade.opened_at).total_seconds() / 60.0
                    )
                    if trade_is_running and trade.opened_at is not None
                    else None
                ),
                "session_label": classify_trading_session(trade.opened_at) if trade.opened_at else "-",
                "is_running": trade_is_running,
                "bundle_pubkey": getattr(trade, "bundle_pubkey", None),
                "behavior_badges": behavior_badge_map.get(get_trade_identity(trade), []),
            }
        )

    current_week_start = now_local.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ) - timedelta(days=now_local.weekday())
    previous_week_start = current_week_start - timedelta(days=7)
    previous_week_end = current_week_start

    current_week_stats = _summarize_week(closed_records, current_week_start)
    previous_week_stats = _summarize_week(
        closed_records,
        previous_week_start,
        end_local=previous_week_end,
    )
    week_on_week_insight = _build_week_on_week_insight(
        current_week_stats,
        previous_week_stats,
    )
    weekly_ai_state = _get_weekly_ai_state(user_id, active_trade_account, timezone_name, user_trades, generate=not is_admin_view)
    weekly_ai_review_text = weekly_ai_state.get("weekly_ai_review_text", "")
    if not weekly_ai_review_text and weekly_ai_state["weekly_ai_review"] is not None:
        weekly_ai_review_text = normalize_dashboard_advice_text(
            weekly_ai_state["weekly_ai_review"].response_text
        )
    review_workflow_banner_state = _get_review_workflow_banner_state(
        user_id,
        active_trade_account,
        user_trades,
    )
    onboarding_banner_state = _get_onboarding_banner_state(user_id)
    has_any_trades = bool(user_trades)
    has_trades = has_any_trades
    has_closed_trades = closed_trade_count > 0
    active_trade_account_id = getattr(active_trade_account, "id", None)
    has_mt5 = (
        active_trade_account_id in mt5_access_state["active_mt5_trade_account_ids"]
        if active_trade_account_id is not None
        else False
    )
    if has_mt5:
        dashboard_state = "state-3"
    elif has_trades:
        dashboard_state = "state-2"
    else:
        dashboard_state = "state-1"
    has_ai_review = weekly_ai_state["weekly_ai_review"] is not None
    show_whats_next_banner = not (has_any_trades and has_ai_review)

    performance_trends = _build_performance_trends(closed_records, now_local)
    ei_trend_data = _build_ei_trend(user_id, active_trade_account_id)

    return render_template(
        "index.html",
        title=f"MyFXJournal | Dashboard [{username}] (Admin View)" if is_admin_view else "MyFXJournal | Dashboard",
        username=username,
        admin_viewer_username=admin_viewer_username,
        active_trade_account=active_trade_account,
        win_rate=summary.get("win_rate"),
        closed_trade_count=closed_trade_count,
        net_pnl_week=summary.get("weekly_pnl"),
        account_pnl_total=summary.get("net_pnl"),
        trades_this_month=trades_this_month,
        avg_win=summary.get("avg_win"),
        avg_loss_abs=summary.get("avg_loss_abs"),
        recent_trades=recent_trades,
        session_stats=session_stats[:4],
        current_week_stats=current_week_stats,
        previous_week_stats=previous_week_stats,
        week_on_week_insight=week_on_week_insight,
        performance_trends=performance_trends,
        ei_trend=ei_trend_data,
        chart_points=chart_points,
        weekly_ai_review=weekly_ai_state["weekly_ai_review"],
        weekly_ai_review_text=weekly_ai_review_text,
        weekly_ai_review_display=weekly_ai_state.get("weekly_ai_review_display"),
        weekly_ai_generated_at_label=weekly_ai_state["weekly_ai_generated_at_label"],
        weekly_ai_period_label=weekly_ai_state["weekly_ai_period_label"],
        weekly_ai_empty_message=weekly_ai_state["weekly_ai_empty_message"],
        weekly_ai_is_generating=weekly_ai_state["weekly_ai_is_generating"],
        show_onboarding_banner=onboarding_banner_state["show_onboarding_banner"],
        onboarding_was_skipped=onboarding_banner_state["onboarding_was_skipped"],
        show_review_workflow_banner=review_workflow_banner_state["show_workflow_banner"],
        review_workflow_stage=review_workflow_banner_state["workflow_stage"],
        review_workflow_title=review_workflow_banner_state["title"],
        review_workflow_note=review_workflow_banner_state["note"],
        review_workflow_button_label=review_workflow_banner_state["button_label"],
        review_workflow_button_href=review_workflow_banner_state["button_href"],
        review_workflow_show_skip=review_workflow_banner_state["show_skip"],
        dashboard_state=dashboard_state,
        has_trades=has_trades,
        has_mt5=has_mt5,
        has_any_trades=has_any_trades,
        has_closed_trades=has_closed_trades,
        has_ai_review=has_ai_review,
        show_whats_next_banner=show_whats_next_banner,
        weekly_ai_min_closed_trades=MIN_CLOSED_TRADES_FOR_ADVICE,
        small_sample_min_trades=SMALL_SAMPLE_MIN_TRADES,
        win_rate_is_limited_sample=has_closed_trades
        and closed_trade_count < SMALL_SAMPLE_MIN_TRADES,
        mt5_cfd_accounts=mt5_sections["mt5_cfd_accounts"],
        linked_mt5_trade_account_ids=mt5_access_state["linked_mt5_trade_account_ids"],
        mt5_selected_row=mt5_sections["mt5_selected_row"],
        mt5_selected_account=mt5_sections["mt5_selected_account"],
        mt5_selected_status=mt5_sections["mt5_selected_status"],
        mt5_dashboard_next=mt5_sections["mt5_dashboard_next"],
        mt5_batch_state=mt5_sections["mt5_batch_state"],
    )


@bp.route("/api/ai-status")
@limiter.limit(
    "120 per minute",
    methods=["GET"],
    error_message="Too many status checks. Please wait and try again.",
)
@login_required
def ai_status():
    user_id = get_effective_user_id()
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_id = getattr(active_trade_account, "id", None)
    weekly_period = get_latest_trade_week_period(
        user_id=user_id,
        trade_account_id=account_id,
    ) or get_weekly_dashboard_period()
    try:
        status = get_ai_status(
            user_id,
            trade_account_id=account_id,
            period_start_utc=weekly_period.get("period_start_utc") if weekly_period else None,
        )
    except CacheUnavailableError as exc:
        current_app.logger.warning("Weekly AI status poll unavailable: %s", exc)
        status = None
    return jsonify({"ready": status is None})


@bp.route("/dashboard/weekly-review/<int:review_id>/chat", methods=["POST"])
@limiter.limit(
    "5 per minute; 120 per hour",
    methods=["POST"],
    error_message="Too many review chat messages. Please wait and try again.",
)
@login_required
def weekly_review_chat(review_id):
    if is_support_view_session_active():
        return (
            jsonify(
                {
                    "error": "That action is not available in read-only support view.",
                }
            ),
            403,
        )
    user_id = get_effective_user_id()
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_id = getattr(active_trade_account, "id", None)
    if account_id is None:
        return jsonify({"error": "No active trade account is selected."}), 404

    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Ask a question about this weekly review first."}), 400
    if len(message) > WEEKLY_REVIEW_CHAT_MAX_CHARS:
        return jsonify({"error": f"Keep questions under {WEEKLY_REVIEW_CHAT_MAX_CHARS} characters."}), 400

    review = (
        AIGeneratedResponse.query.filter(
            AIGeneratedResponse.id == review_id,
            AIGeneratedResponse.user_id == user_id,
            AIGeneratedResponse.trade_account_id == account_id,
            AIGeneratedResponse.kind == WEEKLY_DASHBOARD_KIND,
        )
        .first()
    )
    if review is None:
        return jsonify({"error": "Weekly review not found for this account."}), 404

    chat_history = (
        WeeklyReviewChatMessage.query.filter_by(
            user_id=user_id,
            trade_account_id=account_id,
            ai_response_id=review.id,
        )
        .order_by(WeeklyReviewChatMessage.created_at.desc(), WeeklyReviewChatMessage.id.desc())
        .limit(WEEKLY_REVIEW_CHAT_HISTORY_LIMIT)
        .all()
    )
    chat_history.reverse()

    try:
        reply, _response_payload, model_used = generate_weekly_review_chat_reply(
            review,
            message,
            chat_history=chat_history,
            timezone_name=get_display_timezone_name(),
        )
    except AIRequestError as exc:
        current_app.logger.warning(
            "Weekly review chat AI request failed. user_id=%s trade_account_id=%s review_id=%s error=%s",
            user_id,
            account_id,
            review.id,
            exc,
        )
        return jsonify({"error": "Could not answer that right now. Please try again shortly."}), 502
    except Exception as exc:
        current_app.logger.exception(
            "Weekly review chat failed. user_id=%s trade_account_id=%s review_id=%s",
            user_id,
            account_id,
            review.id,
        )
        return jsonify({"error": "Could not answer that right now. Please try again shortly."}), 500

    db.session.add_all(
        [
            WeeklyReviewChatMessage(
                user_id=user_id,
                trade_account_id=account_id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_USER,
                content=message,
                prompt_version=WEEKLY_REVIEW_CHAT_PROMPT_VERSION,
            ),
            WeeklyReviewChatMessage(
                user_id=user_id,
                trade_account_id=account_id,
                ai_response_id=review.id,
                role=WeeklyReviewChatMessage.ROLE_ASSISTANT,
                content=reply,
                model_used=model_used,
                prompt_version=WEEKLY_REVIEW_CHAT_PROMPT_VERSION,
            ),
        ]
    )
    db.session.commit()
    return jsonify({"reply": reply})


@bp.route("/dashboard/analytics")
@login_required
def analytics():
    user_id = get_effective_user_id()
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_id = getattr(active_trade_account, "id", None)
    timezone_name = get_display_timezone_name()

    analytics_payload = _get_cached_payload(ANALYTICS_CACHE_PREFIX, user_id, account_id)
    rr_summary = _get_cached_payload(RR_SUMMARY_CACHE_PREFIX, user_id, account_id)

    user_trades = None
    if analytics_payload is None or rr_summary is None:
        user_trades = _load_user_trades(user_id, active_trade_account)

    if analytics_payload is None:
        analytics_payload = _build_cached_analytics_payload(
            user_id,
            active_trade_account,
            user_trades,
            timezone_name,
        )
    else:
        analytics_payload.setdefault(
            "weekday_has_reliable_pattern",
            any((item.get("count") or 0) >= 5 for item in (analytics_payload.get("weekday_stats") or [])),
        )
        analytics_payload.setdefault(
            "pair_has_reliable_pattern",
            any((item.get("count") or 0) >= 5 for item in (analytics_payload.get("pair_stats") or [])),
        )
        analytics_payload.setdefault(
            "session_has_reliable_pattern",
            any((item.get("count") or 0) >= 5 for item in (analytics_payload.get("session_stats") or [])),
        )
        analytics_payload.setdefault(
            "summary",
            {},
        )
        analytics_payload["summary"].setdefault(
            "pair_sample_is_diverse",
            len(analytics_payload.get("pair_stats") or []) >= 2,
        )
        analytics_payload["summary"].setdefault(
            "equity_has_outlier_dominance",
            False,
        )
        analytics_payload["summary"].setdefault(
            "closed_before_tp_count",
            0,
        )
        analytics_payload["summary"].setdefault(
            "closed_before_sl_count",
            0,
        )
        analytics_payload.setdefault("behavior", {})

    if rr_summary is None:
        rr_summary = _build_cached_rr_summary(
            user_id,
            active_trade_account,
            user_trades,
        )

    if not analytics_payload.get("behavior"):
        if user_trades is None:
            user_trades = _load_user_trades(user_id, active_trade_account)
        analytics_payload["behavior"] = build_trade_behavior_analytics(
            user_trades,
            timezone_name=timezone_name,
        )

    return render_template(
        "analytics.html",
        title="MyFXJournal | Analytics",
        username=get_effective_username(),
        analytics=analytics_payload,
        analytics_timezone=timezone_name,
        active_trade_account=active_trade_account,
        rr_summary=rr_summary,
        has_any_trades=bool((analytics_payload.get("summary") or {}).get("total_trades")),
        small_sample_min_trades=SMALL_SAMPLE_MIN_TRADES,
    )


@bp.route("/api/running-pnl")
@login_required
def running_pnl_api():
    user_id = get_effective_user_id()
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_id = getattr(active_trade_account, "id", None)

    if account_id is None:
        return jsonify({"events": [], "summary": summarize_running_pnl([])})

    date_from = None
    date_to = None
    raw_from = request.args.get("from", "").strip()
    raw_to = request.args.get("to", "").strip()
    for raw, setter in [(raw_from, "from"), (raw_to, "to")]:
        if raw:
            try:
                parsed = datetime.fromisoformat(raw)
                if setter == "from":
                    date_from = parsed
                else:
                    date_to = parsed
            except ValueError:
                pass

    try:
        closed_trades = (
            Trade.query.filter(
                Trade.user_id == user_id,
                Trade.trade_account_id == account_id,
                or_(
                    Trade.closed_at.isnot(None),
                    Trade.exit_price.isnot(None),
                ),
            )
            .options(
                load_only(
                    Trade.id,
                    Trade.symbol,
                    Trade.side,
                    Trade.pnl,
                    Trade.commission,
                    Trade.swap,
                    Trade.opened_at,
                    Trade.closed_at,
                    Trade.entry_price,
                    Trade.exit_price,
                    Trade.lot_size,
                ),
                selectinload(Trade.trade_account),
            )
            .all()
        )
    except OperationalError:
        db.session.rollback()
        closed_trades = []

    try:
        cash_flows = (
            AccountCashFlow.query.filter_by(
                user_id=user_id,
                trade_account_id=account_id,
            )
            .order_by(AccountCashFlow.occurred_at)
            .all()
        )
    except OperationalError:
        db.session.rollback()
        cash_flows = []

    events = build_running_pnl_events(
        closed_trades,
        cash_flows,
        resolve_trade_pnl=resolve_net_pnl,
        date_from=date_from,
        date_to=date_to,
    )

    timezone_name = get_display_timezone_name()
    serialized_events = []
    for event in events:
        ts = event["timestamp"]
        local_ts = to_display_timezone(ts, timezone_name)
        serialized_events.append({
            "timestamp": ts.isoformat() if ts else None,
            "timestamp_local": local_ts.isoformat() if local_ts else None,
            "date_label": local_ts.strftime("%d %b %Y") if local_ts else None,
            "event_type": event["event_type"],
            "amount": event["amount"],
            "description": event["description"],
            "running_realized_pnl": event["running_realized_pnl"],
            "running_cash_flow": event["running_cash_flow"],
            "running_net_result": event["running_net_result"],
        })

    return jsonify({
        "events": serialized_events,
        "summary": summarize_running_pnl(events),
    })
