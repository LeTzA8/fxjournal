import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, render_template, request, session, url_for
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import load_only, selectinload

from ai_service import (
    AIRequestError,
    MIN_CLOSED_TRADES_FOR_ADVICE,
    WEEKLY_REVIEW_CHAT_PROMPT_VERSION,
    WEEKLY_REVIEW_CHAT_STARTER_PROMPT_LIMIT,
    WEEKLY_DASHBOARD_KIND,
    build_dashboard_review_display,
    count_closed_trade_ideas_in_period,
    generate_weekly_review_chat_reply,
    get_latest_trade_week_period,
    get_latest_weekly_dashboard_advice,
    get_weekly_dashboard_period,
    normalize_dashboard_advice_text,
    pack_weekly_review_chat_assistant_content,
    should_generate_weekly_dashboard_advice,
    unpack_weekly_review_chat_assistant_content,
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
from helpers.entitlements import (
    can_send_weekly_followup_message,
    get_weekly_followup_message_usage,
    start_premium_trial_if_needed,
)
from helpers.running_pnl import build_running_pnl_events, summarize_running_pnl
from helpers.scoring import compute_emotional_index
from helpers.trade_analysis import detect_outliers, get_trade_identity
from helpers.trends import trend_direction_ei_scores, trend_direction_expectancy_weeks, trend_direction_win_rate_weeks
from helpers.weekly_review_ref_rewrite import (
    build_weekly_review_citation_lookup as _build_weekly_review_citation_lookup,
    deserialize_datetime_iso as _deserialize_datetime,
    parse_json_blob as _parse_json_blob,
    rewrite_review_text_refs as _rewrite_review_text_refs,
)
from auth_account import build_external_url, user_has_admin_access
from extensions import limiter
from routes.admin_journal import (
    create_journal_session_from_incoming,
    get_journal_session_for_user,
    journal_post_chat_response,
    journal_session_api_payload,
    journal_update_feedback_response,
    journal_update_tags_response,
)
from helpers.admin_activation import dashboard_row_has_mt5_submission
from helpers.utils import login_required, utcnow_naive
from models import (
    AccountCashFlow,
    AIGeneratedResponse,
    JournalSession,
    Trade,
    User,
    UserProfile,
    WeeklyCheckin,
    WeeklyReviewChatMessage,
    db,
)
from trading import (
    SMALL_SAMPLE_MIN_TRADES,
    build_rr_summary,
    build_trade_analytics,
    classify_trading_session,
    format_duration_minutes,
    format_trade_symbol,
    normalize_account_type,
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
DASHBOARD_CACHE_PREFIX = "dashboard_v4"
ANALYTICS_CACHE_PREFIX = "analytics_v5"
RR_SUMMARY_CACHE_PREFIX = "rr_summary_v5"
WEEKLY_REVIEW_CHAT_MAX_CHARS = 800
WEEKLY_REVIEW_CHAT_HISTORY_LIMIT = 6
LEGACY_WEEKLY_REVIEW_CHAT_PROMPTS = [
    "Explain this simply",
    "Was this bad luck or my execution?",
    "Which trade should I review first?",
]
_WEEKLY_REVIEW_CHAT_REF_CODE_RE = re.compile(r"\b[BT]\d+\b", re.IGNORECASE)
_WEEKLY_REVIEW_CHAT_BRACKETED_REFS_RE = re.compile(
    r"\s*[\(\[\{]\s*[BT]\d+(?:\s*,\s*[BT]\d+)*\s*[\)\]\}]",
    re.IGNORECASE,
)
_WEEKLY_REVIEW_CHAT_SYMBOL_PAIR_REFS_RE = re.compile(
    r"\s*[\(\[\{]\s*[A-Za-z][A-Za-z0-9]{0,15}\s*[-\u2013]\s*[A-Za-z][A-Za-z0-9]{0,15}\s*[\)\]\}]",
    re.IGNORECASE,
)
_WEEKLY_REVIEW_CHAT_DUPLICATE_SYMBOL_PAIR_RE = re.compile(
    r"\s*[\(\[\{]?\s*([A-Za-z][A-Za-z0-9]{0,15})\s*[-\u2013]\s*\1\s*[\)\]\}]?",
    re.IGNORECASE,
)
_WEEKLY_REVIEW_CHAT_REDUNDANT_DATE_SUFFIX_RE = re.compile(
    r"(\d{1,2}\s+[A-Za-z]{3})\s*$",
    re.IGNORECASE,
)


def _build_weekly_journal_preview(user_id, trade_account_id, user_trades, timezone_name):
    if trade_account_id is None:
        return {
            "trade_options": [],
            "recent_sessions": [],
        }

    closed_trades = [
        trade
        for trade in user_trades or []
        if getattr(trade, "trade_account_id", None) == trade_account_id
        and getattr(trade, "closed_at", None) is not None
    ]
    closed_trades.sort(
        key=lambda trade: (getattr(trade, "closed_at", None) or datetime.min, getattr(trade, "id", 0) or 0),
        reverse=True,
    )
    trade_options = []
    cutoff = utcnow_naive() - timedelta(days=60)
    for trade in closed_trades:
        closed_at = getattr(trade, "closed_at", None)
        if closed_at is not None and closed_at < cutoff:
            continue
        closed_local = to_display_timezone(closed_at, timezone_name) if closed_at else None
        date_label = closed_local.strftime("%d %b") if closed_local else "closed"
        pnl_value = resolve_net_pnl(trade)
        pnl_label = ""
        if pnl_value is not None:
            try:
                pnl_label = f" {float(pnl_value):+.2f}"
            except (TypeError, ValueError):
                pnl_label = ""
        trade_options.append(
            {
                "pubkey": getattr(trade, "pubkey", "") or "",
                "label": f"{format_trade_symbol(trade)} - {date_label}{pnl_label}",
            }
        )
        if len(trade_options) >= 12:
            break

    recent_sessions = (
        JournalSession.query.filter_by(user_id=user_id)
        .order_by(JournalSession.started_at.desc(), JournalSession.id.desc())
        .limit(6)
        .all()
    )
    return {
        "trade_options": trade_options,
        "recent_sessions": recent_sessions,
    }


def _serialize_datetime(value):
    if value is None:
        return None
    return value.isoformat()


def _citation_identity(citation):
    if not isinstance(citation, dict):
        return ""
    citation_type = str(citation.get("type") or "trade").strip() or "trade"
    if citation_type == "bundle":
        keys = ("bundle_key", "trade_pubkey", "trade_id", "ref", "label", "inline_label")
    else:
        keys = ("trade_pubkey", "trade_id", "ref", "label", "inline_label")
    for key in keys:
        value = str(citation.get(key) or "").strip()
        if value:
            return f"{citation_type}:{value}"
    return ""


def _dedupe_review_citations(citations, seen_keys):
    deduped = []
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        identity = _citation_identity(citation)
        if not identity or identity in seen_keys:
            continue
        seen_keys.add(identity)
        deduped.append(citation)
    return deduped


def _citation_matches_text(citation, text):
    normalized = str(text or "")
    if not normalized or not isinstance(citation, dict):
        return False
    for raw_label in (
        str(citation.get("label") or "").strip(),
        str(citation.get("inline_label") or "").strip(),
    ):
        if raw_label and re.search(re.escape(raw_label), normalized, flags=re.IGNORECASE):
            return True
    return False


def _augment_citations_from_mentions(text, citations, citation_lookup):
    normalized = str(text or "").strip()
    deduped = [
        citation
        for citation in _dedupe_review_citations(citations, set())
        if _citation_matches_text(citation, normalized)
    ]
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
        identity = _citation_identity(citation)
        if identity:
            seen_keys.add(identity)

    for label, grouped_citations in label_groups.items():
        if len(grouped_citations) != 1:
            continue
        if re.search(rf"\b{re.escape(label)}\b", normalized, flags=re.IGNORECASE) is None:
            continue
        citation = grouped_citations[0]
        identity = _citation_identity(citation)
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
    matched_ranges = []
    matched_keys = set()
    for citation in deduped_citations:
        identity = _citation_identity(citation)
        if identity and identity in matched_keys:
            continue
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
            for found in re.finditer(re.escape(candidate), normalized, flags=re.IGNORECASE):
                if any(found.start() < end and found.end() > start for start, end in matched_ranges):
                    continue
                if best_match is None:
                    best_match = found
                    break
                if found.start() < best_match.start():
                    best_match = found
                    break
                if found.start() == best_match.start() and found.end() > best_match.end():
                    best_match = found
                    break

        if best_match is None:
            continue
        if identity:
            matched_keys.add(identity)
        matched_ranges.append((best_match.start(), best_match.end()))
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
    selected_keys = set()
    for item in matches:
        start = item["start"]
        end = item["end"]
        citation = item["citation"]
        identity = _citation_identity(citation)
        if start < last_end or (identity and identity in selected_keys):
            continue
        selected_matches.append(item)
        last_end = end
        if identity:
            selected_keys.add(identity)

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
                "ref": citation.get("ref"),
                "trade_id": citation.get("trade_id"),
                "trade_pubkey": citation.get("trade_pubkey"),
                "bundle_key": citation.get("bundle_key"),
                "tone": citation.get("tone") or "neutral",
            }
        )
        cursor = end

    if cursor < len(normalized):
        segments.append({"type": "text", "text": normalized[cursor:]})

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


