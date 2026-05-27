import csv
import io
import os
import json
import secrets
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from functools import wraps

from flask import Response, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
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
    MT5ServerSeedShortlist,
    MT5SyncBatch,
    SignupCode,
    Trade,
    TradeAccount,
    TradeBars,
    TradeInterpretation,
    UpgradeWaitlistEntry,
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
    is_local_dev_environment as core_is_local_dev_environment,
    is_support_view_session_active,
    reactivate_mt5_account,
    delete_mt5_account_vm_files,
    queue_mt5_account_cleanup,
    queue_mt5_accounts_cleanup_for_vm,
    sanitize_error_message,
)
from helpers.admin_mt5_ops import (
    build_admin_mt5_vm_overview,
    collect_admin_selectable_vm_ids,
    load_admin_mt5_monitor_snapshot,
    resolve_admin_target_vm_id,
)
from helpers.celery_dispatch import describe_celery_broker, dispatch_celery_task
from helpers.mt5_dispatch import (
    MT5_DISPATCH_SKIPPED_MISSING_VM_MSG,
    dispatch_mt5_priority,
    dispatch_mt5_setup,
    mt5_dispatch_was_skipped,
)
from helpers.trade_bars import has_complete_m5_chart_coverage
from helpers.app_settings import (
    MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY,
    get_bool_app_setting,
    set_bool_app_setting,
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
WAITLIST_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
WAITLIST_ALLOWED_SOURCES = {
    "pricing_page",
    "replay_gate",
    "mt5_trial_expired",
    "dashboard_sidebar",
    "dashboard_mt5_capacity",
    "ai_review_cta",
}
WAITLIST_ALLOWED_FEATURES = {
    "advanced_replay",
    "multi_timeframe_replay",
    "mt5_sync",
    "conversational_review",
    "full_review_history",
    "pattern_tracking",
    "ai_review",
}
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
        "active_asc",
        "active_desc",
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
        "active_desc": (User.last_active_at.desc().nulls_last(), User.id.desc()),
        "active_asc": (User.last_active_at.asc().nulls_last(), User.id.asc()),
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
    error_to_email = (
        os.getenv("ERROR_LOG_TO_EMAIL", "").strip().lower()
        or "error_log@myfxjournal.com"
    )
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


def _public_base_host_is_local(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "[::1]", "::1"}


def get_public_base_url():
    configured_base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured_base:
        base = configured_base
    else:
        base = request.host_url.rstrip("/")
    if (
        base.startswith("http://")
        and not core_is_local_dev_environment()
        and not _public_base_host_is_local(base)
    ):
        base = "https://" + base[len("http://") :]
    return base


def normalize_public_path(path):
    if path is None:
        return "/"
    normalized = str(path).strip()
    if not normalized:
        return "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized != "/":
        normalized = normalized.rstrip("/") or "/"
    return normalized


def build_external_url(path_or_url):
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return f"{get_public_base_url()}{normalize_public_path(path_or_url)}"


def render_app_template(template_name, **context):
    return current_app.jinja_env.get_template(template_name).render(**context)


def _build_admin_mt5_status(*, account, request_row=None):
    if getattr(account, "is_orphaned", False):
        return {
            "label": "Cleanup Pending",
            "chip_class": "default",
        }
    if getattr(account, "cleanup_marked_at", None):
        return {
            "label": "Cleanup Pending",
            "chip_class": "warning-chip",
        }
    if getattr(account, "is_archived", False):
        return {
            "label": "Archived",
            "chip_class": "default",
        }
    if getattr(account, "connection_status", None) == "failed":
        return {
            "label": "Connection Failed",
            "chip_class": "danger-chip",
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
        "title": "Trading Journal for Weekly Trade Review | MyFXJournal",
        "meta_description": (
            "A trading journal for importing trades, reviewing the week, and spotting repeat execution patterns without rebuilding your history by hand."
        ),
        "eyebrow": "Trading journal",
        "hero_title": "Review your trades without rebuilding the week.",
        "hero_body": (
            "Start free, import trade history, and turn closed trades into a short weekly review. Add read-only MT5 sync later when setup capacity is available."
        ),
        "chips": ("Import or sync trades", "Weekly AI-assisted review", "One next step"),
        "panel_kicker": "What you get",
        "intro_title": "Review from trade history",
        "intro_body": (
            "Import closed trades, keep each account separate, and read a weekly review that names what repeated — with one practical focus for next week."
        ),
        "fit_points": (
            "Import, manual entry, or optional read-only MT5 sync when setup capacity is available.",
            "Review repeat behavior, not just win rate and totals.",
            "Leave with one adjustment you can test next week.",
        ),
        "cards_heading": "What manual rebuild costs you",
        "cards": (
            {
                "title": "Weekend reconstruction",
                "body": "Stop rebuilding the week from memory, screenshots, and scattered notes.",
            },
            {
                "title": "Vanity stats only",
                "body": "Win rate alone will not show revenge re-entries or session drift.",
            },
            {
                "title": "No next step",
                "body": "Reviews that end without one practical focus rarely change behavior.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Start free",
                "body": "Create an account and import closed trades — no sync wait required.",
            },
            {
                "title": "Review by account",
                "body": "Keep personal, funded, and demo history in separate review contexts.",
            },
            {
                "title": "Read the weekly review",
                "body": "Spot what repeated and pick one next action for your process.",
            },
        ),
        "workflow_heading": "Import first. Review sooner.",
        "cta_heading": "Turn trade history into a review loop.",
        "cta_body": "Start free, import trades, and get to the first useful review instead of maintaining another spreadsheet.",
        "faq": (
            {
                "question": "Do I need to type every trade manually?",
                "answer": "No. You can import history files, add trades manually when needed, or use read-only MT5 sync during the premium workflow trial when setup capacity is available.",
            },
            {
                "question": "Is this just another trade tracker?",
                "answer": "It stores trades, but the main value is review: organizing closed trades into patterns you can reflect on each week.",
            },
            {
                "question": "Will it tell me what to trade next?",
                "answer": "No. MyFXJournal is for journaling and reflection, not trade signals, predictions, or financial advice.",
            },
        ),
    },
    "mt5-trading-journal": {
        "title": "MT5 Trading Journal - Import or Sync MetaTrader 5 Trades | MyFXJournal",
        "meta_description": (
            "An MT5 trading journal for importing MetaTrader 5 history now, adding read-only sync when available, and reviewing closed trades each week."
        ),
        "eyebrow": "MT5 trading journal",
        "hero_title": "Turn MT5 history into a weekly review.",
        "hero_body": (
            "Import MetaTrader 5 history today. Add read-only sync during the premium workflow trial when setup capacity is available — so future closed trades feed the same weekly review."
        ),
        "chips": ("MT5 import", "Optional read-only sync", "Weekly review"),
        "panel_kicker": "Import vs sync",
        "intro_title": "MT5 stores fills, not review",
        "intro_body": (
            "Your history is already there. The gap is turning those rows into a weekly review: sessions, post-loss behavior, and what deserves attention next week."
        ),
        "fit_points": (
            "Import an MT5 XLSX file immediately — no sync setup required.",
            "Optional sync uses investor or read-only credentials only.",
            "Keep demo, funded, and personal MT5 accounts in separate review contexts.",
        ),
        "cards_heading": "What the export loop costs",
        "cards": (
            {
                "title": "Export cycles",
                "body": "Stop exporting and cleaning MT5 files every weekend just to review the week.",
            },
            {
                "title": "Account mixing",
                "body": "Demo, funded, and personal history blur together without separate review contexts.",
            },
            {
                "title": "Statement-only review",
                "body": "Deal history shows fills — not re-entry timing, sizing shifts, or session context.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Upload MT5 history",
                "body": "Export closed trades from MT5 and import the XLSX.",
            },
            {
                "title": "Keep accounts clean",
                "body": "Route each MT5 account to its own journal context.",
            },
            {
                "title": "Add sync when useful",
                "body": "Optional read-only sync during trial when capacity is available.",
            },
        ),
        "workflow_heading": "Import now. Sync when it removes work.",
        "cta_heading": "Start reviewing MT5 history today.",
        "cta_body": "Import your MT5 trades, keep the account context clean, and add read-only sync later if it fits your workflow.",
        "faq": (
            {
                "question": "Does this need my MT5 trading password?",
                "answer": "No. Optional sync uses investor or read-only credentials. MyFXJournal is designed to read history, not place trades.",
            },
            {
                "question": "Do I need sync before I can use it?",
                "answer": "No. MT5 import works immediately. Sync is optional during the 14-day premium workflow trial when setup capacity is available.",
            },
            {
                "question": "What if I trade on multiple MT5 accounts?",
                "answer": "Keep each MT5 account in its own journal context so imports, optional sync, and weekly review do not blur together.",
            },
        ),
    },
    "free-mt5-sync": {
        "title": "MT5 Sync Trial - Read-Only MetaTrader 5 Sync | MyFXJournal",
        "meta_description": (
            "Try read-only MetaTrader 5 sync during the 14-day premium workflow trial when setup capacity is available, with import as the immediate fallback."
        ),
        "eyebrow": "MT5 sync trial",
        "hero_title": "Try MT5 sync without making it the first blocker.",
        "hero_body": (
            "MT5 sync is included during the 14-day premium workflow trial when setup capacity is available. Import works right away; sync is optional and read-only."
        ),
        "chips": ("Capacity-managed setup", "Read-only access", "Import fallback"),
        "panel_kicker": "What sync changes",
        "intro_title": "Sync feeds review, not dashboards",
        "intro_body": (
            "The point is fewer repeated exports — closed trades flowing into the same account-level weekly review you already get from import."
        ),
        "fit_points": (
            "Start with import if sync capacity is closed or you want to review immediately.",
            "Investor or read-only access only — no trading permissions.",
            "Synced trades feed the same weekly review as imported history.",
        ),
        "cards_heading": "What repeated exports cost",
        "cards": (
            {
                "title": "Capacity-managed setup",
                "body": "Setup opens as capacity allows while the product is small, so import remains the reliable first step.",
            },
            {
                "title": "One account, one review",
                "body": "Each MT5 account stays separate so the context doesn't blur.",
            },
            {
                "title": "Sync feeds activation",
                "body": "The useful moment is the review that comes from synced trades, not a passive dashboard connection.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Start with history",
                "body": "Import an MT5 file immediately, then use the in-product sync setup flow when capacity is available.",
            },
            {
                "title": "Connect read-only",
                "body": "Use investor credentials only. When setup capacity is available, setup starts after you submit details.",
            },
            {
                "title": "Read the review",
                "body": "Synced or imported closed trades feed the weekly review, including likely re-entry and behavior patterns.",
            },
        ),
        "workflow_heading": "Import first. Sync when available.",
        "cta_heading": "Start the MT5 review loop.",
        "cta_body": "Start free, import your MT5 history, then request read-only sync when setup capacity is available.",
        "faq": (
            {
                "question": "How does the trial work?",
                "answer": "Core journaling stays free. MT5 sync is included during the 14-day premium workflow trial when setup capacity is available.",
            },
            {
                "question": "Do I need to share trading access?",
                "answer": "No. The workflow is built around investor or read-only access.",
            },
            {
                "question": "How does the setup actually work?",
                "answer": "Setup is capacity-managed while the product is small. When capacity is available, setup starts after you submit your read-only credentials.",
            },
        ),
    },
    "forex-trading-journal": {
        "title": "Forex Trading Journal for Pair and Session Review | MyFXJournal",
        "meta_description": (
            "A forex trading journal for reviewing pairs, sessions, post-loss behavior, and weekly execution patterns from imported or synced trade history."
        ),
        "eyebrow": "Forex trading journal",
        "hero_title": "Review forex pairs, sessions, and repeat behavior.",
        "hero_body": (
            "Import forex trades, keep each account separate, and review how pairs, sessions, and post-loss decisions shaped the week."
        ),
        "chips": ("Pair review", "Session context", "Weekly AI-assisted review"),
        "panel_kicker": "Best fit",
        "intro_title": "Pair and session context",
        "intro_body": (
            "Forex review needs more than symbol rows — when you traded, what repeated after losses, and whether your process matched the session you were in."
        ),
        "fit_points": (
            "Review pairs and sessions from closed trades, not memory.",
            "Keep demo, personal, and funded accounts in separate contexts.",
            "Pick one session, pair, or behavior rule to test next week.",
        ),
        "cards_heading": "What generic logs hide",
        "cards": (
            {
                "title": "Pairs in context",
                "body": "See where your attention and risk clustered instead of treating every symbol as an isolated row.",
            },
            {
                "title": "Session-aware review",
                "body": "Spot when your trades drifted outside the session or time window where your process usually holds up.",
            },
            {
                "title": "Post-loss behavior",
                "body": "Review fast re-entries, sizing shifts, and repeat attempts after a stop before they blur into a normal week.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import forex history",
                "body": "Upload MT5 history, use manual entry, or add read-only sync later when setup capacity is available.",
            },
            {
                "title": "Scan pairs and sessions",
                "body": "Review closed trades by pair, session, timing, and account before reading the weekly summary.",
            },
            {
                "title": "Carry one rule",
                "body": "Use the weekly review to pick one pair, session, or behavior constraint to test next week.",
            },
        ),
        "workflow_heading": "Review the forex week you actually traded.",
        "cta_heading": "Start a forex review from real trade history.",
        "cta_body": "Start free, import forex trades, and use the first review to choose one practical adjustment.",
        "faq": (
            {
                "question": "Is MyFXJournal only for forex traders?",
                "answer": "No. It supports other instruments too, but this workflow fits forex traders who want pair, session, and execution review from their own history.",
            },
            {
                "question": "Does the product tell me what to trade next?",
                "answer": "No. It is for review and process clarity, not trade signals.",
            },
            {
                "question": "Who is this best for?",
                "answer": "Forex traders who want a lighter weekly review loop and care about execution behavior, not just a ledger.",
            },
        ),
    },
    "weekly-trading-review": {
        "title": "Weekly Trading Review From Trade History | MyFXJournal",
        "meta_description": (
            "Turn closed trade history into a weekly trading review with cited patterns, one process takeaway, and less manual weekend reconstruction."
        ),
        "eyebrow": "Weekly trading review",
        "hero_title": "Weekly review without the weekend drag.",
        "hero_body": (
            "Import or sync closed trades, then read a concise weekly review that names what repeated and one rule worth testing before the next trading week starts."
        ),
        "chips": ("Closed-trade review", "One weekly takeaway", "Cited patterns"),
        "panel_kicker": "First useful outcome",
        "intro_title": "One review, one rule",
        "intro_body": (
            "A useful weekly review answers four things: what happened, what repeated, which trades show it, and what one thing to test next week."
        ),
        "fit_points": (
            "Start from imported or synced closed trades — not screenshots.",
            "Spot revenge sequences, sizing habits, and session drift.",
            "Finish with one adjustment, not passive reading.",
        ),
        "cards_heading": "What weekend rebuilds cost",
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
                "title": "Bring in the week",
                "body": "Import closed trades or use optional sync when available so the review has account history to read.",
            },
            {
                "title": "Name the pattern",
                "body": "Highlight the behavior, sizing, or revenge-style sequence the review surfaced as worth carrying forward.",
            },
            {
                "title": "Choose one rule",
                "body": "End with a specific process rule or experiment that you can check against next week's trades.",
            },
        ),
        "workflow_heading": "A weekly review you can keep up with.",
        "cta_heading": "One review. One rule. Every week.",
        "cta_body": "Start free, import trade history, and turn the first review into a concrete next-week focus.",
        "faq": (
            {
                "question": "What should a weekly trading review produce?",
                "answer": "Usually one or two clear takeaways and one process rule you can actually use next week.",
            },
            {
                "question": "Why not just write the review manually?",
                "answer": "You can. MyFXJournal is for traders who want the same reflection loop with less assembling, tagging, and spreadsheet upkeep.",
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
            "If you study trading but do not improve, the issue may be a repeated execution pattern. Review closed trades to see what keeps showing up."
        ),
        "eyebrow": "Why am I not improving",
        "hero_title": "You may be repeating the same trade behavior.",
        "hero_body": (
            "If more videos and strategy notes are not changing your results, start from closed trades. Review what repeated so the next step is clearer."
        ),
        "chips": ("Pattern review", "Closed-trade evidence", "One next step"),
        "panel_kicker": "What changes after signup",
        "intro_title": "Execution, not just education",
        "intro_body": (
            "Study does not show whether you cut winners early, re-enter after stops, or trade outside your plan. Your journal should make those behaviors visible."
        ),
        "fit_points": (
            "Separate knowledge gaps from execution gaps using trade history.",
            "Review decisions after losses, wins, and missed trades.",
            "Test one behavior next — not your whole system at once.",
        ),
        "cards_heading": "What theory-only review misses",
        "cards": (
            {
                "title": "Separate study from execution",
                "body": "A trader can understand a setup and still break the process under pressure. The review starts with what actually happened.",
            },
            {
                "title": "Make the review repeatable",
                "body": "Import or sync trades so the feedback loop is easier to keep than a blank weekly document.",
            },
            {
                "title": "Change one variable",
                "body": "The weekly review is designed to leave you with one behavior to observe or adjust next week.",
            },
        ),
        "workflow_eyebrow": "How to find the pattern",
        "workflow_steps": (
            {
                "title": "Bring in your trades",
                "body": "Import history, add trades manually, or use optional MT5 sync when setup capacity is available.",
            },
            {
                "title": "Review the sequence",
                "body": "Look at what happened around losses, re-entries, session changes, and exit decisions.",
            },
            {
                "title": "Choose the next test",
                "body": "Use the weekly review to choose one process experiment instead of adding another broad theory.",
            },
        ),
        "workflow_heading": "Use history before adding more theory.",
        "cta_heading": "Find the feedback loop you can act on.",
        "cta_body": "Start free, import your trades, and use the weekly review to see what keeps repeating.",
        "faq": (
            {
                "question": "I've tried journaling before and stopped. Why would this be different?",
                "answer": "Because you can start from imports or optional sync instead of a blank page. The review is meant to reduce assembly work, not replace your judgment.",
            },
            {
                "question": "Will this tell me what to trade?",
                "answer": "No. It is for reviewing closed trade history and process behavior, not trade signals or financial advice.",
            },
            {
                "question": "Do I need to be an experienced trader?",
                "answer": "No. If you've been trading for a few months and feel like you should be further along, this is built for you.",
            },
        ),
    },
    "trade-replay-chart": {
        "title": "Trade Replay Chart (Coming Soon) | MyFXJournal",
        "meta_description": (
            "Trade replay charts are coming to MyFXJournal. Import trades today for weekly review, with chart replay in limited admin preview until rollout opens."
        ),
        "eyebrow": "Trade replay · Coming soon",
        "hero_title": "Trade replay is coming to the journal.",
        "hero_body": (
            "Replay is in limited admin preview. Import trades for weekly review now — when replay opens broadly, it will sit on top of that same history."
        ),
        "chips": ("Coming soon", "Admin preview today", "Uses journal history"),
        "panel_kicker": "What's coming",
        "intro_title": "Chart context for review",
        "intro_body": (
            "Replay will show entries and exits on price — so review feels closer to how you experienced the session, not just rows in a list."
        ),
        "fit_points": (
            "Use weekly AI review for themes today.",
            "Replay later for the picture of one trade or session.",
            "Same journal data — import, manual entry, or optional sync.",
        ),
        "cards_heading": "What flat trade lists miss",
        "cards": (
            {
                "title": "See the trade in context",
                "body": "Entries and exits on a chart timeline so you can relate fills to the move that was unfolding—planned for general access after preview.",
            },
            {
                "title": "Faster post-session recall",
                "body": "Skip mentally rebuilding the candle sequence from a flat list of prices and times once replay ships broadly.",
            },
            {
                "title": "Same data as the journal",
                "body": "Replay is planned to use the trades already in your account, whether they came from import, manual entry, or optional MT5 sync.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Get trades into the journal",
                "body": "Import MT5 or Tradovate files, add manual trades, or use read-only MT5 sync when setup capacity is available.",
            },
            {
                "title": "Open replay on a trade (after launch)",
                "body": "From your trade list or detail view, jump into the replay chart for that position once the feature leaves admin-only preview.",
            },
            {
                "title": "Review with the chart in view",
                "body": "Once replay is available for your account, use it to inspect the path from entry to exit and carry the takeaway into weekly review.",
            },
        ),
        "workflow_heading": "How it will work when replay opens up.",
        "cta_heading": "Use the journal now; replay comes later.",
        "cta_body": "Start free and import history for weekly review today, with replay clearly marked as coming soon.",
        "faq": (
            {
                "question": "Can I use trade replay today?",
                "answer": (
                    "Not yet for normal accounts. The chart replay is in admin-gated preview while we finish it. "
                    "We will open it more broadly when the rollout is ready—this page describes what is coming."
                ),
            },
            {
                "question": "Is trade replay the same as live charting software?",
                "answer": (
                    "No. It is for journaling and review: reconstructing your trade on price for context, not for placing new trades or live analysis."
                ),
            },
            {
                "question": "Will replay need MT5 sync?",
                "answer": (
                    "No. When it launches for your account, any trades already in your journal—imports or manual entries—can feed replay the same way other review features do."
                ),
            },
            {
                "question": "Does this give trade signals?",
                "answer": "No. MyFXJournal is for review and process clarity, not recommendations or signals.",
            },
        ),
    },
    "ai-weekly-trading-review": {
        "title": "AI Weekly Trading Review From Closed Trades | MyFXJournal",
        "meta_description": (
            "An AI weekly trading review that summarizes closed trades with cited evidence, one practical takeaway, and reflection-first coaching. No signals or prediction claims."
        ),
        "eyebrow": "AI weekly trading review",
        "hero_title": "AI-assisted review grounded in your closed trades.",
        "hero_body": (
            "Import or sync trades, then read a weekly AI-assisted review that cites the closed trades behind its takeaways. Reflection after the fact — not live decisions or financial advice."
        ),
        "chips": ("Cited trades", "Weekly summary", "Reflection-first"),
        "panel_kicker": "What you get",
        "intro_title": "Evidence before coaching",
        "intro_body": (
            "Useful AI review starts with actual fills — timing, sessions, exits, and re-entries. MyFXJournal keeps takeaways bounded to trades the journal can cite."
        ),
        "fit_points": (
            "Reads timing, sessions, exits, and re-entries — not just win rate.",
            "Takeaways include cited trade references you can inspect.",
            "Follow-up stays scoped to the review, not a generic chatbot.",
        ),
        "cards_heading": "What generic AI summaries miss",
        "cards": (
            {
                "title": "Evidence first, advice second",
                "body": "The review identifies the most significant pattern, anchors it to specific trades, and explains the implication before suggesting an adjustment.",
            },
            {
                "title": "One coaching insight per week",
                "body": "Not a list of everything that went wrong — one main diagnosis, with the supporting context you need to recognise it again.",
            },
            {
                "title": "Scoped follow-up when available",
                "body": "If your plan includes review follow-up, questions stay tied to the cited trades and current weekly review.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import or sync your trades",
                "body": "Upload an MT5 history file, use MT5 sync, or add trades manually. The review runs on your closed trades for the week.",
            },
            {
                "title": "Read your weekly review",
                "body": "When enough closed trades are available, the review summarizes one main insight, cited trades, and a suggested experiment for next week.",
            },
            {
                "title": "Ask the follow-up questions",
                "body": "Where available, ask about the review in context. The product is not a public trading chatbot or signal engine.",
            },
        ),
        "workflow_heading": "Review that works from your data, not your memory.",
        "cta_heading": "Try AI-assisted review from your own history.",
        "cta_body": "Start free, import trade history, and read the first weekly review when enough closed trades are available.",
        "faq": (
            {
                "question": "Is this just an AI summary of my stats?",
                "answer": "No. The review identifies a specific pattern — like re-entering after a loss, exiting winners early, or trading the wrong session — and tells you which trades show it and why it matters.",
            },
            {
                "question": "What does 'evidence-bounded' mean?",
                "answer": "The review will not claim a pattern exists unless it can cite at least one specific trade that shows it. It will not make up trends or generalise from too little data.",
            },
            {
                "question": "Can I ask questions about the review?",
                "answer": "Review follow-up may depend on your plan. Where available, it is scoped to the current week's review and cited trades, not generic trading advice.",
            },
            {
                "question": "How is this different from ChatGPT?",
                "answer": "ChatGPT does not have your trades. MyFXJournal's review reads your actual closed trade history — entries, exits, timing, session context, and behavioral signals — not a description you typed.",
            },
        ),
    },
    "revenge-trading-journal": {
        "title": "Revenge Trading Journal for Post-Loss Review | MyFXJournal",
        "meta_description": (
            "Review likely revenge-trading patterns from closed trade history: post-loss re-entries, sizing shifts, and weekly behavior with cited examples."
        ),
        "eyebrow": "Revenge trading journal",
        "hero_title": "Review the trades after the loss.",
        "hero_body": (
            "Import closed trades and review what happened after losses: fast re-entries, size changes, repeat attempts, and whether the sequence matched your plan. The goal is reflection, not shame or prediction."
        ),
        "chips": ("Post-loss sequences", "Likely pattern flags", "Weekly review"),
        "panel_kicker": "First useful outcome",
        "intro_title": "Revenge is a sequence",
        "intro_body": (
            "One trade rarely proves motive. A sequence can: loss, fast re-entry, same symbol, larger size. MyFXJournal surfaces those sequences for honest inspection."
        ),
        "fit_points": (
            "Spot likely post-loss re-entries without manual revenge tags.",
            "Verify sequences with cited trades.",
            "Carry one behavioral constraint into next week.",
        ),
        "cards_heading": "What manual tagging misses",
        "cards": (
            {
                "title": "Sequence review, not tag-based",
                "body": "You do not need to label a trade in the moment. The review looks for timing and sequence clues in closed trades.",
            },
            {
                "title": "Outcome is not validation",
                "body": "A post-loss re-entry can win and still reinforce a risky habit. The review keeps the behavior separate from the result.",
            },
            {
                "title": "One experiment per week",
                "body": "The review points toward a concrete constraint to test, such as a delay after losses or no same-symbol re-entry.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Connect your trades",
                "body": "Import via MT5 XLSX, Tradovate CSV, manual entry, or optional MT5 sync when setup capacity is available.",
            },
            {
                "title": "Get your review",
                "body": "When likely post-loss sequences appear in your data, the weekly review can surface them with the specific trades cited.",
            },
            {
                "title": "Test one constraint",
                "body": "The review ends with a specific experiment: a rule to test next week that directly targets the pattern it surfaced.",
            },
        ),
        "workflow_heading": "Catch it in the data, not in the regret.",
        "cta_heading": "Review your post-loss behavior.",
        "cta_body": "Start free, import trades, and use the weekly review to inspect what happens after losses.",
        "faq": (
            {
                "question": "Do I have to manually tag revenge trades?",
                "answer": "No. The review can flag likely post-loss re-entry sequences from timing and trade history, so you do not have to rely only on manual tags.",
            },
            {
                "question": "What counts as a revenge trade in the review?",
                "answer": "A fast re-entry on the same or related symbol after a loss, often with tighter spacing, similar size, or repeating direction. The review looks at the sequence — not just the tag.",
            },
            {
                "question": "What if the revenge trade actually won?",
                "answer": "A win does not automatically validate the process. The review helps you separate outcome from whether the sequence matched your plan.",
            },
            {
                "question": "Is this only for forex?",
                "answer": "No. The behavioral review works for any instrument you trade through MT5 — forex, indices, commodities, crypto.",
            },
        ),
    },
    "why-do-i-keep-losing-forex-trades": {
        "title": "Why Do I Keep Losing Forex Trades? Find the Real Pattern | MyFXJournal",
        "meta_description": (
            "If you keep losing forex trades despite knowing the strategy, review closed trade history for execution patterns: exits, sessions, sizing, and post-loss behavior."
        ),
        "eyebrow": "Why do I keep losing?",
        "hero_title": "The pattern may be in execution, not another strategy.",
        "hero_body": (
            "If you understand the setup but results keep slipping, review closed trades: exits, sessions, sizing shifts, and what you do after losses."
        ),
        "chips": ("Forex execution review", "Cited trade evidence", "One next rule"),
        "panel_kicker": "What the review shows",
        "intro_title": "Look past the entry",
        "intro_body": (
            "Losing weeks often involve quieter problems: cutting winners early, trading outside your best session, sizing up after losses, or repeating a failed idea."
        ),
        "fit_points": (
            "Reads exits, re-entries, sessions, and sizing — not just win rate.",
            "Each diagnosis ties to specific trades in your history.",
            "One experiment next week — not a full system overhaul.",
        ),
        "cards_heading": "What entry-only review misses",
        "cards": (
            {
                "title": "Exit too early, not entry too late",
                "body": "Some losing weeks are less about entries and more about cutting winners short or letting the same losing idea keep running.",
            },
            {
                "title": "Session and context mismatch",
                "body": "If your process works better in one session and drifts elsewhere, the review can make that visible by session and time of day.",
            },
            {
                "title": "Post-loss decision shift",
                "body": "Many traders change their behaviour after a loss — bigger size, faster re-entry, different setup. The review can surface the sequence.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Bring in your trade history",
                "body": "Import your MT5 file, add trades manually, or connect optional read-only MT5 sync when setup capacity is available.",
            },
            {
                "title": "Read this week's review",
                "body": "The weekly review highlights a pattern from recent closed trades with cited examples and appropriate sample-size caution.",
            },
            {
                "title": "Test one change",
                "body": "The review ends with one specific experiment for next week. Not a personality change — a testable rule you can apply to this week's trades.",
            },
        ),
        "workflow_heading": "Find the actual reason, not another theory.",
        "cta_heading": "Review the losing pattern from your own trades.",
        "cta_body": "Start free, import forex history, and use the weekly review to choose one process rule to test.",
        "faq": (
            {
                "question": "I know my strategy works — why am I still losing?",
                "answer": "Strategy knowledge and strategy execution are different things. The review reads execution patterns — timing, exit decisions, post-loss behaviour — not whether you know the rules.",
            },
            {
                "question": "Will the review tell me what to trade next?",
                "answer": "No. It helps you review patterns in your closed trade history. Trade decisions stay yours.",
            },
            {
                "question": "What if I only have a few weeks of history?",
                "answer": "The review runs on whatever is available. Smaller samples get appropriate hedging — the review won't claim a strong pattern from two trades.",
            },
            {
                "question": "Is this only for losing traders?",
                "answer": "No. The review can also help identify habits that worked in stronger weeks so you know what to protect.",
            },
        ),
    },
    "myfxjournal-vs-tradersync": {
        "title": "MyFXJournal vs TraderSync - Review-First vs Tracker-First | MyFXJournal",
        "meta_description": (
            "Compare MyFXJournal and TraderSync by workflow: review-first weekly reflection versus broader tracking, analytics, tags, and broker coverage."
        ),
        "eyebrow": "MyFXJournal vs TraderSync",
        "hero_title": "Choose by workflow, not feature count.",
        "hero_body": (
            "TraderSync is a broader trade tracking and analytics product. MyFXJournal is narrower: import or sync trades, read a weekly AI-assisted review, and turn that review into one next action."
        ),
        "chips": ("Workflow comparison", "Review-first", "Different use cases"),
        "panel_kicker": "Quick compare",
        "intro_title": "Tracker-first vs review-first",
        "intro_body": (
            "TraderSync fits detailed tracking, tags, and broad broker coverage. MyFXJournal fits a lower-friction weekly review loop with less dashboard upkeep."
        ),
        "fit_points": (
            "TraderSync: broad coverage, manual tags, deep analytics.",
            "MyFXJournal: weekly review and one practical takeaway.",
            "MT5 import immediately; optional read-only sync when capacity allows.",
        ),
        "cards_heading": "Where the workflows diverge",
        "cards": (
            {
                "title": "Analytics depth vs review focus",
                "body": "TraderSync offers broader tracking surfaces. MyFXJournal centers the weekly review: what repeated and what to test next.",
            },
            {
                "title": "Manual tagging vs pattern review",
                "body": "TraderSync can support custom tagging. MyFXJournal reduces tag work by looking for behavior patterns from the trade sequence.",
            },
            {
                "title": "First value",
                "body": "With MyFXJournal, activation starts when trades are imported and the first useful review is available, not after building a large tagging system.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import or sync your trades",
                "body": "Bring trades into MyFXJournal through MT5 import, Tradovate CSV, manual entry, or optional read-only MT5 sync when available.",
            },
            {
                "title": "Review your week",
                "body": "Use MyFXJournal for a concise weekly review with cited trades and one process takeaway.",
            },
            {
                "title": "Reflect and adjust",
                "body": "Where available, follow-up stays scoped to the weekly review and your trade history rather than becoming general trading advice.",
            },
        ),
        "workflow_heading": "Two different approaches to the same problem.",
        "cta_heading": "Try the review-first workflow.",
        "cta_body": "Start free, import your trades, and see whether a weekly review loop fits better than a tracker-heavy workflow.",
        "faq": (
            {
                "question": "Is this a fair comparison?",
                "answer": "We think so. TraderSync is a strong product with more broker integrations and a deeper analytics surface. We're focused on something narrower: the weekly review habit and the behavioral patterns underneath it.",
            },
            {
                "question": "Does MyFXJournal work with platforms other than MT5?",
                "answer": "Yes — MT5 XLSX imports, Tradovate CSV, and manual entry. MT5 sync is purpose-built. Other broker integrations are not a current focus.",
            },
            {
                "question": "What happens when MyFXJournal launches paid tiers?",
                "answer": "Core journaling stays free to start. Optional premium workflow access and current pricing are shown on the pricing page so the comparison does not depend on stale plan details.",
            },
        ),
    },
    "myfxjournal-vs-edgewonk": {
        "title": "MyFXJournal vs Edgewonk - Automatic Review vs Manual Discipline | MyFXJournal",
        "meta_description": (
            "Compare MyFXJournal and Edgewonk by journaling habit: automatic weekly review from trade history versus deeper manual process scoring."
        ),
        "eyebrow": "MyFXJournal vs Edgewonk",
        "hero_title": "Do you want a manual process system or a weekly review loop?",
        "hero_body": (
            "Edgewonk is built around deep manual journaling, scoring, and process discipline. MyFXJournal is web-based and review-first: import or sync trade history, then use the weekly review to decide what to work on next."
        ),
        "chips": ("Honest comparison", "Different workflows", "Free to try"),
        "panel_kicker": "Quick compare",
        "intro_title": "Manual discipline vs weekly review",
        "intro_body": (
            "Edgewonk rewards structured manual scoring and tags. MyFXJournal starts from trade history and makes the weekly review the habit."
        ),
        "fit_points": (
            "Edgewonk: deep manual scoring and your own categories.",
            "MyFXJournal: weekly review from imported or synced history.",
            "Check each product's pricing for current plan details.",
        ),
        "cards_heading": "Where the workflows diverge",
        "cards": (
            {
                "title": "Manual discipline vs automatic review",
                "body": "Edgewonk rewards traders who maintain tags and scoring. MyFXJournal reduces setup so the first value is the review itself.",
            },
            {
                "title": "Desktop vs web",
                "body": "Edgewonk is desktop software. MyFXJournal runs in the browser and supports imports plus optional read-only MT5 sync when setup capacity is available.",
            },
            {
                "title": "Coaching style",
                "body": "Edgewonk helps you score against your own process. MyFXJournal highlights likely patterns from trade history and asks you to test one adjustment.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import your trade history",
                "body": "MyFXJournal supports MT5 XLSX, Tradovate CSV, manual entry, and optional read-only MT5 sync during trial when capacity is available.",
            },
            {
                "title": "Review and reflect",
                "body": "Edgewonk shows your custom scoring dashboard. MyFXJournal shows a weekly review with one main insight, cited trades, and one suggested experiment.",
            },
            {
                "title": "Build the habit",
                "body": "Both products only help if you return. MyFXJournal keeps the repeat action simple: import or sync, review, pick one next rule.",
            },
        ),
        "workflow_heading": "Both are useful. The question is what habit you want to build.",
        "cta_heading": "Try the frictionless approach.",
        "cta_body": "Start free, import your trades, and see whether the review-first habit is easier to keep.",
        "faq": (
            {
                "question": "Is Edgewonk better for manual journaling?",
                "answer": "It can be a better fit if you want a deep manual scoring system and are willing to maintain the tagging work consistently.",
            },
            {
                "question": "Does MyFXJournal require manual tagging?",
                "answer": "No. Likely behavioral patterns such as revenge entries, exit habits, and session mismatches can be surfaced from the trade sequence without requiring manual tags first.",
            },
            {
                "question": "Can I use both?",
                "answer": "Yes. Some traders use Edgewonk for deep manual scoring and MyFXJournal for the automatic weekly review. They solve different parts of the problem.",
            },
        ),
    },
    "free-trading-journal": {
        "title": "Free Trading Journal — Import Trades, Review the Week | MyFXJournal",
        "meta_description": (
            "A free trading journal for traders comparing spreadsheets and paid tools. Import trades, review behavior by account, and get a weekly AI-assisted review without rebuilding the week by hand."
        ),
        "eyebrow": "Free trading journal",
        "hero_title": "A free journal when spreadsheets stop scaling.",
        "hero_body": (
            "Start free — no card required. Import trade history, review closed trades by account, and read a weekly AI-assisted summary when enough history is in place."
        ),
        "chips": ("Start free", "Import first", "Weekly AI-assisted review"),
        "panel_kicker": "What you get",
        "intro_title": "What you get for free",
        "intro_body": (
            "Import closed trades, keep each account separate, and get a weekly review that names what repeated — without rebuilding the week in a spreadsheet."
        ),
        "fit_points": (
            "Import MT5 XLSX, Tradovate CSV, or manual entries.",
            "Account-centered review so weeks do not blur across ledgers.",
            "Weekly AI-assisted summary with one focus for next week.",
        ),
        "cards_heading": "What spreadsheets cost you",
        "cards": (
            {
                "title": "Spreadsheet upkeep",
                "body": "Skip formulas, tabs, and weekend copy-paste. Import once and review from the journal.",
            },
            {
                "title": "Scattered notes",
                "body": "Stop jumping between broker exports, screenshots, and a notes doc that never gets finished.",
            },
            {
                "title": "No review routine",
                "body": "Turn closed trades into a weekly summary you can actually repeat — not another empty Sunday doc.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Start free and import",
                "body": "Create an account and upload MT5 or Tradovate history, or add trades manually.",
            },
            {
                "title": "Review by account",
                "body": "See the week's closed trades organized for review — timing, exits, and patterns worth naming.",
            },
            {
                "title": "Read the weekly review",
                "body": "When enough history is in place, get a cited summary and one practical takeaway.",
            },
        ),
        "workflow_heading": "Import first. Review every week.",
        "cta_heading": "Start with a free journal that does the rebuild for you.",
        "cta_body": "Import your trades today and see whether a lighter review loop beats maintaining another spreadsheet tab.",
        "faq": (
            {
                "question": "Is MyFXJournal actually free?",
                "answer": "Core journaling is free to start. Premium workflow features, including MT5 sync during the trial window, are optional — import and manual entry work without them.",
            },
            {
                "question": "How is this different from a Google Sheets template?",
                "answer": "Templates store rows. MyFXJournal organizes review, helps spot behavioral patterns from trade sequences, and produces a weekly summary so you spend less time assembling the week.",
            },
            {
                "question": "Which free journal page should I start with?",
                "answer": "Use this page for the broad workflow. MT5, AI review, forex, spreadsheet-template, and prop-firm searches each have a narrower page with more specific expectations.",
            },
            {
                "question": "Does this give trade signals or guaranteed improvement?",
                "answer": "No. MyFXJournal is for journaling and review. It does not recommend entries, exits, or promise profitability.",
            },
        ),
    },
    "free-mt5-trading-journal": {
        "title": "Free MT5 Trading Journal — Import or Sync MetaTrader 5 History | MyFXJournal",
        "meta_description": (
            "A free MT5 trading journal for importing MetaTrader 5 history first, optionally syncing later, keeping accounts separate, and reviewing weekly behavior without weekend export routines."
        ),
        "eyebrow": "Free MT5 trading journal",
        "hero_title": "Your MT5 history, ready to review — without the export routine.",
        "hero_body": (
            "Import an MT5 history file today — no sync setup required. Add read-only sync later during the premium workflow trial when setup capacity is available."
        ),
        "chips": ("MT5 import first", "Optional sync", "Account routing"),
        "panel_kicker": "What you get",
        "intro_title": "From export to review",
        "intro_body": (
            "MT5 records every fill. The friction is export files, column cleanup, and separate tabs per account — import first, sync when you want to stop repeating exports."
        ),
        "fit_points": (
            "Import MT5 XLSX immediately — no sync wait.",
            "Route each MT5 account separately.",
            "Optional read-only sync feeds the same weekly review.",
        ),
        "cards_heading": "What the export routine costs",
        "cards": (
            {
                "title": "Manual export cycles",
                "body": "Stop exporting, cleaning columns, and re-uploading every weekend just to review the week.",
            },
            {
                "title": "Mixed account history",
                "body": "Challenge, personal, and demo trades stay separate so review does not blur ledgers.",
            },
            {
                "title": "Delayed review",
                "body": "Raw deal history shows prices and times — not re-entry timing, sizing shifts, or session context.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import your MT5 file",
                "body": "Export history from MT5 and upload the XLSX. Your closed trades land in the journal ready for review.",
            },
            {
                "title": "Keep accounts separate",
                "body": "Assign imports to the right account so funded, personal, and demo reviews stay clean.",
            },
            {
                "title": "Add sync when ready",
                "body": "Connect read-only MT5 access during the premium workflow trial when setup capacity is available — then read the weekly AI-assisted review.",
            },
        ),
        "workflow_heading": "Import today. Sync when it saves time.",
        "cta_heading": "Start reviewing MT5 history without another export cycle.",
        "cta_body": "Import your MT5 file today. Add optional sync later if you want history to update without another export.",
        "faq": (
            {
                "question": "Do I need MT5 sync to use the journal?",
                "answer": "No. Import works immediately. Sync is optional during the premium workflow trial when setup capacity is available.",
            },
            {
                "question": "Does sync need my trading password?",
                "answer": "No. Setup uses investor or read-only credentials. MyFXJournal can read history — not place trades.",
            },
            {
                "question": "Can I run multiple MT5 accounts?",
                "answer": "Yes. Each account keeps its own history and review context so prop, demo, and personal ledgers do not blur together.",
            },
            {
                "question": "Is this affiliated with MetaQuotes or my broker?",
                "answer": "No. MyFXJournal is an independent journaling tool that reads MT5 history you import or authorize for read-only sync.",
            },
        ),
    },
    "free-ai-trading-journal": {
        "title": "Free AI Trading Journal — Weekly AI-Assisted Review From Trade History | MyFXJournal",
        "meta_description": (
            "A free AI trading journal that turns closed trades into a weekly AI-assisted review with cited evidence — for journaling and reflection, not trade signals or profit promises."
        ),
        "eyebrow": "Free AI trading journal",
        "hero_title": "Weekly AI-assisted review without writing the entry yourself.",
        "hero_body": (
            "Import closed trades first. When enough history is in place, an AI-assisted review reads re-entries, session mix, and exit habits — with cited trades."
        ),
        "chips": ("Weekly AI-assisted review", "Cited trades", "Reflection-first"),
        "panel_kicker": "What you get",
        "intro_title": "Your fills, not generic chat",
        "intro_body": (
            "Useful AI review starts from closed trade data — timing, sizing, session, and post-loss sequences — with patterns tied to specific trades."
        ),
        "fit_points": (
            "One main pattern per week with trade citations.",
            "Core free workflow: import, organize, read the weekly summary.",
            "Reflection on what happened — not signals or entries.",
        ),
        "cards_heading": "What blank-page journaling costs",
        "cards": (
            {
                "title": "Blank-page journaling",
                "body": "Skip rebuilding a long manual write-up every Sunday.",
            },
            {
                "title": "Vague self-reflection",
                "body": "Diagnoses reference actual closed trades — not generic coaching clichés.",
            },
            {
                "title": "Generic summaries",
                "body": "Structured weekly review grounded in your journal — not open-ended trading chat.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import closed trades",
                "body": "Upload MT5 or Tradovate history, or add trades manually.",
            },
            {
                "title": "Review by account/week",
                "body": "Trade normally — the review runs on closed trades for the period.",
            },
            {
                "title": "Read the AI-assisted review",
                "body": "Get a cited summary and one practical takeaway.",
            },
        ),
        "workflow_heading": "Your data in. A readable review out.",
        "cta_heading": "Try AI-assisted review that starts from your trades.",
        "cta_body": "Start free, import history, and read your first weekly AI-assisted review when enough closed trades are in place.",
        "faq": (
            {
                "question": "Will the AI tell me what to trade?",
                "answer": "No. It summarizes closed trades for review and process clarity. Trade decisions stay yours.",
            },
            {
                "question": "Is follow-up chat always available?",
                "answer": "The weekly AI-assisted review is the core workflow. Follow-up questions about a review may depend on your plan tier — check pricing for current access.",
            },
            {
                "question": "How is this different from ChatGPT?",
                "answer": "ChatGPT does not have your trade history. MyFXJournal's review reads your imported or synced closed trades and cites specific fills.",
            },
            {
                "question": "Does AI review guarantee better results?",
                "answer": "No. It helps you reflect on patterns in your history. Outcomes depend on what you do with that reflection.",
            },
        ),
    },
    "free-forex-trading-journal": {
        "title": "Free Forex Trading Journal — Review Pairs, Sessions, and Habits | MyFXJournal",
        "meta_description": (
            "A free forex trading journal for reviewing pair concentration, session timing, and repeat execution habits from imported trade history — with a weekly AI-assisted summary, not trade signals."
        ),
        "eyebrow": "Free forex trading journal",
        "hero_title": "Review the pairs and sessions you actually traded.",
        "hero_body": (
            "Start free and import forex history from MT5 or add trades manually. Review pair concentration, session mix, and repeat behaviors — then read a weekly AI-assisted summary."
        ),
        "chips": ("Pairs and sessions", "Repeat behavior", "Weekly AI-assisted review"),
        "panel_kicker": "Best fit",
        "intro_title": "Session and pair context",
        "intro_body": (
            "Currency-agnostic spreadsheets hide session drift — London setups in New York chop, repeating the same pair after a stop, sizing up into the close."
        ),
        "fit_points": (
            "Separate demo, funded, and personal forex accounts.",
            "See pair concentration and session distribution from closed trades.",
            "Weekly review names repeat behaviors with cited examples.",
        ),
        "cards_heading": "What generic forex logs hide",
        "cards": (
            {
                "title": "Pair and session drift",
                "body": "See where you concentrated risk and when you traded outside your usual session window.",
            },
            {
                "title": "Repeating mistakes",
                "body": "Spot impulsive re-entries and early exits before they blur into a normal week.",
            },
            {
                "title": "Inconsistent review",
                "body": "A journal you review every week beats a perfect pair log you abandon after two sessions.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import forex history",
                "body": "Upload MT5 exports or add closed trades manually. Start from real fills, not a blank pair list.",
            },
            {
                "title": "Scan pairs and sessions",
                "body": "Review closed trades with pair and session context — where repeat behaviors clustered and where execution held.",
            },
            {
                "title": "Carry one rule forward",
                "body": "Read the weekly AI-assisted review for a cited summary and one session or pair rule to test — not a strategy overhaul.",
            },
        ),
        "workflow_heading": "Review the week you actually traded.",
        "cta_heading": "Keep forex review light enough to repeat.",
        "cta_body": "Start free, import your forex history, and build a weekly review habit focused on pairs and sessions.",
        "faq": (
            {
                "question": "Is this only for MT5 forex traders?",
                "answer": "MT5 import is a common path, but manual entry and other import formats work too. The review focuses on forex execution behavior, not one platform.",
            },
            {
                "question": "Does it track which session I traded?",
                "answer": "The review uses timing and session context from your closed trades to help spot when you traded outside your usual window.",
            },
            {
                "question": "Does it recommend pairs or sessions to trade?",
                "answer": "No. It helps you review what you already traded. It does not provide signals or market calls.",
            },
            {
                "question": "How does this relate to the general free trading journal?",
                "answer": "This page is forex-specific. The broader free journal page covers the general workflow across imports, accounts, and weekly review.",
            },
        ),
    },
    "free-trading-journal-template": {
        "title": "Free Trading Journal Template Alternative — Software, Not Another Sheet | MyFXJournal",
        "meta_description": (
            "Looking for a free trading journal template or spreadsheet? MyFXJournal is free journal software — import trades, skip manual row upkeep, and review the week without downloading another XLSX."
        ),
        "eyebrow": "Template alternative",
        "hero_title": "Stop maintaining the template. Start reviewing the week.",
        "hero_body": (
            "Templates mean another spreadsheet to format, formula, and refill every week. MyFXJournal is free journal software: import trades and read a weekly AI-assisted review."
        ),
        "chips": ("No spreadsheet upkeep", "Import trades", "Software alternative"),
        "panel_kicker": "What this replaces",
        "intro_title": "Replace the template loop",
        "intro_body": (
            "Broken formulas, copy-paste from the broker, and a review doc that never gets finished — structure without rebuilding the same file every Sunday."
        ),
        "fit_points": (
            "Import broker history instead of retyping template rows.",
            "Separate accounts without duplicating tabs.",
            "Weekly AI-assisted summary instead of an empty notes column.",
        ),
        "cards_heading": "What template maintenance costs",
        "cards": (
            {
                "title": "Formula upkeep",
                "body": "Skip broken VLOOKUPs, manual tags, and Sunday row cleanup.",
            },
            {
                "title": "Tab sprawl",
                "body": "Stop duplicating sheets per account and versioning files every month.",
            },
            {
                "title": "Weekend copy-paste",
                "body": "Import closed trades once and review from one place — not broker, chart, and spreadsheet tabs.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Skip the blank template",
                "body": "Create a free account and import history from MT5, Tradovate, or manual entry — start from closed trades.",
            },
            {
                "title": "Review in one place",
                "body": "See the week organized for reflection instead of jumping between broker, chart, and spreadsheet tabs.",
            },
            {
                "title": "Replace the Sunday refill",
                "body": "Read a weekly AI-assisted review that names what repeated and one thing to test — instead of copying another week of rows.",
            },
        ),
        "workflow_heading": "Replace the template loop with a review loop.",
        "cta_heading": "Try the software path instead of another download.",
        "cta_body": "Start free and import your trades. See if it beats maintaining another journal spreadsheet.",
        "faq": (
            {
                "question": "Do you offer a downloadable spreadsheet template?",
                "answer": "No. MyFXJournal is journal software. You import trade history into the product instead of filling a template file.",
            },
            {
                "question": "Can I export back to Excel?",
                "answer": "The focus is review inside the journal. If you need a spreadsheet archive, keep your broker export separately and import a copy for review.",
            },
            {
                "question": "Is this really free?",
                "answer": "Core journaling is free to start. Optional premium workflow features, including MT5 sync during trial when setup capacity is available, are separate — import works without them.",
            },
            {
                "question": "How does this relate to the general free trading journal?",
                "answer": "This page is for spreadsheet and template searchers. The broader free journal page covers the general workflow across imports, accounts, and weekly review.",
            },
        ),
    },
    "free-prop-firm-trading-journal": {
        "title": "Free Prop Firm Trading Journal — Review Rules and Discipline | MyFXJournal",
        "meta_description": (
            "A free trading journal for funded and prop-style accounts — review rule discipline, post-loss behavior, and weekly execution patterns from imported history. Not affiliated with any prop firm."
        ),
        "eyebrow": "Free prop firm trading journal",
        "hero_title": "Review funded-account discipline without mixing ledgers.",
        "hero_body": (
            "Start free and import challenge or funded account history separately. Review post-loss behavior, rule-adjacent slips, and weekly execution patterns. Independent software — not affiliated with any prop firm."
        ),
        "chips": ("Separate accounts", "Rule discipline", "Post-loss review"),
        "panel_kicker": "Best fit",
        "intro_title": "Separate accounts, honest review",
        "intro_body": (
            "You may know the rules cold and still break them under pressure — revenge after a daily loss, oversizing into the close, trading outside the allowed session."
        ),
        "fit_points": (
            "Keep challenge, funded, and personal accounts separate.",
            "Surface post-loss re-entries and sizing shifts from history.",
            "One discipline focus for the next evaluation week.",
        ),
        "cards_heading": "What mixed ledgers hide",
        "cards": (
            {
                "title": "Rule drift",
                "body": "Mixing prop and personal history hides the behavior that matters for the evaluation account.",
            },
            {
                "title": "Post-loss behavior",
                "body": "Spot re-entry timing and size changes after losses — where discipline usually slips first.",
            },
            {
                "title": "Challenge review gaps",
                "body": "See whether rule-adjacent slips cluster on certain sessions or pairs.",
            },
        ),
        "workflow_steps": (
            {
                "title": "Import the evaluation account",
                "body": "Upload MT5 history now. Optional read-only sync is available during the premium workflow trial when setup capacity is available.",
            },
            {
                "title": "Review the week's discipline",
                "body": "Scan closed trades for rule-adjacent behavior — oversizing, off-session entries, rapid re-entries after stops.",
            },
            {
                "title": "Set one rule for next week",
                "body": "Read the weekly AI-assisted review for a cited summary and one concrete discipline experiment to test.",
            },
        ),
        "workflow_heading": "Separate accounts. Honest weekly review.",
        "cta_heading": "Journal the evaluation account on its own.",
        "cta_body": "Start free, import your funded account history separately, and review discipline patterns from closed trades.",
        "faq": (
            {
                "question": "Is MyFXJournal affiliated with a prop firm?",
                "answer": "No. It is independent journaling software. You import or sync your own account history for personal review.",
            },
            {
                "question": "Will this help me pass a challenge?",
                "answer": "No guarantees. It helps you see execution and discipline patterns in your history so you can reflect and adjust — outcomes depend on your trading.",
            },
            {
                "question": "Can I track multiple evaluations?",
                "answer": "Yes. Use separate accounts so each evaluation or funded ledger keeps its own history and review.",
            },
            {
                "question": "Does it enforce prop firm rules automatically?",
                "answer": "No. It reviews closed trades and behavioral patterns. You define what rules matter and interpret the review for your program.",
            },
        ),
    },
}

