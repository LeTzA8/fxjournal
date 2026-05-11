from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy.orm import selectinload

from ai_service import (
    AIRequestError,
    JOURNAL_CHAT_PROMPT_VERSION,
    generate_journal_chat_reply,
)
from auth_account import user_has_admin_access, user_has_root_admin_access
from helpers.core import get_active_trade_account_for_user
from helpers.journal_context import build_journal_payload
from helpers.trade_state import trade_is_closed
from helpers.utils import login_required, utcnow_naive
from models import JournalMessage, JournalSession, Trade, User, db


bp = Blueprint("admin_journal", __name__, url_prefix="/admin/journal")

JOURNAL_CHAT_MAX_CHARS = 1200
JOURNAL_CHAT_HISTORY_LIMIT = 16
JOURNAL_ALLOWED_SCOPES = {
    JournalSession.SCOPE_TRADE,
    JournalSession.SCOPE_DAY,
    JournalSession.SCOPE_FREEFORM,
}
JOURNAL_ALLOWED_FEEDBACK = {
    JournalMessage.FEEDBACK_USEFUL,
    JournalMessage.FEEDBACK_GENERIC,
    JournalMessage.FEEDBACK_NEEDED_MORE_CONTEXT,
    JournalMessage.FEEDBACK_MISSING_FEATURE,
}
JOURNAL_TAG_SUGGESTIONS = [
    "emotion:frustrated",
    "emotion:confident",
    "emotion:bored",
    "emotion:tilted",
    "emotion:fearful",
    "theme:revenge",
    "theme:fomo",
    "theme:early-exit",
    "theme:oversized",
    "theme:no-plan",
    "rule:stop-after-2-losses",
    "feature_needed:per-strategy-winrate",
    "missing_context:screenshots",
]
_JOURNAL_REF_RE = re.compile(r"\[\s*(T\d+)\s*\]", re.IGNORECASE)


def _current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def _admin_journal_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        user = _current_user()
        if user is None or not user_has_admin_access(user):
            abort(404)
        return view_func(*args, **kwargs)

    return wrapped


def _admin_shell_context(user, *, section="journal", page_heading="Journal - admin research", page_subtitle="Dogfood conversational reflection against structured trade data."):
    return {
        "section": section,
        "page_heading": page_heading,
        "page_subtitle": page_subtitle,
        "username": getattr(user, "username", "admin"),
        "is_root_admin": user_has_root_admin_access(user),
        "overview": {},
        "registration_paused": False,
        "auto_approve_new_users": False,
        "signup_code_mode": "unknown",
    }


def get_journal_session_for_user(user_id, session_id):
    return (
        JournalSession.query.filter_by(id=session_id, user_id=user_id)
        .options(selectinload(JournalSession.messages))
        .first()
    )


def _session_or_404(user_id, session_id):
    journal_session = get_journal_session_for_user(user_id, session_id)
    if journal_session is None:
        abort(404)
    return journal_session


def _parse_tags(value):
    if isinstance(value, list):
        raw_tags = value
    else:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        raw_tags = parsed if isinstance(parsed, list) else text.split(",")
    tags = []
    seen = set()
    for raw_tag in raw_tags:
        tag = str(raw_tag or "").strip()
        if not tag or tag in seen:
            continue
        tags.append(tag[:80])
        seen.add(tag)
    return tags[:30]


def _session_tags(session_row):
    try:
        parsed = json.loads(session_row.tags_json or "[]")
    except (TypeError, ValueError):
        return []
    return [str(tag) for tag in parsed if str(tag or "").strip()]


def _trade_label(trade):
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
    return f"{symbol} - {date_label}{pnl_label}"


def _recent_trade_options(user_id, trade_account_id):
    since = utcnow_naive() - timedelta(days=60)
    trades = (
        Trade.query.filter(
            Trade.user_id == user_id,
            Trade.trade_account_id == trade_account_id,
            Trade.closed_at.isnot(None),
            Trade.closed_at >= since,
        )
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .limit(100)
        .all()
    )
    return [{"pubkey": trade.pubkey, "label": _trade_label(trade)} for trade in trades if trade_is_closed(trade)]