def _extract_weekly_review_chat_refs(text, citation_lookup):
    if not text or not citation_lookup:
        return []

    refs = []
    seen = set()
    for match in _WEEKLY_REVIEW_CHAT_REF_CODE_RE.finditer(str(text)):
        ref = str(match.group(0) or "").strip().upper()
        if not ref or ref not in citation_lookup or ref in seen:
            continue
        refs.append(ref)
        seen.add(ref)
    return refs


def _strip_weekly_review_chat_ref_codes(text):
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = _WEEKLY_REVIEW_CHAT_BRACKETED_REFS_RE.sub("", normalized)
    normalized = _WEEKLY_REVIEW_CHAT_SYMBOL_PAIR_REFS_RE.sub("", normalized)
    normalized = _WEEKLY_REVIEW_CHAT_DUPLICATE_SYMBOL_PAIR_RE.sub(" ", normalized)
    normalized = _WEEKLY_REVIEW_CHAT_REF_CODE_RE.sub("", normalized)
    normalized = re.sub(r"\s{2,}", " ", normalized)
    normalized = re.sub(r"\s+([,.;:!?])", r"\1", normalized)
    return normalized.strip()


def _rewrite_weekly_review_chat_reply_text(text, citation_lookup):
    normalized = str(text or "").strip()
    if not normalized or not citation_lookup:
        return normalized

    ref_codes = [ref for ref in citation_lookup.keys() if ref]
    if not ref_codes:
        return normalized

    ref_pattern = "|".join(re.escape(ref) for ref in sorted(ref_codes, key=len, reverse=True))
    bracket_close = (
        r"\s*[\)\]\}](?:['\u2019']?[ds](?=\s|[,.;:!?]|$))?"
    )
    bracketed_ref_pattern = re.compile(
        rf"[\(\[\{{]\s*(?:{ref_pattern})(?:\s*,\s*(?:{ref_pattern}))*{bracket_close}",
        re.IGNORECASE,
    )
    clitic_after_bracket_re = re.compile(
        r"[\)\]\}]\s*['\u2019']?[ds](?=\s|[,.;:!?]|$)",
        re.IGNORECASE,
    )

    def replace_bracket_group(match):
        group = match.group(0) or ""
        if clitic_after_bracket_re.search(group):
            return " "
        refs = re.findall(rf"\b(?:{ref_pattern})\b", group, flags=re.IGNORECASE)
        labels = []
        for raw_ref in refs:
            citation = citation_lookup.get(str(raw_ref or "").strip().upper())
            if not isinstance(citation, dict):
                continue
            label = str(citation.get("inline_label") or citation.get("label") or "").strip()
            if label and label not in labels:
                labels.append(label)
        if not labels:
            return " "
        prefix = normalized[: match.start()]
        context_window = prefix[-80:]
        if all(
            re.search(rf"(?<!\w){re.escape(label)}(?!\w)", context_window, flags=re.IGNORECASE)
            for label in labels
        ):
            return " "
        if len(labels) == 1:
            return f" {labels[0]} "
        return f" {', '.join(labels)} "

    normalized = bracketed_ref_pattern.sub(replace_bracket_group, normalized)
    return _rewrite_review_text_refs(normalized, citation_lookup)


def _trim_redundant_date_prefix_before_chat_citations(segments):
    if not segments:
        return segments

    trimmed = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") != "citation":
            trimmed.append(segment)
            continue

        label = str(segment.get("label") or segment.get("inline_label") or "").strip()
        if trimmed and trimmed[-1].get("type") == "text" and label:
            previous_text = str(trimmed[-1].get("text") or "")
            date_match = _WEEKLY_REVIEW_CHAT_REDUNDANT_DATE_SUFFIX_RE.search(previous_text)
            if date_match:
                date_token = date_match.group(1)
                if re.search(re.escape(date_token), label, flags=re.IGNORECASE):
                    leading = previous_text[: date_match.start()].rstrip()
                    if leading:
                        trimmed[-1] = {"type": "text", "text": f"{leading} "}
                    else:
                        trimmed.pop()
        trimmed.append(segment)
    return trimmed


def _build_weekly_review_chat_reply_display(review_record, reply_text, timezone_name):
    citation_lookup = _build_weekly_review_citation_lookup(
        getattr(review_record, "payload_json", None),
        timezone_name,
    )
    explicit_refs = _extract_weekly_review_chat_refs(reply_text, citation_lookup)
    display_text = _rewrite_weekly_review_chat_reply_text(reply_text, citation_lookup)
    display_text = _strip_weekly_review_chat_ref_codes(display_text)
    citations = _augment_citations_from_mentions(
        display_text,
        _resolve_weekly_review_citations(explicit_refs, citation_lookup),
        citation_lookup,
    )
    segments = _trim_redundant_date_prefix_before_chat_citations(
        _build_review_text_segments(display_text, citations)
    )
    segments = _merge_adjacent_chat_text_segments(segments)
    return {
        "text": display_text,
        "segments": segments,
    }


