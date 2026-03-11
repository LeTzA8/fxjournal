import os
import re
import hashlib
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import g, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from models import (
    Trade,
    TradeAccount,
    TradeProfile,
    TradeProfileVersion,
    User,
    db,
    generate_trade_account_pubkey,
    generate_trade_pubkey,
)
from trading import (
    get_timezone,
    normalize_account_type,
    normalize_symbol,
    to_display_timezone,
)
from utils import env_bool, env_int, utcnow_naive


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

def get_app_timezone_name():
    return os.getenv("APP_TIMEZONE", "Asia/Singapore").strip() or "Asia/Singapore"


def normalize_timezone_name(value, default=None):
    text_value = str(value or "").strip()
    if not text_value:
        return default
    try:
        ZoneInfo(text_value)
    except ZoneInfoNotFoundError:
        return default
    return text_value


def get_display_timezone_name():
    return normalize_timezone_name(session.get("display_timezone"), get_app_timezone_name()) or "UTC"


def parse_local_datetime_input(raw_value):
    text_value = str(raw_value or "").strip()
    if not text_value:
        return None
    try:
        local_value = datetime.strptime(text_value, "%Y-%m-%dT%H:%M")
    except ValueError:
        try:
            local_value = datetime.strptime(text_value, "%Y-%m-%d")
        except ValueError:
            return None
    timezone_name = get_display_timezone_name()
    local_aware = local_value.replace(tzinfo=get_timezone(timezone_name))
    return local_aware.astimezone(get_timezone("UTC")).replace(tzinfo=None)


def format_local_datetime_input(value):
    local_value = to_display_timezone(value, get_display_timezone_name())
    if local_value is None:
        return ""
    return local_value.strftime("%Y-%m-%dT%H:%M")


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def is_local_dev_environment():
    app_env = os.getenv("APP_ENV", "").strip().lower()
    flask_env = os.getenv("FLASK_ENV", "").strip().lower()
    return (
        env_bool("FLASK_DEBUG", False)
        or app_env in {"local", "development", "dev"}
        or flask_env in {"local", "development", "dev"}
    )


def sanitize_error_message(message):
    text = str(message or "").strip()
    if not text:
        return "(no error message)"
    text = re.sub(r"https?://\S+", "[redacted-url]", text)
    text = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", "[redacted-token]", text)
    text = re.sub(r"\s+", " ", text)
    return text[:500]


# ---------------------------------------------------------------------------
# Trade size / display helpers
# ---------------------------------------------------------------------------

def get_trade_size_label(account_type):
    return "Contracts" if normalize_account_type(account_type) == "FUTURES" else "Lots"


# ---------------------------------------------------------------------------
# Trade deduplication
# ---------------------------------------------------------------------------

def build_trade_duplicate_key(
    *,
    symbol,
    contract_code=None,
    side,
    entry_price,
    exit_price,
    lot_size,
    opened_at,
    closed_at,
    pnl,
):
    def quantize(value, digits=8):
        if value is None:
            return None
        return round(float(value), digits)

    return (
        normalize_symbol(contract_code or symbol),
        str(side or "").strip().upper(),
        quantize(entry_price),
        quantize(exit_price),
        quantize(lot_size),
        quantize(pnl, digits=2),
        opened_at.isoformat() if opened_at else None,
        closed_at.isoformat() if closed_at else None,
    )


def build_trade_import_dedupe_key(
    *,
    account_type,
    symbol,
    contract_code=None,
    side,
    entry_price,
    exit_price,
    lot_size,
    opened_at,
    closed_at,
    pnl,
    mt5_position=None,
):
    normalized_account_type = normalize_account_type(account_type)
    if normalized_account_type == "CFD":
        position_text = str(mt5_position or "").strip()
        if not position_text:
            return None
        payload = f"cfd:{position_text}"
    else:
        duplicate_key = build_trade_duplicate_key(
            symbol=symbol,
            contract_code=contract_code,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            lot_size=lot_size,
            opened_at=opened_at,
            closed_at=closed_at,
            pnl=pnl,
        )
        payload = "futures:" + repr(duplicate_key)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Trade account queries