# Paths included in `sitemap.xml` (must match canonical URLs on those pages — no trailing slash except `/`).
SEO_SITEMAP_PATHS = (
    "/",
    "/pricing",
    "/dashboard",
    "/login",
    "/register",
    "/contact",
    "/privacy",
    "/terms",
    "/faq/mt5-server",
) + tuple(f"/{slug}" for slug in SEO_PAGE_DEFINITIONS)


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


def _resolve_email_reply_to():
    """Return the address user replies should go to (even when From is noreply)."""
    return (
        os.getenv("EMAIL_REPLY_TO", "").strip()
        or os.getenv("FEEDBACK_TO_EMAIL", "").strip()
        or "support@myfxjournal.com"
    )


ADMIN_EMAIL_NAME_PLACEHOLDER = "{{name}}"


def _resolve_admin_broadcast_from_header():
    """From address for admin broadcast emails (distinct from transactional noreply)."""
    raw = os.getenv("ADMIN_EMAIL_FROM", "admin@myfxjournal.com").strip()
    if "<" in raw and ">" in raw:
        return raw
    name = os.getenv("EMAIL_FROM_NAME", "MyFXJournal").strip()
    if name:
        return f"{name} <{raw}>"
    return raw


def apply_admin_email_placeholders(content, *, recipient_name):
    if not content:
        return content
    return content.replace(ADMIN_EMAIL_NAME_PLACEHOLDER, recipient_name)