def _merge_adjacent_chat_text_segments(segments):
    if not segments:
        return segments

    merged = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if (
            merged
            and segment.get("type") == "text"
            and merged[-1].get("type") == "text"
        ):
            merged[-1] = {
                "type": "text",
                "text": f"{merged[-1].get('text') or ''}{segment.get('text') or ''}",
            }
            continue
        merged.append(segment)
    return merged


def _build_weekly_review_chat_message_display(review_record, message, timezone_name):
    role = getattr(message, "role", "")
    content = getattr(message, "content", "") or ""
    if role == WeeklyReviewChatMessage.ROLE_ASSISTANT:
        reply_text, suggested_prompts = unpack_weekly_review_chat_assistant_content(content)
        display = _build_weekly_review_chat_reply_display(
            review_record,
            reply_text,
            timezone_name,
        )
        return {
            "role": role,
            "text": display["text"],
            "segments": display["segments"],
            "suggested_prompts": suggested_prompts,
        }
    return {
        "role": role,
        "text": content,
        "segments": [{"type": "text", "text": content}],
        "suggested_prompts": [],
    }


def _mark_last_assistant_followup_prompts(chat_history):
    if not chat_history:
        return chat_history
    marked = []
    last_assistant_index = None
    for index, message in enumerate(chat_history):
        if message.get("role") == WeeklyReviewChatMessage.ROLE_ASSISTANT:
            last_assistant_index = index
    for index, message in enumerate(chat_history):
        item = dict(message)
        item["show_followup_prompts"] = (
            index == last_assistant_index
            and bool(item.get("suggested_prompts"))
        )
        marked.append(item)
    return marked


def _load_weekly_review_chat_history_display(review_record, user_id, trade_account_id, timezone_name):
    if review_record is None:
        return []
    messages = (
        WeeklyReviewChatMessage.query.filter_by(
            user_id=user_id,
            trade_account_id=trade_account_id,
            ai_response_id=review_record.id,
        )
        .order_by(WeeklyReviewChatMessage.created_at.asc(), WeeklyReviewChatMessage.id.asc())
        .all()
    )
    history = [
        _build_weekly_review_chat_message_display(review_record, message, timezone_name)
        for message in messages
    ]
    return _mark_last_assistant_followup_prompts(history)


def _weekly_review_display_text(display, key):
    section = display.get(key) if isinstance(display, dict) else None
    if not isinstance(section, dict):
        return ""
    return str(section.get("text") or "").strip()


def _weekly_review_prompt_label(display):
    if not isinstance(display, dict):
        return ""

    for section_key in ("summary", "takeaways"):
        section = display.get(section_key)
        items = section if isinstance(section, list) else [section]
        for item in items:
            if not isinstance(item, dict):
                continue
            citations = item.get("citations") if isinstance(item.get("citations"), list) else []
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                label = str(citation.get("inline_label") or citation.get("label") or "").strip()
                if not label:
                    continue
                return label.split("|", 1)[0].strip()
    return ""


def _weekly_review_prompt_issue_type(display):
    text_parts = [
        _weekly_review_display_text(display, "summary"),
        _weekly_review_display_text(display, "improvement"),
        _weekly_review_display_text(display, "experiment"),
    ]
    for takeaway in (display.get("takeaways") if isinstance(display, dict) else []) or []:
        if isinstance(takeaway, dict):
            text_parts.append(str(takeaway.get("text") or ""))
    normalized = " ".join(text_parts).lower()

    issue_terms = [
        ("risk", ("risk", "sizing", "oversized", "size", "lot", "too much weight", "heavy loss")),
        ("behavior", ("after a loss", "revenge", "re-entry", "reentry", "sequence", "post-loss", "impulsive")),
        ("exit", ("exit", "closed early", "closed before", "post-exit", "continued after", "took profit", "cut")),
        ("momentum", ("chase", "chasing", "momentum", "large candle", "after the move", "strong candle")),
        ("timing", ("early", "late", "timing", "entry", "entered", "wait", "confirmation")),
        ("context", ("range", "session", "volatility", "london", "new york", "ny", "asia", "overlap", "context")),
    ]
    best_issue = "generic"
    best_score = 0
    for issue, terms in issue_terms:
        score = sum(1 for term in terms if term in normalized)
        if score > best_score:
            best_issue = issue
            best_score = score
    return best_issue


def _dedupe_chat_prompts(prompts, limit=5):
    deduped = []
    seen = set()
    for prompt in prompts:
        text = re.sub(r"\s+", " ", str(prompt or "")).strip()
        if not text:
            continue
        if not text.endswith("?"):
            text = f"{text}?"
        key = text.lower()
        if key in seen:
            continue
        deduped.append(text)
        seen.add(key)
        if len(deduped) >= limit:
            break
    return deduped


def _build_weekly_review_chat_prompts(display):
    has_review_text = bool(
        _weekly_review_display_text(display, "summary")
        or _weekly_review_display_text(display, "improvement")
        or _weekly_review_display_text(display, "experiment")
        or any(
            str(item.get("text") or "").strip()
            for item in ((display.get("takeaways") if isinstance(display, dict) else []) or [])
            if isinstance(item, dict)
        )
    )
    if not has_review_text:
        return []

    label = _weekly_review_prompt_label(display)
    issue = _weekly_review_prompt_issue_type(display)

    if issue == "risk":
        prompts = [
            f"Why did {label} carry so much weight?" if label else "Why did risk drive this review?",
            "Was this mostly sizing or execution?",
            "What would fixed risk have changed this week?",
            "Where did one trade distort the review?",
        ]
    elif issue == "behavior":
        prompts = [
            "What happened after the loss?",
            f"How did {label} fit the behavior pattern?" if label else "Which trade shows the behavior pattern?",
            "Was this revenge trading or poor selection?",
            "What should I pause after next time?",
        ]
    elif issue == "exit":
        prompts = [
            "What was wrong with my exits?",
            f"What happened after {label} closed?" if label else "Which trade best shows the exit issue?",
            "Did I close too early or too late?",
            "What exit rule would have changed this week?",
        ]
    elif issue == "momentum":
        prompts = [
            "Where did I chase the move?",
            f"What made {label} look tempting?" if label else "Which entry best shows the chase?",
            "What should I wait for after a strong candle?",
            "How much of the week came from chasing?",
        ]
    elif issue == "timing":
        prompts = [
            "Where was my timing off?",
            f"What does {label} show about my entry?" if label else "Which trade best shows the timing issue?",
            "Was I too early or too late?",
            "What should I wait for next time?",
        ]
    elif issue == "context":
        prompts = [
            "What context did I miss?",
            f"Why did the context matter on {label}?" if label else "Which trade best shows the context problem?",
            "Was session or range the bigger issue?",
            "What should I check before taking this setup again?",
        ]
    else:
        prompts = [
            "What is the main thing this review is saying?",
            f"Why does {label} matter here?" if label else "Which trade should I review first?",
            "What evidence supports the main insight?",
            "What should I inspect before next week?",
        ]

    return _dedupe_chat_prompts(prompts, limit=5)