# ---------------------------------------------------------------------------

def get_user_trade_accounts(user_id):
    return (
        TradeAccount.query.filter_by(user_id=user_id)
        .order_by(TradeAccount.is_default.desc(), TradeAccount.id.asc())
        .all()
    )


def ensure_trade_account_for_user(user_id):
    changed = False
    accounts = (
        TradeAccount.query.filter_by(user_id=user_id)
        .order_by(TradeAccount.id.asc())
        .all()
    )
    if not accounts:
        default_account = TradeAccount(
            user_id=user_id,
            name="Main Account",
            account_type="CFD",
            is_default=True,
        )
        db.session.add(default_account)
        db.session.flush()
        accounts = [default_account]
        changed = True

    if not any(account.is_default for account in accounts):
        accounts[0].is_default = True
        changed = True

    return accounts, changed


def ensure_trade_accounts_backfill():
    changed = False
    user_rows = db.session.query(User.id).all()

    for (user_id,) in user_rows:
        accounts, account_changed = ensure_trade_account_for_user(user_id)
        if account_changed:
            changed = True

        default_account = next((account for account in accounts if account.is_default), None)
        if default_account is None:
            default_account = accounts[0]

        linked = Trade.query.filter_by(user_id=user_id, trade_account_id=None).update(
            {"trade_account_id": default_account.id},
            synchronize_session=False,
        )
        if linked:
            changed = True

    if changed:
        db.session.commit()


def normalize_trade_account_name(value):
    return " ".join(str(value or "").strip().split())


def parse_trade_account_size(value):
    text_value = str(value or "").strip().replace(",", "")
    if not text_value:
        return None
    try:
        account_size = float(text_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid account size") from exc
    if account_size <= 0:
        raise ValueError("invalid account size")
    return account_size


# ---------------------------------------------------------------------------
# URL / redirect helpers
# ---------------------------------------------------------------------------

def append_query_params(path_or_url, **params):
    parsed = urlsplit(str(path_or_url or ""))
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            query_items.pop(key, None)
            continue
        text_value = str(value).strip()
        if not text_value:
            query_items.pop(key, None)
            continue
        query_items[key] = text_value
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_items, doseq=True),
            parsed.fragment,
        )
    )


def get_safe_internal_next(default_endpoint):
    next_value = (
        request.form.get("next", "").strip()
        or request.args.get("next", "").strip()
        or request.referrer
        or ""
    )
    if next_value.startswith("/") and not next_value.startswith("//"):
        return next_value
    return url_for(default_endpoint)


# ---------------------------------------------------------------------------
# Active trade account resolution
# ---------------------------------------------------------------------------

def resolve_active_trade_account(user_id, requested_account_id=None, requested_account_pubkey=None):
    accounts, changed = ensure_trade_account_for_user(user_id)
    if changed:
        db.session.commit()
        accounts = get_user_trade_accounts(user_id)

    if requested_account_pubkey is not None:
        requested_pubkey = str(requested_account_pubkey).strip()
        if requested_pubkey:
            requested_match = next(
                (account for account in accounts if account.pubkey == requested_pubkey),
                None,
            )
            if requested_match:
                session["active_trade_account_id"] = requested_match.id
                return requested_match, accounts

    if requested_account_id is not None:
        try:
            requested_id = int(str(requested_account_id).strip())
        except (TypeError, ValueError):
            requested_id = None
        if requested_id is not None:
            requested_match = next(
                (account for account in accounts if account.id == requested_id),
                None,
            )
            if requested_match:
                session["active_trade_account_id"] = requested_match.id
                return requested_match, accounts

    try:
        active_id = int(str(session.get("active_trade_account_id", "")).strip())
    except (TypeError, ValueError):
        active_id = None
    active_account = next(
        (account for account in accounts if account.id == active_id),
        None,
    )
    if not active_account:
        active_account = next(
            (account for account in accounts if account.is_default),
            accounts[0],
        )
        session["active_trade_account_id"] = active_account.id
    return active_account, accounts