def html_to_plain_email_text(html_body):
    if not html_body:
        return ""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_body, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_ADMIN_EMAIL_HTML_TAG_RE = re.compile(
    r"<\s*(/?)\s*(img|a|p|div|span|br|strong|em|ul|ol|li|h[1-6])\b([^>]*)>",
    re.I,
)
_ADMIN_EMAIL_ATTR_RE = re.compile(
    r'([a-zA-Z_:][\w:.-]*)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))',
    re.I,
)
_ADMIN_EMAIL_ALLOWED_IMG_ATTRS = frozenset({"src", "alt", "height", "width", "style"})
_ADMIN_EMAIL_ALLOWED_LINK_ATTRS = frozenset({"href", "style"})


def _admin_email_attr_is_safe(name, value):
    lowered = name.lower()
    if lowered.startswith("on"):
        return False
    if "javascript:" in value.lower():
        return False
    if lowered in {"src", "href"}:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https", "mailto"}
    return True


def _sanitize_admin_email_tag(tag_name, attrs_text, *, self_closing=False):
    allowed_attrs = (
        _ADMIN_EMAIL_ALLOWED_IMG_ATTRS
        if tag_name.lower() == "img"
        else _ADMIN_EMAIL_ALLOWED_LINK_ATTRS
        if tag_name.lower() == "a"
        else frozenset()
    )
    safe_attrs = []
    for match in _ADMIN_EMAIL_ATTR_RE.finditer(attrs_text or ""):
        attr_name = match.group(1)
        attr_value = match.group(3) or match.group(4) or match.group(5) or ""
        if attr_name.lower() not in allowed_attrs:
            continue
        if not _admin_email_attr_is_safe(attr_name, attr_value):
            continue
        safe_attrs.append(f'{attr_name}="{attr_value}"')
    attr_suffix = f" {' '.join(safe_attrs)}" if safe_attrs else ""
    if tag_name.lower() == "br" or self_closing:
        return f"<{tag_name}{attr_suffix} />"
    return f"<{tag_name}{attr_suffix}>"