def _carry_rewrite_refs_from_original(display, original_display):
    if not isinstance(display, dict) or not isinstance(original_display, dict):
        return display

    summary = display.get("summary")
    original_summary = original_display.get("summary") or {}
    if isinstance(summary, dict) and not summary.get("refs"):
        summary["refs"] = list(original_summary.get("refs") or [])

    takeaways = display.get("takeaways") if isinstance(display.get("takeaways"), list) else []
    original_takeaways = (
        original_display.get("takeaways")
        if isinstance(original_display.get("takeaways"), list)
        else []
    )
    for index, takeaway in enumerate(takeaways):
        if not isinstance(takeaway, dict) or takeaway.get("refs"):
            continue
        if index >= len(original_takeaways) or not isinstance(original_takeaways[index], dict):
            continue
        takeaway["refs"] = list(original_takeaways[index].get("refs") or [])
    return display


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
        original_takeaways = original_display.get("takeaways") or []
        rewritten_takeaways = display.get("takeaways") or []
        if len(rewritten_takeaways) != len(original_takeaways):
            display = original_display
        else:
            display = _carry_rewrite_refs_from_original(display, original_display)
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
            "checkin_complete": True,
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
            "checkin_complete": False,
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
            "checkin_complete": True,
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
            "checkin_complete": True,
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
            "checkin_complete": False,
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
            "checkin_complete": False,
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
        "checkin_complete": False,
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
            "cost_drag": summary.get("cost_drag"),
            "cost_drag_coverage": summary.get("cost_drag_coverage"),
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


def _week_expectancy(week_stats):
    trade_count = (week_stats or {}).get("trade_count") or 0
    if trade_count <= 0:
        return None
    return ((week_stats or {}).get("net_pnl") or 0.0) / trade_count


def _build_week_on_week_performance_trends(current_week_stats, previous_week_stats):
    """Direct comparison of this dashboard week against the previous dashboard week."""
    current_win_rate = (current_week_stats or {}).get("win_rate")
    previous_win_rate = (previous_week_stats or {}).get("win_rate")
    current_expectancy = _week_expectancy(current_week_stats)
    previous_expectancy = _week_expectancy(previous_week_stats)

    weeks_available = sum(
        1
        for stats in (current_week_stats, previous_week_stats)
        if (stats or {}).get("trade_count")
    )
    has_limited_sample = any(
        0 < ((stats or {}).get("trade_count") or 0) < SMALL_SAMPLE_MIN_TRADES
        for stats in (current_week_stats, previous_week_stats)
    )

    return {
        "win_rate_trend": trend_direction_win_rate_weeks(
            [current_win_rate, previous_win_rate]
        ),
        "expectancy_trend": trend_direction_expectancy_weeks(
            [current_expectancy, previous_expectancy]
        ),
        "weeks_available": weeks_available,
        "has_limited_sample": has_limited_sample,
        "current_win_rate": current_win_rate,
        "previous_win_rate": previous_win_rate,
        "current_expectancy": current_expectancy,
        "previous_expectancy": previous_expectancy,
    }


def _closed_trades_in_local_window(trades, timezone_name, start_local, end_local=None):
    result = []
    for trade in trades or []:
        if getattr(trade, "closed_at", None) is None:
            continue
        realized_local = to_display_timezone(
            getattr(trade, "closed_at", None),
            timezone_name,
        ) or to_display_timezone(getattr(trade, "opened_at", None), timezone_name)
        if realized_local is None or realized_local < start_local:
            continue
        if end_local is not None and realized_local >= end_local:
            continue
        result.append(trade)
    return result


def _objective_behavior_score(trades):
    emotional_index = compute_emotional_index(trades=trades, weekly_checkin=None)
    score = (emotional_index or {}).get("score")
    return float(score) if score is not None else None


def _build_week_on_week_behavior_trend(
    user_trades,
    timezone_name,
    current_week_start,
    previous_week_start,
    previous_week_end,
):
    """Compare objective trade-behavior pressure this week vs the previous week."""
    current_trades = _closed_trades_in_local_window(
        user_trades,
        timezone_name,
        current_week_start,
    )
    previous_trades = _closed_trades_in_local_window(
        user_trades,
        timezone_name,
        previous_week_start,
        previous_week_end,
    )
    current_score = _objective_behavior_score(current_trades)
    previous_score = _objective_behavior_score(previous_trades)

    return {
        "behavior_trend": trend_direction_ei_scores([current_score, previous_score]),
        "current_behavior_score": current_score,
        "previous_behavior_score": previous_score,
        "current_trade_count": len(current_trades),
        "previous_trade_count": len(previous_trades),
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


def _get_weekly_ai_state(user_id, active_trade_account, timezone_name, user_trades, generate=True, checkin_complete=True):
    account_id = getattr(active_trade_account, "id", None)
    weekly_ai_review = None
    weekly_ai_review_text = ""
    weekly_ai_review_display = None
    weekly_ai_generated_at_label = ""
    weekly_ai_period_label = ""
    weekly_ai_empty_message = DEFAULT_WEEKLY_AI_EMPTY_MESSAGE
    weekly_ai_is_generating = False
    weekly_ai_needs_checkin = False
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

        ai_status = None
        try:
            ai_status = get_ai_status(
                user_id,
                trade_account_id=account_id,
                period_start_utc=latest_trade_period["period_start_utc"],
            )
        except CacheUnavailableError as exc:
            current_app.logger.warning("Weekly AI status unavailable: %s", exc)

        # When checkin is not complete and no review exists for the current period,
        # suppress the previous week's fallback and block auto-generation so the
        # AI panel can prompt the user to complete the check-in first.
        # Exception: if a review is already in-flight (user already clicked "Get Review Now"),
        # let the generating state show rather than the checkin prompt.
        if not checkin_complete and current_period_review is None and ai_status not in {"queued", "running"}:
            weekly_ai_review = None
            weekly_ai_needs_checkin = True
            generate = False
        else:
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
        "weekly_ai_needs_checkin": weekly_ai_needs_checkin,
    }


def _build_latest_trade_snapshot(user_trades, timezone_name):
    latest_trade = None
    for trade in user_trades or []:
        if getattr(trade, "closed_at", None) is None:
            continue
        if latest_trade is None or trade.closed_at > latest_trade.closed_at:
            latest_trade = trade

    if latest_trade is None:
        return None

    opened_local = to_display_timezone(latest_trade.opened_at, timezone_name)
    closed_local = to_display_timezone(latest_trade.closed_at, timezone_name)
    pnl_value = resolve_net_pnl(latest_trade)
    duration_label = "-"
    if latest_trade.opened_at is not None and latest_trade.closed_at is not None:
        duration_label = format_duration_minutes(
            (latest_trade.closed_at - latest_trade.opened_at).total_seconds() / 60.0
        )

    pnl_tone = "flat"
    if pnl_value is not None:
        if pnl_value > 0:
            pnl_tone = "good"
        elif pnl_value < 0:
            pnl_tone = "bad"

    trade_pubkey = getattr(latest_trade, "pubkey", None) or ""
    chart_data_url = ""
    if trade_pubkey and getattr(latest_trade, "mt5_position", None) and latest_trade.closed_at is not None:
        chart_data_url = url_for("trades.trade_chart_data", trade_pubkey=trade_pubkey)

    return {
        "trade_pubkey": trade_pubkey,
        "detail_url": url_for("trades.trade_detail", trade_pubkey=trade_pubkey) if trade_pubkey else "",
        "chart_data_url": chart_data_url,
        "symbol": format_trade_symbol(latest_trade),
        "side": (latest_trade.side or "-").upper(),
        "pnl": pnl_value,
        "pnl_tone": pnl_tone,
        "opened_label": opened_local.strftime("%d %b %Y %H:%M") if opened_local else "-",
        "closed_label": closed_local.strftime("%d %b %Y %H:%M") if closed_local else "-",
        "session_label": classify_trading_session(latest_trade.opened_at) if latest_trade.opened_at else "-",
        "duration_label": duration_label,
    }