def _session_preview(session_row):
    last_message = (
        JournalMessage.query.filter_by(session_id=session_row.id)
        .order_by(JournalMessage.created_at.desc(), JournalMessage.id.desc())
        .first()
    )
    preview = str(getattr(last_message, "content", "") or "").strip()
    if len(preview) > 120:
        preview = preview[:117].rstrip() + "..."
    return {
        "session": session_row,
        "tags": _session_tags(session_row),
        "last_message_preview": preview,
    }


def _build_citation_lookup(payload):
    lookup = {}
    trades = payload.get("trades") if isinstance(payload, dict) else []
    if not isinstance(trades, list):
        return lookup
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        ref = str(trade.get("ref") or "").strip().upper()
        if not ref:
            continue
        symbol = str(trade.get("symbol") or "").strip() or ref
        closed_at = str(trade.get("closed_at") or "").strip()
        label = symbol if not closed_at else f"{symbol} {closed_at[:10]}"
        lookup[ref] = {
            "label": label,
            "citation_type": "trade",
            "trade_pubkey": trade.get("trade_pubkey"),
            "trade_url": url_for("trades.trade_detail", trade_pubkey=trade.get("trade_pubkey")) if trade.get("trade_pubkey") else "",
            "tone": "neutral",
        }
    return lookup


def _display_segments(text, payload):
    normalized = str(text or "").strip()
    if not normalized:
        return []
    lookup = _build_citation_lookup(payload)
    segments = []
    cursor = 0
    for match in _JOURNAL_REF_RE.finditer(normalized):
        ref = str(match.group(1) or "").upper()
        citation = lookup.get(ref)
        if citation is None:
            continue
        if match.start() > cursor:
            segments.append({"type": "text", "text": normalized[cursor:match.start()]})
        segments.append({"type": "citation", **citation})
        cursor = match.end()
    if cursor < len(normalized):
        segments.append({"type": "text", "text": normalized[cursor:]})
    if not segments:
        segments.append({"type": "text", "text": normalized})
    return segments


def _display_message(message, payload):
    content = str(getattr(message, "content", "") or "")
    if getattr(message, "role", None) == JournalMessage.ROLE_ASSISTANT:
        return {
            "id": message.id,
            "role": message.role,
            "text": _JOURNAL_REF_RE.sub("", content).strip(),
            "segments": _display_segments(content, payload),
            "feedback": message.feedback,
            "feedback_note": message.feedback_note,
        }
    return {
        "id": message.id,
        "role": message.role,
        "text": content,
        "segments": [{"type": "text", "text": content}],
        "feedback": message.feedback,
        "feedback_note": message.feedback_note,
    }


def _context_summary(payload):
    trades = payload.get("trades") if isinstance(payload, dict) else []
    refs = [
        {
            "ref": trade.get("ref"),
            "label": f"{trade.get('symbol') or 'Trade'} {str(trade.get('closed_at') or '')[:10]}".strip(),
            "url": url_for("trades.trade_detail", trade_pubkey=trade.get("trade_pubkey")) if trade.get("trade_pubkey") else "",
        }
        for trade in trades
        if isinstance(trade, dict)
    ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "line": f"{payload.get('scope_type', 'session')} scope with {len(refs)} trade refs.",
        "refs": refs,
        "raw": summary,
    }


def create_journal_session_from_incoming(user, incoming):
    """
    Create and persist a journal session for the given user.
    Returns (JournalSession, None) on success, or (None, err_payload) where err_payload
    is a dict suitable for jsonify (no status code).
    """
    incoming = incoming or {}
    scope_type = str(incoming.get("scope_type") or "").strip().lower()
    if scope_type not in JOURNAL_ALLOWED_SCOPES:
        return None, {"error": "invalid_scope", "message": "Choose a valid reflection scope."}

    active_account = get_active_trade_account_for_user(user.id)
    trade_account_id = getattr(active_account, "id", None)
    scope_trade_pubkey = None
    scope_date = None

    if scope_type == JournalSession.SCOPE_TRADE:
        scope_trade_pubkey = str(incoming.get("scope_trade_pubkey") or "").strip()
        if not scope_trade_pubkey:
            return None, {"error": "missing_trade", "message": "Pick a closed trade to reflect on."}
        trade = Trade.query.filter_by(user_id=user.id, pubkey=scope_trade_pubkey).first()
        if trade is None:
            return None, {"error": "trade_not_found", "message": "That trade was not found."}
        trade_account_id = trade.trade_account_id
    elif scope_type == JournalSession.SCOPE_DAY:
        raw_date = str(incoming.get("scope_date") or "").strip()
        try:
            scope_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            return None, {"error": "invalid_date", "message": "Use a valid calendar date."}
    elif scope_type == JournalSession.SCOPE_FREEFORM:
        scope_trade_pubkey = None

    journal_session = JournalSession(
        user_id=user.id,
        trade_account_id=trade_account_id,
        scope_type=scope_type,
        scope_trade_pubkey=scope_trade_pubkey,
        scope_date=scope_date,
        started_at=utcnow_naive(),
    )
    db.session.add(journal_session)
    db.session.commit()
    return journal_session, None