def sanitize_admin_broadcast_html(html_body):
    if not html_body:
        return ""

    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html_body, flags=re.I | re.S)
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.S)

    def _replace_tag(match):
        closing = match.group(1)
        tag_name = match.group(2).lower()
        attrs_text = match.group(3) or ""
        if closing:
            return f"</{tag_name}>"
        self_closing = tag_name == "br" or attrs_text.rstrip().endswith("/")
        return _sanitize_admin_email_tag(tag_name, attrs_text, self_closing=self_closing)

    sanitized = _ADMIN_EMAIL_HTML_TAG_RE.sub(_replace_tag, cleaned)
    sanitized = re.sub(
        r"<\s*(?!/?\s*(?:img|a|p|div|span|br|strong|em|ul|ol|li|h[1-6])\b)[^>]+>",
        "",
        sanitized,
        flags=re.I,
    )
    return sanitized.strip()


def admin_broadcast_message_contains_html(message):
    if not message:
        return False
    return bool(_ADMIN_EMAIL_HTML_TAG_RE.search(message))


ADMIN_BROADCAST_SIGNATURES_KEY = "admin_broadcast_signatures"
MAX_ADMIN_BROADCAST_SIGNATURES = 24
MAX_ADMIN_BROADCAST_SIGNATURE_NAME_LEN = 80
MAX_ADMIN_BROADCAST_SIGNATURE_BODY_LEN = 8192