def _build_dashboard_continuity_row(
    *,
    active_trade_account,
    user_trades,
    mt5_selected_row,
    mt5_selected_status,
    weekly_ai_state,
    review_workflow_banner_state,
    dashboard_state,
    timezone_name,
    has_any_trades,
    has_closed_trades,
):
    if dashboard_state == "state-1":
        return None

    account_id = getattr(active_trade_account, "id", None)
    status = mt5_selected_status
    if status is None and mt5_selected_row is not None:
        status = mt5_selected_row.get("status")

    last_sync_label = "Not linked"
    if mt5_selected_row is not None:
        mt5_account = mt5_selected_row.get("mt5_account")
        if mt5_account is not None and getattr(mt5_account, "last_synced_at", None) is not None:
            synced_local = to_display_timezone(mt5_account.last_synced_at, timezone_name)
            if synced_local is not None:
                last_sync_label = f"Synced {synced_local.strftime('%d %b %H:%M')}"
            else:
                last_sync_label = "Synced"
        elif status == "linked":
            last_sync_label = "Connected"
        elif status in {"queued", "setting_up", "pending"}:
            last_sync_label = "Setting up"
        elif status == "failed":
            last_sync_label = "Setup failed"
        elif status == "paused":
            last_sync_label = "Sync paused"
        elif status == "archived":
            last_sync_label = "Inactive"

    week_threshold = utcnow_naive() - timedelta(days=7)
    new_trades_count = 0
    if account_id is not None:
        for trade in user_trades or []:
            if getattr(trade, "trade_account_id", None) != account_id:
                continue
            opened_at = getattr(trade, "opened_at", None)
            if opened_at is not None and opened_at >= week_threshold:
                new_trades_count += 1

    if new_trades_count == 1:
        new_trades_label = "1 new trade"
    elif new_trades_count > 1:
        new_trades_label = f"{new_trades_count} new trades"
    else:
        new_trades_label = "None this week"

    if review_workflow_banner_state.get("show_workflow_banner"):
        stage = review_workflow_banner_state.get("workflow_stage")
        review_status_by_stage = {
            "bundle_review": "Bundle review due",
            "classification": "Revenge review due",
            "weekly_checkin": "Check-in due",
        }
        review_status_label = review_status_by_stage.get(stage, "Review in progress")
    elif weekly_ai_state.get("weekly_ai_is_generating"):
        review_status_label = "Generating"
    elif weekly_ai_state.get("weekly_ai_review") is not None:
        review_status_label = "Ready"
    elif not has_closed_trades:
        review_status_label = "Needs closed trades"
    else:
        review_status_label = "Waiting for week"

    return {
        "last_sync_label": last_sync_label,
        "new_trades_label": new_trades_label,
        "review_status_label": review_status_label,
    }