def get_active_trade_account_for_user(user_id):
    active_account = getattr(g, "active_trade_account", None)
    if active_account and active_account.user_id == user_id:
        return active_account
    active_account, _accounts = resolve_active_trade_account(user_id)
    return active_account


# ---------------------------------------------------------------------------
# Pubkey lookups
# ---------------------------------------------------------------------------

def get_user_trade_account_by_pubkey(user_id, trade_account_pubkey):
    return TradeAccount.query.filter_by(
        user_id=user_id,
        pubkey=str(trade_account_pubkey or "").strip(),
    ).first()


def get_user_trade_account_by_pubkey_or_404(user_id, trade_account_pubkey):
    return TradeAccount.query.filter_by(
        user_id=user_id,
        pubkey=str(trade_account_pubkey or "").strip(),
    ).first_or_404()


def get_user_trade_by_pubkey_or_404(user_id, trade_pubkey):
    return Trade.query.filter_by(
        user_id=user_id,
        pubkey=str(trade_pubkey or "").strip(),
    ).first_or_404()


# ---------------------------------------------------------------------------
# Unique pubkey builders
# ---------------------------------------------------------------------------

def build_unique_trade_pubkey(reserved_pubkeys=None):
    reserved = reserved_pubkeys if reserved_pubkeys is not None else set()
    while True:
        candidate = generate_trade_pubkey()
        if candidate in reserved:
            continue
        exists = db.session.query(Trade.id).filter_by(pubkey=candidate).first()
        if exists:
            continue
        reserved.add(candidate)
        return candidate


def build_unique_trade_account_pubkey(reserved_pubkeys=None):
    reserved = reserved_pubkeys if reserved_pubkeys is not None else set()
    while True:
        candidate = generate_trade_account_pubkey()
        if candidate in reserved:
            continue
        exists = db.session.query(TradeAccount.id).filter_by(pubkey=candidate).first()
        if exists:
            continue
        reserved.add(candidate)
        return candidate


def build_unique_trade_profile_pubkey(reserved_pubkeys=None):
    reserved = reserved_pubkeys if reserved_pubkeys is not None else set()
    while True:
        candidate = generate_trade_pubkey()
        if candidate in reserved:
            continue
        exists = db.session.query(TradeProfile.id).filter_by(pubkey=candidate).first()
        if exists:
            continue
        reserved.add(candidate)
        return candidate


# ---------------------------------------------------------------------------
# Trade profiles
# ---------------------------------------------------------------------------