def _normalize_admin_broadcast_signature_item(item):
    if not isinstance(item, dict):
        return None
    signature_id = str(item.get("id") or "").strip()
    name = str(item.get("name") or "").strip()
    body = str(item.get("body") or "").strip()
    updated_at = str(item.get("updated_at") or "").strip()
    if not signature_id or not name or not body:
        return None
    if len(name) > MAX_ADMIN_BROADCAST_SIGNATURE_NAME_LEN:
        name = name[:MAX_ADMIN_BROADCAST_SIGNATURE_NAME_LEN].rstrip()
    if len(body) > MAX_ADMIN_BROADCAST_SIGNATURE_BODY_LEN:
        body = body[:MAX_ADMIN_BROADCAST_SIGNATURE_BODY_LEN].rstrip()
    return {
        "id": signature_id,
        "name": name,
        "body": body,
        "updated_at": updated_at,
    }


def list_admin_broadcast_signatures():
    from helpers.app_settings import get_app_setting_value

    raw = get_app_setting_value(ADMIN_BROADCAST_SIGNATURES_KEY, "[]")
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        loaded = []
    if not isinstance(loaded, list):
        return []
    signatures = []
    for item in loaded:
        normalized = _normalize_admin_broadcast_signature_item(item)
        if normalized is not None:
            signatures.append(normalized)
    signatures.sort(key=lambda row: row["name"].casefold())
    return signatures


def _persist_admin_broadcast_signatures(signatures, *, updated_by_user_id=None):
    from models import AppSetting

    payload = json.dumps(signatures, sort_keys=True)
    setting = db.session.get(AppSetting, ADMIN_BROADCAST_SIGNATURES_KEY)
    if setting is None:
        setting = AppSetting(key=ADMIN_BROADCAST_SIGNATURES_KEY, value=payload)
        db.session.add(setting)
    else:
        setting.value = payload
    setting.updated_at = utcnow_naive()
    setting.updated_by_user_id = updated_by_user_id
    db.session.commit()
    return signatures


def save_admin_broadcast_signature(*, signature_id=None, name, body, updated_by_user_id=None):
    cleaned_name = str(name or "").strip()
    cleaned_body = str(body or "").strip()
    if not cleaned_name:
        raise ValueError("missing_name")
    if not cleaned_body:
        raise ValueError("missing_body")
    if len(cleaned_name) > MAX_ADMIN_BROADCAST_SIGNATURE_NAME_LEN:
        raise ValueError("name_too_long")
    if len(cleaned_body) > MAX_ADMIN_BROADCAST_SIGNATURE_BODY_LEN:
        raise ValueError("body_too_long")

    signatures = list_admin_broadcast_signatures()
    normalized_id = str(signature_id or "").strip()
    timestamp = utcnow_naive().replace(microsecond=0).isoformat() + "Z"
    saved = None

    if normalized_id:
        for index, item in enumerate(signatures):
            if item["id"] != normalized_id:
                continue
            signatures[index] = {
                "id": normalized_id,
                "name": cleaned_name,
                "body": cleaned_body,
                "updated_at": timestamp,
            }
            saved = signatures[index]
            break
        if saved is None:
            raise ValueError("not_found")
    else:
        if len(signatures) >= MAX_ADMIN_BROADCAST_SIGNATURES:
            raise ValueError("limit_reached")
        saved = {
            "id": secrets.token_hex(8),
            "name": cleaned_name,
            "body": cleaned_body,
            "updated_at": timestamp,
        }
        signatures.append(saved)

    _persist_admin_broadcast_signatures(signatures, updated_by_user_id=updated_by_user_id)
    return saved


def delete_admin_broadcast_signature(*, signature_id, updated_by_user_id=None):
    normalized_id = str(signature_id or "").strip()
    if not normalized_id:
        raise ValueError("missing_id")
    signatures = list_admin_broadcast_signatures()
    next_signatures = [item for item in signatures if item["id"] != normalized_id]
    if len(next_signatures) == len(signatures):
        raise ValueError("not_found")
    _persist_admin_broadcast_signatures(next_signatures, updated_by_user_id=updated_by_user_id)
    return True


