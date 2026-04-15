import os
import json
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Response, abort, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import case, func, or_
from sqlalchemy.orm import contains_eager, joinedload, load_only, selectinload
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash
from ai_service import MIN_CLOSED_TRADES_FOR_ADVICE, WEEKLY_DASHBOARD_KIND, get_latest_trade_week_period
from models import (
    AIGeneratedResponse,
    AllowedSignupEmailDomain,
    CFDSymbol,
    MT5Account,
    MT5AccessRequest,
    MT5SyncBatch,
    SignupCode,
    Trade,
    TradeAccount,
    TradeBars,
    TradeInterpretation,
    User,
    UserProfile,
    db,
)
from helpers.core import (
    SUPPORT_VIEW_ACTIVE_TRADE_ACCOUNT_SESSION_KEY,
    SUPPORT_VIEW_ADMIN_USER_SESSION_KEY,
    SUPPORT_VIEW_ADMIN_USERNAME_SESSION_KEY,
    SUPPORT_VIEW_TARGET_USER_SESSION_KEY,
    archive_mt5_account,
    clear_support_view_session,
    delete_users_with_related_data,
    get_mt5_sync_batch_state,
    reactivate_mt5_account,
    queue_mt5_account_cleanup,
    sanitize_error_message,
)
from trading import (
    clear_cfd_symbol_cache,
    collect_active_cfd_alias_key_conflicts,
    format_cfd_aliases_for_storage,
)
from helpers.trade_analysis import detect_outliers
from helpers.trade_interpretation import apply_interpretation
from helpers.utils import (
    encrypt_password,
    env_bool as _env_bool,
    env_int as _env_int,
    login_required,
    utcnow_naive,
)
from extensions import oauth

TOKEN_PURPOSE_PENDING_REGISTRATION = "pending_registration"
TOKEN_PURPOSE_EMAIL_CHANGE = "email_change"
TOKEN_PURPOSE_PASSWORD_RESET = "password_reset"
GOOGLE_AUTH_INTENT_LOGIN = "login"
GOOGLE_AUTH_INTENT_REGISTER = "register"
GOOGLE_AUTH_SESSION_INTENT_KEY = "google_auth_intent"
GOOGLE_AUTH_SESSION_SIGNUP_CODE_KEY = "google_auth_signup_code"
PENDING_REGISTRATIONS = {}
SIGNUP_STATUS_PENDING = "pending"
SIGNUP_STATUS_APPROVED = "approved"
SIGNUP_STATUS_REJECTED = "rejected"
SIGNUP_STATUS_SUSPENDED = "suspended"
VALID_SIGNUP_STATUSES = {
    SIGNUP_STATUS_PENDING,
    SIGNUP_STATUS_APPROVED,
    SIGNUP_STATUS_REJECTED,
    SIGNUP_STATUS_SUSPENDED,
}
SIGNUP_CODE_MODE_OFF = "off"
SIGNUP_CODE_MODE_OPTIONAL = "optional"
SIGNUP_CODE_MODE_REQUIRED = "required"
VALID_SIGNUP_CODE_MODES = {
    SIGNUP_CODE_MODE_OFF,
    SIGNUP_CODE_MODE_OPTIONAL,
    SIGNUP_CODE_MODE_REQUIRED,
}
ONBOARDING_TRADING_STYLE_OPTIONS = (
    {"value": "scalper", "label": "Scalper", "hint": "Minutes"},
    {"value": "intraday", "label": "Intraday", "hint": "Hours"},
    {"value": "swing", "label": "Swing", "hint": "Days"},
    {"value": "position", "label": "Position", "hint": "Weeks"},
)
ONBOARDING_INSTRUMENT_OPTIONS = (
    {"value": "forex", "label": "Forex only", "hint": ""},
    {"value": "indices", "label": "Indices only", "hint": ""},
    {"value": "gold", "label": "Gold / Commodities", "hint": ""},
    {"value": "mixed", "label": "Mixed", "hint": "Multiple markets"},
)
ONBOARDING_EXPERIENCE_LEVEL_OPTIONS = (
    {"value": "beginner", "label": "Just starting out", "hint": ""},
    {"value": "intermediate", "label": "Some experience", "hint": "1-2 years"},
    {"value": "experienced", "label": "Experienced", "hint": "3+ years"},
)
VALID_ONBOARDING_TRADING_STYLES = {option["value"] for option in ONBOARDING_TRADING_STYLE_OPTIONS}
VALID_ONBOARDING_INSTRUMENTS = {option["value"] for option in ONBOARDING_INSTRUMENT_OPTIONS}
VALID_ONBOARDING_EXPERIENCE_LEVELS = {
    option["value"] for option in ONBOARDING_EXPERIENCE_LEVEL_OPTIONS
}

ADMIN_USERS_SORT_DEFAULT = "default"
ADMIN_USERS_SORT_CHOICES = frozenset(
    {
        ADMIN_USERS_SORT_DEFAULT,
        "created_desc",
        "created_asc",
        "approved_desc",
        "approved_asc",
        "username_asc",
        "username_desc",
        "email_asc",
        "email_desc",
        "login_desc",
        "login_asc",
        "id_desc",
        "id_asc",
    }
)

ADMIN_MT5_SORT_DEFAULT = "created_desc"
ADMIN_MT5_SORT_CHOICES = frozenset(
    {
        ADMIN_MT5_SORT_DEFAULT,
        "created_asc",
        "sync_desc",
        "sync_asc",
        "login_asc",
        "login_desc",
        "server_asc",
        "server_desc",
        "id_desc",
        "id_asc",
    }
)


def normalize_admin_users_sort(raw_value):
    key = (raw_value or "").strip().lower()
    return key if key in ADMIN_USERS_SORT_CHOICES else ADMIN_USERS_SORT_DEFAULT


def normalize_admin_mt5_sort(raw_value):
    key = (raw_value or "").strip().lower()
    return key if key in ADMIN_MT5_SORT_CHOICES else ADMIN_MT5_SORT_DEFAULT


def apply_admin_users_sort(query, sort_key):
    sort_key = normalize_admin_users_sort(sort_key)
    if sort_key == ADMIN_USERS_SORT_DEFAULT:
        return query.order_by(
            User.signup_status.asc(),
            User.email_verified.asc(),
            User.id.desc(),
        )
    ordering = {
        "created_desc": (User.created_at.desc(), User.id.desc()),
        "created_asc": (User.created_at.asc(), User.id.asc()),
        "approved_desc": (User.approved_at.desc(), User.id.desc()),
        "approved_asc": (User.approved_at.asc(), User.id.asc()),
        "username_asc": (User.username.asc(), User.id.asc()),
        "username_desc": (User.username.desc(), User.id.desc()),
        "email_asc": (User.email.asc(), User.id.asc()),
        "email_desc": (User.email.desc(), User.id.desc()),
        "login_desc": (User.last_login_at.desc().nulls_last(), User.id.desc()),
        "login_asc": (User.last_login_at.asc().nulls_last(), User.id.asc()),
        "id_desc": (User.id.desc(),),
        "id_asc": (User.id.asc(),),
    }[sort_key]
    return query.order_by(*ordering)


def apply_admin_mt5_sort(query, sort_key):
    sort_key = normalize_admin_mt5_sort(sort_key)
    ordering = {
        "created_desc": (MT5Account.created_at.desc(), MT5Account.id.desc()),
        "created_asc": (MT5Account.created_at.asc(), MT5Account.id.asc()),
        "sync_desc": (MT5Account.last_synced_at.desc().nulls_last(), MT5Account.id.desc()),
        "sync_asc": (MT5Account.last_synced_at.asc().nulls_last(), MT5Account.id.asc()),
        "login_asc": (MT5Account.account_number.asc(), MT5Account.id.asc()),
        "login_desc": (MT5Account.account_number.desc(), MT5Account.id.desc()),
        "server_asc": (MT5Account.server.asc(), MT5Account.id.asc()),
        "server_desc": (MT5Account.server.desc(), MT5Account.id.desc()),
        "id_desc": (MT5Account.id.desc(),),
        "id_asc": (MT5Account.id.asc(),),
    }[sort_key]
    return query.order_by(*ordering)


def get_registration_paused():
    return _env_bool("REGISTRATION_PAUSED", False)


def get_auto_approve_new_users():
    return _env_bool("AUTO_APPROVE_NEW_USERS", True)


def get_signup_code_mode():
    raw = os.getenv("SIGNUP_CODE_MODE", SIGNUP_CODE_MODE_OFF).strip().lower()
    return raw if raw in VALID_SIGNUP_CODE_MODES else SIGNUP_CODE_MODE_OFF


def get_signup_code_query_param():
    return (os.getenv("REFERRAL_LINK_QUERY_PARAM", "ref").strip().lower() or "ref")


def get_admin_user_emails():
    raw = os.getenv("ADMIN_USER_EMAILS", "").strip()
    return {part.strip().lower() for part in raw.split(",") if part and part.strip()}


def is_root_admin_email(email):
    candidate = (email or "").strip().lower()
    return bool(candidate and candidate in get_admin_user_emails())


def is_admin_email(email):
    return is_root_admin_email(email)


def normalize_signup_status(value, default=SIGNUP_STATUS_APPROVED):
    candidate = str(value or "").strip().lower()
    return candidate if candidate in VALID_SIGNUP_STATUSES else default


def normalize_signup_code(value):
    cleaned = "".join(ch for ch in str(value or "").strip().upper() if ch.isalnum())
    return cleaned[:32]


def generate_signup_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def send_error_log_email(*, subject, body):
    error_to_email = os.getenv("ERROR_LOG_TO_EMAIL", "").strip().lower()
    if not error_to_email:
        return
    try:
        email_result = send_email_placeholder(error_to_email, subject, body)
        if not (email_result or {}).get("sent"):
            current_app.logger.warning(
                "Error notification email was not sent: to=%s subject=%s mode=%s",
                error_to_email,
                subject,
                (email_result or {}).get("mode", "unknown"),
            )
    except Exception as notify_exc:
        current_app.logger.warning("Error notification email failed: %s", notify_exc)


def build_unique_signup_code():
    while True:
        candidate = generate_signup_code()
        existing = SignupCode.query.filter_by(code=candidate).first()
        if existing is None:
            return candidate


def get_signup_code_validation_message(mode):
    if mode == SIGNUP_CODE_MODE_REQUIRED:
        return "A valid referral code is required to register right now."
    return "Referral code is invalid or no longer active."


def find_signup_code(code_value):
    normalized = normalize_signup_code(code_value)
    if not normalized:
        return None
    return SignupCode.query.filter_by(code=normalized).first()


def is_signup_code_usable(code_row):
    if not code_row or not code_row.is_active:
        return False
    if code_row.expires_at and code_row.expires_at <= utcnow_naive():
        return False
    if code_row.max_uses is not None and code_row.used_count >= code_row.max_uses:
        return False
    return True


def get_initial_signup_status():
    return SIGNUP_STATUS_APPROVED if get_auto_approve_new_users() else SIGNUP_STATUS_PENDING


def get_google_auth_enabled():
    if oauth is None:
        return False
    return bool(
        str(current_app.config.get("GOOGLE_CLIENT_ID", "") or "").strip()
        and str(current_app.config.get("GOOGLE_CLIENT_SECRET", "") or "").strip()
    )


def build_unique_google_username(*, email, profile_name=""):
    candidates = []
    profile_text = str(profile_name or "").strip().lower()
    if profile_text:
        profile_seed = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in profile_text)
        candidates.append(profile_seed)

    local_part = str(email or "").strip().lower().split("@", 1)[0]
    if local_part:
        local_seed = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in local_part)
        candidates.append(local_seed)
    candidates.append("trader")

    base = next(
        (
            candidate.strip("._")
            for candidate in candidates
            if candidate and candidate.strip("._")
        ),
        "trader",
    )
    base = "_".join(part for part in base.split("_") if part) or "trader"
    base = base[:72] or "trader"

    attempt = 0
    while True:
        suffix = "" if attempt == 0 else str(attempt + 1)
        candidate = f"{base[:80 - len(suffix)]}{suffix}".strip("._") or "trader"
        exists = User.query.filter(func.lower(User.username) == candidate.lower()).first()
        if exists is None:
            return candidate
        attempt += 1


def user_has_admin_access(user):
    if not user:
        return False
    if not getattr(user, "email_verified", False):
        return False
    if normalize_signup_status(getattr(user, "signup_status", None)) != SIGNUP_STATUS_APPROVED:
        return False
    return bool(getattr(user, "is_admin", False) or is_root_admin_email(getattr(user, "email", "")))


def user_has_root_admin_access(user):
    if not user:
        return False
    if not getattr(user, "email_verified", False):
        return False
    if normalize_signup_status(getattr(user, "signup_status", None)) != SIGNUP_STATUS_APPROVED:
        return False
    return is_root_admin_email(getattr(user, "email", ""))


def get_public_base_url():
    configured_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured_base:
        return configured_base
    return request.host_url.rstrip("/")


def build_external_url(path_or_url):
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return f"{get_public_base_url()}{path_or_url}"


def render_app_template(template_name, **context):
    return current_app.jinja_env.get_template(template_name).render(**context)


def _build_admin_mt5_status(*, account, request_row=None):
    if getattr(account, "is_orphaned", False):
        return {
            "label": "Cleanup Pending",
            "chip_class": "default",
        }
    if getattr(account, "is_archived", False):
        return {
            "label": "Archived",
            "chip_class": "default",
        }

    terminal_exists = bool(
        str(getattr(account, "terminal_path", "") or "").strip()
        or str(getattr(account, "appdata_hash", "") or "").strip()
    )
    request_status = str(getattr(request_row, "status", "") or "").strip().lower()

    if bool(getattr(account, "is_active", False)):
        return {
            "label": "Active",
            "chip_class": "success-chip",
        }
    if terminal_exists:
        return {
            "label": "Inactive",
            "chip_class": "danger-chip",
        }
    if request_status == MT5AccessRequest.STATUS_PENDING:
        return {
            "label": "Setup Queued",
            "chip_class": "requested-chip",
        }
    return {
        "label": "Setting Up",
        "chip_class": "warning-chip",
    }