def get_user_trade_profiles(user_id):
    try:
        return (
            TradeProfile.query.filter_by(user_id=user_id, is_archived=False)
            .order_by(TradeProfile.name.asc(), TradeProfile.id.asc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        return []


def get_trade_profile_version_snapshot(profile, version_number=None):
    if profile is None:
        return None
    normalized_version = version_number or profile.current_version_number
    version = (
        TradeProfileVersion.query.filter_by(
            trade_profile_id=profile.id,
            version_number=normalized_version,
        )
        .order_by(TradeProfileVersion.id.desc())
        .first()
    )
    if version is not None:
        return version
    return (
        TradeProfileVersion.query.filter_by(trade_profile_id=profile.id)
        .order_by(TradeProfileVersion.version_number.desc(), TradeProfileVersion.id.desc())
        .first()
    )


def get_user_trade_profile_by_pubkey(user_id, profile_pubkey):
    normalized_pubkey = str(profile_pubkey or "").strip()
    if not normalized_pubkey:
        return None
    return TradeProfile.query.filter_by(
        user_id=user_id,
        pubkey=normalized_pubkey,
        is_archived=False,
    ).first()


def resolve_trade_profile_form_state(user_id, trade=None):
    attached_profile = getattr(trade, "trade_profile", None)
    selected_profile_pubkey = ""
    if attached_profile is not None:
        selected_profile_pubkey = attached_profile.pubkey
    return {
        "trade_profile_options": get_user_trade_profiles(user_id),
        "selected_trade_profile_pubkey": selected_profile_pubkey,
    }


def assign_trade_profile_to_trade(user_id, trade, profile_pubkey):
    selected_profile = get_user_trade_profile_by_pubkey(user_id, profile_pubkey)
    if not str(profile_pubkey or "").strip():
        trade.trade_profile = None
        trade.trade_profile_version = None
        trade.trade_profile_id = None
        trade.trade_profile_version_id = None
        return
    if selected_profile is None:
        raise ValueError("Trade profile not found.")
    current_version = get_trade_profile_version_snapshot(selected_profile)
    if current_version is None:
        raise ValueError("Trade profile has no version history.")
    trade.trade_profile = selected_profile
    trade.trade_profile_version = current_version


def create_trade_profile(user_id, name, short_description=None):
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("Trade profile name is required.")
    normalized_description = str(short_description or "").strip() or None
    profile = TradeProfile(
        pubkey=build_unique_trade_profile_pubkey(),
        user_id=user_id,
        name=normalized_name,
        short_description=normalized_description,
        current_version_number=1,
    )
    db.session.add(profile)
    db.session.flush()
    version = TradeProfileVersion(
        trade_profile_id=profile.id,
        version_number=1,
        name=normalized_name,
        short_description=normalized_description,
    )
    db.session.add(version)
    db.session.flush()
    return profile, version


def update_trade_profile(profile, name, short_description=None):
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("Trade profile name is required.")
    normalized_description = str(short_description or "").strip() or None
    current_version = get_trade_profile_version_snapshot(profile)
    if (
        current_version is not None
        and normalized_name == (current_version.name or "")
        and normalized_description == current_version.short_description
    ):
        return current_version

    next_version_number = max(int(profile.current_version_number or 0), 0) + 1
    profile.name = normalized_name
    profile.short_description = normalized_description
    profile.current_version_number = next_version_number
    profile.updated_at = utcnow_naive()
    version = TradeProfileVersion(
        trade_profile_id=profile.id,
        version_number=next_version_number,
        name=normalized_name,
        short_description=normalized_description,
    )
    db.session.add(version)
    db.session.flush()
    return version


# ---------------------------------------------------------------------------
# User deletion / purge
# ---------------------------------------------------------------------------

def delete_users_with_related_data(user_ids):
    normalized_ids = sorted({int(uid) for uid in user_ids if uid is not None})
    if not normalized_ids:
        return 0

    users = (
        User.query.options(
            selectinload(User.trades),
            selectinload(User.trade_accounts),
            selectinload(User.trade_profiles).selectinload(TradeProfile.versions),
            selectinload(User.ai_generated_responses),
        )
        .filter(User.id.in_(normalized_ids))
        .all()
    )
    for user in users:
        db.session.delete(user)
    db.session.flush()
    return len(users)


def purge_expired_unverified_users(app_logger):
    max_age_seconds = env_int("EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS", 86400)
    cutoff = utcnow_naive() - timedelta(seconds=max_age_seconds)
    expired_user_ids = [
        uid
        for uid, in db.session.query(User.id).filter(
            User.email_verified.is_(False),
            User.verification_sent_at.isnot(None),
            User.verification_sent_at < cutoff,
        )
    ]
    if not expired_user_ids:
        return 0

    try:
        deleted_count = delete_users_with_related_data(expired_user_ids)
        db.session.commit()
        return deleted_count
    except (OperationalError, IntegrityError):
        db.session.rollback()
        app_logger.exception("Failed to purge expired unverified users.")
        return 0