def send_email_placeholder(to_email, subject, text_body, html_body=None, *, from_header=None):
    provider = os.getenv("EMAIL_PROVIDER", "placeholder").strip().lower()
    sender = from_header or _resolve_email_from_header()
    reply_to = _resolve_email_reply_to()
    send_enabled = os.getenv("EMAIL_SEND_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    api_key = os.getenv("RESEND_API_KEY", "").strip() or os.getenv("EMAIL_API_KEY", "").strip()
    log_email_bodies = _should_log_email_bodies()

    if provider in {"console", "placeholder"} or not send_enabled:
        current_app.logger.info(
            "Email placeholder (console/disabled) -> to=%s from=%s reply_to=%s subject=%s",
            to_email,
            sender,
            reply_to,
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
                "reply_to": reply_to,
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
                "Core journaling is free to start, and premium workflow features "
                "include a 14-day trial with no credit card required. "
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
                "Create a free MyFXJournal account. Import trade history, review by account, "
                "and try premium workflow features for 14 days without spreadsheet overhead."
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
            "send_email": "admin_send_email",
        }
        endpoint = endpoint_map.get(section, "admin_signup_users")
        if message:
            flash(message, status)
        return redirect(url_for(endpoint))

    def _admin_selectable_vm_ids():
        linked_vm_ids = [
            row[0]
            for row in MT5Account.query.with_entities(MT5Account.vm_id)
            .filter(MT5Account.vm_id.isnot(None))
            .distinct()
            .all()
            if row[0]
        ]
        monitor_snapshot = load_admin_mt5_monitor_snapshot(mt5_accounts=())
        return collect_admin_selectable_vm_ids(
            vm_ids=linked_vm_ids,
            worker_states_by_vm=monitor_snapshot.get("worker_states_by_vm") or {},
        )

    def _parse_admin_target_vm_id():
        vm_id, error = resolve_admin_target_vm_id(
            request.form.get("target_vm_id"),
            selectable_vm_ids=_admin_selectable_vm_ids(),
        )
        return vm_id, error

    def _parse_admin_vm_id_field(field_name):
        vm_id, error = resolve_admin_target_vm_id(
            request.form.get(field_name),
            selectable_vm_ids=_admin_selectable_vm_ids(),
        )
        return vm_id, error

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
            "waitlist_people": db.session.query(
                func.count(func.distinct(UpgradeWaitlistEntry.email))
            ).scalar()
            or 0,
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
            "mt5_auto_bar_sync_public_users": get_bool_app_setting(
                MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY,
                False,
            ),
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
        summary = payload.get("week_summary") if isinstance(payload.get("week_summary"), dict) else {}
        if not summary and isinstance(payload.get("summary"), dict):
            summary = payload.get("summary")
        emotional_context = (
            payload.get("emotional_context")
            if isinstance(payload.get("emotional_context"), dict)
            else {}
        )
        if not emotional_context and isinstance(payload.get("emotional_index"), dict):
            emotional_context = payload.get("emotional_index")
        historical_context = (
            payload.get("historical_context_summary")
            if isinstance(payload.get("historical_context_summary"), dict)
            else {}
        )
        if not historical_context and isinstance(payload.get("historical_context"), dict):
            historical_context = payload.get("historical_context")
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
            "evidence_boundary": payload.get("evidence_boundary"),
            "sample_context": payload.get("sample_context")
            or (
                {
                    key: payload["evidence_boundary"][key]
                    for key in ("claim_scope", "review_mode")
                    if isinstance(payload.get("evidence_boundary"), dict)
                    and payload["evidence_boundary"].get(key) is not None
                }
                or None
            ),
            "account_age_days": payload.get("account_age_days"),
            "summary": summary,
            "week_summary": summary,
            "emotional_context": emotional_context,
            "emotional_index": emotional_context,
            "historical_context": historical_context,
            "historical_context_summary": historical_context,
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

    @app.route("/ai-weekly-trading-review")
    def ai_weekly_trading_review_page():
        return _render_public_seo_page("ai-weekly-trading-review")

    @app.route("/revenge-trading-journal")
    def revenge_trading_journal_page():
        return _render_public_seo_page("revenge-trading-journal")

    @app.route("/why-do-i-keep-losing-forex-trades")
    def why_keep_losing_page():
        return _render_public_seo_page("why-do-i-keep-losing-forex-trades")

    @app.route("/myfxjournal-vs-tradersync")
    def vs_tradersync_page():
        return _render_public_seo_page("myfxjournal-vs-tradersync")

    @app.route("/myfxjournal-vs-edgewonk")
    def vs_edgewonk_page():
        return _render_public_seo_page("myfxjournal-vs-edgewonk")

    @app.route("/free-trading-journal")
    def free_trading_journal_page():
        return _render_public_seo_page("free-trading-journal")

    @app.route("/free-mt5-trading-journal")
    def free_mt5_trading_journal_page():
        return _render_public_seo_page("free-mt5-trading-journal")

    @app.route("/free-ai-trading-journal")
    def free_ai_trading_journal_page():
        return _render_public_seo_page("free-ai-trading-journal")

    @app.route("/free-forex-trading-journal")
    def free_forex_trading_journal_page():
        return _render_public_seo_page("free-forex-trading-journal")

    @app.route("/free-trading-journal-template")
    def free_trading_journal_template_page():
        return _render_public_seo_page("free-trading-journal-template")

    @app.route("/free-prop-firm-trading-journal")
    def free_prop_firm_trading_journal_page():
        return _render_public_seo_page("free-prop-firm-trading-journal")

    @app.route("/pricing")
    def pricing_page():
        return render_template(
            "pricing.html",
            title="Pricing | MyFXJournal - 14-day premium workflow trial",
            meta_description="Core journaling stays free. Try premium workflow features for 14 days, including MT5 sync when setup capacity is available, and join the Trader/Pro waitlist.",
            canonical_url=build_external_url("/pricing"),
            body_class="landing-layout",
            user_logged_in=bool(session.get("user_id")),
        )

    @app.route("/pricing/waitlist", methods=["POST"])
    @limiter.limit("5 per minute;40 per hour")
    def pricing_waitlist_post():
        from models import UpgradeWaitlistEntry

        if is_support_view_session_active():
            return jsonify(
                {
                    "ok": False,
                    "error": "support_view_read_only",
                    "message": "That action is not available in read-only support view.",
                }
            ), 403

        try:
            data = request.get_json(silent=True) or {}
            email = (data.get("email") or "").strip().lower()
            tier = (data.get("tier") or "trader").strip().lower()
            source = (data.get("source") or "pricing_page").strip().lower()
            feature_interest = (data.get("feature_interest") or "").strip().lower()
            cta_context = (data.get("cta_context") or "").strip().lower()
        except Exception:
            return jsonify({"ok": False, "error": "Invalid request."}), 400

        if (
            not email
            or len(email) > 254
            or not WAITLIST_EMAIL_RE.match(email)
            or ".." in email
        ):
            return jsonify({"ok": False, "error": "Please enter a valid email address."}), 400

        allowed_tiers = {"trader", "pro"}
        if tier not in allowed_tiers:
            tier = "trader"

        if source not in WAITLIST_ALLOWED_SOURCES:
            source = "pricing_page"

        if not feature_interest:
            feature_interest = "advanced_replay" if tier == "trader" else "multi_timeframe_replay"
        elif feature_interest not in WAITLIST_ALLOWED_FEATURES:
            feature_interest = "advanced_replay" if tier == "trader" else "multi_timeframe_replay"

        if cta_context and len(cta_context) > 96:
            cta_context = cta_context[:96]

        user_id = session.get("user_id")

        def _enrich_existing_entry(entry):
            if entry is None:
                return
            changed = False
            if user_id and not getattr(entry, "user_id", None):
                entry.user_id = user_id
                changed = True
            if cta_context and not getattr(entry, "cta_context", None):
                entry.cta_context = cta_context
                changed = True
            if not changed:
                return
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    "Failed to enrich waitlist entry email=%s tier=%s source=%s feature=%s",
                    email,
                    tier,
                    source,
                    feature_interest,
                )

        existing = UpgradeWaitlistEntry.query.filter_by(
            email=email,
            tier_intent=tier,
            source=source,
            feature_interest=feature_interest,
        ).first()
        if existing:
            _enrich_existing_entry(existing)
        else:
            entry = UpgradeWaitlistEntry(
                email=email,
                tier_intent=tier,
                source=source,
                feature_interest=feature_interest,
                cta_context=cta_context or None,
                user_id=user_id,
            )
            db.session.add(entry)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                existing = UpgradeWaitlistEntry.query.filter_by(
                    email=email,
                    tier_intent=tier,
                    source=source,
                    feature_interest=feature_interest,
                ).first()
                _enrich_existing_entry(existing)
            except Exception:
                db.session.rollback()
                current_app.logger.exception(
                    "Failed to save waitlist entry email=%s tier=%s source=%s feature=%s",
                    email,
                    tier,
                    source,
                    feature_interest,
                )
                return jsonify({"ok": False, "error": "Something went wrong — please try again."}), 500

        tier_label_map = {"trader": "Trader", "pro": "Pro"}
        tier_label = tier_label_map.get(tier, "Trader")
        try:
            html_body = render_template(
                "emails/waitlist-confirmation.html",
                tier_label=tier_label,
                dashboard_url=build_external_url(url_for("dashboard.home")),
                site_url=build_external_url("/"),
                site_label="myfxjournal.com",
            )
            send_email_placeholder(
                email,
                f"You're on the waitlist — MyFXJournal {tier_label}",
                f"You're on the MyFXJournal {tier_label} waitlist. We'll reach out when early access opens. "
                "Core journaling stays free, and premium workflow features include a 14-day trial "
                "with no credit card required.",
                html_body=html_body,
            )
        except Exception:
            current_app.logger.warning("Waitlist confirmation email failed for %s", email)

        return jsonify({"ok": True})

    @app.route("/robots.txt")
    def robots_txt():
        robots_lines = [
            "User-agent: *",
            "Allow: /",
            "Disallow: /password/",
            "Disallow: /verify-email",
            "Disallow: /onboarding",
            "Disallow: /auth/",
            "Disallow: /session/",
            "Sitemap: " + build_external_url("/sitemap.xml"),
        ]
        return Response("\n".join(robots_lines) + "\n", mimetype="text/plain")

    @app.route("/sitemap.xml")
    def sitemap_xml():
        public_urls = tuple(build_external_url(path) for path in SEO_SITEMAP_PATHS)
        sitemap_items = "\n".join(f"  <url><loc>{url}</loc></url>" for url in public_urls)
        sitemap = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{sitemap_items}\n"
            "</urlset>\n"
        )
        return Response(sitemap, mimetype="application/xml")

    @app.route("/privacy")
    def privacy_policy():
        return render_template(
            "privacy_policy.html",
            title="Privacy Policy | MyFXJournal data, MT5 sync and your rights",
            meta_description="Read how MyFXJournal handles personal data, privacy requests, MT5 sync information, and account data for the trading journal service.",
            canonical_url=build_external_url("/privacy"),
            last_updated=legal_last_updated,
        )

    @app.route("/privacy-policy")
    def privacy_policy_legacy_path():
        return redirect(url_for("privacy_policy"), code=301)

    @app.route("/terms")
    def terms_and_conditions():
        return render_template(
            "terms_and_conditions.html",
            title="Terms of use | MyFXJournal trading journal service",
            meta_description="Review the terms for using MyFXJournal, including account responsibilities, acceptable use, MT5 sync conditions, and service limitations.",
            canonical_url=build_external_url("/terms"),
            last_updated=legal_last_updated,
        )

    @app.route("/terms-and-conditions")
    def terms_and_conditions_legacy_path():
        return redirect(url_for("terms_and_conditions"), code=301)

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

        status_filter = request.args.get("status", "approved").strip().lower()
        if status_filter not in {
            "pending",
            "approved",
            "rejected",
            "suspended",
            "admins",
            "all",
        }:
            status_filter = "approved"
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

        from helpers.entitlements import build_admin_user_trial_display

        trial_by_user = {
            user.id: build_admin_user_trial_display(user)
            for user in users
        }

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
            trial_by_user=trial_by_user,
        )

    @app.route("/dashboard/admin/access/users/<int:user_id>/extend-trial", methods=["POST"])
    @root_admin_required
    def admin_signup_extend_user_trial(user_id):
        from helpers.entitlements import (
            ADMIN_TRIAL_EXTEND_DEFAULT_DAYS,
            ADMIN_TRIAL_EXTEND_MAX_DAYS,
            ADMIN_TRIAL_EXTEND_MIN_DAYS,
            extend_premium_trial,
        )

        user = User.query.filter_by(id=user_id).first()
        if not user:
            return build_admin_redirect("users", "User not found.", "error")

        raw_days = request.form.get("days", ADMIN_TRIAL_EXTEND_DEFAULT_DAYS)
        try:
            result = extend_premium_trial(user, raw_days)
        except ValueError as exc:
            error = str(exc)
            messages = {
                "invalid_days": (
                    f"Enter a trial extension between {ADMIN_TRIAL_EXTEND_MIN_DAYS} "
                    f"and {ADMIN_TRIAL_EXTEND_MAX_DAYS} days."
                ),
                "not_extendable": "That user is grandfathered, paid, or admin — trial extension does not apply.",
                "trial_not_ended": "Trial extension is only available after the trial has ended or MT5 sync was paused.",
                "schema_unsupported": "Trial extension is unavailable until entitlement columns are migrated.",
            }
            return build_admin_redirect(
                "users",
                messages.get(error, "Could not extend that user's trial."),
                "error",
            )

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return build_admin_redirect(
                "users",
                "Trial extension could not be saved. Please try again.",
                "error",
            )

        days_granted = result.get("days_granted", raw_days)
        resumed_mt5_count = int(result.get("resumed_mt5_count") or 0)
        message = f"Extended {user.username}'s trial by {days_granted} days."
        if resumed_mt5_count:
            message += f" Resumed MT5 sync setup for {resumed_mt5_count} linked account(s)."
        return build_admin_redirect("users", message, "success")

    @app.route("/dashboard/admin/access/users/export")
    @admin_required
    def admin_signup_users_export():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ["name", "email", "signup date", "last login date", "last active date"]
        )
        users = User.query.order_by(User.created_at.asc(), User.id.asc()).all()
        for user in users:
            writer.writerow(
                [
                    user.username,
                    user.email,
                    _format_admin_timestamp(user.created_at),
                    _format_admin_timestamp(user.last_login_at),
                    _format_admin_timestamp(user.last_active_at),
                ]
            )
        filename = f"myfxjournal-users-{utcnow_naive().strftime('%Y%m%d')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

        weekly_ai_broker = "unknown"
        try:
            from celery_workers.cache import CacheUnavailableError, clear_ai_status
            from celery_workers.weekly_tasks import generate_weekly_ai_task
            weekly_ai_broker = describe_celery_broker(generate_weekly_ai_task)

            try:
                clear_ai_status(
                    user_id,
                    trade_account_id=account.id,
                    period_start_utc=period["period_start_utc"],
                )
            except CacheUnavailableError:
                pass

            dispatch_result = dispatch_celery_task(
                generate_weekly_ai_task,
                args=[
                    user_id,
                    account.id,
                    "dashboard_advice.txt",
                    period["period_start_utc"].isoformat(),
                ],
                kwargs={
                    "force_regenerate": True,
                    "send_weekly_email": False,
                },
                log=current_app.logger,
                label="admin_ai_regeneration",
                extra={
                    "user_id": user_id,
                    "trade_account_id": account.id,
                    "admin_user_id": session.get("user_id"),
                },
            )
            current_app.logger.info(
                "Admin queued weekly AI regeneration user_id=%s trade_account_id=%s task_id=%s broker=%s",
                user_id,
                account.id,
                getattr(dispatch_result, "id", None),
                weekly_ai_broker,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "Admin AI regeneration unavailable: user_id=%s trade_account_id=%s broker=%s error=%s",
                user_id,
                trade_account_id,
                weekly_ai_broker,
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

    def _parse_admin_email_inactive_days(raw_value, *, default=30):
        try:
            days = int(raw_value)
        except (TypeError, ValueError):
            days = default
        return max(1, min(days, 3650))

    def _parse_admin_email_send_delay_ms(raw_value, *, default=400):
        try:
            delay_ms = int(raw_value)
        except (TypeError, ValueError):
            delay_ms = default
        return max(100, min(delay_ms, 60_000))

    def _query_admin_email_recipients(*, inactive_days, inactive_only, search_query):
        now = utcnow_naive()
        cutoff = now - timedelta(days=inactive_days)
        users_query = User.query
        if search_query:
            search_like = f"%{search_query}%"
            search_filters = [
                User.username.ilike(search_like),
                User.email.ilike(search_like),
            ]
            if search_query.isdigit():
                search_filters.append(User.id == int(search_query))
            users_query = users_query.filter(or_(*search_filters))
        users = users_query.order_by(User.username.asc(), User.id.asc()).all()
        recipients = []
        for user in users:
            last_login = user.last_login_at
            is_inactive = last_login is None or last_login < cutoff
            if inactive_only and not is_inactive:
                continue
            recipients.append(
                {
                    "id": user.id,
                    "name": user.username,
                    "email": user.email,
                    "signup_status": user.signup_status,
                    "last_login_at": last_login.isoformat() if last_login else None,
                    "inactive": is_inactive,
                }
            )
        return recipients

    def render_admin_send_email_page(*, admin_user, **extra_context):
        page_ctx = build_admin_page_context(
            admin_user=admin_user,
            section="send_email",
            title="MyFXJournal | Send Email",
            page_heading="Send Email",
            page_subtitle=(
                "Compose one message and send it as individual Resend emails to selected users."
            ),
        )
        page_ctx.update(extra_context)
        return render_template("admin_send_email.html", **page_ctx)

    @app.route("/dashboard/admin/access/send-email")
    @admin_required
    def admin_send_email():
        admin_user = get_current_admin_user()
        default_inactive_days = _parse_admin_email_inactive_days(
            os.getenv("ADMIN_EMAIL_INACTIVE_DAYS_DEFAULT", "30"),
            default=30,
        )
        return render_admin_send_email_page(
            admin_user=admin_user,
            default_inactive_days=default_inactive_days,
            admin_broadcast_from=_resolve_admin_broadcast_from_header(),
            admin_broadcast_reply_to=_resolve_email_reply_to(),
            admin_broadcast_logo_url=build_external_url("/static/site-logo.png"),
            send_delay_ms=_parse_admin_email_send_delay_ms(
                os.getenv("ADMIN_EMAIL_SEND_DELAY_MS", "400"),
                default=400,
            ),
        )

    @app.route("/dashboard/admin/access/send-email/recipients")
    @admin_required
    def admin_send_email_recipients():
        inactive_days = _parse_admin_email_inactive_days(request.args.get("inactive_days"))
        inactive_only = request.args.get("inactive_only", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        search_query = (request.args.get("q") or "").strip()
        recipients = _query_admin_email_recipients(
            inactive_days=inactive_days,
            inactive_only=inactive_only,
            search_query=search_query,
        )
        return jsonify(
            {
                "recipients": recipients,
                "inactive_days": inactive_days,
                "inactive_only": inactive_only,
            }
        )

    @app.route("/dashboard/admin/access/send-email/send-one", methods=["POST"])
    @admin_required
    def admin_send_email_send_one():
        payload = request.get_json(silent=True) or {}
        user_id = payload.get("user_id")
        subject = str(payload.get("subject") or "").strip()
        html_body = str(payload.get("html_body") or "").strip()
        text_body = str(payload.get("text_body") or "").strip()

        if not user_id:
            return jsonify({"error": "missing_user", "message": "Recipient is required."}), 400
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_user", "message": "Invalid recipient."}), 400
        if not subject:
            return jsonify({"error": "missing_subject", "message": "Subject is required."}), 400
        if len(subject) > 200:
            return jsonify({"error": "subject_too_long", "message": "Subject is too long."}), 400
        if not html_body and not text_body:
            return jsonify({"error": "missing_body", "message": "Message body is required."}), 400
        if len(html_body) > 512_000 or len(text_body) > 512_000:
            return jsonify({"error": "body_too_large", "message": "Message body is too large."}), 400

        recipient = db.session.get(User, user_id)
        if recipient is None:
            return jsonify({"error": "user_not_found", "message": "Recipient not found."}), 404

        recipient_name = recipient.username
        personalized_subject = apply_admin_email_placeholders(subject, recipient_name=recipient_name)
        if html_body:
            personalized_html = sanitize_admin_broadcast_html(
                apply_admin_email_placeholders(html_body, recipient_name=recipient_name)
            )
        elif admin_broadcast_message_contains_html(text_body):
            personalized_html = sanitize_admin_broadcast_html(
                apply_admin_email_placeholders(text_body, recipient_name=recipient_name)
            )
        else:
            personalized_html = ""
        personalized_text = apply_admin_email_placeholders(
            text_body or html_to_plain_email_text(personalized_html),
            recipient_name=recipient_name,
        )

        result = send_email_placeholder(
            recipient.email,
            personalized_subject,
            personalized_text,
            html_body=personalized_html or None,
            from_header=_resolve_admin_broadcast_from_header(),
        )
        sent = bool(result.get("sent"))
        return jsonify(
            {
                "ok": sent,
                "sent": sent,
                "mode": result.get("mode"),
                "user_id": recipient.id,
                "email": recipient.email,
                "name": recipient_name,
            }
        ), (200 if sent else 502)

    @app.route("/dashboard/admin/access/send-email/signatures")
    @admin_required
    def admin_send_email_signatures():
        return jsonify({"signatures": list_admin_broadcast_signatures()})

    @app.route("/dashboard/admin/access/send-email/signatures/save", methods=["POST"])
    @admin_required
    def admin_send_email_signatures_save():
        admin_user = get_current_admin_user()
        payload = request.get_json(silent=True) or {}
        signature_id = str(payload.get("id") or "").strip() or None
        name = str(payload.get("name") or "").strip()
        body = str(payload.get("body") or "").strip()
        try:
            saved = save_admin_broadcast_signature(
                signature_id=signature_id,
                name=name,
                body=body,
                updated_by_user_id=admin_user.id if admin_user else None,
            )
        except ValueError as exc:
            error = str(exc)
            messages = {
                "missing_name": "Signature name is required.",
                "missing_body": "Signature body is required.",
                "name_too_long": "Signature name is too long.",
                "body_too_long": "Signature body is too long.",
                "limit_reached": "Signature limit reached. Delete one before saving another.",
                "not_found": "Signature not found.",
            }
            return jsonify({"error": error, "message": messages.get(error, "Could not save signature.")}), 400
        return jsonify({"signature": saved, "signatures": list_admin_broadcast_signatures()})

    @app.route("/dashboard/admin/access/send-email/signatures/delete", methods=["POST"])
    @admin_required
    def admin_send_email_signatures_delete():
        admin_user = get_current_admin_user()
        payload = request.get_json(silent=True) or {}
        signature_id = str(payload.get("id") or "").strip()
        if not signature_id:
            return jsonify({"error": "missing_id", "message": "Signature id is required."}), 400
        try:
            delete_admin_broadcast_signature(
                signature_id=signature_id,
                updated_by_user_id=admin_user.id if admin_user else None,
            )
        except ValueError as exc:
            error = str(exc)
            messages = {
                "missing_id": "Signature id is required.",
                "not_found": "Signature not found.",
            }
            return jsonify({"error": error, "message": messages.get(error, "Could not delete signature.")}), 400
        return jsonify({"ok": True, "signatures": list_admin_broadcast_signatures()})

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
        mt5_accounts = apply_admin_mt5_sort(
            MT5Account.query.options(
                joinedload(MT5Account.user),
                joinedload(MT5Account.trade_account),
            ),
            mt5_sort,
        ).all()
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
        if trade_account_ids:
            latest_request_subq = (
                db.session.query(
                    MT5AccessRequest.trade_account_id,
                    func.max(MT5AccessRequest.id).label("latest_request_id"),
                )
                .filter(MT5AccessRequest.trade_account_id.in_(trade_account_ids))
                .group_by(MT5AccessRequest.trade_account_id)
                .subquery()
            )
            request_rows = (
                MT5AccessRequest.query.join(
                    latest_request_subq,
                    MT5AccessRequest.id == latest_request_subq.c.latest_request_id,
                ).all()
            )
            latest_request_by_trade_account_id = {
                request_row.trade_account_id: request_row for request_row in request_rows
            }
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
        failed_mt5_count = sum(
            1
            for account in mt5_accounts
            if getattr(account, "connection_status", None) == "failed"
            and not account.is_orphaned
        )
        mt5_vm_overview = build_admin_mt5_vm_overview(
            mt5_accounts=mt5_accounts,
            mt5_statuses_by_account_id=mt5_statuses_by_account_id,
        )
        from helpers.mt5_server_seed_shortlist import list_active_mt5_server_seed_shortlist

        mt5_server_seed_shortlist = list_active_mt5_server_seed_shortlist()
        mt5_server_seed_open_count = sum(
            1 for row in mt5_server_seed_shortlist if row.status == MT5ServerSeedShortlist.STATUS_OPEN
        )
        return render_admin_page(
            admin_user=admin_user,
            section="mt5",
            mt5_accounts=mt5_accounts,
            mt5_sort=mt5_sort,
            mt5_trade_counts_by_account=mt5_trade_counts_by_account,
            mt5_statuses_by_account_id=mt5_statuses_by_account_id,
            orphaned_mt5_count=orphaned_mt5_count,
            failed_mt5_count=failed_mt5_count,
            mt5_vm_overview=mt5_vm_overview,
            mt5_server_seed_shortlist=mt5_server_seed_shortlist,
            mt5_server_seed_open_count=mt5_server_seed_open_count,
        )

    @app.route(
        "/dashboard/admin/access/mt5/server-seed-shortlist/<int:entry_id>/mark-seeded",
        methods=["POST"],
    )
    @root_admin_required
    def admin_mt5_server_seed_shortlist_mark_seeded(entry_id):
        from helpers.mt5_server_seed_shortlist import mark_mt5_server_seed_shortlist_seeded

        row = mark_mt5_server_seed_shortlist_seeded(
            entry_id=entry_id,
            admin_user_id=session.get("user_id"),
        )
        if row is None:
            return build_admin_redirect(
                "mt5",
                "That server seed shortlist entry was not found.",
                "error",
            )
        return build_admin_redirect(
            "mt5",
            f"Marked {row.server_name} as seeded on the VM. Retry setup for affected accounts when ready.",
            "success",
        )

    @app.route(
        "/dashboard/admin/access/mt5/server-seed-shortlist/<int:entry_id>/mark-resolved",
        methods=["POST"],
    )
    @root_admin_required
    def admin_mt5_server_seed_shortlist_mark_resolved(entry_id):
        from helpers.mt5_server_seed_shortlist import mark_mt5_server_seed_shortlist_resolved

        row = mark_mt5_server_seed_shortlist_resolved(entry_id=entry_id)
        if row is None:
            return build_admin_redirect(
                "mt5",
                "That server seed shortlist entry was not found.",
                "error",
            )
        return build_admin_redirect(
            "mt5",
            f"Cleared {row.server_name} from the active server seed shortlist.",
            "success",
        )

    @app.route("/dashboard/admin/access/mt5/auto-bar-sync", methods=["POST"])
    @root_admin_required
    def admin_mt5_auto_bar_sync():
        admin_user = get_current_root_admin_user()
        enabled = str(request.form.get("enabled") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            set_bool_app_setting(
                MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY,
                enabled,
                updated_by_user_id=admin_user.id if admin_user else None,
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "Admin MT5 auto bar sync setting update failed: %s",
                sanitize_error_message(exc),
            )
            return build_admin_redirect(
                "mt5",
                "Could not update automatic chart-bar sync right now.",
                "error",
            )
        current_app.logger.info(
            "Admin set MT5 automatic bar sync for public users enabled=%s admin_user_id=%s",
            enabled,
            session.get("user_id"),
        )
        return build_admin_redirect(
            "mt5",
            (
                "Automatic chart-bar sync for public users is now "
                f"{'enabled' if enabled else 'disabled'}."
            ),
            "success",
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

        setup_broker = "unknown"
        target_vm_id, target_vm_error = _parse_admin_target_vm_id()
        if target_vm_error:
            return build_admin_redirect("mt5", target_vm_error, "error")
        try:
            from celery_workers.mt5_setup_tasks import setup_mt5_terminal
            setup_broker = describe_celery_broker(setup_mt5_terminal)

            dispatch_result = dispatch_mt5_setup(
                setup_mt5_terminal,
                mt5_account.id,
                target_vm_id=target_vm_id,
                allow_failover=not bool(target_vm_id),
                label="admin_mt5_add_and_setup",
                extra={
                    "mt5_account_id": mt5_account.id,
                    "account_number": account_number,
                    "admin_user_id": session.get("user_id"),
                },
                log=current_app.logger,
            )
            current_app.logger.info(
                "Admin queued MT5 setup_terminal mt5_account_id=%s queue=mt5_setup task_id=%s broker=%s",
                mt5_account.id,
                getattr(dispatch_result, "id", None),
                setup_broker,
            )
        except Exception as exc:
            current_app.logger.warning(
                "MT5 setup queue failed for mt5_account_id=%s broker=%s: %s",
                mt5_account.id,
                setup_broker,
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
        if account.cleanup_marked_at is not None:
            return build_admin_redirect(
                "mt5",
                "That MT5 account is still waiting for VM cleanup to finish. Try Setup Terminal again after cleanup completes.",
                "error",
            )

        setup_broker = "unknown"
        target_vm_id, target_vm_error = _parse_admin_target_vm_id()
        if target_vm_error:
            return build_admin_redirect("mt5", target_vm_error, "error")
        try:
            from celery_workers.mt5_setup_tasks import setup_mt5_terminal
            setup_broker = describe_celery_broker(setup_mt5_terminal)

            dispatch_result = dispatch_mt5_setup(
                setup_mt5_terminal,
                mt5_account_id,
                target_vm_id=target_vm_id,
                allow_failover=False,
                label="admin_mt5_setup_terminal",
                extra={
                    "mt5_account_id": mt5_account_id,
                    "admin_user_id": session.get("user_id"),
                    "target_vm_id": target_vm_id,
                },
                log=current_app.logger,
            )
            current_app.logger.info(
                "Admin queued MT5 setup_terminal mt5_account_id=%s target_vm_id=%s task_id=%s broker=%s",
                mt5_account_id,
                target_vm_id or "default",
                getattr(dispatch_result, "id", None),
                setup_broker,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 setup queue failed for mt5_account_id=%s broker=%s: %s",
                mt5_account_id,
                setup_broker,
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

    @app.route("/dashboard/admin/access/mt5/vm-delete-files", methods=["POST"])
    @root_admin_required
    def admin_mt5_vm_delete_files():
        vm_id, vm_error = _parse_admin_vm_id_field("vm_id")
        if vm_error:
            return build_admin_redirect("mt5", vm_error, "error")
        if not vm_id:
            return build_admin_redirect("mt5", "Choose a VM before queueing terminal file cleanup.", "error")

        mt5_accounts = MT5Account.query.all()
        queued, message = queue_mt5_accounts_cleanup_for_vm(
            vm_id=vm_id,
            mt5_accounts=mt5_accounts,
            log_context="admin vm delete-files",
        )
        if queued:
            current_app.logger.info(
                "Admin queued VM terminal cleanup vm_id=%s queued=%s admin_user_id=%s",
                vm_id,
                queued,
                session.get("user_id"),
            )
        return build_admin_redirect("mt5", message, "success" if queued else "error")

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
        target_vm_id, target_vm_error = _parse_admin_target_vm_id()
        if target_vm_error:
            return build_admin_redirect("mt5", target_vm_error, "error")
        ok, message = reactivate_mt5_account(
            mt5_account=account,
            log_context="admin reactivate",
            target_vm_id=target_vm_id,
            strict_target_vm=True,
        )
        if ok:
            current_app.logger.info(
                "Admin queued MT5 reactivation mt5_account_id=%s target_vm_id=%s queue=mt5_setup",
                mt5_account_id,
                target_vm_id or "default",
            )
        return build_admin_redirect(
            "mt5",
            message,
            "success" if ok else "error",
        )

    @app.route("/dashboard/admin/access/mt5/<int:mt5_account_id>/delete-vm-files", methods=["POST"])
    @root_admin_required
    def admin_mt5_delete_account_vm_files(mt5_account_id):
        account = MT5Account.query.filter_by(id=mt5_account_id).first_or_404()
        target_vm_id, target_vm_error = _parse_admin_target_vm_id()
        if target_vm_error:
            return build_admin_redirect("mt5", target_vm_error, "error")
        ok, message = delete_mt5_account_vm_files(
            mt5_account=account,
            log_context="admin delete-vm-files",
            target_vm_id=target_vm_id,
        )
        if ok:
            current_app.logger.info(
                "Admin queued MT5 account VM file cleanup mt5_account_id=%s",
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

        sync_broker = "unknown"
        try:
            from celery_workers.mt5_sync_tasks import sync_mt5_account
            sync_broker = describe_celery_broker(sync_mt5_account)

            dispatch_result = dispatch_mt5_priority(
                sync_mt5_account,
                mt5_account_id,
                account_vm_id=account.vm_id,
                kwargs={"full_history": True, "trigger_source": "manual"},
                label="admin_mt5_trigger_sync",
                extra={
                    "mt5_account_id": mt5_account_id,
                    "admin_user_id": session.get("user_id"),
                    "trigger_source": "manual",
                },
                log=current_app.logger,
            )
            if mt5_dispatch_was_skipped(dispatch_result):
                return build_admin_redirect("mt5", MT5_DISPATCH_SKIPPED_MISSING_VM_MSG, "error")
            current_app.logger.info(
                "Admin queued sync_mt5_account mt5_account_id=%s full_history=True trigger=manual task_id=%s broker=%s",
                mt5_account_id,
                getattr(dispatch_result, "id", None),
                sync_broker,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 sync queue failed for mt5_account_id=%s broker=%s: %s",
                mt5_account_id,
                sync_broker,
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
        sync_broker = "unknown"
        try:
            from celery_workers.mt5_sync_tasks import sync_mt5_account
            sync_broker = describe_celery_broker(sync_mt5_account)

            task_ids = []
            skipped_vm = 0
            for account in eligible:
                dispatch_result = dispatch_mt5_priority(
                    sync_mt5_account,
                    account.id,
                    account_vm_id=account.vm_id,
                    kwargs={
                        "full_history": True,
                        "trigger_source": "admin_recalibrate_times",
                        "recalibrate_trade_timestamps": True,
                    },
                    label="admin_mt5_recalibrate_times_all",
                    extra={
                        "mt5_account_id": account.id,
                        "admin_user_id": session.get("user_id"),
                        "trigger_source": "admin_recalibrate_times",
                    },
                    log=current_app.logger,
                )
                if mt5_dispatch_was_skipped(dispatch_result):
                    skipped_vm += 1
                    continue
                task_ids.append(getattr(dispatch_result, "id", None))
            if skipped_vm and not task_ids:
                return build_admin_redirect("mt5", MT5_DISPATCH_SKIPPED_MISSING_VM_MSG, "error")
            current_app.logger.info(
                "Admin queued sync_mt5_account recalibrate_times for %s mt5_account_id(s) task_ids=%s broker=%s",
                len(task_ids),
                task_ids,
                sync_broker,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 recalibrate-times queue failed broker=%s: %s",
                sync_broker,
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
                f"Queued full-history MT5 sync with timestamp recalibration for {len(task_ids)} account(s). "
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
        sync_broker = "unknown"
        try:
            from celery_workers.mt5_sync_tasks import sync_mt5_account
            sync_broker = describe_celery_broker(sync_mt5_account)

            dispatch_result = dispatch_mt5_priority(
                sync_mt5_account,
                mt5_account_id,
                account_vm_id=account.vm_id,
                kwargs={
                    "full_history": True,
                    "trigger_source": "admin_recalibrate_times",
                    "recalibrate_trade_timestamps": True,
                },
                label="admin_mt5_recalibrate_times_single",
                extra={
                    "mt5_account_id": mt5_account_id,
                    "admin_user_id": session.get("user_id"),
                    "trigger_source": "admin_recalibrate_times",
                },
                log=current_app.logger,
            )
            if mt5_dispatch_was_skipped(dispatch_result):
                return build_admin_redirect("mt5", MT5_DISPATCH_SKIPPED_MISSING_VM_MSG, "error")
            current_app.logger.info(
                "Admin queued sync_mt5_account mt5_account_id=%s recalibrate_trade_timestamps=True task_id=%s broker=%s",
                mt5_account_id,
                getattr(dispatch_result, "id", None),
                sync_broker,
            )
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(
                "MT5 recalibrate-times queue failed for mt5_account_id=%s broker=%s: %s",
                mt5_account_id,
                sync_broker,
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
            bar_coverage_rows = (
                db.session.query(
                    TradeBars.trade_id,
                    func.count(TradeBars.id).label("bar_count"),
                    func.min(TradeBars.bar_time).label("min_bar_time"),
                    func.max(TradeBars.bar_time).label("max_bar_time"),
                )
                .filter(
                    TradeBars.trade_id.in_(closed_trade_ids),
                    TradeBars.timeframe == "M5",
                )
                .group_by(TradeBars.trade_id)
                .all()
            )
            bar_coverage_by_trade_id = {
                trade_id: {
                    "bar_count": int(bar_count or 0),
                    "min_bar_time": min_bar_time,
                    "max_bar_time": max_bar_time,
                }
                for trade_id, bar_count, min_bar_time, max_bar_time in bar_coverage_rows
            }

            def _trade_has_complete_m5_coverage(trade):
                coverage = bar_coverage_by_trade_id.get(trade.id)
                if not coverage:
                    return False
                return has_complete_m5_chart_coverage(
                    opened_at=trade.opened_at,
                    closed_at=trade.closed_at,
                    bar_count=coverage["bar_count"],
                    min_bar_time=coverage["min_bar_time"],
                    max_bar_time=coverage["max_bar_time"],
                )

            already_backfilled_trade_ids = {
                trade.id for trade in closed_trades if _trade_has_complete_m5_coverage(trade)
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
                    "All closed MT5 trades already have complete M5 bars. Use Clear Bars first if you want "
                    "to refetch everything."
                ),
                "info",
            )

        backfill_broker = "unknown"
        try:
            from celery_workers.mt5_sync_tasks import fetch_trade_bars
            backfill_broker = describe_celery_broker(fetch_trade_bars)
            queued = 0
            task_ids = []
            for trade in trades_to_queue:
                dispatch_result = dispatch_mt5_priority(
                    fetch_trade_bars,
                    mt5_account_id,
                    trade.id,
                    account_vm_id=account.vm_id,
                    label="admin_mt5_backfill_bars",
                    extra={
                        "mt5_account_id": mt5_account_id,
                        "trade_id": trade.id,
                        "admin_user_id": session.get("user_id"),
                        "force_backfill": force_backfill,
                    },
                    log=current_app.logger,
                )
                if mt5_dispatch_was_skipped(dispatch_result):
                    continue
                task_ids.append(getattr(dispatch_result, "id", None))
                queued += 1
            if queued == 0:
                return build_admin_redirect("mt5", MT5_DISPATCH_SKIPPED_MISSING_VM_MSG, "error")
            current_app.logger.info(
                "Admin queued fetch_trade_bars mt5_account_id=%s tasks=%s closed=%s skipped_existing=%s force=%s queue=mt5_priority task_ids=%s broker=%s",
                mt5_account_id,
                queued,
                len(closed_trades),
                max(len(closed_trades) - queued, 0),
                force_backfill,
                task_ids,
                backfill_broker,
            )
        except Exception as exc:
            current_app.logger.warning(
                "Bar backfill dispatch failed for mt5_account_id=%s broker=%s: %s",
                mt5_account_id,
                backfill_broker,
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

        if account.is_cleanup_only:
            # Already an orphan with no credentials — safe to delete immediately.
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
                f"Deleted cleanup-only MT5 record {account_number}.",
                "success",
            )

        # Queue cleanup BEFORE marking — paths are read from the account at queue time.
        had_runtime_artifacts = bool(
            str(account.terminal_path or "").strip() or str(account.appdata_hash or "").strip()
        )

        if had_runtime_artifacts:
            target_vm_id, target_vm_error = _parse_admin_target_vm_id()
            if target_vm_error:
                return build_admin_redirect("mt5", target_vm_error, "error")

            from helpers.core import resolve_mt5_cleanup_target_vm

            resolved_vm_id, cleanup_vm_error = resolve_mt5_cleanup_target_vm(
                mt5_account=account,
                target_vm_id=target_vm_id,
            )
            if cleanup_vm_error:
                return build_admin_redirect("mt5", cleanup_vm_error, "error")

            cleanup_warning = queue_mt5_account_cleanup(
                mt5_account=account,
                log_context="admin delete",
                delete_row_on_success=True,
                target_vm_id=resolved_vm_id,
            )
            if cleanup_warning:
                return build_admin_redirect("mt5", cleanup_warning, "error")

        account_number = account.account_number

        try:
            account.mark_for_cleanup()
            db.session.commit()
        except (OperationalError, IntegrityError):
            db.session.rollback()
            return build_admin_redirect(
                "mt5",
                "Could not delete that MT5 account right now. Please try again.",
                "error",
            )

        if had_runtime_artifacts:
            success_message = (
                f"MT5 account {account_number} marked for cleanup. "
                "DB row will be removed after terminal cleanup succeeds."
            )
        else:
            success_message = f"MT5 account {account_number} marked for cleanup."

        return build_admin_redirect(
            "mt5",
            success_message,
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