SEO_PAGE_DEFINITIONS = {
    "trading-journal": {
        "title": "Trading Journal That Finds Your Biggest Mistake | MyFXJournal",
        "meta_description": (
            "Find your biggest trading mistake without extra work. MyFXJournal syncs with MT5, reviews your week automatically, and shows you the one thing holding you back."
        ),
        "eyebrow": "Trading journal",
        "hero_title": "Find the mistake that's costing you money.",
        "hero_body": (
            "MyFXJournal syncs with MetaTrader 5, reviews your trades automatically, and tells you the one mistake worth fixing this week. No spreadsheets. No screenshots. No second job."
        ),
        "chips": ("Automated weekly review", "MT5 sync", "Zero manual logging"),
        "intro_title": "If this sounds familiar",
        "intro_body": (
            "You've taken courses. Watched the videos. Read the books. Your equity curve still looks the same. "
            "The problem isn't knowledge — it's one mistake you keep making without seeing it. "
            "A journal should show you that, not create more homework."
        ),
        "fit_points": (
            "Most traders repeat the same 1–2 mistakes for months. MyFXJournal finds them for you.",
            "No screenshots, no manual entries. Connect MT5 and your trades flow in automatically.",
            "A short weekly review tells you exactly what went wrong — and what to protect next week.",
        ),
        "cards": (
            {
                "title": "Stop guessing what went wrong",
                "body": "Most traders think they know their mistakes. The data usually tells a different story.",
            },
            {
                "title": "No effort, no excuses",
                "body": "If it takes zero work, you'll actually do it. That's the whole point.",
            },
            {
                "title": "Fix one thing at a time",
                "body": "You don't need ten improvements. You need the right one, repeated.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Connect MT5",
                "body": "Link your MetaTrader 5 account with read-only access. Takes two minutes. Your trades sync automatically from there.",
            },
            {
                "title": "Trade normally",
                "body": "No screenshots, no notes, no extra tabs. Just trade. MyFXJournal pulls your history in the background.",
            },
            {
                "title": "Get your weekly review",
                "body": "Every week: what happened, what repeated, and the one thing to fix. Short enough to read before markets open.",
            },
        ),
        "workflow_heading": "Three steps. Zero journaling.",
        "cta_heading": "Your biggest mistake is hiding in your trade history.",
        "cta_body": "Connect MT5, trade normally, and let MyFXJournal find the pattern that's costing you the most.",
        "faq": (
            {
                "question": "Do I need to log my trades manually?",
                "answer": "No. Connect MT5 and your trades sync automatically. You can also import history files if you prefer.",
            },
            {
                "question": "Is this just another trade tracker?",
                "answer": "No. Most trackers store trades. MyFXJournal reviews them for you and tells you what to fix.",
            },
            {
                "question": "Shouldn't I just review my own trades?",
                "answer": "You should. Most don't — the process gets too heavy. MyFXJournal does the analysis automatically so the week doesn't slip past without one.",
            },
        ),
    },
    "mt5-trading-journal": {
        "title": "MT5 Trading Journal — Automatic Sync, Weekly Review | MyFXJournal",
        "meta_description": (
            "The MT5 trading journal that syncs your MetaTrader 5 history automatically, finds your biggest mistake, and delivers a weekly review — no manual journaling required."
        ),
        "eyebrow": "MT5 trading journal",
        "hero_title": "Your MT5 history, reviewed. No exports needed.",
        "hero_body": (
            "Connect MetaTrader 5 with read-only access. Your trades sync automatically. "
            "Every week, MyFXJournal tells you the one mistake worth fixing — no exports, no spreadsheets, no journaling homework."
        ),
        "chips": ("Automatic MT5 sync", "Weekly AI review", "Zero export hassle"),
        "intro_title": "The data's already there",
        "intro_body": (
            "Every trade is already in MT5. Raw history just doesn't tell you what you're doing wrong. "
            "You need something that reads that data and shows you the pattern that's actually costing you."
        ),
        "fit_points": (
            "MT5 already has your data. MyFXJournal turns it into a clear weekly review without you touching a thing.",
            "No exporting reports. No pasting into spreadsheets. Connect once, and the rest is automatic.",
            "See revenge trades, sizing mistakes, and patterns you'd never catch scrolling through MT5 history.",
        ),
        "cards": (
            {
                "title": "No more MT5 report exports",
                "body": "Stop downloading statement files every weekend. Your history flows in automatically.",
            },
            {
                "title": "See what MT5 history can't show you",
                "body": "Raw trade logs don't flag revenge trades or sizing creep. MyFXJournal does.",
            },
            {
                "title": "Built for MetaTrader 5 traders",
                "body": "Not a generic journal with MT5 bolted on. The sync, the review, and the workflow are designed around how MT5 traders actually work.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Connect your MT5",
                "body": "Use read-only (investor) access. Setup takes two minutes. No trading permissions needed.",
            },
            {
                "title": "Trade like you always do",
                "body": "MyFXJournal pulls your closed trades automatically every few minutes. You don't do anything.",
            },
            {
                "title": "Read your weekly review",
                "body": "Each week, get a short review: what cost you money, what worked, and the one adjustment worth making.",
            },
        ),
        "workflow_heading": "Connect once. Review every week.",
        "cta_heading": "Your MT5 history already holds the answer.",
        "cta_body": "Connect MetaTrader 5. Stop exporting. Let the review find your biggest mistake.",
        "faq": (
            {
                "question": "Does this need my MT5 trading password?",
                "answer": "No. You connect with investor (read-only) credentials. MyFXJournal can only read your history — never place trades.",
            },
            {
                "question": "Is this free?",
                "answer": "MT5 sync is free during beta. Import-based journaling is always free to start.",
            },
            {
                "question": "What if I trade on multiple MT5 accounts?",
                "answer": "Each MT5 account gets its own sync and its own weekly review, so the context stays clean.",
            },
        ),
    },
    "free-mt5-sync": {
        "title": "MyFXJournal | Free MT5 Sync",
        "meta_description": (
            "Free during open beta. Use batch-based read-only MetaTrader 5 (MT5) sync in MyFXJournal and turn account history into a weekly AI review without repeated exports."
        ),
        "eyebrow": "Free MT5 sync",
        "hero_title": "Connect MT5 once. Stop exporting forever.",
        "hero_body": (
            "MT5 sync is free during beta. Connect with read-only access and your trades feed the weekly review automatically — no exports, no cleanup."
        ),
        "chips": ("Free during beta", "Read-only access", "No export routine"),
        "intro_title": "The export routine stops here",
        "intro_body": (
            "The sync isn't the goal. It's how you stop rebuilding the week from scratch every Sunday."
        ),
        "fit_points": (
            "No more weekend exports. MT5 history flows in on its own.",
            "Catch revenge patterns and sizing mistakes the raw log won't flag.",
            "Connect with investor or read-only access. No trading permissions needed.",
        ),
        "cards": (
            {
                "title": "Start with sync",
                "body": "Use the open beta flow. No separate pipeline to set up.",
            },
            {
                "title": "One account, one review",
                "body": "Each MT5 account stays separate so the context doesn't blur.",
            },
            {
                "title": "Don't let it collect dust",
                "body": "Synced trades feed the review loop — not just a history tab you never open.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Claim an open slot",
                "body": "Use the in-product beta flow when a free sync slot is open. Manual import still works right away.",
            },
            {
                "title": "Connect read-only",
                "body": "Use investor credentials only. When a batch slot is open, setup starts after you submit details.",
            },
            {
                "title": "Watch the review build",
                "body": "Automatic MT5 sync feeds the weekly review and revenge-pattern flags. No rebuilding the week by hand.",
            },
        ),
        "workflow_heading": "Automatic sync in. Weekly review out.",
        "cta_heading": "Free sync. No export routine.",
        "cta_body": "Connect read-only, skip the export routine, and start a review loop that actually runs.",
        "faq": (
            {
                "question": "Is MT5 sync really free?",
                "answer": "Yes, during open beta. That can change later, but it is free right now.",
            },
            {
                "question": "Do I need to share trading access?",
                "answer": "No. The workflow is built around investor or read-only access.",
            },
            {
                "question": "How does the setup actually work?",
                "answer": "Not fully automated yet. During beta, sync opens in batches. When a slot is open, setup starts after you submit your read-only credentials.",
            },
        ),
    },
    "forex-trading-journal": {
        "title": "MyFXJournal | Forex Trading Journal",
        "meta_description": (
            "MyFXJournal is a forex trading journal built to cut journaling overhead, review trades by account, and turn the week into a lighter AI review."
        ),
        "eyebrow": "Forex trading journal",
        "hero_title": "A forex journal that stays out of the way.",
        "hero_body": (
            "Track the week, spot the pattern, and keep reflection close to the trades without turning journaling into weekend homework."
        ),
        "chips": ("Weekly AI review", "Behavior-aware review", "Revenge-pattern flags"),
        "intro_title": "Why this works",
        "intro_body": (
            "Most traders do not skip review because they do not care. They skip it because the process gets heavy after the market closes."
        ),
        "fit_points": (
            "Keep the review focused on execution, psychology, and revenge-style patterns that are easy to miss in raw logs.",
            "Use one account-centered workflow instead of piecing review together across charts and notes.",
            "Keep a journaling habit that supports trading instead of getting in the way of it.",
        ),
        "cards": (
            {
                "title": "See the week clearly",
                "body": "Start from the actual account activity, not memory or scattered screenshots.",
            },
            {
                "title": "Spot what repeated",
                "body": "Catch clusters of good execution, impulsive follow-ups, sloppy losses, and revenge-style re-entries before they blur together.",
            },
            {
                "title": "Keep the next step small",
                "body": "Tighten one rule or one habit instead of overwhelming yourself with ten fixes.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Start clean",
                "body": "Begin from synced or imported history. The journal doesn't start with a blank page.",
            },
            {
                "title": "See the pattern",
                "body": "Let the journal organize the performance and behavior context, including likely revenge patterns, into one review flow.",
            },
            {
                "title": "Carry one adjustment",
                "body": "Move into next week with one useful adjustment instead of a full process rewrite.",
            },
        ),
        "workflow_heading": "Capture less. Learn faster.",
        "cta_heading": "A review habit you'll actually keep.",
        "cta_body": "Track the week, spot the pattern, and carry one adjustment forward without turning it into homework.",
        "faq": (
            {
                "question": "Is MyFXJournal only for forex traders?",
                "answer": "No, but it fits forex traders especially well because the workflow stays light.",
            },
            {
                "question": "Does the product tell me what to trade next?",
                "answer": "No. It is for review and process clarity, not trade signals.",
            },
            {
                "question": "Who is this best for?",
                "answer": "Traders who want a lighter review loop and care about execution and behavior, not just a ledger.",
            },
        ),
    },
    "weekly-trading-review": {
        "title": "MyFXJournal | Weekly Trading Review",
        "meta_description": (
            "Use MyFXJournal to turn trade history into a lighter weekly AI review with clearer patterns, one rule worth carrying forward, and less journaling overhead."
        ),
        "eyebrow": "Weekly trading review",
        "hero_title": "Weekly review without the weekend drag.",
        "hero_body": (
            "Turn the week into a readable review, a clearer pattern, and one rule worth testing before the next trading week starts."
        ),
        "chips": ("Weekly AI review", "Repeatable review loop", "Revenge-pattern flags"),
        "intro_title": "Why this matters",
        "intro_body": (
            "The problem is rarely knowing review matters. The problem is staying consistent once the process starts to drag."
        ),
        "fit_points": (
            "Look for patterns that repeat — revenge sequences, sizing habits, execution breaks — not just one-off reactions.",
            "Keep reflection structured enough to be useful, but light enough to keep doing.",
            "Turn this week's trades into a next-week adjustment without making review feel like a project.",
        ),
        "cards": (
            {
                "title": "Summarize what mattered",
                "body": "See the week through the major winners, the costly follow-ups, and the pattern that deserves attention.",
            },
            {
                "title": "Make it a rule",
                "body": "Turn trade evidence into a process rule you can reuse next week.",
            },
            {
                "title": "Keep the loop going",
                "body": "Keep the review practical enough to repeat instead of turning it into a monthly catch-up task.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Start from the week",
                "body": "Start from synced or imported trade history instead of reconstructing the week from scratch.",
            },
            {
                "title": "Name the pattern",
                "body": "Highlight the behavior, sizing, or revenge-style sequence the review surfaced as worth carrying forward.",
            },
            {
                "title": "Leave with one rule",
                "body": "End the week with a cleaner next-step rule instead of another long document.",
            },
        ),
        "workflow_heading": "A weekly review you can keep up with.",
        "cta_heading": "One review. One rule. Every week.",
        "cta_body": "Turn this week's trades into a takeaway short enough to act on before the next week starts.",
        "faq": (
            {
                "question": "What should a weekly trading review produce?",
                "answer": "Usually one or two clear takeaways and one process rule you can actually use next week.",
            },
            {
                "question": "Why not just write the review manually?",
                "answer": "You can, but many traders stop because the workload grows too quickly. MyFXJournal keeps it lighter.",
            },
            {
                "question": "Is the weekly review meant to replace thinking?",
                "answer": "No. It helps organize reflection so you spend less time assembling the review and more time learning from it.",
            },
        ),
    },
    "why-am-i-not-improving-in-trading": {
        "title": "Why Am I Not Improving in Trading? | MyFXJournal",
        "meta_description": (
            "You study, you practise, but your trading results stay flat. The reason is usually one mistake you keep repeating without noticing. Here's how to find it."
        ),
        "eyebrow": "Why am I not improving",
        "hero_title": "You're not stuck. You're repeating the same mistake.",
        "hero_body": (
            "Most traders who feel stuck aren't lacking knowledge. They're repeating one or two mistakes without realising it. "
            "The fix isn't more education — it's seeing the pattern clearly enough to stop doing it."
        ),
        "chips": ("Find your pattern", "Automated review", "No extra work"),
        "intro_title": "If this sounds like you",
        "intro_body": (
            "You've watched the courses. Read the books. Studied charts for months. "
            "But your results haven't changed. The problem isn't what you know — it's what you keep doing without noticing."
        ),
        "fit_points": (
            "You've learned plenty, but your account balance doesn't reflect it.",
            "You know you should journal, but it feels like a second job on top of trading.",
            "You suspect you're repeating mistakes, but you can't pinpoint which ones.",
        ),
        "cards": (
            {
                "title": "It's not about learning more",
                "body": "You already know enough. The bottleneck is one or two habits you repeat under pressure without noticing.",
            },
            {
                "title": "Journaling shouldn't be the problem",
                "body": "If review takes too much effort, you won't do it. MyFXJournal makes it automatic so there's no excuse left.",
            },
            {
                "title": "One mistake, one fix",
                "body": "You don't need a personality overhaul. You need to see the specific pattern and stop feeding it.",
            },
        ),
        "workflow_eyebrow": "How to find the mistake",
        "workflow_steps": (
            {
                "title": "Connect your trades",
                "body": "Link MT5 or import your history. No manual logging. Your trades are already recorded — MyFXJournal just reads them.",
            },
            {
                "title": "Trade for a week",
                "body": "Don't change anything. Just trade the way you normally do. That's the data the review needs.",
            },
            {
                "title": "See the mistake",
                "body": "Your weekly review shows you exactly where the damage happened — revenge trades, bad sizing, impulsive entries — and gives you one thing to fix.",
            },
        ),
        "workflow_heading": "Find it without doing extra work.",
        "cta_heading": "The answer is already in your trade history.",
        "cta_body": "Stop guessing why you're stuck. Connect your trades, get a weekly review, and find the one mistake that's been there the whole time.",
        "faq": (
            {
                "question": "I've tried journaling before and stopped. Why would this be different?",
                "answer": "Because you don't have to do anything. Your trades sync automatically, and the review writes itself. There's nothing to keep up with.",
            },
            {
                "question": "Will this tell me what to trade?",
                "answer": "No. No signals. It shows what's going wrong in your execution — the rest is on you.",
            },
            {
                "question": "Do I need to be an experienced trader?",
                "answer": "No. If you've been trading for a few months and feel like you should be further along, this is built for you.",
            },
        ),
    },
    "trade-replay-chart": {
        "title": "Trade Replay Chart for Review | MyFXJournal",
        "meta_description": (
            "Replay trades on a price chart inside MyFXJournal. See entries, exits, and context in one view so review is about what happened on the chart—not just rows in a table."
        ),
        "eyebrow": "Trade replay",
        "hero_title": "Replay the trade on the chart—not just the spreadsheet row.",
        "hero_body": (
            "MyFXJournal’s trade replay chart puts each trade back onto price so you can see where you entered, "
            "where you exited, and how the move developed. Built for review sessions when you want the chart story, not only the numbers."
        ),
        "chips": ("Chart-native review", "Entry & exit context", "Works with imported & synced history"),
        "intro_title": "When the table view is not enough",
        "intro_body": (
            "Closed-trade lists are fast for totals, but they rarely show the sequence you felt in the session. "
            "Replay ties the journal back to the market structure you traded so the same mistake is easier to recognise next time."
        ),
        "fit_points": (
            "You already log or import trades and want review to feel closer to how you experienced the session.",
            "You want a single place to scan entries and exits against price instead of jumping between the journal and a separate platform.",
            "You use weekly AI review for themes—and replay when you need the picture of one trade or one session.",
        ),
        "cards": (
            {
                "title": "See the trade in context",
                "body": "Entries and exits sit on the chart timeline so you can relate fills to the move that was unfolding.",
            },
            {
                "title": "Faster post-session recall",
                "body": "Skip mentally rebuilding the candle sequence from a flat list of prices and times.",
            },
            {
                "title": "Same data as the journal",
                "body": "Replay uses the trades already in your account—whether they came from import or optional MT5 sync.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Get trades into the journal",
                "body": "Import MT5 or Tradovate files, add manual trades, or use read-only MT5 sync when a slot is open.",
            },
            {
                "title": "Open replay on a trade",
                "body": "From your trade list or detail view, jump into the replay chart for that position.",
            },
            {
                "title": "Review with the chart in view",
                "body": "Walk the path from entry to exit, then carry the takeaway into your weekly review or next-week rules.",
            },
        ),
        "workflow_heading": "From history row to chart story.",
        "cta_heading": "Review trades the way you remember them.",
        "cta_body": "Start free, bring in your history, and use trade replay when you want the chart—not just the log.",
        "faq": (
            {
                "question": "Is trade replay the same as live charting software?",
                "answer": (
                    "No. It is for journaling and review: reconstructing your trade on price for context, not for placing new trades or live analysis."
                ),
            },
            {
                "question": "Does replay work without MT5 sync?",
                "answer": "Yes. Any trades in your journal—imports or manual entries—can be used for review features that depend on stored trade data.",
            },
            {
                "question": "Does this give trade signals?",
                "answer": "No. MyFXJournal is for review and process clarity, not recommendations or signals.",
            },
        ),
    },
}


def get_allowed_signup_email_domains():
    raw = os.getenv("ALLOWED_SIGNUP_EMAIL_DOMAINS", "").strip()
    if not raw:
        try:
            rows = (
                AllowedSignupEmailDomain.query.filter_by(is_active=True)
                .order_by(AllowedSignupEmailDomain.domain.asc())
                .all()
            )
        except (OperationalError, ProgrammingError):
            current_app.logger.warning(
                "Allowed signup domain table is unavailable. Falling back to env-only allowlist."
            )
            return set()
        return {row.domain.strip().lower() for row in rows if row.domain and row.domain.strip()}
    return {part.strip().lower() for part in raw.split(",") if part and part.strip()}


def is_allowed_signup_email_domain(email):
    candidate = (email or "").strip().lower()
    if "@" not in candidate:
        return False
    domain = candidate.rsplit("@", 1)[1].strip()
    if not domain:
        return False
    return domain in get_allowed_signup_email_domains()


def get_token_serializer():
    token_salt = os.getenv("TOKEN_SALT", "fxjournal-token-salt")
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=token_salt)


def generate_auth_token(email, purpose):
    serializer = get_token_serializer()
    return serializer.dumps({"email": (email or "").strip().lower(), "purpose": purpose})


def rotate_password_reset_nonce(user):
    reset_nonce = secrets.token_urlsafe(24)
    user.password_reset_nonce = reset_nonce
    return reset_nonce


def generate_password_reset_token(email, purpose, reset_nonce):
    serializer = get_token_serializer()
    return serializer.dumps(
        {
            "email": (email or "").strip().lower(),
            "purpose": purpose,
            "reset_nonce": str(reset_nonce or "").strip(),
        }
    )