def journal_session_api_payload(user, journal_session):
    """Serializable session state for dashboard inline journal."""
    payload = build_journal_payload(user, journal_session)
    messages = (
        JournalMessage.query.filter_by(session_id=journal_session.id, user_id=user.id)
        .order_by(JournalMessage.created_at.asc(), JournalMessage.id.asc())
        .all()
    )
    return {
        "session_id": journal_session.id,
        "context_summary": _context_summary(payload),
        "messages": [_display_message(message, payload) for message in messages],
        "session_tags": _session_tags(journal_session),
        "title": journal_session.title or "",
        "notes": journal_session.notes or "",
    }


def journal_post_chat_response(user, journal_session, message):
    """Run journal chat turn; returns (response_dict, http_status)."""
    message = str(message or "").strip()
    if not message:
        return {"error": "Ask a journal question first."}, 400
    if len(message) > JOURNAL_CHAT_MAX_CHARS:
        return {"error": f"Keep messages under {JOURNAL_CHAT_MAX_CHARS} characters."}, 400

    journal_payload = build_journal_payload(user, journal_session)
    chat_history = (
        JournalMessage.query.filter_by(session_id=journal_session.id, user_id=user.id)
        .order_by(JournalMessage.created_at.desc(), JournalMessage.id.desc())
        .limit(JOURNAL_CHAT_HISTORY_LIMIT)
        .all()
    )
    chat_history.reverse()

    try:
        reply, _response_payload, model_used = generate_journal_chat_reply(
            journal_session,
            journal_payload,
            message,
            chat_history=chat_history,
        )
    except AIRequestError as exc:
        current_app.logger.warning(
            "Journal chat AI request failed. user_id=%s session_id=%s error=%s",
            user.id,
            journal_session.id,
            exc,
        )
        return {"error": "Could not answer that right now. Please try again shortly."}, 502
    except Exception:
        current_app.logger.exception("Journal chat failed. user_id=%s session_id=%s", user.id, journal_session.id)
        return {"error": "Could not answer that right now. Please try again shortly."}, 500

    reply_segments = _display_segments(reply, journal_payload)
    citations_json = json.dumps([segment for segment in reply_segments if segment.get("type") == "citation"])
    user_row = JournalMessage(
        session_id=journal_session.id,
        user_id=user.id,
        role=JournalMessage.ROLE_USER,
        content=message,
        prompt_version=JOURNAL_CHAT_PROMPT_VERSION,
    )
    assistant_row = JournalMessage(
        session_id=journal_session.id,
        user_id=user.id,
        role=JournalMessage.ROLE_ASSISTANT,
        content=reply,
        model_used=model_used,
        prompt_version=JOURNAL_CHAT_PROMPT_VERSION,
        citations_json=citations_json,
    )
    db.session.add_all([user_row, assistant_row])
    db.session.commit()
    return (
        {
            "reply": _JOURNAL_REF_RE.sub("", reply).strip(),
            "segments": reply_segments,
            "message_id": assistant_row.id,
        },
        200,
    )