def _build_dashboard_mt5_sections(*, account_rows, active_trade_account, mt5_access_state, current_user=None):
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
        mt5_trial_state = None
        is_paused = False
        if mt5_account is not None:
            from helpers.entitlements import get_mt5_trial_state, is_mt5_sync_paused
            is_paused = is_mt5_sync_paused(mt5_account)
            if current_user is not None:
                mt5_trial_state = get_mt5_trial_state(current_user, mt5_account)
                is_paused = is_paused or mt5_trial_state.get("state") == "paused"
        has_setup_artifacts = bool(
            str(getattr(mt5_account, "terminal_path", "") or "").strip()
            or str(getattr(mt5_account, "appdata_hash", "") or "").strip()
        )

        if is_paused:
            status = "paused"
            status_label = "Sync Paused"
            note = (
                "MT5 sync is paused for this account. Your trade history is safe; "
                "view pricing to join the Trader waitlist for sync reactivation."
            )
        elif is_active_linked:
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
            note = "Connect MT5 below. We handle terminal setup and email you when sync is live."

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
                "is_paused": is_paused,
                "mt5_trial_state": mt5_trial_state,
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
    if not is_admin_view:
        target_user = db.session.get(User, user_id)
    mt5_access_state = build_mt5_access_state(user_id, account_rows)
    mt5_sections = _build_dashboard_mt5_sections(
        account_rows=account_rows,
        active_trade_account=active_trade_account,
        mt5_access_state=mt5_access_state,
        current_user=target_user,
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
        trade_date_label = opened_local.strftime("%d %b %Y (%a)") if opened_local else ""
        trade_date_value = opened_local.strftime("%Y-%m-%d") if opened_local else ""
        opened_at_value = opened_local.isoformat() if opened_local else ""
        trade_profile = getattr(trade, "trade_profile", None)
        trade_profile_version = getattr(trade, "trade_profile_version", None)
        recent_trades.append(
            {
                "trade_id": getattr(trade, "id", None),
                "trade_pubkey": getattr(trade, "pubkey", None) or "",
                "date": trade_date,
                "date_label": trade_date_label,
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
                "has_trade_note": bool((trade.trade_note or "").strip()),
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
    review_workflow_banner_state = _get_review_workflow_banner_state(
        user_id,
        active_trade_account,
        user_trades,
    )
    checkin_complete = review_workflow_banner_state.get("checkin_complete", True)
    force_review = request.args.get("get_review") == "1" and not is_admin_view
    weekly_ai_state = _get_weekly_ai_state(
        user_id,
        active_trade_account,
        timezone_name,
        user_trades,
        generate=(not is_admin_view) and (checkin_complete or force_review),
        checkin_complete=checkin_complete or force_review,
    )
    weekly_ai_review_text = weekly_ai_state.get("weekly_ai_review_text", "")
    if not weekly_ai_review_text and weekly_ai_state["weekly_ai_review"] is not None:
        weekly_ai_review_text = normalize_dashboard_advice_text(
            weekly_ai_state["weekly_ai_review"].response_text
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
    has_mt5_submission = dashboard_row_has_mt5_submission(mt5_sections["mt5_selected_row"])
    is_pure_zero_data = not has_any_trades and not has_mt5_submission
    active_account_type = (
        normalize_account_type(active_trade_account.account_type)
        if active_trade_account is not None
        else "CFD"
    )
    is_active_cfd = active_account_type == "CFD"
    is_active_futures = active_account_type == "FUTURES"
    show_mt5_panel = is_active_cfd
    user_created_at = getattr(target_user, "created_at", None)
    user_age_days = (
        (utcnow_naive() - user_created_at).days if user_created_at is not None else 0
    )
    show_zero_data_soft_waitlist = is_pure_zero_data and user_age_days >= 1
    show_whats_next_banner = (
        not (has_any_trades and has_ai_review)
        and not review_workflow_banner_state["show_workflow_banner"]
    )
    show_onboarding_banner = (
        onboarding_banner_state["show_onboarding_banner"]
        and not show_whats_next_banner
        and not review_workflow_banner_state["show_workflow_banner"]
    )
    dashboard_continuity = _build_dashboard_continuity_row(
        active_trade_account=active_trade_account,
        user_trades=user_trades,
        mt5_selected_row=mt5_sections["mt5_selected_row"],
        mt5_selected_status=mt5_sections["mt5_selected_status"],
        weekly_ai_state=weekly_ai_state,
        review_workflow_banner_state=review_workflow_banner_state,
        dashboard_state=dashboard_state,
        timezone_name=timezone_name,
        has_any_trades=has_any_trades,
        has_closed_trades=has_closed_trades,
    )
    week_on_week_trends = _build_week_on_week_performance_trends(
        current_week_stats,
        previous_week_stats,
    )
    week_on_week_behavior_data = _build_week_on_week_behavior_trend(
        user_trades,
        timezone_name,
        current_week_start,
        previous_week_start,
        previous_week_end,
    )
    latest_trade_snapshot = _build_latest_trade_snapshot(user_trades, timezone_name)
    weekly_review_chat_prompts = _build_weekly_review_chat_prompts(
        weekly_ai_state.get("weekly_ai_review_display"),
    ) or list(LEGACY_WEEKLY_REVIEW_CHAT_PROMPTS)
    weekly_review_chat_starter_prompts = weekly_review_chat_prompts[
        :WEEKLY_REVIEW_CHAT_STARTER_PROMPT_LIMIT
    ]
    weekly_review = weekly_ai_state["weekly_ai_review"]
    weekly_review_chat_send_state = None
    weekly_review_chat_usage = None
    weekly_review_chat_history = []
    if (
        weekly_review is not None
        and getattr(weekly_review, "id", None) is not None
        and active_trade_account_id is not None
    ):
        weekly_review_chat_send_state = can_send_weekly_followup_message(
            target_user,
            weekly_review,
        )
        weekly_review_chat_usage = (
            weekly_review_chat_send_state.get("usage")
            or get_weekly_followup_message_usage(target_user, weekly_review)
        )
        weekly_review_chat_history = _load_weekly_review_chat_history_display(
            weekly_review,
            user_id,
            active_trade_account_id,
            timezone_name,
        )
    weekly_review_chat_has_user_messages = any(
        message.get("role") == WeeklyReviewChatMessage.ROLE_USER
        for message in (weekly_review_chat_history or [])
    )
    # AI journal carousel is internal dogfood only: same gate as /admin/journal (not customers).
    show_weekly_journal_preview = bool(
        target_user is not None
        and user_has_admin_access(target_user)
        and not is_admin_view
    )
    weekly_journal_preview = (
        _build_weekly_journal_preview(user_id, active_trade_account_id, user_trades, timezone_name)
        if show_weekly_journal_preview
        else None
    )

    return render_template(
        "index.html",
        title=f"MyFXJournal | Dashboard [{username}] (Admin View)" if is_admin_view else "MyFXJournal | Dashboard",
        username=username,
        user_email=(target_user.email or "") if target_user is not None else "",
        admin_viewer_username=admin_viewer_username,
        active_trade_account=active_trade_account,
        now_local=now_local,
        win_rate=summary.get("win_rate"),
        closed_trade_count=closed_trade_count,
        net_pnl_week=summary.get("weekly_pnl"),
        account_pnl_total=summary.get("net_pnl"),
        trading_costs=summary.get("cost_drag"),
        trading_costs_coverage=summary.get("cost_drag_coverage"),
        has_cost_data=bool((summary.get("cost_drag_coverage") or 0) > 0),
        trades_this_month=trades_this_month,
        avg_win=summary.get("avg_win"),
        avg_loss_abs=summary.get("avg_loss_abs"),
        recent_trades=recent_trades,
        session_stats=session_stats[:4],
        current_week_stats=current_week_stats,
        previous_week_stats=previous_week_stats,
        week_on_week_insight=week_on_week_insight,
        week_on_week_trends=week_on_week_trends,
        week_on_week_behavior=week_on_week_behavior_data,
        latest_trade_snapshot=latest_trade_snapshot,
        chart_points=chart_points,
        weekly_ai_review=weekly_review,
        weekly_ai_review_text=weekly_ai_review_text,
        weekly_ai_review_display=weekly_ai_state.get("weekly_ai_review_display"),
        weekly_review_chat_prompts=weekly_review_chat_prompts,
        weekly_review_chat_starter_prompts=weekly_review_chat_starter_prompts,
        weekly_review_chat_has_user_messages=weekly_review_chat_has_user_messages,
        weekly_review_chat_send_state=weekly_review_chat_send_state,
        weekly_review_chat_usage=weekly_review_chat_usage,
        weekly_review_chat_history=weekly_review_chat_history,
        show_weekly_journal_preview=show_weekly_journal_preview,
        weekly_journal_preview=weekly_journal_preview,
        weekly_ai_generated_at_label=weekly_ai_state["weekly_ai_generated_at_label"],
        weekly_ai_period_label=weekly_ai_state["weekly_ai_period_label"],
        weekly_ai_empty_message=weekly_ai_state["weekly_ai_empty_message"],
        weekly_ai_is_generating=weekly_ai_state["weekly_ai_is_generating"],
        weekly_ai_needs_checkin=weekly_ai_state.get("weekly_ai_needs_checkin", False),
        show_onboarding_banner=show_onboarding_banner,
        onboarding_was_skipped=onboarding_banner_state["onboarding_was_skipped"],
        dashboard_continuity=dashboard_continuity,
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
        has_mt5_submission=has_mt5_submission,
        is_pure_zero_data=is_pure_zero_data,
        show_zero_data_soft_waitlist=show_zero_data_soft_waitlist,
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
        active_account_type=active_account_type,
        is_active_cfd=is_active_cfd,
        is_active_futures=is_active_futures,
        show_mt5_panel=show_mt5_panel,
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

    user_row = db.session.get(User, user_id)
    send_gate = can_send_weekly_followup_message(user_row, review)
    if not send_gate["allowed"]:
        status_code = 429 if send_gate.get("reason") in (
            "trial_message_limit_reached",
            "rate_limit_exceeded",
        ) else 403
        return jsonify({
            "error": send_gate.get("error") or send_gate.get("reason") or "upgrade_required",
            "message": send_gate.get("message"),
            "cta": send_gate.get("cta"),
            "usage": send_gate.get("usage"),
        }), status_code

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
        reply, suggested_prompts, _response_payload, model_used = generate_weekly_review_chat_reply(
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

    reply_display = _build_weekly_review_chat_reply_display(
        review,
        reply,
        get_display_timezone_name(),
    )

    start_premium_trial_if_needed(user_row, getattr(review, "trade_account", None))
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
                content=pack_weekly_review_chat_assistant_content(reply, suggested_prompts),
                model_used=model_used,
                prompt_version=WEEKLY_REVIEW_CHAT_PROMPT_VERSION,
            ),
        ]
    )
    db.session.commit()
    return jsonify(
        {
            "reply": reply_display["text"],
            "segments": reply_display["segments"],
            "suggested_prompts": suggested_prompts,
        }
    )


def _dashboard_journal_support_blocked():
    return (
        jsonify(
            {
                "error": "support_view_read_only",
                "message": "That action is not available in read-only support view.",
            }
        ),
        403,
    )


def _dashboard_journal_admin_only(user):
    if user is None or not user_has_admin_access(user):
        return (
            jsonify(
                {
                    "error": "admin_only",
                    "message": "AI journal is only available to admin accounts.",
                }
            ),
            403,
        )
    return None


def _dashboard_journal_urls(session_id):
    return {
        "chat_url": url_for("dashboard.dashboard_journal_chat", session_id=session_id),
        "tags_url": url_for("dashboard.dashboard_journal_tags", session_id=session_id),
        "feedback_url_template": url_for("dashboard.dashboard_journal_feedback", message_id=0),
    }


_DASHBOARD_JOURNAL_MARKET_TZ = ZoneInfo("America/New_York")
_DASHBOARD_JOURNAL_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_DASHBOARD_JOURNAL_UPPER_TOKEN_RE = re.compile(r"\b[A-Z0-9]{4,12}\b")


def _dashboard_journal_normalize_symbol(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _dashboard_journal_parse_dates(message):
    dates = []
    seen = set()
    for raw in _DASHBOARD_JOURNAL_DATE_RE.findall(message or ""):
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        if parsed in seen:
            continue
        dates.append(parsed)
        seen.add(parsed)
    return dates


def _dashboard_journal_trade_label(trade):
    symbol = str(getattr(trade, "symbol", "") or "").strip() or "Trade"
    closed_at = getattr(trade, "closed_at", None)
    date_label = closed_at.strftime("%Y-%m-%d") if closed_at else "open"
    pnl = getattr(trade, "pnl", None)
    pnl_label = ""
    if pnl is not None:
        try:
            pnl_label = f" ({float(pnl):+.2f})"
        except (TypeError, ValueError):
            pnl_label = ""
    return f"{symbol} {date_label}{pnl_label}"


def _dashboard_journal_candidate(candidate_id, scope_type, label, reason, session_payload, score):
    return {
        "id": candidate_id,
        "scope_type": scope_type,
        "label": label,
        "reason": reason,
        "session_payload": session_payload,
        "score": score,
    }


def _dashboard_journal_week_scope_date(user_id, trade_account_id):
    period = (
        get_latest_trade_week_period(user_id=user_id, trade_account_id=trade_account_id)
        or get_weekly_dashboard_period()
    )
    start_utc = period.get("period_start_utc") if isinstance(period, dict) else None
    if not isinstance(start_utc, datetime):
        return utcnow_naive().date()
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    else:
        start_utc = start_utc.astimezone(timezone.utc)
    return start_utc.astimezone(_DASHBOARD_JOURNAL_MARKET_TZ).date()


def _dashboard_journal_week_candidate(user_id, trade_account_id, score=50):
    week_start = _dashboard_journal_week_scope_date(user_id, trade_account_id)
    return _dashboard_journal_candidate(
        f"week:{week_start.isoformat()}",
        JournalSession.SCOPE_WEEK,
        f"Dashboard review week starting {week_start.isoformat()}",
        "Best default when the first message is broad or exploratory.",
        {"scope_type": JournalSession.SCOPE_WEEK, "scope_date": week_start.isoformat()},
        score,
    )


def _dashboard_journal_freeform_candidate(score=40, *, reason=None):
    return _dashboard_journal_candidate(
        "freeform:recent",
        JournalSession.SCOPE_FREEFORM,
        "Full account context",
        reason or "Use broad active-account context. For now this uses the latest closed trades.",
        {"scope_type": JournalSession.SCOPE_FREEFORM},
        score,
    )


def _dashboard_journal_recent_trade_candidates(trades, *, limit=6):
    candidates = []
    for trade in trades[:limit]:
        candidates.append(
            _dashboard_journal_candidate(
                f"trade:{trade.pubkey}",
                JournalSession.SCOPE_TRADE,
                _dashboard_journal_trade_label(trade),
                "Use only this closed trade as context.",
                {"scope_type": JournalSession.SCOPE_TRADE, "scope_trade_pubkey": trade.pubkey},
                35,
            )
        )
    return candidates


def _dashboard_journal_context_candidates(user, message):
    active_account = get_active_trade_account_for_user(user.id)
    trade_account_id = getattr(active_account, "id", None)
    raw_message = str(message or "")
    compact_message = _dashboard_journal_normalize_symbol(raw_message)
    parsed_dates = _dashboard_journal_parse_dates(raw_message)
    explicit_unknown_token = any(
        token not in {"WHAT", "WHEN", "WEEK", "THIS", "THAT", "TRADE", "TRADES", "REFLECT", "ABOUT"}
        for token in _DASHBOARD_JOURNAL_UPPER_TOKEN_RE.findall(raw_message)
    )

    query = Trade.query.filter(
        Trade.user_id == user.id,
        Trade.closed_at.isnot(None),
    )
    if trade_account_id is not None:
        query = query.filter(Trade.trade_account_id == trade_account_id)
    trades = query.order_by(Trade.closed_at.desc(), Trade.id.desc()).limit(200).all()

    matched_symbols = {
        _dashboard_journal_normalize_symbol(trade.symbol)
        for trade in trades
        if _dashboard_journal_normalize_symbol(trade.symbol)
        and _dashboard_journal_normalize_symbol(trade.symbol) in compact_message
    }
    date_set = set(parsed_dates)
    primary_candidates = []

    symbol_date_matches = [
        trade
        for trade in trades
        if _dashboard_journal_normalize_symbol(trade.symbol) in matched_symbols
        and getattr(trade, "closed_at", None) is not None
        and trade.closed_at.date() in date_set
    ]
    for trade in symbol_date_matches[:3]:
        closed_date = trade.closed_at.date().isoformat()
        primary_candidates.append(
            _dashboard_journal_candidate(
                f"trade:{trade.pubkey}",
                JournalSession.SCOPE_TRADE,
                _dashboard_journal_trade_label(trade),
                f"Matched {trade.symbol} and {closed_date} in your message.",
                {"scope_type": JournalSession.SCOPE_TRADE, "scope_trade_pubkey": trade.pubkey},
                95,
            )
        )

    if not primary_candidates and matched_symbols:
        for trade in trades:
            symbol_key = _dashboard_journal_normalize_symbol(trade.symbol)
            if symbol_key not in matched_symbols:
                continue
            primary_candidates.append(
                _dashboard_journal_candidate(
                    f"trade:{trade.pubkey}",
                    JournalSession.SCOPE_TRADE,
                    _dashboard_journal_trade_label(trade),
                    f"Matched the {trade.symbol} symbol and chose the latest closed trade.",
                    {"scope_type": JournalSession.SCOPE_TRADE, "scope_trade_pubkey": trade.pubkey},
                    85,
                )
            )
            break

    if not primary_candidates and parsed_dates:
        for parsed_date in parsed_dates[:3]:
            day_trades = [
                trade
                for trade in trades
                if getattr(trade, "closed_at", None) is not None and trade.closed_at.date() == parsed_date
            ]
            if len(day_trades) == 1:
                trade = day_trades[0]
                primary_candidates.append(
                    _dashboard_journal_candidate(
                        f"trade:{trade.pubkey}",
                        JournalSession.SCOPE_TRADE,
                        _dashboard_journal_trade_label(trade),
                        f"Only one closed trade matched {parsed_date.isoformat()}.",
                        {"scope_type": JournalSession.SCOPE_TRADE, "scope_trade_pubkey": trade.pubkey},
                        82,
                    )
                )
            elif len(day_trades) > 1:
                primary_candidates.append(
                    _dashboard_journal_candidate(
                        f"day:{parsed_date.isoformat()}",
                        JournalSession.SCOPE_DAY,
                        f"{parsed_date.isoformat()} trading day",
                        f"Matched {len(day_trades)} closed trades on that date.",
                        {"scope_type": JournalSession.SCOPE_DAY, "scope_date": parsed_date.isoformat()},
                        80,
                    )
                )

    dated_context_candidates = []
    for parsed_date in parsed_dates[:3]:
        if any(candidate["id"] == f"day:{parsed_date.isoformat()}" for candidate in primary_candidates):
            continue
        dated_context_candidates.append(
            _dashboard_journal_candidate(
                f"day:{parsed_date.isoformat()}",
                JournalSession.SCOPE_DAY,
                f"{parsed_date.isoformat()} trading day",
                "Use the closed trades from this UTC date as context.",
                {"scope_type": JournalSession.SCOPE_DAY, "scope_date": parsed_date.isoformat()},
                55,
            )
        )

    fallback_candidates = [
        _dashboard_journal_week_candidate(user.id, trade_account_id),
        _dashboard_journal_freeform_candidate(
            reason="Use broad active-account context when you want the journal to look across recent trades."
            if parsed_dates or explicit_unknown_token
            else None
        ),
        *_dashboard_journal_recent_trade_candidates(trades),
    ]
    candidates = []
    seen_ids = set()
    for candidate in [*primary_candidates, *dated_context_candidates, *fallback_candidates]:
        if candidate["id"] in seen_ids:
            continue
        candidates.append(candidate)
        seen_ids.add(candidate["id"])

    if primary_candidates:
        recommended = primary_candidates[0]
    elif parsed_dates or explicit_unknown_token:
        recommended = next(
            (candidate for candidate in candidates if candidate["scope_type"] == JournalSession.SCOPE_FREEFORM),
            candidates[0] if candidates else None,
        )
    else:
        recommended = next(
            (candidate for candidate in candidates if candidate["scope_type"] == JournalSession.SCOPE_WEEK),
            candidates[0] if candidates else None,
        )
    return {"recommended": recommended, "candidates": candidates}


@bp.route("/dashboard/journal/context-candidates", methods=["POST"])
@login_required
def dashboard_journal_context_candidates():
    if is_support_view_session_active():
        return _dashboard_journal_support_blocked()
    user_id = get_effective_user_id()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "not_found", "message": "User not found."}), 404
    deny = _dashboard_journal_admin_only(user)
    if deny:
        return deny
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message_required", "message": "Type a reflection prompt first."}), 400
    if len(message) > 1200:
        return jsonify({"error": "message_too_long", "message": "Keep messages under 1200 characters."}), 400
    result = _dashboard_journal_context_candidates(user, message)
    return jsonify({"message": message, **result})


@bp.route("/dashboard/journal/sessions", methods=["POST"])
@login_required
def dashboard_journal_create_session():
    if is_support_view_session_active():
        return _dashboard_journal_support_blocked()
    user_id = get_effective_user_id()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "not_found", "message": "User not found."}), 404
    deny = _dashboard_journal_admin_only(user)
    if deny:
        return deny
    payload = request.get_json(silent=True) or {}
    journal_session, err = create_journal_session_from_incoming(user, payload)
    if err is not None:
        current_app.logger.info(
            "Dashboard journal session create rejected. user_id=%s scope_type=%s error=%s",
            user.id,
            str(payload.get("scope_type") or "").strip().lower(),
            err.get("error"),
        )
        status = 404 if err.get("error") == "trade_not_found" else 400
        return jsonify(err), status
    api = journal_session_api_payload(user, journal_session)
    api.update(_dashboard_journal_urls(journal_session.id))
    return jsonify(api)