def generate_email_change_token(*, user_id, current_email, new_email, channel):
    normalized_channel = str(channel or "").strip().lower()
    if normalized_channel not in {"current", "new"}:
        raise ValueError("Email change token channel must be 'current' or 'new'.")
    serializer = get_token_serializer()
    return serializer.dumps(
        {
            "purpose": TOKEN_PURPOSE_EMAIL_CHANGE,
            "user_id": int(user_id),
            "current_email": (current_email or "").strip().lower(),
            "new_email": (new_email or "").strip().lower(),
            "channel": normalized_channel,
        }
    )


def generate_pending_registration_token(registration_id, email):
    serializer = get_token_serializer()
    return serializer.dumps(
        {
            "registration_id": str(registration_id or "").strip(),
            "email": (email or "").strip().lower(),
            "purpose": TOKEN_PURPOSE_PENDING_REGISTRATION,
        }
    )


def verify_auth_token(token, purpose, max_age_seconds):
    serializer = get_token_serializer()
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("purpose") != purpose:
        return None

    email = str(payload.get("email", "")).strip().lower()
    return email or None


def verify_password_reset_token(token, purpose, max_age_seconds):
    serializer = get_token_serializer()
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("purpose") != purpose:
        return None

    email = str(payload.get("email", "")).strip().lower()
    reset_nonce = str(payload.get("reset_nonce", "")).strip()
    if not email or not reset_nonce:
        return None
    return {"email": email, "reset_nonce": reset_nonce}


def verify_email_change_token(token, max_age_seconds):
    serializer = get_token_serializer()
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("purpose") != TOKEN_PURPOSE_EMAIL_CHANGE:
        return None

    try:
        user_id = int(payload.get("user_id"))
    except (TypeError, ValueError):
        return None

    current_email = str(payload.get("current_email", "")).strip().lower()
    new_email = str(payload.get("new_email", "")).strip().lower()
    channel = str(payload.get("channel", "")).strip().lower()
    if not current_email or not new_email or channel not in {"current", "new"}:
        return None

    return {
        "user_id": user_id,
        "current_email": current_email,
        "new_email": new_email,
        "channel": channel,
    }