def journal_update_tags_response(user, journal_session, payload):
    title = str(payload.get("title") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    tags = _parse_tags(payload.get("tags") or payload.get("tags_json") or "")
    journal_session.title = title[:200] or None
    journal_session.notes = notes or None
    journal_session.tags_json = json.dumps(tags) if tags else None
    db.session.commit()
    return {
        "ok": True,
        "title": journal_session.title,
        "tags": tags,
        "notes": journal_session.notes or "",
    }


def journal_update_feedback_response(user, message_id, payload):
    message = JournalMessage.query.filter_by(id=message_id, user_id=user.id).first()
    if message is None or message.role != JournalMessage.ROLE_ASSISTANT:
        return {"error": "not_found"}, 404
    feedback = str(payload.get("feedback") or "").strip()
    if feedback not in JOURNAL_ALLOWED_FEEDBACK:
        return {"error": "Unsupported feedback value."}, 400
    message.feedback = feedback
    message.feedback_note = str(payload.get("feedback_note") or "").strip()[:1000] or None
    db.session.commit()
    return {"ok": True, "feedback": message.feedback, "feedback_note": message.feedback_note or ""}, 200


@bp.route("", methods=["GET"])
@_admin_journal_required
@login_required
def journal_home():
    user = _current_user()
    active_account = get_active_trade_account_for_user(user.id)
    account_id = getattr(active_account, "id", None)
    sessions = (
        JournalSession.query.filter_by(user_id=user.id)
        .order_by(JournalSession.started_at.desc(), JournalSession.id.desc())
        .limit(50)
        .all()
    )
    recent_trades = _recent_trade_options(user.id, account_id) if account_id is not None else []
    context = _admin_shell_context(user)
    return render_template(
        "admin_journal.html",
        **context,
        active_trade_account=active_account,
        recent_trades=recent_trades,
        session_rows=[_session_preview(row) for row in sessions],
        today_utc=utcnow_naive().date().isoformat(),
    )


@bp.route("/sessions", methods=["POST"])
@_admin_journal_required
@login_required
def create_session():
    user = _current_user()
    incoming = request.get_json(silent=True) if request.is_json else request.form
    journal_session, err = create_journal_session_from_incoming(user, incoming or {})
    if err is not None:
        abort(400)
    return redirect(url_for("admin_journal.view_session", session_id=journal_session.id))


@bp.route("/sessions/<int:session_id>", methods=["GET"])
@_admin_journal_required
@login_required
def view_session(session_id):
    user = _current_user()
    journal_session = _session_or_404(user.id, session_id)
    payload = build_journal_payload(user, journal_session)
    messages = (
        JournalMessage.query.filter_by(session_id=journal_session.id, user_id=user.id)
        .order_by(JournalMessage.created_at.asc(), JournalMessage.id.asc())
        .all()
    )
    context = _admin_shell_context(
        user,
        page_heading="Journal session",
        page_subtitle="Single-thread research chat. Keep it scoped to the selected trade data.",
    )
    return render_template(
        "admin_journal_session.html",
        **context,
        journal_session=journal_session,
        session_tags=_session_tags(journal_session),
        suggested_tags=JOURNAL_TAG_SUGGESTIONS,
        context_summary=_context_summary(payload),
        messages=[_display_message(message, payload) for message in messages],
        csrf_token_value="",
    )


@bp.route("/sessions/<int:session_id>/chat", methods=["POST"])
@_admin_journal_required
@login_required
def post_chat(session_id):
    user = _current_user()
    journal_session = _session_or_404(user.id, session_id)
    payload = request.get_json(silent=True) or {}
    message = str(payload.get("message") or "").strip()
    body, status = journal_post_chat_response(user, journal_session, message)
    return jsonify(body), status


@bp.route("/sessions/<int:session_id>/tags", methods=["POST"])
@_admin_journal_required
@login_required
def update_tags(session_id):
    user = _current_user()
    journal_session = _session_or_404(user.id, session_id)
    payload = request.get_json(silent=True) if request.is_json else request.form
    return jsonify(journal_update_tags_response(user, journal_session, payload or {}))


@bp.route("/messages/<int:message_id>/feedback", methods=["POST"])
@_admin_journal_required
@login_required
def update_feedback(message_id):
    user = _current_user()
    payload = request.get_json(silent=True) or {}
    body, status = journal_update_feedback_response(user, message_id, payload)
    if status == 404:
        abort(404)
    return jsonify(body), status


@bp.route("/sessions/<int:session_id>/end", methods=["POST"])
@_admin_journal_required
@login_required
def end_session(session_id):
    user = _current_user()
    journal_session = _session_or_404(user.id, session_id)
    if journal_session.ended_at is None:
        journal_session.ended_at = utcnow_naive()
        db.session.commit()
    return redirect(url_for("admin_journal.view_session", session_id=journal_session.id))