@bp.route("/dashboard/journal/sessions/<int:session_id>", methods=["GET"])
@login_required
def dashboard_journal_session_detail(session_id):
    user_id = get_effective_user_id()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "not_found", "message": "User not found."}), 404
    deny = _dashboard_journal_admin_only(user)
    if deny:
        return deny
    journal_session = get_journal_session_for_user(user.id, session_id)
    if journal_session is None:
        return jsonify({"error": "session_not_found", "message": "Session not found."}), 404
    api = journal_session_api_payload(user, journal_session)
    api.update(_dashboard_journal_urls(journal_session.id))
    return jsonify(api)


@bp.route("/dashboard/journal/sessions/<int:session_id>/chat", methods=["POST"])
@login_required
def dashboard_journal_chat(session_id):
    if is_support_view_session_active():
        return _dashboard_journal_support_blocked()
    user_id = get_effective_user_id()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "not_found"}), 404
    deny = _dashboard_journal_admin_only(user)
    if deny:
        return deny
    journal_session = get_journal_session_for_user(user.id, session_id)
    if journal_session is None:
        return jsonify({"error": "session_not_found", "message": "Session not found."}), 404
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    body, status = journal_post_chat_response(user, journal_session, message)
    return jsonify(body), status


@bp.route("/dashboard/journal/sessions/<int:session_id>/tags", methods=["POST"])
@login_required
def dashboard_journal_tags(session_id):
    if is_support_view_session_active():
        return _dashboard_journal_support_blocked()
    user_id = get_effective_user_id()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "not_found"}), 404
    deny = _dashboard_journal_admin_only(user)
    if deny:
        return deny
    journal_session = get_journal_session_for_user(user.id, session_id)
    if journal_session is None:
        return jsonify({"error": "session_not_found"}), 404
    payload = request.get_json(silent=True) if request.is_json else request.form
    return jsonify(journal_update_tags_response(user, journal_session, payload or {}))


@bp.route("/dashboard/journal/messages/<int:message_id>/feedback", methods=["POST"])
@login_required
def dashboard_journal_feedback(message_id):
    if is_support_view_session_active():
        return _dashboard_journal_support_blocked()
    user_id = get_effective_user_id()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"error": "not_found"}), 404
    deny = _dashboard_journal_admin_only(user)
    if deny:
        return deny
    payload = request.get_json(silent=True) or {}
    body, status = journal_update_feedback_response(user, message_id, payload)
    return jsonify(body), status


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

    active_account_type = (
        normalize_account_type(active_trade_account.account_type)
        if active_trade_account is not None
        else "CFD"
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
        active_account_type=active_account_type,
        is_active_cfd=active_account_type == "CFD",
        is_active_futures=active_account_type == "FUTURES",
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