def verify_pending_registration_token(token, max_age_seconds):
    serializer = get_token_serializer()
    try:
        payload = serializer.loads(token, max_age=max_age_seconds)
    except (SignatureExpired, BadSignature):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("purpose") != TOKEN_PURPOSE_PENDING_REGISTRATION:
        return None

    registration_id = str(payload.get("registration_id", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    if not registration_id or not email:
        return None
    return {"registration_id": registration_id, "email": email}


def cleanup_pending_registrations(max_age_seconds):
    now = utcnow_naive()
    expired = []
    for registration_id, item in PENDING_REGISTRATIONS.items():
        created_at = item.get("created_at")
        if not isinstance(created_at, datetime):
            expired.append(registration_id)
            continue
        age = (now - created_at).total_seconds()
        if age > max_age_seconds:
            expired.append(registration_id)
    for registration_id in expired:
        PENDING_REGISTRATIONS.pop(registration_id, None)


def create_pending_registration(username, email, password_hash):
    expiry_seconds = _env_int("EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS", 86400)
    cleanup_pending_registrations(expiry_seconds)

    registration_id = secrets.token_urlsafe(24)
    PENDING_REGISTRATIONS[registration_id] = {
        "username": username,
        "email": email,
        "password_hash": password_hash,
        "created_at": utcnow_naive(),
    }
    return registration_id


def get_pending_registration(registration_id):
    if not registration_id:
        return None
    return PENDING_REGISTRATIONS.get(registration_id)


def pop_pending_registration(registration_id):
    if not registration_id:
        return None
    return PENDING_REGISTRATIONS.pop(registration_id, None)


def _should_log_email_bodies():
    app_env = os.getenv("APP_ENV", "").strip().lower()
    flask_env = os.getenv("FLASK_ENV", "").strip().lower()
    return (
        _env_bool("FLASK_DEBUG", False)
        or app_env in {"local", "development", "dev"}
        or flask_env in {"local", "development", "dev"}
    )


def _log_email_payloads(text_body, html_body=None):
    current_app.logger.info("Email body:\n%s", text_body)
    if html_body:
        current_app.logger.info("Email HTML body:\n%s", html_body)


def _resolve_email_from_header():
    """Return Resend/SMTP-style From value (optional display name + address)."""
    raw = os.getenv("EMAIL_FROM", "support@example.com").strip()
    if "<" in raw and ">" in raw:
        return raw
    name = os.getenv("EMAIL_FROM_NAME", "MyFXJournal").strip()
    if name:
        return f"{name} <{raw}>"
    return raw


def send_email_placeholder(to_email, subject, text_body, html_body=None):
    provider = os.getenv("EMAIL_PROVIDER", "placeholder").strip().lower()
    sender = _resolve_email_from_header()
    send_enabled = os.getenv("EMAIL_SEND_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    api_key = os.getenv("RESEND_API_KEY", "").strip() or os.getenv("EMAIL_API_KEY", "").strip()
    log_email_bodies = _should_log_email_bodies()

    if provider in {"console", "placeholder"} or not send_enabled:
        current_app.logger.info(
            "Email placeholder (console/disabled) -> to=%s from=%s subject=%s",
            to_email,
            sender,
            subject,
        )
        if log_email_bodies:
            _log_email_payloads(text_body, html_body)
        return {"sent": False, "mode": "placeholder"}

    if not api_key:
        current_app.logger.warning(
            "EMAIL_SEND_ENABLED is true but RESEND_API_KEY/EMAIL_API_KEY is missing. Using placeholder mode."
        )
        if log_email_bodies:
            _log_email_payloads(text_body, html_body)
        return {"sent": False, "mode": "missing_api_key"}

    if provider == "resend":
        try:
            import resend
        except ImportError:
            current_app.logger.warning("Resend SDK is not installed. Falling back to placeholder logging.")
            if log_email_bodies:
                _log_email_payloads(text_body, html_body)
            return {"sent": False, "mode": "missing_resend_sdk"}

        html_payload = html_body
        if not html_payload:
            safe_text = (
                text_body.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\n", "<br>")
            )
            html_payload = f"<div>{safe_text}</div>"

        try:
            resend.api_key = api_key
            payload = {
                "from": sender,
                "to": [to_email],
                "subject": subject,
                "html": html_payload,
            }
            response = resend.Emails.send(payload)
            return {"sent": True, "mode": "resend", "response": response}
        except Exception as exc:
            current_app.logger.warning("Resend send failed: %s", exc)
            if log_email_bodies:
                _log_email_payloads(text_body, html_body)
            return {"sent": False, "mode": "resend_error"}

    current_app.logger.warning(
        "Email provider '%s' is configured but integration is not implemented.", provider
    )
    if log_email_bodies:
        _log_email_payloads(text_body, html_body)
    return {"sent": False, "mode": "not_implemented"}


def register_public_auth_routes(
    app,
    limiter,
    *,
    legal_last_updated,
    token_purpose_verify_email,
    token_purpose_password_reset,
    env_int,
    is_local_dev_environment,
    resolve_active_trade_account,
):
    def get_current_user():
        user_id = session.get("user_id")
        if not user_id:
            return None
        return User.query.filter_by(id=user_id).first()

    def get_current_admin_user():
        user = get_current_user()
        if not user_has_admin_access(user):
            return None
        return user

    def get_current_root_admin_user():
        user = get_current_user()
        if not user_has_root_admin_access(user):
            return None
        return user

    def _build_logo_url():
        return build_external_url("/static/site-logo.png")

    def _render_email_html(template_name, **context):
        context.setdefault("logo_url", _build_logo_url())
        return render_template(template_name, **context)

    def _should_show_public_mt5_slot_urgency():
        current_user_id = session.get("user_id")
        if not current_user_id:
            return True
        try:
            normalized_user_id = int(str(current_user_id).strip())
        except (TypeError, ValueError):
            return True

        has_existing_mt5_link = (
            db.session.query(MT5Account.id)
            .filter(MT5Account.user_id == normalized_user_id)
            .limit(1)
            .first()
            is not None
        )
        if has_existing_mt5_link:
            return False

        has_mt5_request_in_flight = (
            db.session.query(MT5AccessRequest.id)
            .filter(
                MT5AccessRequest.user_id == normalized_user_id,
                MT5AccessRequest.status.in_(
                    [
                        MT5AccessRequest.STATUS_PENDING,
                        MT5AccessRequest.STATUS_APPROVED,
                    ]
                ),
            )
            .limit(1)
            .first()
            is not None
        )
        return not has_mt5_request_in_flight

    def _send_welcome_email(user):
        welcome_html = _render_email_html(
            "emails/welcome.html",
            name=user.username,
            dashboard_url=build_external_url(url_for("dashboard.home")),
            unsubscribe_url="",
        )
        send_email_placeholder(
            user.email,
            "Welcome to MyFXJournal",
            (
                f"Hi {user.username}, your MyFXJournal account is ready. "
                "Head to your dashboard to get started."
            ),
            html_body=welcome_html,
        )

    def admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            admin_user = get_current_admin_user()
            if admin_user is None:
                abort(404)
            return f(*args, **kwargs)

        return decorated

    def root_admin_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            root_admin_user = get_current_root_admin_user()
            if root_admin_user is None:
                abort(404)
            return f(*args, **kwargs)

        return decorated

    def require_admin_user():
        admin_user = get_current_admin_user()
        if admin_user is None:
            abort(404)
        return admin_user

    def require_root_admin_user():
        root_admin_user = get_current_root_admin_user()
        if root_admin_user is None:
            abort(404)
        return root_admin_user

    def render_login_page(*, error=None, success=None, info=None, email=""):
        return render_template(
            "login.html",
            title="Sign in to MyFXJournal | Forex trading journal with weekly AI review",
            meta_description=(
                "Sign in to MyFXJournal to open your trading dashboard, analytics, MT5 sync, "
                "and weekly AI review for your forex and CFD accounts."
            ),
            canonical_url=build_external_url("/login"),
            body_class="auth-layout",
            error=error,
            success=success,
            info=info,
            email_value=email,
            google_auth_enabled=get_google_auth_enabled(),
        )

    def render_register_page(
        *,
        error=None,
        success=None,
        info=None,
        username="",
        email="",
        signup_code="",
    ):
        signup_code_mode = get_signup_code_mode()
        signup_code_query_param = get_signup_code_query_param()
        show_signup_code_input = signup_code_mode != SIGNUP_CODE_MODE_OFF
        return render_template(
            "register.html",
            title="Create a MyFXJournal account | Free forex trading journal",
            meta_description=(
                "Create a free MyFXJournal account during open beta. Import or sync trade history, "
                "review by account, and get a weekly AI trading review without spreadsheet overhead."
            ),
            canonical_url=build_external_url("/register"),
            body_class="auth-layout",
            error=error,
            success=success,
            info=info,
            username_value=username,
            email_value=email,
            signup_code_value=signup_code,
            signup_code_mode=signup_code_mode,
            signup_code_query_param=signup_code_query_param,
            show_signup_code_input=show_signup_code_input,
            registrations_paused=get_registration_paused(),
            google_auth_enabled=get_google_auth_enabled(),
            weekly_ai_min_closed_trades=MIN_CLOSED_TRADES_FOR_ADVICE,
            manual_signup_review_enabled=not get_auto_approve_new_users(),
        )

    def clear_google_auth_session():
        session.pop(GOOGLE_AUTH_SESSION_INTENT_KEY, None)
        session.pop(GOOGLE_AUTH_SESSION_SIGNUP_CODE_KEY, None)

    def get_google_client():
        if not get_google_auth_enabled():
            return None
        if oauth is None:
            return None
        try:
            client = oauth.create_client("google")
        except Exception:
            client = None

        if client is not None:
            return client

        try:
            oauth.register(
                "google",
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={"scope": "openid email profile"},
            )
            return oauth.create_client("google")
        except Exception as exc:
            current_app.logger.warning("Google OAuth client unavailable: %s", exc)
            return None

    def complete_successful_login(user):
        user.last_login_at = utcnow_naive()
        session["user_id"] = user.id
        session.permanent = True
        session["username"] = user.username
        active_account, _accounts = resolve_active_trade_account(user.id)
        session["active_trade_account_id"] = active_account.id
        db.session.commit()
        profile = get_user_profile(user.id)
        if not user_profile_is_done(profile):
            return redirect(url_for("onboarding"))
        return redirect(url_for("dashboard.home"))

    def finalize_google_authenticated_user(user, *, newly_created=False):
        if session.get("pending_verify_email", "").strip().lower() == (user.email or "").strip().lower():
            session.pop("pending_verify_email", None)

        signup_status = normalize_signup_status(user.signup_status)
        if signup_status == SIGNUP_STATUS_PENDING:
            flash("Your Google email is verified. Your account is now waiting for approval.", "info")
            return redirect(url_for("login"))
        if signup_status == SIGNUP_STATUS_REJECTED:
            return render_login_page(
                error="Your registration was not approved. Contact support if you believe this is a mistake.",
                email=user.email,
            )
        if signup_status == SIGNUP_STATUS_SUSPENDED:
            return render_login_page(
                error="This account is currently suspended. Contact support if you need help.",
                email=user.email,
            )

        if newly_created:
            try:
                _send_welcome_email(user)
            except Exception as exc:
                current_app.logger.warning("Welcome email failed: %s", exc)
        return complete_successful_login(user)

    def get_user_profile(user_id):
        if not user_id:
            return None
        return UserProfile.query.filter_by(user_id=user_id).first()

    def user_profile_is_done(profile):
        return profile is not None and (
            getattr(profile, "completed_at", None) is not None
            or bool(getattr(profile, "skipped", False))
        )

    def render_onboarding_page(*, error=None, profile=None, form_data=None):
        form_data = form_data or {}
        return render_template(
            "onboarding.html",
            title="MyFXJournal | Onboarding",
            body_class="auth-layout",
            error=error,
            trading_style_options=ONBOARDING_TRADING_STYLE_OPTIONS,
            instrument_options=ONBOARDING_INSTRUMENT_OPTIONS,
            experience_level_options=ONBOARDING_EXPERIENCE_LEVEL_OPTIONS,
            trading_style_value=(
                form_data.get("trading_style")
                or getattr(profile, "trading_style", "")
                or ""
            ),
            instruments_value=(
                form_data.get("instruments")
                or getattr(profile, "instruments", "")
                or ""
            ),
            experience_level_value=(
                form_data.get("experience_level")
                or getattr(profile, "experience_level", "")
                or ""
            ),
        )

    def build_admin_redirect(section="users", message="", status="info"):
        endpoint_map = {
            "codes": "admin_signup_codes",
            "mt5": "admin_mt5_accounts",
            "cfd_symbols": "admin_cfd_symbols",
            "weekly_report": "admin_weekly_report",
        }
        endpoint = endpoint_map.get(section, "admin_signup_users")
        if message:
            flash(message, status)
        return redirect(url_for(endpoint))

    def build_admin_overview():
        # One aggregation round-trip for user breakdown; separate counts for other tables.
        totals = db.session.query(
            func.count(User.id),
            func.coalesce(
                func.sum(case((User.signup_status == SIGNUP_STATUS_PENDING, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((User.signup_status == SIGNUP_STATUS_APPROVED, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((User.signup_status == SIGNUP_STATUS_REJECTED, 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((User.signup_status == SIGNUP_STATUS_SUSPENDED, 1), else_=0)),
                0,
            ),
            func.coalesce(func.sum(case((User.is_admin.is_(True), 1), else_=0)), 0),
        ).one()
        return {
            "total_users": int(totals[0] or 0),
            "pending_users": int(totals[1] or 0),
            "approved_users": int(totals[2] or 0),
            "rejected_users": int(totals[3] or 0),
            "suspended_users": int(totals[4] or 0),
            "admin_users": int(totals[5] or 0),
            "signup_codes": db.session.query(func.count(SignupCode.id)).scalar() or 0,
            "mt5_accounts": db.session.query(func.count(MT5Account.id)).scalar() or 0,
        }

    def build_admin_page_context(
        *,
        admin_user,
        section,
        title="MyFXJournal | Access Control",
        page_heading="Access Control",
        page_subtitle=(
            "Review registrations, control admin access, and manage signup codes "
            "without exposing these routes to normal users."
        ),
    ):
        return {
            "title": title,
            "username": admin_user.username,
            "section": section,
            "page_heading": page_heading,
            "page_subtitle": page_subtitle,
            "page_kicker": "Restricted Internal Area",
            "is_root_admin": user_has_root_admin_access(admin_user),
            "root_admin_emails": get_admin_user_emails(),
            "overview": build_admin_overview(),
            "registration_paused": get_registration_paused(),
            "auto_approve_new_users": get_auto_approve_new_users(),
            "signup_code_mode": get_signup_code_mode(),
            "signup_code_query_param": get_signup_code_query_param(),
            "public_register_url": build_external_url(url_for("register")),
        }

    def render_admin_page(*, admin_user, section, **extra_context):
        page_ctx = build_admin_page_context(
            admin_user=admin_user,
            section=section,
        )
        page_ctx.update(extra_context)
        return render_template("admin_signup_access.html", **page_ctx)

    ADMIN_USERS_PER_PAGE = 25

    def render_admin_weekly_report_page(*, admin_user, **extra_context):
        page_ctx = build_admin_page_context(
            admin_user=admin_user,
            section="weekly_report",
            title="MyFXJournal | Weekly AI Audit",
            page_heading="Weekly AI Audit",
            page_subtitle=(
                "Inspect stored weekly AI payloads, compare trend shifts over time, "
                "and review the exact model output for each saved review."
            ),
        )
        page_ctx.update(extra_context)
        return render_template("admin_weekly_report.html", **page_ctx)

    def _format_admin_timestamp(value):
        if not isinstance(value, datetime):
            return "-"
        return value.strftime("%Y-%m-%d %H:%M UTC")

    def _format_admin_week_label(period_start_utc, period_end_utc):
        if isinstance(period_start_utc, datetime) and isinstance(period_end_utc, datetime):
            display_end = period_end_utc - timedelta(seconds=1)
            if display_end.date() <= period_start_utc.date():
                return period_start_utc.strftime("%b %d, %Y")
            if display_end.year == period_start_utc.year:
                return (
                    f"{period_start_utc.strftime('%b %d')} - "
                    f"{display_end.strftime('%b %d, %Y')}"
                )
            return (
                f"{period_start_utc.strftime('%b %d, %Y')} - "
                f"{display_end.strftime('%b %d, %Y')}"
            )
        if isinstance(period_start_utc, datetime):
            return f"Week of {period_start_utc.strftime('%b %d, %Y')}"
        if isinstance(period_end_utc, datetime):
            return f"Ending {period_end_utc.strftime('%b %d, %Y')}"
        return "Unknown period"

    def _parse_admin_ai_payload(payload_json):
        if not payload_json:
            return None, None
        try:
            parsed = json.loads(payload_json)
        except (TypeError, ValueError):
            return None, "Payload JSON could not be parsed."
        if not isinstance(parsed, dict):
            return None, "Payload JSON did not deserialize into an object."
        return parsed, None

    def _serialize_admin_weekly_record(record, *, generation_count=1, trade_account_label=""):
        payload, payload_error = _parse_admin_ai_payload(record.payload_json)
        payload = payload or {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        emotional_index = (
            payload.get("emotional_index")
            if isinstance(payload.get("emotional_index"), dict)
            else {}
        )
        historical_context = (
            payload.get("historical_context")
            if isinstance(payload.get("historical_context"), dict)
            else {}
        )
        prompt_history = getattr(record, "prompt_history", None)
        prompt_created_at = getattr(prompt_history, "created_at", None)

        return {
            "id": record.id,
            "period_start_utc": record.period_start_utc.isoformat() if record.period_start_utc else None,
            "period_end_utc": record.period_end_utc.isoformat() if record.period_end_utc else None,
            "period_label": _format_admin_week_label(record.period_start_utc, record.period_end_utc),
            "generated_at": record.generated_at.isoformat() if record.generated_at else None,
            "generated_at_label": _format_admin_timestamp(record.generated_at),
            "generation_count": max(int(generation_count or 1), 1),
            "trade_account_id": record.trade_account_id,
            "trade_account_label": trade_account_label or "Unassigned account",
            "trade_count_used": int(record.trade_count_used or 0),
            "model": (record.model or "").strip() or "-",
            "response_text": record.response_text or "",
            "payload": payload or None,
            "payload_raw": record.payload_json or "",
            "payload_parse_error": payload_error,
            "notes_coverage": payload.get("notes_coverage"),
            "notes_confidence": payload.get("notes_confidence"),
            "notes_with_content": payload.get("notes_with_content"),
            "notes_missing": payload.get("notes_missing"),
            "account_age_days": payload.get("account_age_days"),
            "summary": summary,
            "emotional_index": emotional_index,
            "historical_context": historical_context,
            "prompt": {
                "id": getattr(prompt_history, "prompt_id", None),
                "text": getattr(prompt_history, "prompt_text", None),
                "source_path": getattr(prompt_history, "source_path", None),
                "created_at": prompt_created_at.isoformat() if prompt_created_at else None,
                "created_at_label": _format_admin_timestamp(prompt_created_at),
            },
        }

    def _render_public_seo_page(page_slug):
        page = SEO_PAGE_DEFINITIONS[page_slug]
        mt5_batch_state = get_mt5_sync_batch_state()
        show_public_mt5_slot_urgency = _should_show_public_mt5_slot_urgency()
        return render_template(
            "seo_page.html",
            title=page["title"],
            meta_description=page["meta_description"],
            canonical_url=build_external_url(f"/{page_slug}"),
            body_class="landing-layout",
            user_logged_in=bool(session.get("user_id")),
            seo_page=page,
            seo_page_slug=page_slug,
            mt5_batch_state=mt5_batch_state,
            show_public_mt5_slot_urgency=show_public_mt5_slot_urgency,
        )

    @app.route("/")
    def landing():
        mt5_batch_state = get_mt5_sync_batch_state()
        show_public_mt5_slot_urgency = _should_show_public_mt5_slot_urgency()
        return render_template(
            "landing.html",
            title="MyFXJournal | Free Forex Trading Journal With Weekly AI Review",
            meta_description=(
                "Import MT5 and Tradovate history, review trades by account, and get a weekly AI review that surfaces patterns worth carrying into next week."
            ),
            body_class="landing-layout",
            canonical_url=build_external_url("/"),
            user_logged_in=bool(session.get("user_id")),
            weekly_ai_min_closed_trades=MIN_CLOSED_TRADES_FOR_ADVICE,
            manual_signup_review_enabled=not get_auto_approve_new_users(),
            signup_code_mode=get_signup_code_mode(),
            mt5_batch_state=mt5_batch_state,
            show_public_mt5_slot_urgency=show_public_mt5_slot_urgency,
        )

    @app.route("/trading-journal")
    def trading_journal_page():
        return _render_public_seo_page("trading-journal")

    @app.route("/mt5-trading-journal")
    def mt5_trading_journal_page():
        return _render_public_seo_page("mt5-trading-journal")

    @app.route("/free-mt5-sync")
    def free_mt5_sync_page():
        return _render_public_seo_page("free-mt5-sync")

    @app.route("/forex-trading-journal")
    def forex_trading_journal_page():
        return _render_public_seo_page("forex-trading-journal")

    @app.route("/weekly-trading-review")
    def weekly_trading_review_page():
        return _render_public_seo_page("weekly-trading-review")

    @app.route("/why-am-i-not-improving-in-trading")
    def why_not_improving_page():
        return _render_public_seo_page("why-am-i-not-improving-in-trading")

    @app.route("/trade-replay-chart")
    def trade_replay_chart_page():
        return _render_public_seo_page("trade-replay-chart")

    @app.route("/robots.txt")
    def robots_txt():
        robots_lines = [
            "User-agent: *",
            "Allow: /",
            "Disallow: /password/",
            "Disallow: /verify-email/",
            "Disallow: /onboarding",
            "Disallow: /auth/google",
            "Sitemap: " + build_external_url("/sitemap.xml"),
        ]
        return Response("\n".join(robots_lines) + "\n", mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        public_urls = (
            build_external_url("/"),
            build_external_url("/dashboard"),
            build_external_url("/login"),
            build_external_url("/register"),
            build_external_url("/contact"),
            build_external_url("/privacy"),
            build_external_url("/terms"),
            build_external_url("/faq/mt5-server"),
        ) + tuple(build_external_url(f"/{slug}") for slug in SEO_PAGE_DEFINITIONS)
        sitemap_items = "\n".join(f"  <url><loc>{url}</loc></url>" for url in public_urls)
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{sitemap_items}\n"
            "</urlset>\n"
        )
        return Response(sitemap, mimetype="application/xml")

    @app.route("/privacy")
    @app.route("/privacy-policy")
    def privacy_policy():
        return render_template(
            "privacy_policy.html",
            title="Privacy Policy | MyFXJournal data, MT5 sync and your rights",
            meta_description="Read how MyFXJournal handles personal data, privacy requests, MT5 sync information, and account data for the trading journal service.",
            canonical_url=build_external_url("/privacy"),
            last_updated=legal_last_updated,
        )

    @app.route("/terms")
    @app.route("/terms-and-conditions")
    def terms_and_conditions():
        return render_template(
            "terms_and_conditions.html",
            title="Terms of use | MyFXJournal trading journal service",
            meta_description="Review the terms for using MyFXJournal, including account responsibilities, acceptable use, MT5 sync conditions, and service limitations.",
            canonical_url=build_external_url("/terms"),
            last_updated=legal_last_updated,
        )

    @app.route("/faq/mt5-server")
    def faq_mt5_server():
        return render_template(
            "faq_mt5_server.html",
            title="Where to find your MT5 server | MyFXJournal help",
            meta_description="See where your MT5 server name appears in MetaTrader 5 desktop and mobile so you can paste the correct value into MyFXJournal MT5 sync.",
            canonical_url=build_external_url("/faq/mt5-server"),
        )

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit(
        "8 per minute;40 per hour",
        methods=["POST"],
        error_message="Too many sign-in attempts. Please wait and try again.",
    )
    def login():
        if session.get("user_id"):
            return redirect(url_for("dashboard.home"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")

            user = User.query.filter_by(email=email).first()
            if user and check_password_hash(user.password, password):
                if os.getenv("REQUIRE_EMAIL_VERIFICATION", "true").strip().lower() in {"1", "true", "yes", "on"} and not user.email_verified:
                    session["pending_verify_email"] = user.email
                    return render_login_page(
                        error="Please verify your email address before signing in.",
                        email=email,
                    )
                signup_status = normalize_signup_status(user.signup_status)
                if signup_status == SIGNUP_STATUS_PENDING:
                    return render_login_page(
                        info="Your email has been verified. Your account is now waiting for approval.",
                        email=email,
                    )
                if signup_status == SIGNUP_STATUS_REJECTED:
                    return render_login_page(
                        error="Your registration was not approved. Contact support if you believe this is a mistake.",
                        email=email,
                    )
                if signup_status == SIGNUP_STATUS_SUSPENDED:
                    return render_login_page(
                        error="This account is currently suspended. Contact support if you need help.",
                        email=email,
                    )
                return complete_successful_login(user)

            return render_login_page(
                error="Invalid email or password.",
                email=email,
            )
        return render_login_page()

    @app.route("/auth/google", methods=["GET"])
    def google_login_start():
        if session.get("user_id"):
            return redirect(url_for("dashboard.home"))

        google_client = get_google_client()
        if google_client is None:
            return render_login_page(error="Google sign-in is not available right now.")

        clear_google_auth_session()
        session[GOOGLE_AUTH_SESSION_INTENT_KEY] = GOOGLE_AUTH_INTENT_LOGIN
        redirect_uri = build_external_url(url_for("google_auth_callback"))
        return google_client.authorize_redirect(redirect_uri, prompt="select_account")

    @app.route("/auth/google/register", methods=["POST"])
    def google_register_start():
        if session.get("user_id"):
            return redirect(url_for("dashboard.home"))

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        signup_code_value = normalize_signup_code(
            request.form.get("signup_code") or request.args.get(get_signup_code_query_param(), "")
        )

        google_client = get_google_client()
        if google_client is None:
            return render_register_page(
                error="Google sign-in is not available right now.",
                username=username,
                email=email,
                signup_code=signup_code_value,
            )

        if get_registration_paused():
            return render_register_page(
                info="New registrations are temporarily paused.",
                username=username,
                email=email,
                signup_code=signup_code_value,
            )

        accepted_legal = request.form.get("accept_legal") == "on"
        if not accepted_legal:
            return render_register_page(
                error="You must agree to the Terms and acknowledge the Privacy Policy.",
                username=username,
                email=email,
                signup_code=signup_code_value,
            )

        signup_code_mode = get_signup_code_mode()
        signup_code_row = None
        if signup_code_mode != SIGNUP_CODE_MODE_OFF and signup_code_value:
            signup_code_row = find_signup_code(signup_code_value)
            if not is_signup_code_usable(signup_code_row):
                return render_register_page(
                    error=get_signup_code_validation_message(signup_code_mode),
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )
        elif signup_code_mode == SIGNUP_CODE_MODE_REQUIRED:
            return render_register_page(
                error="A valid referral code is required to create an account right now.",
                username=username,
                email=email,
                signup_code=signup_code_value,
            )

        clear_google_auth_session()
        session[GOOGLE_AUTH_SESSION_INTENT_KEY] = GOOGLE_AUTH_INTENT_REGISTER
        session[GOOGLE_AUTH_SESSION_SIGNUP_CODE_KEY] = signup_code_value
        redirect_uri = build_external_url(url_for("google_auth_callback"))
        return google_client.authorize_redirect(redirect_uri, prompt="select_account")

    @app.route("/auth/google/callback", methods=["GET"])
    def google_auth_callback():
        intent = session.get(GOOGLE_AUTH_SESSION_INTENT_KEY, GOOGLE_AUTH_INTENT_LOGIN)
        signup_code_value = normalize_signup_code(session.get(GOOGLE_AUTH_SESSION_SIGNUP_CODE_KEY, ""))
        google_client = get_google_client()
        if google_client is None:
            clear_google_auth_session()
            return render_login_page(error="Google sign-in is not available right now.")

        try:
            token = google_client.authorize_access_token()
        except Exception as exc:
            current_app.logger.warning("Google OAuth callback failed: %s", exc)
            clear_google_auth_session()
            if intent == GOOGLE_AUTH_INTENT_REGISTER:
                return render_register_page(
                    error="Google sign-in could not be completed. Please try again.",
                    signup_code=signup_code_value,
                )
            return render_login_page(error="Google sign-in could not be completed. Please try again.")

        userinfo = token.get("userinfo") or {}
        google_sub = str(userinfo.get("sub") or "").strip()
        email = str(userinfo.get("email") or "").strip().lower()
        email_verified = bool(userinfo.get("email_verified"))
        display_name = str(userinfo.get("name") or "").strip()

        if not google_sub or not email or not email_verified:
            clear_google_auth_session()
            message = "Google did not return a verified email address for this account."
            if intent == GOOGLE_AUTH_INTENT_REGISTER:
                return render_register_page(error=message, signup_code=signup_code_value)
            return render_login_page(error=message)

        linked_user = User.query.filter_by(google_sub=google_sub).first()
        if linked_user is not None:
            clear_google_auth_session()
            return finalize_google_authenticated_user(linked_user)

        existing_user = User.query.filter_by(email=email).first()
        if existing_user is not None:
            existing_status = normalize_signup_status(existing_user.signup_status)
            if existing_status == SIGNUP_STATUS_REJECTED:
                clear_google_auth_session()
                return render_login_page(
                    error="Your registration was not approved. Contact support if you believe this is a mistake.",
                    email=email,
                )
            if existing_status == SIGNUP_STATUS_SUSPENDED:
                clear_google_auth_session()
                return render_login_page(
                    error="This account is currently suspended. Contact support if you need help.",
                    email=email,
                )
            if existing_user.google_sub and existing_user.google_sub != google_sub:
                clear_google_auth_session()
                return render_login_page(
                    error=(
                        "This email is already linked to a different Google account. "
                        "Use your existing sign-in method or contact support."
                    ),
                    email=email,
                )
            existing_user.google_sub = google_sub
            existing_user.email_verified = True
            db.session.commit()
            clear_google_auth_session()
            return finalize_google_authenticated_user(existing_user)

        if intent != GOOGLE_AUTH_INTENT_REGISTER:
            clear_google_auth_session()
            return render_login_page(
                error=(
                    "No MyFXJournal account was found for this Google email. "
                    "Use the Google button on the registration page to create one."
                ),
                email=email,
            )

        if get_registration_paused():
            clear_google_auth_session()
            return render_register_page(
                info="New registrations are temporarily paused.",
                email=email,
                signup_code=signup_code_value,
            )

        signup_code_mode = get_signup_code_mode()
        signup_code_row = None
        if signup_code_mode != SIGNUP_CODE_MODE_OFF and signup_code_value:
            signup_code_row = find_signup_code(signup_code_value)
            if not is_signup_code_usable(signup_code_row):
                clear_google_auth_session()
                return render_register_page(
                    error=get_signup_code_validation_message(signup_code_mode),
                    email=email,
                    signup_code=signup_code_value,
                )
        elif signup_code_mode == SIGNUP_CODE_MODE_REQUIRED:
            clear_google_auth_session()
            return render_register_page(
                error="A valid referral code is required to create an account right now.",
                email=email,
                signup_code=signup_code_value,
            )

        if not is_allowed_signup_email_domain(email):
            clear_google_auth_session()
            return render_register_page(
                error=(
                    "Please use a common email provider "
                    "(for example Gmail, Outlook, Yahoo, iCloud, or Proton)."
                ),
                email=email,
                signup_code=signup_code_value,
            )

        generated_password = generate_password_hash(secrets.token_urlsafe(32))
        initial_signup_status = get_initial_signup_status()
        user = User(
            username=build_unique_google_username(email=email, profile_name=display_name),
            email=email,
            google_sub=google_sub,
            password=generated_password,
            email_verified=True,
            signup_status=initial_signup_status,
            signup_code_used=signup_code_row.code if signup_code_row else None,
            approved_at=utcnow_naive() if initial_signup_status == SIGNUP_STATUS_APPROVED else None,
        )
        db.session.add(user)
        if signup_code_row and is_signup_code_usable(signup_code_row):
            signup_code_row.used_count += 1
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            clear_google_auth_session()
            return render_register_page(
                error="Account setup could not be completed. Please try again.",
                email=email,
                signup_code=signup_code_value,
            )

        clear_google_auth_session()
        return finalize_google_authenticated_user(user, newly_created=True)

    @app.route("/onboarding", methods=["GET", "POST"])
    @login_required
    def onboarding():
        user_id = session["user_id"]
        profile = get_user_profile(user_id)
        if profile is not None and getattr(profile, "completed_at", None) is not None:
            return redirect(url_for("dashboard.home"))

        if request.method == "POST":
            trading_style = request.form.get("trading_style", "").strip().lower()
            instruments = request.form.get("instruments", "").strip().lower()
            experience_level = request.form.get("experience_level", "").strip().lower()
            form_data = {
                "trading_style": trading_style,
                "instruments": instruments,
                "experience_level": experience_level,
            }
            if (
                trading_style not in VALID_ONBOARDING_TRADING_STYLES
                or instruments not in VALID_ONBOARDING_INSTRUMENTS
                or experience_level not in VALID_ONBOARDING_EXPERIENCE_LEVELS
            ):
                return render_onboarding_page(
                    error="Please answer all three questions or skip for now.",
                    profile=profile,
                    form_data=form_data,
                )

            if profile is None:
                profile = UserProfile(user_id=user_id)
                db.session.add(profile)
            profile.trading_style = trading_style
            profile.instruments = instruments
            profile.experience_level = experience_level
            profile.completed_at = utcnow_naive()
            profile.skipped = False
            db.session.commit()
            flash("You're all set! Taking you to your dashboard...", "success")
            return redirect(url_for("dashboard.home"))

        return render_onboarding_page(profile=profile)

    @app.route("/onboarding/skip", methods=["POST"])
    @login_required
    def onboarding_skip():
        user_id = session["user_id"]
        profile = get_user_profile(user_id)
        if user_profile_is_done(profile):
            return redirect(url_for("dashboard.home"))

        if profile is None:
            profile = UserProfile(user_id=user_id)
            db.session.add(profile)
        profile.skipped = True
        profile.completed_at = None
        db.session.commit()
        return redirect(url_for("dashboard.home"))

    @app.route("/register", methods=["GET", "POST"])
    @limiter.limit(
        "5 per minute;20 per hour",
        methods=["POST"],
        error_message="Too many registration attempts. Please wait and try again.",
    )
    def register():
        if session.get("user_id"):
            return redirect(url_for("dashboard.home"))
        query_param = get_signup_code_query_param()
        signup_code_prefill = normalize_signup_code(request.args.get(query_param, ""))

        if get_registration_paused():
            return render_register_page(
                info="New registrations are temporarily paused.",
                signup_code=signup_code_prefill,
            )

        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            accepted_legal = request.form.get("accept_legal") == "on"
            signup_code_value = normalize_signup_code(
                request.form.get("signup_code") or request.args.get(query_param, "")
            )
            signup_code_mode = get_signup_code_mode()
            signup_code_row = None

            if signup_code_mode != SIGNUP_CODE_MODE_OFF and signup_code_value:
                signup_code_row = find_signup_code(signup_code_value)
                if not is_signup_code_usable(signup_code_row):
                    return render_register_page(
                        error=get_signup_code_validation_message(signup_code_mode),
                        username=username,
                        email=email,
                        signup_code=signup_code_value,
                    )
            elif signup_code_mode == SIGNUP_CODE_MODE_REQUIRED:
                return render_register_page(
                    error="A valid referral code is required to create an account right now.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if not username or not email or not password:
                return render_register_page(
                    error="All fields are required.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if len(password) < 8:
                return render_register_page(
                    error="Password must be at least 8 characters.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if not is_allowed_signup_email_domain(email):
                return render_register_page(
                    error=(
                        "Please use a common email provider "
                        "(for example Gmail, Outlook, Yahoo, iCloud, or Proton)."
                    ),
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            if not accepted_legal:
                return render_register_page(
                    error="You must agree to the Terms and acknowledge the Privacy Policy.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            existing_user = User.query.filter(
                or_(User.username == username, User.email == email)
            ).first()
            if existing_user:
                existing_status = normalize_signup_status(existing_user.signup_status)
                if existing_user.email == email and existing_status == SIGNUP_STATUS_REJECTED:
                    return render_register_page(
                        error="This email is linked to a rejected registration. Contact support if you need help.",
                        username=username,
                        email=email,
                        signup_code=signup_code_value,
                    )
                if existing_user.email == email and existing_status == SIGNUP_STATUS_SUSPENDED:
                    return render_register_page(
                        error="This email belongs to a suspended account.",
                        username=username,
                        email=email,
                        signup_code=signup_code_value,
                    )
                if existing_user.email == email and not existing_user.email_verified:
                    session["pending_verify_email"] = existing_user.email
                    session.pop("pending_registration_id", None)
                    verify_token = generate_auth_token(
                        existing_user.email,
                        token_purpose_verify_email,
                    )
                    verify_link = build_external_url(
                        url_for("verify_email_token", token=verify_token)
                    )
                    email_subject = "Verify your MyFXJournal email"
                    email_body = (
                        f"Hi {existing_user.username},\n\n"
                        "You requested a new verification link.\n"
                        f"{verify_link}\n\n"
                        "If you did not request this, you can ignore this email."
                    )
                    html_body = _render_email_html(
                        "emails/verify-email.html",
                        name=existing_user.username,
                        verify_url=verify_link,
                    )
                    email_result = send_email_placeholder(
                        existing_user.email,
                        email_subject,
                        email_body,
                        html_body=html_body,
                    )
                    flash("Verification email sent. Please check your inbox.", "success")
                    verify_kwargs = {}
                    if is_local_dev_environment() and not email_result.get("sent"):
                        verify_kwargs["verify_link"] = verify_link
                    return redirect(url_for("verify_email_pending", **verify_kwargs))
                return render_register_page(
                    error="Username or email already exists.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            hashed_password = generate_password_hash(password)
            initial_signup_status = get_initial_signup_status()
            user = User(
                username=username,
                email=email,
                password=hashed_password,
                email_verified=False,
                signup_status=initial_signup_status,
                signup_code_used=signup_code_row.code if signup_code_row else None,
                approved_at=utcnow_naive() if initial_signup_status == SIGNUP_STATUS_APPROVED else None,
                verification_sent_at=utcnow_naive(),
            )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return render_register_page(
                    error="Username or email already exists.",
                    username=username,
                    email=email,
                    signup_code=signup_code_value,
                )

            session["pending_verify_email"] = user.email
            session.pop("pending_registration_id", None)
            verify_token = generate_auth_token(
                user.email,
                token_purpose_verify_email,
            )
            verify_link = build_external_url(
                url_for("verify_email_token", token=verify_token)
            )
            email_subject = "Verify your MyFXJournal email"
            email_body = (
                f"Hi {user.username},\n\n"
                "Thanks for registering.\n"
                "Verify your email by opening this link:\n"
                f"{verify_link}\n\n"
                "If you did not create this account, you can ignore this email."
            )
            html_body = _render_email_html(
                "emails/verify-email.html",
                name=user.username,
                verify_url=verify_link,
            )
            email_result = send_email_placeholder(
                user.email,
                email_subject,
                email_body,
                html_body=html_body,
            )

            flash("Verification email sent. Please check your inbox.", "success")
            verify_kwargs = {}
            if is_local_dev_environment() and not email_result.get("sent"):
                verify_kwargs["verify_link"] = verify_link
            return redirect(url_for("verify_email_pending", **verify_kwargs))

        return render_register_page(
            signup_code=signup_code_prefill,
        )

    @app.route("/verify-email/pending", methods=["GET", "POST"])
    @limiter.limit(
        "5 per minute;20 per hour",
        methods=["POST"],
        error_message="Too many attempts. Please wait and try again.",
    )
    def verify_email_pending():
        pending_email = session.get("pending_verify_email", "").strip().lower()
        pending_username = ""
        pending_id = ""
        pending = None
        using_legacy_pending = False
        awaiting_approval = False

        if pending_email:
            user = User.query.filter_by(email=pending_email).first()
            if not user:
                session.pop("pending_verify_email", None)
                pending_email = ""
            elif user.email_verified:
                session.pop("pending_verify_email", None)
                if normalize_signup_status(user.signup_status) == SIGNUP_STATUS_APPROVED:
                    flash("Email verified. You can now log in.", "success")
                    return redirect(url_for("login"))
                flash("Email verified. You can now log in.", "success")
                flash(
                    "Your email has been verified. Your account is now waiting for approval.",
                    "info",
                )
                return redirect(url_for("login"))
            else:
                pending_username = user.username
                awaiting_approval = normalize_signup_status(user.signup_status) == SIGNUP_STATUS_PENDING

        if not pending_email:
            pending_id = session.get("pending_registration_id", "").strip()
            pending = get_pending_registration(pending_id)
            if not pending:
                flash("Your verification session has expired. Please register again.", "error")
                return redirect(url_for("register"))
            pending_email = pending["email"]
            pending_username = pending["username"]
            using_legacy_pending = True
            awaiting_approval = get_initial_signup_status() == SIGNUP_STATUS_PENDING

        success = ""
        error = ""
        debug_verify_link = request.args.get("verify_link", "").strip()

        if request.method == "POST":
            if using_legacy_pending:
                verify_token = generate_pending_registration_token(
                    registration_id=pending_id,
                    email=pending_email,
                )
            else:
                user = User.query.filter_by(email=pending_email).first()
                if not user:
                    session.pop("pending_verify_email", None)
                    flash("Your verification session has expired. Please register again.", "error")
                    return redirect(url_for("register"))
                if user.email_verified:
                    session.pop("pending_verify_email", None)
                    flash("Email verified. You can now log in.", "success")
                    return redirect(url_for("login"))
                verify_token = generate_auth_token(
                    pending_email,
                    token_purpose_verify_email,
                )
            verify_link = build_external_url(
                url_for("verify_email_token", token=verify_token)
            )
            email_subject = "Verify your MyFXJournal email"
            email_body = (
                f"Hi {pending_username},\n\n"
                "You requested a new verification link.\n"
                f"{verify_link}\n\n"
                "If you did not request this, you can ignore this email."
            )
            html_body = _render_email_html(
                "emails/verify-email.html",
                name=pending_username,
                verify_url=verify_link,
            )
            email_result = send_email_placeholder(
                pending_email,
                email_subject,
                email_body,
                html_body=html_body,
            )
            success = "Verification email sent. Please check your inbox."
            if is_local_dev_environment() and not email_result.get("sent"):
                debug_verify_link = verify_link

        return render_template(
            "resend_verification.html",
            title="MyFXJournal | Verify Email",
            body_class="auth-layout",
            pending_email=pending_email,
            success=success or None,
            error=error or None,
            debug_verify_link=debug_verify_link,
            awaiting_approval=awaiting_approval,
        )

    @app.route("/verify-email/resend", methods=["GET"])
    def resend_verification_email_alias():
        return redirect(url_for("verify_email_pending"))

    @app.route("/verify-email/<token>")
    def verify_email_token(token):
        max_age_seconds = env_int("EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS", 86400)
        pending_payload = verify_pending_registration_token(
            token=token,
            max_age_seconds=max_age_seconds,
        )
        if pending_payload:
            registration_id = pending_payload["registration_id"]
            pending = get_pending_registration(registration_id)
            if not pending:
                flash("This verification link is invalid or has expired. Please register again.", "error")
                return redirect(url_for("register"))

            if pending["email"] != pending_payload["email"]:
                pop_pending_registration(registration_id)
                if session.get("pending_registration_id") == registration_id:
                    session.pop("pending_registration_id", None)
                flash("We could not verify this registration request. Please register again.", "error")
                return redirect(url_for("register"))

            existing_user = User.query.filter(
                or_(User.username == pending["username"], User.email == pending["email"])
            ).first()
            if existing_user:
                pop_pending_registration(registration_id)
                if session.get("pending_registration_id") == registration_id:
                    session.pop("pending_registration_id", None)
                if (
                    existing_user.username == pending["username"]
                    and existing_user.email == pending["email"]
                    and existing_user.email_verified
                ):
                    flash("Email verified. You can now log in.", "success")
                    return redirect(url_for("login"))
                flash(
                    "That username or email is no longer available. Please register again.",
                    "error",
                )
                return redirect(url_for("register"))

            initial_signup_status = get_initial_signup_status()
            user = User(
                username=pending["username"],
                email=pending["email"],
                password=pending["password_hash"],
                email_verified=True,
                signup_status=initial_signup_status,
                approved_at=utcnow_naive() if initial_signup_status == SIGNUP_STATUS_APPROVED else None,
                verification_sent_at=utcnow_naive(),
            )
            db.session.add(user)
            db.session.commit()

            pop_pending_registration(registration_id)
            if session.get("pending_registration_id") == registration_id:
                session.pop("pending_registration_id", None)

            if normalize_signup_status(user.signup_status) == SIGNUP_STATUS_APPROVED:
                try:
                    _send_welcome_email(user)
                except Exception as exc:
                    current_app.logger.warning("Welcome email failed: %s", exc)
                flash("Email verified. You can now log in.", "success")
                return redirect(url_for("login"))
            flash("Email verified. You can now log in.", "success")
            flash(
                "Your email has been verified. Your account is now waiting for approval.",
                "info",
            )
            return redirect(url_for("login"))

        email = verify_auth_token(
            token=token,
            purpose=token_purpose_verify_email,
            max_age_seconds=max_age_seconds,
        )
        if not email:
            flash("This verification link is invalid or has expired. Please register again.", "error")
            return redirect(url_for("register"))

        user = User.query.filter_by(email=email).first()
        if not user:
            flash("This verification link is invalid or has expired. Please register again.", "error")
            return redirect(url_for("register"))

        just_verified = False
        if not user.email_verified:
            user.email_verified = True
            just_verified = True
            if user.signup_code_used:
                signup_code = find_signup_code(user.signup_code_used)
                if is_signup_code_usable(signup_code):
                    signup_code.used_count += 1
            db.session.commit()

        if session.get("pending_verify_email", "").strip().lower() == email:
            session.pop("pending_verify_email", None)

        if normalize_signup_status(user.signup_status) == SIGNUP_STATUS_APPROVED:
            try:
                if just_verified:
                    _send_welcome_email(user)
            except Exception as exc:
                current_app.logger.warning("Welcome email failed: %s", exc)
            flash("Email verified. You can now log in.", "success")
            return redirect(url_for("login"))
        flash("Email verified. You can now log in.", "success")
        flash(
            "Your email has been verified. Your account is now waiting for approval.",
            "info",
        )
        return redirect(url_for("login"))

    @app.route("/dashboard/admin/access")
    @admin_required
    def admin_signup_access():
        return redirect(url_for("admin_signup_users"))

    @app.route("/dashboard/admin/background-preview")
    @admin_required
    def admin_background_preview():
        return render_template(
            "admin_background_preview.html",
            title="MyFXJournal | Background Preview",
        )

    @app.route("/dashboard/admin/access/users")
    @admin_required
    def admin_signup_users():
        admin_user = get_current_admin_user()

        status_filter = request.args.get("status", "pending").strip().lower()
        if status_filter not in {
            "pending",
            "approved",
            "rejected",
            "suspended",
            "admins",
            "all",
        }:
            status_filter = "pending"
        search_query = (request.args.get("q") or "").strip()
        users_sort = normalize_admin_users_sort(request.args.get("sort"))
        page = request.args.get("page", 1, type=int) or 1
        page = max(page, 1)

        users_query = User.query
        if status_filter == "admins":
            root_admin_emails = sorted(get_admin_user_emails())
            if root_admin_emails:
                users_query = users_query.filter(
                    or_(User.is_admin.is_(True), User.email.in_(root_admin_emails))
                )
            else:
                users_query = users_query.filter(User.is_admin.is_(True))
        elif status_filter != "all":
            users_query = users_query.filter_by(signup_status=status_filter)

        if search_query:
            search_like = f"%{search_query}%"
            search_filters = [
                User.username.ilike(search_like),
                User.email.ilike(search_like),
            ]
            if search_query.isdigit():
                search_filters.append(User.id == int(search_query))
            users_query = users_query.filter(or_(*search_filters))

        total_user_count = users_query.order_by(None).count()
        total_pages = max((total_user_count + ADMIN_USERS_PER_PAGE - 1) // ADMIN_USERS_PER_PAGE, 1)
        if page > total_pages:
            page = total_pages
        page_offset = (page - 1) * ADMIN_USERS_PER_PAGE

        users = (
            apply_admin_users_sort(users_query, users_sort)
            .offset(page_offset)
            .limit(ADMIN_USERS_PER_PAGE)
            .all()
        )
        pending_users = (
            User.query.filter_by(signup_status=SIGNUP_STATUS_PENDING)
            .order_by(User.email_verified.asc(), User.verification_sent_at.asc(), User.id.asc())
            .limit(6)
            .all()
        )
        user_ids = [user.id for user in users]
        accounts_by_user = {user.id: [] for user in users}
        if user_ids:
            account_rows = (
                TradeAccount.query.filter(TradeAccount.user_id.in_(user_ids))
                .order_by(
                    TradeAccount.user_id.asc(),
                    TradeAccount.is_default.desc(),
                    TradeAccount.id.asc(),
                )
                .all()
            )
            for account in account_rows:
                accounts_by_user.setdefault(account.user_id, []).append(account)
        user_stats_by_user = {
            user.id: {
                "trade_account_count": len(accounts_by_user.get(user.id, [])),
                "total_trades": 0,
                "last_trade_at": None,
                "mt5_account_count": 0,
            }
            for user in users
        }
        if user_ids:
            trade_stat_rows = (
                db.session.query(
                    Trade.user_id,
                    func.count(Trade.id),
                    func.max(func.coalesce(Trade.closed_at, Trade.opened_at)),
                )
                .filter(Trade.user_id.in_(user_ids))
                .group_by(Trade.user_id)
                .all()
            )
            for row_user_id, total_trades, last_trade_at in trade_stat_rows:
                user_stats_by_user.setdefault(
                    row_user_id,
                    {
                        "trade_account_count": 0,
                        "total_trades": 0,
                        "last_trade_at": None,
                        "mt5_account_count": 0,
                    },
                )
                user_stats_by_user[row_user_id]["total_trades"] = total_trades or 0
                user_stats_by_user[row_user_id]["last_trade_at"] = last_trade_at

            mt5_count_rows = (
                db.session.query(MT5Account.user_id, func.count(MT5Account.id))
                .filter(MT5Account.user_id.in_(user_ids))
                .group_by(MT5Account.user_id)
                .all()
            )
            for row_user_id, mt5_account_count in mt5_count_rows:
                user_stats_by_user.setdefault(
                    row_user_id,
                    {
                        "trade_account_count": 0,
                        "total_trades": 0,
                        "last_trade_at": None,
                        "mt5_account_count": 0,
                    },
                )
                user_stats_by_user[row_user_id]["mt5_account_count"] = mt5_account_count or 0

        users_showing_from = page_offset + 1 if total_user_count else 0
        users_showing_to = min(page_offset + len(users), total_user_count) if total_user_count else 0

        return render_admin_page(
            admin_user=admin_user,
            section="users",
            users=users,
            status_filter=status_filter,
            search_query=search_query,
            users_sort=users_sort,
            users_page=page,
            users_total_pages=total_pages,
            users_total_count=total_user_count,
            users_page_size=ADMIN_USERS_PER_PAGE,
            users_showing_from=users_showing_from,
            users_showing_to=users_showing_to,
            pending_users=pending_users,
            accounts_by_user=accounts_by_user,
            user_stats_by_user=user_stats_by_user,
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/regenerate-ai-advice", methods=["POST"])
    @root_admin_required
    def admin_regenerate_ai_advice(user_id):
        admin_user = get_current_root_admin_user()

        target_user = User.query.filter_by(id=user_id).first_or_404()
        trade_account_id = request.form.get("trade_account_id", type=int)
        if not trade_account_id:
            return build_admin_redirect("users", "No trade account selected.", "info")

        account = TradeAccount.query.filter_by(id=trade_account_id, user_id=user_id).first_or_404()
        period = get_latest_trade_week_period(user_id=user_id, trade_account_id=account.id)
        if not period:
            return build_admin_redirect(
                "users",
                f"No active trading period found for {target_user.email} / {account.name}.",
                "info",
            )

        try:
            from celery_workers.cache import CacheUnavailableError, clear_ai_status
            from celery_workers.weekly_tasks import generate_weekly_ai_task

            try:
                clear_ai_status(
                    user_id,
                    trade_account_id=account.id,
                    period_start_utc=period["period_start_utc"],
                )
            except CacheUnavailableError:
                pass

            generate_weekly_ai_task.delay(
                user_id,
                account.id,
                "dashboard_advice.txt",
                period["period_start_utc"].isoformat(),
                force_regenerate=True,
                send_weekly_email=False,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "Admin AI regeneration unavailable: user_id=%s trade_account_id=%s error=%s",
                user_id,
                trade_account_id,
                exc,
            )
            occurred_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            app_env = os.getenv("APP_ENV", "").strip() or os.getenv("FLASK_ENV", "").strip() or "unknown"
            subject = f"[FX Journal Error] {type(exc).__name__} on admin_regenerate_ai_advice"
            body = (
                "Handled admin AI regeneration failure\n\n"
                f"Occurred at: {occurred_at}\n"
                f"Environment: {app_env}\n"
                f"Method: {request.method}\n"
                f"Endpoint: {request.endpoint or '-'}\n"
                f"Route pattern: {request.url_rule.rule if request.url_rule else '-'}\n"
                f"Admin user ID: {getattr(admin_user, 'id', '-')}\n"
                f"Target user ID: {user_id}\n"
                f"Target user email: {target_user.email}\n"
                f"Trade account ID: {account.id}\n"
                f"Trade account name: {account.name}\n"
                f"Exception type: {type(exc).__name__}\n"
                f"Exception message: {sanitize_error_message(exc)}\n"
            )
            send_error_log_email(subject=subject, body=body)
            return build_admin_redirect(
                "users",
                "AI advice regeneration is temporarily unavailable. Please try again in a little while.",
                "error",
            )

        return build_admin_redirect(
            "users",
            f"AI review generation queued for {target_user.email} / {account.name}.",
            "success",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/backfill-bundles", methods=["POST"])
    @root_admin_required
    def admin_backfill_trade_bundles(user_id):
        target_user = User.query.filter_by(id=user_id).first_or_404()
        trade_account_id = request.form.get("trade_account_id", type=int)
        if not trade_account_id:
            return build_admin_redirect("users", "No trade account selected for bundle backfill.", "info")

        account = TradeAccount.query.filter_by(id=trade_account_id, user_id=user_id).first_or_404()
        closed_trades = (
            Trade.query.filter_by(user_id=user_id, trade_account_id=account.id)
            .options(selectinload(Trade.interpretation))
            .filter(Trade.closed_at.isnot(None))
            .order_by(Trade.closed_at.desc(), Trade.id.desc())
            .all()
        )
        if not closed_trades:
            return build_admin_redirect(
                "users",
                f"No closed trades found for {target_user.email} / {account.name}.",
                "info",
            )

        outliers = detect_outliers(closed_trades)
        bundle_candidates = outliers.get("bundle_candidates") or []
        if not bundle_candidates:
            return build_admin_redirect(
                "users",
                f"No historical bundle candidates found for {target_user.email} / {account.name}.",
                "info",
            )
        account.bundle_review_requested_at = utcnow_naive()
        db.session.commit()
        return build_admin_redirect(
            "users",
            (
                f"Queued bundle review for {target_user.email} / {account.name}. "
                f"The user will be prompted to review {len(bundle_candidates)} historical bundle"
                f"{'s' if len(bundle_candidates) != 1 else ''} on their dashboard."
            ),
            "success",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/unbundle-trades", methods=["POST"])
    @root_admin_required
    def admin_unbundle_trade_account(user_id):
        target_user = User.query.filter_by(id=user_id).first_or_404()
        trade_account_id = request.form.get("trade_account_id", type=int)
        if not trade_account_id:
            return build_admin_redirect("users", "No trade account selected for unbundling.", "info")

        account = TradeAccount.query.filter_by(id=trade_account_id, user_id=user_id).first_or_404()
        bundled_trades = (
            Trade.query.join(TradeInterpretation)
            .options(contains_eager(Trade.interpretation))
            .filter(Trade.user_id == user_id, Trade.trade_account_id == account.id)
            .filter(TradeInterpretation.bundle_pubkey.isnot(None))
            .all()
        )
        had_review_state = bool(account.bundle_review_requested_at or account.bundle_review_completed_at)

        if not bundled_trades and not had_review_state:
            return build_admin_redirect(
                "users",
                f"No bundled trades found for {target_user.email} / {account.name}.",
                "info",
            )

        admin_user = get_current_root_admin_user()
        admin_id = admin_user.id if admin_user is not None else None
        for trade in bundled_trades:
            apply_interpretation(
                trade,
                bundle_pubkey=None,
                source="admin_unbundle",
                user_id=admin_id,
            )
        account.bundle_review_requested_at = None
        account.bundle_review_completed_at = None
        db.session.commit()

        bundle_count = len(bundled_trades)
        if bundle_count:
            return build_admin_redirect(
                "users",
                (
                    f"Removed {bundle_count} bundled trade link"
                    f"{'s' if bundle_count != 1 else ''} for {target_user.email} / {account.name}. "
                    "Reactive and corrective flags were left untouched."
                ),
                "success",
            )
        return build_admin_redirect(
            "users",
            f"Cleared pending bundle review state for {target_user.email} / {account.name}.",
            "success",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/approve", methods=["POST"])
    @root_admin_required
    def admin_signup_approve_user(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")

        current_status = normalize_signup_status(user.signup_status)
        if current_status == SIGNUP_STATUS_APPROVED:
            return build_admin_redirect("users", "That user is already approved.", "info")

        user.signup_status = SIGNUP_STATUS_APPROVED
        user.approved_at = utcnow_naive()
        user.approved_by_user_id = admin_user.id
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"{'Reactivated' if current_status in {SIGNUP_STATUS_REJECTED, SIGNUP_STATUS_SUSPENDED} else 'Approved'} {user.username}.",
            "success",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/reject", methods=["POST"])
    @root_admin_required
    def admin_signup_reject_user(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")

        if user.id == admin_user.id:
            return build_admin_redirect("users", "You cannot reject your own account.", "error")

        current_status = normalize_signup_status(user.signup_status)
        if current_status != SIGNUP_STATUS_PENDING:
            return build_admin_redirect(
                "users",
                "Only pending signups can be rejected. Approved accounts should be suspended instead.",
                "error",
            )

        user.signup_status = SIGNUP_STATUS_REJECTED
        user.approved_at = None
        user.approved_by_user_id = None
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"Rejected {user.username}.",
            "info",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/suspend", methods=["POST"])
    @root_admin_required
    def admin_signup_suspend_user(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")

        if user.id == admin_user.id:
            return build_admin_redirect("users", "You cannot suspend your own account.", "error")

        current_status = normalize_signup_status(user.signup_status)
        if current_status != SIGNUP_STATUS_APPROVED:
            return build_admin_redirect(
                "users",
                "Only approved accounts can be suspended.",
                "error",
            )

        user.signup_status = SIGNUP_STATUS_SUSPENDED
        user.approved_at = None
        user.approved_by_user_id = None
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"Suspended {user.username}.",
            "info",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/delete", methods=["POST"])
    @root_admin_required
    def admin_signup_delete_user(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")
        if user.id == admin_user.id:
            return build_admin_redirect("users", "You cannot delete your own account.", "error")
        if is_root_admin_email(user.email):
            return build_admin_redirect(
                "users",
                "Root admin accounts cannot be deleted from this panel.",
                "error",
            )

        username = user.username
        try:
            delete_users_with_related_data([user.id])
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            current_app.logger.exception("Admin user delete failed for user_id=%s", user_id)
            return build_admin_redirect(
                "users",
                f"Could not delete {username}. Try again or check server logs.",
                "error",
            )

        return build_admin_redirect(
            "users",
            f"Deleted account {username} and related data.",
            "success",
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/admin-toggle", methods=["POST"])
    @root_admin_required
    def admin_signup_toggle_user_admin(user_id):
        admin_user = get_current_root_admin_user()

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")
        if user.id == admin_user.id:
            return build_admin_redirect(
                "users",
                "Use ADMIN_USER_EMAILS to control your own root access; this toggle is for other users.",
                "error",
            )
        if is_root_admin_email(user.email):
            return build_admin_redirect(
                "users",
                "Root admin emails keep access from environment settings and cannot be changed here.",
                "error",
            )
        if normalize_signup_status(user.signup_status) != SIGNUP_STATUS_APPROVED:
            return build_admin_redirect(
                "users",
                "Only approved users can be granted admin access.",
                "error",
            )

        user.is_admin = not user.is_admin
        db.session.commit()
        return build_admin_redirect(
            "users",
            f"{'Granted' if user.is_admin else 'Removed'} admin access for {user.username}.",
            "success",
        )

    @app.route("/dashboard/admin/access/codes")
    @admin_required
    def admin_signup_codes():
        admin_user = get_current_admin_user()

        signup_codes = (
            SignupCode.query.order_by(SignupCode.created_at.desc(), SignupCode.id.desc()).all()
        )
        return render_admin_page(
            admin_user=admin_user,
            section="codes",
            signup_codes=signup_codes,
        )

    @app.route("/dashboard/admin/access/cfd-symbols")
    @root_admin_required
    def admin_cfd_symbols():
        admin_user = get_current_root_admin_user()
        cfd_rows = CFDSymbol.query.order_by(
            CFDSymbol.sort_order.asc(),
            CFDSymbol.symbol.asc(),
        ).all()
        return render_admin_page(
            admin_user=admin_user,
            section="cfd_symbols",
            title="MyFXJournal | CFD broker aliases",
            page_heading="CFD broker aliases",
            page_subtitle=(
                "Comma-separated MT5/broker strings mapped to each canonical CFD symbol. "
                "Saves apply immediately (symbol cache cleared). Inactive rows are stored but ignored for sync until activated."
            ),
            cfd_symbol_rows=cfd_rows,
        )

    @app.route("/dashboard/admin/access/cfd-symbols/<int:symbol_id>/aliases", methods=["POST"])
    @root_admin_required
    def admin_cfd_symbol_update_aliases(symbol_id):
        row = CFDSymbol.query.filter_by(id=symbol_id).first()
        if row is None:
            flash("CFD symbol not found.", "error")
            return redirect(url_for("admin_cfd_symbols"))

        proposed = request.form.get("aliases", "")
        all_rows = CFDSymbol.query.order_by(
            CFDSymbol.sort_order.asc(),
            CFDSymbol.symbol.asc(),
        ).all()
        if row.is_active:
            conflicts = collect_active_cfd_alias_key_conflicts(
                all_rows,
                updated_row_id=row.id,
                updated_aliases_text=proposed,
            )
            if conflicts:
                for msg in conflicts[:5]:
                    flash(msg, "error")
                if len(conflicts) > 5:
                    flash(f"...and {len(conflicts) - 5} more conflicts.", "error")
                return redirect(url_for("admin_cfd_symbols"))

        row.aliases = format_cfd_aliases_for_storage(proposed)
        db.session.commit()
        clear_cfd_symbol_cache()
        flash(f"Updated aliases for {row.symbol}.", "success")
        return redirect(url_for("admin_cfd_symbols"))

    @app.route("/dashboard/admin/access/mt5")
    @root_admin_required
    def admin_mt5_accounts():
        admin_user = get_current_root_admin_user()
        mt5_sort = normalize_admin_mt5_sort(request.args.get("sort"))
        mt5_accounts = apply_admin_mt5_sort(MT5Account.query, mt5_sort).all()
        mt5_trade_counts_by_account = {}
        trade_account_ids = sorted(
            {
                account.trade_account_id
                for account in mt5_accounts
                if account.trade_account_id is not None
            }
        )
        if trade_account_ids:
            trade_count_rows = (
                db.session.query(Trade.trade_account_id, func.count(Trade.id))
                .filter(Trade.trade_account_id.in_(trade_account_ids))
                .group_by(Trade.trade_account_id)
                .all()
            )
            trade_counts_by_trade_account = {
                trade_account_id: trade_count
                for trade_account_id, trade_count in trade_count_rows
            }
            mt5_trade_counts_by_account = {
                account.id: trade_counts_by_trade_account.get(account.trade_account_id, 0)
                for account in mt5_accounts
            }
        latest_request_by_trade_account_id = {}
        request_trade_account_ids = sorted(
            {
                account.trade_account_id
                for account in mt5_accounts
                if account.trade_account_id is not None
            }
        )
        if request_trade_account_ids:
            request_rows = (
                MT5AccessRequest.query.filter(
                    MT5AccessRequest.trade_account_id.in_(request_trade_account_ids)
                )
                .order_by(MT5AccessRequest.created_at.desc(), MT5AccessRequest.id.desc())
                .all()
            )
            for request_row in request_rows:
                latest_request_by_trade_account_id.setdefault(
                    request_row.trade_account_id,
                    request_row,
                )
        mt5_statuses_by_account_id = {}
        for account in mt5_accounts:
            request_row = None
            if account.trade_account_id is not None:
                request_row = latest_request_by_trade_account_id.get(account.trade_account_id)
            mt5_statuses_by_account_id[account.id] = _build_admin_mt5_status(
                account=account,
                request_row=request_row,
            )
        orphaned_mt5_count = sum(1 for account in mt5_accounts if account.is_orphaned)
        mt5_batches = (
            MT5SyncBatch.query.order_by(
                MT5SyncBatch.created_at.desc(),
                MT5SyncBatch.id.desc(),
            ).all()
        )
        batch_usage_by_id = {}
        batch_ids = [batch.id for batch in mt5_batches]
        if batch_ids:
            batch_usage_by_id = dict(
                db.session.query(
                    MT5AccessRequest.batch_id,
                    func.count(MT5AccessRequest.id),
                )
                .filter(
                    MT5AccessRequest.batch_id.in_(batch_ids),
                    MT5AccessRequest.status.in_(
                        [
                            MT5AccessRequest.STATUS_PENDING,
                            MT5AccessRequest.STATUS_APPROVED,
                        ]
                    ),
                )
                .group_by(MT5AccessRequest.batch_id)
                .all()
            )
        active_mt5_batch = None
        for batch in mt5_batches:
            batch.active_slots_used = int(batch_usage_by_id.get(batch.id, 0) or 0)
            batch.claimed_slots = max(
                int(batch.total_slots_claimed or 0),
                batch.active_slots_used,
            )
            batch.slots_remaining = max(
                int(batch.capacity_total or 0) - batch.claimed_slots,
                0,
            )
            if batch.is_open and active_mt5_batch is None:
                active_mt5_batch = batch
        # List below the open-batch card should not repeat the active row.
        mt5_batches_history = [
            b
            for b in mt5_batches
            if active_mt5_batch is None or b.id != active_mt5_batch.id
        ]
        return render_admin_page(
            admin_user=admin_user,
            section="mt5",
            mt5_accounts=mt5_accounts,
            mt5_sort=mt5_sort,
            mt5_trade_counts_by_account=mt5_trade_counts_by_account,
            mt5_statuses_by_account_id=mt5_statuses_by_account_id,
            orphaned_mt5_count=orphaned_mt5_count,
            mt5_batches=mt5_batches,
            mt5_batches_history=mt5_batches_history,
            active_mt5_batch=active_mt5_batch,
        )

    @app.route("/dashboard/admin/users/<int:target_user_id>/view-dashboard")
    @root_admin_required
    def admin_view_user_dashboard(target_user_id):
        target_user = db.session.get(User, target_user_id)
        if target_user is None:
            return build_admin_redirect("users", "User not found.", "error")
        admin_user = get_current_root_admin_user()
        admin_username = admin_user.username if admin_user else session.get("username", "admin")
        session[SUPPORT_VIEW_ADMIN_USER_SESSION_KEY] = session.get("user_id")
        session[SUPPORT_VIEW_ADMIN_USERNAME_SESSION_KEY] = admin_username
        session[SUPPORT_VIEW_TARGET_USER_SESSION_KEY] = target_user.id
        session.pop(SUPPORT_VIEW_ACTIVE_TRADE_ACCOUNT_SESSION_KEY, None)
        current_app.logger.info(
            "Admin support view started: admin_user_id=%s (%s) target_user_id=%s (%s)",
            session.get("user_id"),
            admin_username,
            target_user_id,
            target_user.username,
        )
        return redirect(url_for("dashboard.home"))

    @app.route("/dashboard/admin/support-view/exit")
    @root_admin_required
    def admin_exit_support_view():
        current_app.logger.info(
            "Admin support view ended: admin_user_id=%s target_user_id=%s",
            session.get("user_id"),
            session.get(SUPPORT_VIEW_TARGET_USER_SESSION_KEY),
        )
        clear_support_view_session()
        return redirect(url_for("admin_signup_users"))

    @app.route("/dashboard/admin/access/weekly-report")
    @app.route("/dashboard/admin/access/weekly-report/<int:user_id>")
    @root_admin_required
    def admin_weekly_report(user_id=None):
        admin_user = get_current_root_admin_user()
        requested_user_id = user_id or request.args.get("user_id", type=int)
        requested_trade_account_id = request.args.get("trade_account_id", type=int)

        latest_record = (
            AIGeneratedResponse.query.filter_by(kind=WEEKLY_DASHBOARD_KIND)
            .options(
                load_only(
                    AIGeneratedResponse.id,
                    AIGeneratedResponse.user_id,
                    AIGeneratedResponse.trade_account_id,
                )
            )
            .order_by(AIGeneratedResponse.generated_at.desc(), AIGeneratedResponse.id.desc())
            .first()
        )

        if requested_user_id is not None:
            selected_user = User.query.filter_by(id=requested_user_id).first_or_404()
        elif latest_record is not None and latest_record.user_id is not None:
            selected_user = User.query.filter_by(id=latest_record.user_id).first() or admin_user
        else:
            selected_user = admin_user

        audit_users = (
            User.query.join(AIGeneratedResponse, AIGeneratedResponse.user_id == User.id)
            .filter(AIGeneratedResponse.kind == WEEKLY_DASHBOARD_KIND)
            .distinct()
            .order_by(User.username.asc(), User.id.asc())
            .all()
        )
        audit_user_ids = {row.id for row in audit_users}
        if selected_user and selected_user.id not in audit_user_ids:
            audit_users = [selected_user, *audit_users]

        user_trade_accounts = (
            TradeAccount.query.filter_by(user_id=selected_user.id)
            .order_by(
                TradeAccount.is_default.desc(),
                TradeAccount.name.asc(),
                TradeAccount.id.asc(),
            )
            .all()
        )

        ai_count_rows = (
            db.session.query(
                AIGeneratedResponse.trade_account_id,
                func.count(AIGeneratedResponse.id),
            )
            .filter_by(user_id=selected_user.id, kind=WEEKLY_DASHBOARD_KIND)
            .group_by(AIGeneratedResponse.trade_account_id)
            .all()
        )
        ai_counts_by_account_id = {
            trade_account_id: int(record_count or 0)
            for trade_account_id, record_count in ai_count_rows
            if trade_account_id is not None
        }

        account_options = []
        known_account_ids = set()
        for account in user_trade_accounts:
            known_account_ids.add(account.id)
            record_count = ai_counts_by_account_id.get(account.id, 0)
            account_type_label = str(account.account_type or "Unknown").strip().title() or "Unknown"
            account_label = f"{account.name} ({account_type_label})"
            if record_count:
                account_label = f"{account_label} - {record_count} stored review{'s' if record_count != 1 else ''}"
            account_options.append(
                {
                    "id": account.id,
                    "label": account_label,
                    "record_count": record_count,
                }
            )

        archived_account_ids = sorted(
            account_id
            for account_id in ai_counts_by_account_id
            if account_id not in known_account_ids
        )
        for account_id in archived_account_ids:
            record_count = ai_counts_by_account_id.get(account_id, 0)
            account_options.append(
                {
                    "id": account_id,
                    "label": (
                        f"Archived account [ID: {account_id}] - "
                        f"{record_count} stored review{'s' if record_count != 1 else ''}"
                    ),
                    "record_count": record_count,
                }
            )

        account_labels_by_id = {
            option["id"]: option["label"]
            for option in account_options
        }

        selected_trade_account_id = None
        if requested_trade_account_id is not None:
            if requested_trade_account_id not in account_labels_by_id:
                abort(404)
            selected_trade_account_id = requested_trade_account_id
        elif latest_record is not None and latest_record.user_id == selected_user.id:
            latest_account_id = latest_record.trade_account_id
            if latest_account_id in account_labels_by_id:
                selected_trade_account_id = latest_account_id
        if selected_trade_account_id is None:
            selected_option = next(
                (
                    option
                    for option in account_options
                    if int(option.get("record_count") or 0) > 0
                ),
                None,
            )
            if selected_option is not None:
                selected_trade_account_id = selected_option["id"]
            elif account_options:
                selected_trade_account_id = account_options[0]["id"]

        selected_trade_account = next(
            (
                account
                for account in user_trade_accounts
                if account.id == selected_trade_account_id
            ),
            None,
        )
        selected_trade_account_label = (
            account_labels_by_id.get(selected_trade_account_id)
            or "Unassigned account"
        )

        records_query = AIGeneratedResponse.query.filter_by(
            user_id=selected_user.id,
            kind=WEEKLY_DASHBOARD_KIND,
        )
        if selected_trade_account_id is not None:
            records_query = records_query.filter_by(trade_account_id=selected_trade_account_id)
        else:
            records_query = records_query.filter(AIGeneratedResponse.trade_account_id.is_(None))

        ordered_records = (
            records_query.options(joinedload(AIGeneratedResponse.prompt_history))
            .order_by(
                AIGeneratedResponse.period_start_utc.desc(),
                AIGeneratedResponse.generated_at.desc(),
                AIGeneratedResponse.id.desc(),
            ).all()
        )

        record_counts_by_period_key = {}
        for row in ordered_records:
            period_key = row.period_start_utc.isoformat() if row.period_start_utc else f"generated:{row.id}"
            record_counts_by_period_key[period_key] = record_counts_by_period_key.get(period_key, 0) + 1

        unique_records = []
        seen_period_keys = set()
        for row in ordered_records:
            period_key = row.period_start_utc.isoformat() if row.period_start_utc else f"generated:{row.id}"
            if period_key in seen_period_keys:
                continue
            seen_period_keys.add(period_key)
            unique_records.append(
                _serialize_admin_weekly_record(
                    row,
                    generation_count=record_counts_by_period_key.get(period_key, 1),
                    trade_account_label=selected_trade_account_label,
                )
            )
            if len(unique_records) >= 12:
                break

        weekly_audit_page_data = {
            "initial_record_id": unique_records[0]["id"] if unique_records else None,
            "records": unique_records,
        }

        return render_admin_weekly_report_page(
            admin_user=admin_user,
            audit_users=audit_users,
            selected_user=selected_user,
            user_trade_accounts=user_trade_accounts,
            account_options=account_options,
            selected_trade_account=selected_trade_account,
            selected_trade_account_id=selected_trade_account_id,
            selected_trade_account_label=selected_trade_account_label,
            weekly_report_records=unique_records,
            weekly_audit_page_data=weekly_audit_page_data,
        )

    @app.route("/dashboard/admin/access/mt5/create", methods=["POST"])
    @root_admin_required
    def admin_mt5_create_account():
        user_id = request.form.get("user_id", type=int)
        trade_account_id = request.form.get("trade_account_id", type=int)
        account_number = (request.form.get("account_number") or "").strip()
        investor_password = request.form.get("investor_password") or ""
        server = (request.form.get("server") or "").strip()

        if not user_id or not trade_account_id or not account_number or not investor_password or not server:
            return build_admin_redirect("mt5", "All required MT5 account fields must be provided.", "error")

        user = User.query.filter_by(id=user_id).first()
        if user is None:
            return build_admin_redirect("mt5", "User not found.", "error")

        trade_account = TradeAccount.query.filter_by(id=trade_account_id, user_id=user_id).first()
        if trade_account is None:
            return build_admin_redirect(
                "mt5",
                "Trade account not found for that user.",
                "error",
            )
        if str(trade_account.account_type or "").strip().upper() != "CFD":
            return build_admin_redirect("mt5", "MT5 sync currently supports CFD trade accounts only.", "error")
        existing_mt5_account = MT5Account.query.filter_by(trade_account_id=trade_account.id).first()
        if existing_mt5_account is not None:
            return build_admin_redirect(
                "mt5",
                (
                    f"Trade account {trade_account.name} already has MT5 account "
                    f"{existing_mt5_account.account_number} linked to it."
                ),
                "error",
            )

        try:
            mt5_account = MT5Account(
                user_id=user.id,
                trade_account_id=trade_account.id,
                account_number=account_number,
                investor_password_encrypted=encrypt_password(investor_password),
                server=server,
                terminal_path=None,
                appdata_hash=None,
                is_active=False,
            )
            db.session.add(mt5_account)
            db.session.commit()
        except (RuntimeError, ValueError) as exc:
            db.session.rollback()
            return build_admin_redirect("mt5", str(exc), "error")
        except IntegrityError:
            db.session.rollback()
            return build_admin_redirect(
                "mt5",
                "That trade account already has an MT5 account linked to it.",
                "error",
            )
        except OperationalError:
            db.session.rollback()
            return build_admin_redirect(
                "mt5",
                "Could not save that MT5 account right now. Please try again.",
                "error",
            )

        try:
            from celery_workers.mt5_setup_tasks import setup_mt5_terminal

            setup_mt5_terminal.apply_async(
                args=[mt5_account.id],
                queue="mt5_setup",
            )
            current_app.logger.info(
                "Admin queued MT5 setup_terminal mt5_account_id=%s queue=mt5_setup",
                mt5_account.id,
            )
        except Exception as exc:
            current_app.logger.warning(
                "MT5 setup queue failed for mt5_account_id=%s: %s",
                mt5_account.id,
                sanitize_error_message(exc),
            )
            return build_admin_redirect(
                "mt5",
                "Account created but setup could not be queued. Click Setup Terminal to retry.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            f"Added MT5 account {account_number} for {user.email}. Terminal setup queued.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/setup", methods=["POST"])
    @root_admin_required
    def admin_mt5_setup_terminal(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        if account.is_orphaned:
            return build_admin_redirect(
                "mt5",
                "That MT5 record is cleanup-only now. Delete it manually from admin when you're ready.",
                "error",
            )
        if account.is_archived:
            return build_admin_redirect(
                "mt5",
                "That MT5 account is archived. Use Reactivate to rebuild its VM terminal.",
                "error",
            )

        try:
            from celery_workers.mt5_setup_tasks import setup_mt5_terminal

            setup_mt5_terminal.apply_async(
                args=[mt5_account_id],
                queue="mt5_setup",
            )
            current_app.logger.info(
                "Admin queued MT5 setup_terminal mt5_account_id=%s queue=mt5_setup",
                mt5_account_id,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 setup queue failed for mt5_account_id=%s: %s",
                mt5_account_id,
                sanitize_error_message(exc),
            )
            return build_admin_redirect(
                "mt5",
                "MT5 terminal setup could not be queued right now. Click Setup Terminal to retry.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            f"MT5 terminal setup queued for account {account.account_number}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/archive", methods=["POST"])
    @root_admin_required
    def admin_mt5_archive_account(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        ok, message = archive_mt5_account(
            mt5_account=account,
            archive_reason=MT5Account.ARCHIVE_REASON_INACTIVITY,
            log_context="admin archive",
        )
        if ok:
            message = (
                f"Archived MT5 account {account.account_number}. Reactivation remains available from the dashboard."
            )
        return build_admin_redirect(
            "mt5",
            message,
            "success" if ok else "error",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/reactivate", methods=["POST"])
    @root_admin_required
    def admin_mt5_reactivate_account(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        ok, message = reactivate_mt5_account(
            mt5_account=account,
            log_context="admin reactivate",
        )
        if ok:
            current_app.logger.info(
                "Admin queued MT5 reactivation mt5_account_id=%s queue=mt5_setup",
                mt5_account_id,
            )
        return build_admin_redirect(
            "mt5",
            message,
            "success" if ok else "error",
        )

    @app.route("/dashboard/admin/access/mt5/batches/create", methods=["POST"])
    @root_admin_required
    def admin_mt5_create_batch():
        admin_user = get_current_root_admin_user()
        existing_open_batch = MT5SyncBatch.query.filter_by(is_open=True).first()
        if existing_open_batch is not None:
            return build_admin_redirect(
                "mt5",
                f"Close the current open batch ({existing_open_batch.name}) before creating a new one.",
                "error",
            )

        raw_name = (request.form.get("name") or "").strip()
        raw_notes = (request.form.get("notes") or "").strip()
        raw_capacity = (request.form.get("capacity_total") or "").strip()

        if len(raw_name) > 120:
            return build_admin_redirect("mt5", "Batch name must be 120 characters or less.", "error")
        if len(raw_notes) > 1000:
            return build_admin_redirect("mt5", "Batch notes must be 1000 characters or less.", "error")
        try:
            capacity_total = int(raw_capacity)
        except (TypeError, ValueError):
            return build_admin_redirect("mt5", "Batch capacity must be a whole number.", "error")
        if capacity_total <= 0:
            return build_admin_redirect("mt5", "Batch capacity must be greater than zero.", "error")

        batch = MT5SyncBatch(
            name=raw_name or f"MT5 Batch {utcnow_naive().strftime('%Y-%m-%d %H:%M UTC')}",
            notes=raw_notes or None,
            capacity_total=capacity_total,
            total_slots_claimed=0,
            is_open=True,
            opened_at=utcnow_naive(),
            created_by_user_id=admin_user.id if admin_user else None,
            updated_by_user_id=admin_user.id if admin_user else None,
        )
        try:
            db.session.add(batch)
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return build_admin_redirect("mt5", "Could not create that MT5 batch right now. Please try again.", "error")

        return build_admin_redirect(
            "mt5",
            f"Opened MT5 sync batch '{batch.name}' with {batch.capacity_total} slot{'s' if batch.capacity_total != 1 else ''}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/batches/<int:batch_id>/add-slots", methods=["POST"])
    @root_admin_required
    def admin_mt5_add_batch_slots(batch_id):
        admin_user = get_current_root_admin_user()
        batch = MT5SyncBatch.query.filter_by(id=batch_id).first_or_404()
        raw_slots = (request.form.get("additional_slots") or "").strip()
        try:
            additional_slots = int(raw_slots)
        except (TypeError, ValueError):
            return build_admin_redirect("mt5", "Additional slots must be a whole number.", "error")
        if additional_slots <= 0:
            return build_admin_redirect("mt5", "Additional slots must be greater than zero.", "error")

        try:
            batch.capacity_total = int(batch.capacity_total or 0) + additional_slots
            batch.updated_by_user_id = admin_user.id if admin_user else None
            batch.updated_at = utcnow_naive()
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return build_admin_redirect("mt5", "Could not add MT5 batch slots right now. Please try again.", "error")

        return build_admin_redirect(
            "mt5",
            f"Added {additional_slots} MT5 sync slot{'s' if additional_slots != 1 else ''} to {batch.name}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/batches/<int:batch_id>/close", methods=["POST"])
    @root_admin_required
    def admin_mt5_close_batch(batch_id):
        admin_user = get_current_root_admin_user()
        batch = MT5SyncBatch.query.filter_by(id=batch_id).first_or_404()
        if not batch.is_open:
            return build_admin_redirect("mt5", f"{batch.name} is already closed.", "info")

        try:
            batch.is_open = False
            batch.closed_at = utcnow_naive()
            batch.updated_by_user_id = admin_user.id if admin_user else None
            batch.updated_at = batch.closed_at
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return build_admin_redirect("mt5", "Could not close that MT5 batch right now. Please try again.", "error")

        return build_admin_redirect("mt5", f"Closed MT5 sync batch '{batch.name}'.", "success")

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/sync", methods=["POST"])
    @root_admin_required
    def admin_mt5_trigger_sync(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        if account.is_orphaned:
            return build_admin_redirect(
                "mt5",
                "That MT5 record is cleanup-only now. Delete it manually from admin when you're ready.",
                "error",
            )
        if not account.is_active:
            return build_admin_redirect("mt5", "That MT5 account is inactive.", "error")

        try:
            from celery_workers.mt5_sync_tasks import sync_mt5_account

            sync_mt5_account.apply_async(
                args=[mt5_account_id],
                kwargs={"full_history": True, "trigger_source": "manual"},
                queue="mt5_sync",
            )
            current_app.logger.info(
                "Admin queued sync_mt5_account mt5_account_id=%s full_history=True trigger=manual",
                mt5_account_id,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 sync queue failed for mt5_account_id=%s: %s",
                mt5_account_id,
                sanitize_error_message(exc),
            )
            return build_admin_redirect(
                "mt5",
                "MT5 sync could not be queued right now. Please try again shortly.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            f"MT5 sync queued for account {account.account_number}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/recalibrate-trade-times", methods=["POST"])
    @root_admin_required
    def admin_mt5_recalibrate_all_trade_times():
        from sqlalchemy.orm import joinedload

        accounts = (
            MT5Account.query.options(joinedload(MT5Account.trade_account))
            .filter(
                MT5Account.is_active.is_(True),
                MT5Account.user_id.isnot(None),
                MT5Account.trade_account_id.isnot(None),
            )
            .all()
        )
        eligible = [
            a
            for a in accounts
            if a.trade_account is not None
            and str(a.trade_account.account_type or "").strip().upper() == "CFD"
        ]
        if not eligible:
            return build_admin_redirect(
                "mt5",
                "No active CFD MT5 accounts to recalibrate trade times for.",
                "info",
            )
        try:
            from celery_workers.mt5_sync_tasks import sync_mt5_account

            for account in eligible:
                sync_mt5_account.apply_async(
                    args=[account.id],
                    kwargs={
                        "full_history": True,
                        "trigger_source": "admin_recalibrate_times",
                        "recalibrate_trade_timestamps": True,
                    },
                    queue="mt5_sync",
                )
            current_app.logger.info(
                "Admin queued sync_mt5_account recalibrate_times for %s mt5_account_id(s)",
                len(eligible),
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 recalibrate-times queue failed: %s",
                sanitize_error_message(exc),
            )
            return build_admin_redirect(
                "mt5",
                "Recalibration could not be queued right now. Please try again shortly.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            (
                f"Queued full-history MT5 sync with timestamp recalibration for {len(eligible)} account(s). "
                "Runs on the worker; use Backfill Bars per account if trade charts need refreshing."
            ),
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/clear-all-trade-bars", methods=["POST"])
    @root_admin_required
    def admin_mt5_clear_all_trade_bars():
        try:
            deleted = TradeBars.query.delete(synchronize_session=False)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "Admin clear-all-trade-bars failed: %s",
                sanitize_error_message(exc),
            )
            return build_admin_redirect(
                "mt5",
                "Could not clear stored chart bars. Check logs and try again.",
                "error",
            )
        current_app.logger.info("Admin cleared all trade_bars (%s rows)", deleted)
        return build_admin_redirect(
            "mt5",
            (
                f"Removed all stored chart bars ({deleted} row{'s' if deleted != 1 else ''}). "
                "Use Backfill Bars per MT5 account to refetch from the broker."
            ),
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/recalibrate-trade-times", methods=["POST"])
    @root_admin_required
    def admin_mt5_recalibrate_trade_times(mt5_account_id):
        from sqlalchemy.orm import joinedload

        account = (
            MT5Account.query.options(joinedload(MT5Account.trade_account))
            .filter_by(id=mt5_account_id)
            .first_or_404()
        )
        if account.is_orphaned:
            return build_admin_redirect(
                "mt5",
                "That MT5 record is cleanup-only. Cannot recalibrate.",
                "error",
            )
        if not account.is_active:
            return build_admin_redirect("mt5", "That MT5 account is inactive.", "error")
        if not account.trade_account or str(account.trade_account.account_type or "").strip().upper() != "CFD":
            return build_admin_redirect(
                "mt5",
                "Trade time recalibration applies to CFD MT5 accounts only.",
                "error",
            )
        try:
            from celery_workers.mt5_sync_tasks import sync_mt5_account

            sync_mt5_account.apply_async(
                args=[mt5_account_id],
                kwargs={
                    "full_history": True,
                    "trigger_source": "admin_recalibrate_times",
                    "recalibrate_trade_timestamps": True,
                },
                queue="mt5_sync",
            )
            current_app.logger.info(
                "Admin queued sync_mt5_account mt5_account_id=%s recalibrate_trade_timestamps=True",
                mt5_account_id,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 recalibrate-times queue failed for mt5_account_id=%s: %s",
                mt5_account_id,
                sanitize_error_message(exc),
            )
            return build_admin_redirect(
                "mt5",
                "Recalibration could not be queued right now. Please try again shortly.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            f"Queued timestamp recalibration (full history) for MT5 account {account.account_number}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/backfill-bars", methods=["POST"])
    @root_admin_required
    def admin_mt5_backfill_bars(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        if account.is_orphaned:
            return build_admin_redirect("mt5", "That MT5 record is cleanup-only. Cannot backfill bars.", "error")
        if not account.is_active:
            return build_admin_redirect("mt5", "That MT5 account is inactive. Cannot backfill bars.", "error")
        force_backfill = str(request.form.get("force_backfill_bars") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        closed_trades = (
            Trade.query.filter_by(
                user_id=account.user_id,
                trade_account_id=account.trade_account_id,
            )
            .filter(Trade.closed_at.isnot(None), Trade.mt5_position.isnot(None))
            .order_by(Trade.closed_at.desc(), Trade.id.desc())
            .all()
        )

        if not closed_trades:
            return build_admin_redirect("mt5", "No closed MT5 trades found to backfill bars for.", "info")

        closed_trade_ids = [trade.id for trade in closed_trades]
        already_backfilled_trade_ids = set()
        if not force_backfill and closed_trade_ids:
            already_backfilled_trade_ids = {
                trade_id
                for (trade_id,) in (
                    db.session.query(TradeBars.trade_id)
                    .filter(
                        TradeBars.trade_id.in_(closed_trade_ids),
                        TradeBars.timeframe == "M5",
                    )
                    .distinct()
                    .all()
                )
            }
        trades_to_queue = (
            closed_trades
            if force_backfill
            else [trade for trade in closed_trades if trade.id not in already_backfilled_trade_ids]
        )
        if not trades_to_queue:
            return build_admin_redirect(
                "mt5",
                (
                    "All closed MT5 trades already have M5 bars. Use Clear Bars first if you want "
                    "to refetch everything."
                ),
                "info",
            )

        try:
            from celery_workers.mt5_sync_tasks import fetch_trade_bars
            queued = 0
            for trade in trades_to_queue:
                fetch_trade_bars.apply_async(
                    args=[mt5_account_id, trade.id],
                    queue="mt5_sync",
                )
                queued += 1
            current_app.logger.info(
                "Admin queued fetch_trade_bars mt5_account_id=%s tasks=%s closed=%s skipped_existing=%s force=%s queue=mt5_sync",
                mt5_account_id,
                queued,
                len(closed_trades),
                max(len(closed_trades) - queued, 0),
                force_backfill,
            )
        except Exception as exc:
            current_app.logger.warning(
                "Bar backfill dispatch failed for mt5_account_id=%s: %s",
                mt5_account_id,
                sanitize_error_message(exc),
            )
            return build_admin_redirect("mt5", "Bar backfill could not be queued. Please try again.", "error")

        return build_admin_redirect(
            "mt5",
            f"Queued bar backfill for {queued} closed trade{'s' if queued != 1 else ''} on account {account.account_number}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/clear-bars", methods=["POST"])
    @root_admin_required
    def admin_mt5_clear_bars(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        trade_ids = [
            trade_id
            for (trade_id,) in db.session.query(Trade.id).filter_by(
                user_id=account.user_id,
                trade_account_id=account.trade_account_id,
            ).all()
        ] if not account.is_orphaned else []

        if not trade_ids:
            return build_admin_redirect("mt5", "No trades found for that MT5 account — nothing to clear.", "info")

        try:
            deleted = (
                TradeBars.query.filter(TradeBars.trade_id.in_(trade_ids))
                .delete(synchronize_session=False)
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "Admin clear-bars failed for mt5_account_id=%s: %s",
                mt5_account_id,
                sanitize_error_message(exc),
            )
            return build_admin_redirect("mt5", "Could not clear chart bars. Check logs and try again.", "error")

        current_app.logger.info(
            "Admin cleared trade_bars for mt5_account_id=%s (%s rows)", mt5_account_id, deleted
        )
        return build_admin_redirect(
            "mt5",
            (
                f"Cleared {deleted} bar row{'s' if deleted != 1 else ''} for MT5 account "
                f"{account.account_number}. Use Backfill Bars to refetch."
            ),
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/requests/<int:request_id>/approve", methods=["POST"])
    @root_admin_required
    def admin_mt5_approve_request(request_id):
        admin_user = get_current_root_admin_user()
        request_row = MT5AccessRequest.query.filter_by(id=request_id).first_or_404()
        if request_row.status != MT5AccessRequest.STATUS_PENDING:
            return build_admin_redirect(
                "mt5",
                "That MT5 access request has already been reviewed.",
                "info",
            )

        try:
            request_row.status = MT5AccessRequest.STATUS_APPROVED
            request_row.reviewed_at = utcnow_naive()
            request_row.reviewed_by_user_id = admin_user.id if admin_user else None
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return build_admin_redirect(
                "mt5",
                "Could not approve that MT5 access request right now. Please try again.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            f"Approved MT5 access request for {request_row.trade_account.name}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/requests/<int:request_id>/reject", methods=["POST"])
    @root_admin_required
    def admin_mt5_reject_request(request_id):
        admin_user = get_current_root_admin_user()
        request_row = MT5AccessRequest.query.filter_by(id=request_id).first_or_404()
        if request_row.status != MT5AccessRequest.STATUS_PENDING:
            return build_admin_redirect(
                "mt5",
                "That MT5 access request has already been reviewed.",
                "info",
            )

        try:
            mt5_account = MT5Account.query.filter_by(trade_account_id=request_row.trade_account_id).first()
            request_row.status = MT5AccessRequest.STATUS_REJECTED
            request_row.reviewed_at = utcnow_naive()
            request_row.reviewed_by_user_id = admin_user.id if admin_user else None
            if (
                mt5_account is not None
                and not mt5_account.is_active
                and not mt5_account.terminal_path
                and not mt5_account.appdata_hash
            ):
                db.session.delete(mt5_account)
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return build_admin_redirect(
                "mt5",
                "Could not reject that MT5 access request right now. Please try again.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            f"Rejected MT5 access request for {request_row.trade_account.name}.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/delete", methods=["POST"])
    @root_admin_required
    def admin_mt5_delete_account(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        cleanup_warning = queue_mt5_account_cleanup(
            mt5_account=account,
            log_context="admin delete",
        )
        cleanup_suffix = f" {cleanup_warning}" if cleanup_warning else ""

        account_number = account.account_number

        try:
            db.session.delete(account)
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return build_admin_redirect(
                "mt5",
                "Could not delete that MT5 account right now. Please try again.",
                "error",
            )

        return build_admin_redirect(
            "mt5",
            f"Deleted MT5 account {account_number}.{cleanup_suffix}",
            "success",
        )

    @app.route("/dashboard/admin/access/codes/create", methods=["POST"])
    @root_admin_required
    def admin_signup_create_code():
        admin_user = get_current_root_admin_user()

        requested_code = normalize_signup_code(request.form.get("code"))
        notes = (request.form.get("notes") or "").strip() or None
        max_uses_raw = (request.form.get("max_uses") or "").strip()
        expires_on_raw = (request.form.get("expires_on") or "").strip()

        if requested_code and len(requested_code) < 4:
            return build_admin_redirect("codes", "Manual codes must be at least 4 characters.", "error")

        if max_uses_raw:
            try:
                max_uses = int(max_uses_raw)
            except ValueError:
                return build_admin_redirect("codes", "Max uses must be a whole number.", "error")
            if max_uses <= 0:
                return build_admin_redirect("codes", "Max uses must be greater than zero.", "error")
        else:
            max_uses = None

        if expires_on_raw:
            try:
                expires_at = datetime.fromisoformat(f"{expires_on_raw}T23:59:59")
            except ValueError:
                return build_admin_redirect("codes", "Expiry date is invalid.", "error")
        else:
            expires_at = None

        code_value = requested_code or build_unique_signup_code()
        existing_code = SignupCode.query.filter_by(code=code_value).first()
        if existing_code:
            return build_admin_redirect("codes", "That signup code already exists.", "error")

        signup_code = SignupCode(
            code=code_value,
            created_by_user_id=admin_user.id,
            notes=notes,
            is_active=True,
            max_uses=max_uses,
            used_count=0,
            expires_at=expires_at,
        )
        db.session.add(signup_code)
        db.session.commit()
        return build_admin_redirect(
            "codes",
            f"Created signup code {signup_code.code}.",
            "success",
        )

    @app.route("/dashboard/admin/access/codes/<int:code_id>/toggle", methods=["POST"])
    @root_admin_required
    def admin_signup_toggle_code(code_id):
        signup_code = SignupCode.query.filter_by(id=code_id).first()
        if not signup_code:
            return build_admin_redirect("codes", "Signup code not found.", "error")

        signup_code.is_active = not signup_code.is_active
        db.session.commit()
        return build_admin_redirect(
            "codes",
            f"{'Activated' if signup_code.is_active else 'Deactivated'} signup code {signup_code.code}.",
            "success",
        )

    @app.route("/password/forgot", methods=["GET", "POST"])
    @limiter.limit(
        "5 per minute;20 per hour",
        methods=["POST"],
        error_message="Too many attempts. Please wait and try again.",
    )
    def forgot_password():
        success = ""
        error = ""
        debug_reset_link = ""

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            generic_success = (
                "Password reset request received. If the address matches an account, the email is on its way."
            )
            if not email:
                error = "Email is required."
            else:
                user = User.query.filter_by(email=email).first()
                if user:
                    reset_nonce = rotate_password_reset_nonce(user)
                    try:
                        db.session.commit()
                    except OperationalError as exc:
                        db.session.rollback()
                        current_app.logger.warning(
                            "Password reset nonce persistence failed for user_id=%s: %s",
                            user.id,
                            exc,
                        )
                    else:
                        reset_token = generate_password_reset_token(
                            user.email,
                            token_purpose_password_reset,
                            reset_nonce,
                        )
                        reset_link = build_external_url(
                            url_for("reset_password_token", token=reset_token)
                        )
                        email_subject = "Reset your MyFXJournal password"
                        email_body = (
                            f"Hi {user.username},\n\n"
                            "You requested a password reset.\n"
                            "Open this link to set a new password:\n"
                            f"{reset_link}\n\n"
                            "If you did not request this, you can ignore this email."
                        )
                        html_body = _render_email_html(
                            "emails/password-reset.html",
                            name=user.username,
                            reset_url=reset_link,
                        )
                        email_result = send_email_placeholder(
                            user.email,
                            email_subject,
                            email_body,
                            html_body=html_body,
                        )
                        if is_local_dev_environment() and not email_result.get("sent"):
                            debug_reset_link = reset_link
                success = generic_success

        return render_template(
            "forgot_password.html",
            title="MyFXJournal | Forgot Password",
            body_class="auth-layout",
            success=success or None,
            error=error or None,
            debug_reset_link=debug_reset_link,
        )

    @app.route("/password/reset/<token>", methods=["GET", "POST"])
    def reset_password_token(token):
        max_age_seconds = env_int("PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS", 3600)
        reset_claim = verify_password_reset_token(
            token=token,
            purpose=token_purpose_password_reset,
            max_age_seconds=max_age_seconds,
        )
        if not reset_claim:
            return render_template(
                "reset_password.html",
                title="MyFXJournal | Reset Password",
                body_class="auth-layout",
                error="This password reset link is invalid or has expired.",
                token_valid=False,
            )

        user = User.query.filter_by(email=reset_claim["email"]).first()
        if not user or not user.password_reset_nonce or user.password_reset_nonce != reset_claim["reset_nonce"]:
            return render_template(
                "reset_password.html",
                title="MyFXJournal | Reset Password",
                body_class="auth-layout",
                error="This password reset link is invalid or has expired.",
                token_valid=False,
            )

        if request.method == "POST":
            new_password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not new_password or not confirm_password:
                return render_template(
                    "reset_password.html",
                    title="MyFXJournal | Reset Password",
                    body_class="auth-layout",
                    error="Both password fields are required.",
                    token_valid=True,
                )
            if new_password != confirm_password:
                return render_template(
                    "reset_password.html",
                    title="MyFXJournal | Reset Password",
                    body_class="auth-layout",
                    error="Passwords do not match.",
                    token_valid=True,
                )
            if len(new_password) < 8:
                return render_template(
                    "reset_password.html",
                    title="MyFXJournal | Reset Password",
                    body_class="auth-layout",
                    error="Password must be at least 8 characters.",
                    token_valid=True,
                )

            user.password = generate_password_hash(new_password)
            user.password_reset_nonce = None
            db.session.commit()
            flash("Password reset successful. Please log in.", "success")
            return redirect(url_for("login"))

        return render_template(
            "reset_password.html",
            title="MyFXJournal | Reset Password",
            body_class="auth-layout",
            token_valid=True,
        )

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))
