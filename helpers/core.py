import os
import re
import hashlib
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import g, request, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import load_only, selectinload

from models import (
    MT5AccessRequest,
    MT5Account,
    MT5SyncBatch,
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
    calc_pnl_values,
    canonicalize_symbol,
    get_symbol_options,
    get_trade_level_validation_issues,
    get_timezone,
    normalize_account_type,
    normalize_symbol,
    resolve_pnl,
    to_display_timezone,
)
from .utils import env_bool, env_int, utcnow_naive

SUPPORT_VIEW_TARGET_USER_SESSION_KEY = "support_view_target_user_id"
SUPPORT_VIEW_ADMIN_USER_SESSION_KEY = "support_view_admin_user_id"
SUPPORT_VIEW_ADMIN_USERNAME_SESSION_KEY = "support_view_admin_username"
SUPPORT_VIEW_ACTIVE_TRADE_ACCOUNT_SESSION_KEY = "support_view_active_trade_account_id"


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


def clear_support_view_session():
    for key in (
        SUPPORT_VIEW_TARGET_USER_SESSION_KEY,
        SUPPORT_VIEW_ADMIN_USER_SESSION_KEY,
        SUPPORT_VIEW_ADMIN_USERNAME_SESSION_KEY,
        SUPPORT_VIEW_ACTIVE_TRADE_ACCOUNT_SESSION_KEY,
    ):
        session.pop(key, None)


def is_support_view_session_active():
    return bool(getattr(g, "support_view_session_active", False))


def is_support_view_active():
    return bool(getattr(g, "support_view_active", False))


def get_support_view_target_user():
    return getattr(g, "support_view_target_user", None)


def get_support_view_admin_user():
    return getattr(g, "support_view_admin_user", None)


def get_support_view_admin_username(default="Admin"):
    admin_user = get_support_view_admin_user()
    if admin_user is not None and getattr(admin_user, "username", None):
        return admin_user.username
    return str(session.get(SUPPORT_VIEW_ADMIN_USERNAME_SESSION_KEY) or "").strip() or default


def get_effective_user_id():
    if is_support_view_active():
        target_user = get_support_view_target_user()
        if target_user is not None:
            return target_user.id
    return session.get("user_id")


def get_effective_username(default="User"):
    if is_support_view_active():
        target_user = get_support_view_target_user()
        if target_user is not None and getattr(target_user, "username", None):
            return target_user.username
    return session.get("username", default)


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


def get_trade_size_label(account_type):
    return "Contracts" if normalize_account_type(account_type) == "FUTURES" else "Lots"


def trade_has_close_signal(*, exit_price=None, closed_at=None, pnl=None):
    return closed_at is not None or exit_price is not None


def is_trade_running(trade):
    if trade is None:
        return False
    return not trade_has_close_signal(
        exit_price=getattr(trade, "exit_price", None),
        closed_at=getattr(trade, "closed_at", None),
        pnl=resolve_pnl(trade),
    )


def is_weekly_checkin_complete(checkin):
    if checkin is None:
        return False
    emotional_state = str(getattr(checkin, "emotional_state", "") or "").strip()
    plan_adherence = str(getattr(checkin, "plan_adherence", "") or "").strip()
    execution_quality = str(getattr(checkin, "execution_quality", "") or "").strip()
    return bool(emotional_state and plan_adherence and execution_quality)


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


def build_import_validation_reasons():
    return {
        "missing_mt5_position": 0,
        "invalid_mt5_position": 0,
        "invalid_side": 0,
        "invalid_entry_or_lot": 0,
        "invalid_exit_price": 0,
        "missing_open_time": 0,
        "negative_holding_time": 0,
        "invalid_stop_distance": 0,
        "invalid_stop_loss": 0,
        "invalid_take_profit": 0,
    }


def build_normalized_trade_insert_batch(
    *,
    user_id,
    trade_account,
    rows,
    import_signature=None,
    use_import_dedupe_key=True,
    dedupe_by_mt5_position_only=False,
    default_system_trade_note=None,
    fallback_source_timezone=None,
):
    account_type = normalize_account_type(getattr(trade_account, "account_type", "CFD"))
    allowed_symbols = set(get_symbol_options(account_type))
    failed_symbols = set()
    validation_reasons = build_import_validation_reasons()
    validation_skipped = 0
    duplicate_count = 0

    default_trade_profile_id, default_trade_profile_version_id = (
        resolve_import_default_trade_profile_ids(user_id, trade_account)
    )

    existing_positions = set()
    import_positions = set()
    existing_trade_keys = set()
    import_trade_keys = set()

    if account_type == "FUTURES" and not dedupe_by_mt5_position_only:
        existing_trades = Trade.query.filter_by(
            user_id=user_id,
            trade_account_id=trade_account.id,
        ).options(
            load_only(
                Trade.symbol,
                Trade.contract_code,
                Trade.side,
                Trade.entry_price,
                Trade.exit_price,
                Trade.lot_size,
                Trade.opened_at,
                Trade.closed_at,
                Trade.pnl,
            )
        ).all()
        existing_trade_keys = {
            build_trade_duplicate_key(
                symbol=trade.symbol,
                contract_code=trade.contract_code,
                side=trade.side,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                lot_size=trade.lot_size,
                opened_at=trade.opened_at,
                closed_at=trade.closed_at,
                pnl=resolve_pnl(trade),
            )
            for trade in existing_trades
        }
    else:
        existing_positions = {
            pos
            for (pos,) in db.session.query(Trade.mt5_position)
            .filter_by(
                user_id=user_id,
                trade_account_id=trade_account.id,
            )
            .filter(Trade.mt5_position.isnot(None))
            .all()
            if pos
        }

    insert_batch = []
    reserved_pubkeys = set()

    for row in rows:
        symbol = canonicalize_symbol(row.get("symbol"), account_type)
        side = str(row.get("side") or "").strip().upper()
        lot_size = row.get("lot_size")
        entry_price = row.get("entry_price")
        exit_price = row.get("exit_price")
        pnl = row.get("pnl")
        opened_at = row.get("opened_at")
        closed_at = row.get("closed_at")
        contract_code = row.get("contract_code")
        mt5_position = row.get("mt5_position")
        mt5_position = str(mt5_position).strip() if mt5_position is not None else None
        mt5_position = mt5_position or None
        mt5_position_raw = row.get("mt5_position_raw", mt5_position)

        if symbol not in allowed_symbols:
            if symbol:
                failed_symbols.add(symbol)
            validation_skipped += 1
            continue

        if account_type == "CFD":
            if mt5_position is None:
                validation_skipped += 1
                raw_text = str(mt5_position_raw or "").strip()
                if raw_text:
                    validation_reasons["invalid_mt5_position"] += 1
                else:
                    validation_reasons["missing_mt5_position"] += 1
                continue

        if side not in {"BUY", "SELL"}:
            validation_skipped += 1
            validation_reasons["invalid_side"] += 1
            continue

        if (
            lot_size is None
            or lot_size <= 0
            or entry_price is None
            or entry_price <= 0
        ):
            validation_skipped += 1
            validation_reasons["invalid_entry_or_lot"] += 1
            continue

        if exit_price is not None and exit_price <= 0:
            validation_skipped += 1
            validation_reasons["invalid_exit_price"] += 1
            continue

        if opened_at is None:
            validation_skipped += 1
            validation_reasons["missing_open_time"] += 1
            continue

        if closed_at is not None and closed_at < opened_at:
            validation_skipped += 1
            validation_reasons["negative_holding_time"] += 1
            continue

        validation_issues = get_trade_level_validation_issues(
            entry_price,
            row.get("stop_loss"),
            row.get("take_profit"),
            side,
            symbol,
            instrument_type=account_type,
            contract_code=contract_code,
        )
        if validation_issues["stop_loss_too_close"]:
            validation_skipped += 1
            validation_reasons["invalid_stop_distance"] += 1
            continue
        if validation_issues["invalid_stop_loss_side"]:
            validation_skipped += 1
            validation_reasons["invalid_stop_loss"] += 1
            continue
        if validation_issues["take_profit_too_close"] or validation_issues["invalid_take_profit_side"]:
            validation_skipped += 1
            validation_reasons["invalid_take_profit"] += 1
            continue

        if pnl is None and exit_price is not None:
            pnl = calc_pnl_values(
                symbol,
                side,
                entry_price,
                exit_price,
                lot_size,
                instrument_type=account_type,
                contract_code=contract_code,
            )

        if account_type == "FUTURES" and not dedupe_by_mt5_position_only:
            trade_key = build_trade_duplicate_key(
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
            if trade_key in existing_trade_keys or trade_key in import_trade_keys:
                duplicate_count += 1
                continue
            import_trade_keys.add(trade_key)
        else:
            if mt5_position in existing_positions or mt5_position in import_positions:
                duplicate_count += 1
                continue
            import_positions.add(mt5_position)

        system_trade_note = row.get("system_trade_note")
        if system_trade_note is None:
            system_trade_note = row.get("trade_note")
        system_trade_note_text = (
            str(system_trade_note).strip() if system_trade_note is not None else ""
        )
        source_timezone = str(
            row.get("source_timezone") or fallback_source_timezone or ""
        ).strip() or None
        insert_batch.append(
            Trade(
                pubkey=build_unique_trade_pubkey(reserved_pubkeys),
                user_id=user_id,
                trade_account_id=trade_account.id,
                source_timezone=source_timezone,
                symbol=symbol,
                mt5_position=mt5_position,
                import_signature=import_signature,
                import_dedupe_key=(
                    build_trade_import_dedupe_key(
                        account_type=account_type,
                        symbol=symbol,
                        contract_code=contract_code,
                        side=side,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        lot_size=lot_size,
                        opened_at=opened_at,
                        closed_at=closed_at,
                        pnl=pnl,
                        mt5_position=mt5_position,
                    )
                    if use_import_dedupe_key
                    else None
                ),
                contract_code=contract_code,
                side=side,
                entry_price=float(entry_price),
                exit_price=float(exit_price) if exit_price is not None else None,
                lot_size=float(lot_size),
                pnl=float(pnl) if pnl is not None else None,
                stop_loss=float(row.get("stop_loss")) if row.get("stop_loss") is not None else None,
                take_profit=float(row.get("take_profit")) if row.get("take_profit") is not None else None,
                commission=float(row.get("commission")) if row.get("commission") is not None else None,
                swap=float(row.get("swap")) if row.get("swap") is not None else None,
                opened_at=opened_at,
                closed_at=closed_at,
                trade_note=None,
                system_trade_note=system_trade_note_text or default_system_trade_note,
                trade_profile_id=default_trade_profile_id,
                trade_profile_version_id=default_trade_profile_version_id,
            )
        )

    return {
        "insert_batch": insert_batch,
        "validation_skipped": validation_skipped,
        "duplicate_count": duplicate_count,
        "failed_symbols": failed_symbols,
        "validation_reasons": validation_reasons,
    }


def get_user_trade_accounts(user_id, *, eager_load_default_trade_profile=False):
    query = TradeAccount.query.filter_by(user_id=user_id)
    if eager_load_default_trade_profile:
        query = query.options(selectinload(TradeAccount.default_trade_profile))
    return query.order_by(
        TradeAccount.is_default.desc(), TradeAccount.id.asc()
    ).all()


def resolve_import_default_trade_profile_ids(user_id, trade_account):
    """
    When set and valid, return (trade_profile_id, trade_profile_version_id) for
    newly inserted import/sync trades. Otherwise (None, None).
    """
    raw_id = getattr(trade_account, "default_trade_profile_id", None)
    if not raw_id:
        return None, None
    profile = TradeProfile.query.filter_by(
        id=raw_id, user_id=user_id, is_archived=False
    ).first()
    if profile is None:
        return None, None
    version = get_trade_profile_version_snapshot(profile)
    if version is None:
        return None, None
    return profile.id, version.id


def build_mt5_access_state(user_id, trade_accounts=None):
    account_rows = trade_accounts if trade_accounts is not None else get_user_trade_accounts(user_id)
    account_ids = [account.id for account in account_rows]
    pending_requests_by_trade_account = {}
    approved_requests_by_trade_account = {}
    mt5_accounts_by_trade_account = {}
    linked_mt5_trade_account_ids = set()
    active_mt5_trade_account_ids = set()
    batch_state = get_mt5_sync_batch_state()

    if account_ids:
        mt5_accounts = (
            MT5Account.query.filter(
                MT5Account.trade_account_id.in_(account_ids),
                MT5Account.trade_account_id.isnot(None),
            )
            .order_by(MT5Account.created_at.desc(), MT5Account.id.desc())
            .all()
        )
        for mt5_account in mt5_accounts:
            mt5_accounts_by_trade_account.setdefault(
                mt5_account.trade_account_id,
                mt5_account,
            )
        linked_mt5_trade_account_ids = set(mt5_accounts_by_trade_account.keys())
        active_mt5_trade_account_ids = {
            trade_account_id
            for trade_account_id, mt5_account in mt5_accounts_by_trade_account.items()
            if bool(getattr(mt5_account, "is_active", False))
        }
        request_rows = (
            MT5AccessRequest.query.filter(
                MT5AccessRequest.trade_account_id.in_(account_ids),
                MT5AccessRequest.status.in_(
                    [
                        MT5AccessRequest.STATUS_PENDING,
                        MT5AccessRequest.STATUS_APPROVED,
                    ]
                ),
            )
            .order_by(MT5AccessRequest.created_at.desc(), MT5AccessRequest.id.desc())
            .all()
        )
        for request_row in request_rows:
            if request_row.status == MT5AccessRequest.STATUS_PENDING:
                pending_requests_by_trade_account.setdefault(
                    request_row.trade_account_id,
                    request_row,
                )
                continue
            if request_row.status == MT5AccessRequest.STATUS_APPROVED:
                approved_requests_by_trade_account.setdefault(
                    request_row.trade_account_id,
                    request_row,
                )

    return {
        "pending_requests_by_trade_account": pending_requests_by_trade_account,
        "approved_requests_by_trade_account": approved_requests_by_trade_account,
        "mt5_accounts_by_trade_account": mt5_accounts_by_trade_account,
        "linked_mt5_trade_account_ids": linked_mt5_trade_account_ids,
        "active_mt5_trade_account_ids": active_mt5_trade_account_ids,
        "batch_state": batch_state,
    }


def get_mt5_sync_batch_state(*, for_update=False):
    batches_enabled = db.session.query(MT5SyncBatch.id).limit(1).first() is not None

    query = (
        MT5SyncBatch.query.filter_by(is_open=True)
        .order_by(MT5SyncBatch.created_at.desc(), MT5SyncBatch.id.desc())
    )
    if for_update:
        query = query.with_for_update()
    active_batch = query.first()

    state = {
        "batches_enabled": batches_enabled,
        "active_batch": active_batch,
        "active_slots_used": 0,
        "slots_remaining": None,
        "can_accept_requests": not batches_enabled,
        "status_label": "Legacy Open Access",
        "status_message": "MT5 sync is currently using legacy open access mode.",
        "public_badge": None,
        "request_blocked_message": None,
    }

    if not batches_enabled:
        return state

    if active_batch is None:
        state.update(
            {
                "can_accept_requests": False,
                "status_label": "No Open Batch",
                "status_message": "No free MT5 sync batch is open right now.",
                "public_badge": "No free MT5 sync batch open right now",
                "request_blocked_message": (
                    "No free MT5 sync batch is open right now. Start with file import and check back for the next batch."
                ),
            }
        )
        return state

    active_slots_used = (
        db.session.query(func.count(MT5AccessRequest.id))
        .filter(
            MT5AccessRequest.batch_id == active_batch.id,
            MT5AccessRequest.status.in_(
                [
                    MT5AccessRequest.STATUS_PENDING,
                    MT5AccessRequest.STATUS_APPROVED,
                ]
            ),
        )
        .scalar()
        or 0
    )
    claimed_slots = max(
        int(active_batch.total_slots_claimed or 0),
        int(active_slots_used),
    )
    slots_remaining = max(int(active_batch.capacity_total or 0) - claimed_slots, 0)

    active_batch.active_slots_used = active_slots_used
    active_batch.claimed_slots = claimed_slots
    active_batch.slots_remaining = slots_remaining

    slot_word = "slot" if slots_remaining == 1 else "slots"
    if slots_remaining > 0:
        state.update(
            {
                "active_slots_used": active_slots_used,
                "slots_remaining": slots_remaining,
                "can_accept_requests": True,
                "status_label": "Batch Open",
                "status_message": f"{slots_remaining} free MT5 sync {slot_word} left in {active_batch.name}.",
                "public_badge": f"{slots_remaining} free MT5 sync {slot_word} left",
            }
        )
        return state

    state.update(
        {
            "active_slots_used": active_slots_used,
            "slots_remaining": 0,
            "can_accept_requests": False,
            "status_label": "Batch Full",
            "status_message": f"{active_batch.name} is full right now.",
            "public_badge": "Current free MT5 sync batch is full",
            "request_blocked_message": (
                f"{active_batch.name} is full right now. Start with file import and join the next MT5 sync batch."
            ),
        }
    )
    return state


def queue_mt5_account_cleanup(*, mt5_account, log_context):
    """
    Queue VM cleanup for an MT5 account's terminal/AppData pair when present.

    Returns a short warning string when cleanup could not be queued, otherwise
    ``None``. Missing terminal metadata is treated as a no-op.
    """
    from flask import current_app

    terminal_path = str(getattr(mt5_account, "terminal_path", "") or "").strip()
    appdata_hash = str(getattr(mt5_account, "appdata_hash", "") or "").strip()
    if not terminal_path or not appdata_hash:
        return None

    try:
        from celery_workers.mt5_setup_tasks import cleanup_mt5_terminal

        cleanup_mt5_terminal.apply_async(
            args=[terminal_path, appdata_hash],
            queue="mt5_setup",
        )
    except Exception as exc:
        current_app.logger.warning(
            "MT5 cleanup queue failed for %s mt5_account_id=%s: %s",
            log_context,
            getattr(mt5_account, "id", None),
            sanitize_error_message(exc),
        )
        return "Cleanup could not be queued; terminal files may need manual removal."
    return None


def archive_mt5_account(
    *,
    mt5_account,
    archive_reason=MT5Account.ARCHIVE_REASON_INACTIVITY,
    log_context="archive",
):
    """
    Archive an MT5 account so sync stops and VM files can be removed while the
    DB record and encrypted credentials stay available for reactivation.
    """
    if mt5_account is None:
        return False, "MT5 account not found."
    if mt5_account.is_orphaned:
        return False, "Cleanup-only MT5 records cannot be archived."
    if mt5_account.is_archived:
        return False, "That MT5 account is already archived."
    if not str(mt5_account.investor_password_encrypted or "").strip():
        return (
            False,
            "That MT5 account no longer has saved credentials, so it cannot be archived for reactivation.",
        )

    terminal_exists = bool(
        str(getattr(mt5_account, "terminal_path", "") or "").strip()
        or str(getattr(mt5_account, "appdata_hash", "") or "").strip()
    )
    if not mt5_account.is_active and not terminal_exists:
        return False, "That MT5 account is not active on the VM."

    cleanup_warning = queue_mt5_account_cleanup(
        mt5_account=mt5_account,
        log_context=log_context,
    )

    try:
        mt5_account.is_active = False
        mt5_account.archived_at = utcnow_naive()
        mt5_account.archive_reason = str(archive_reason or "").strip() or None
        mt5_account.terminal_path = None
        mt5_account.appdata_hash = None
        db.session.commit()
    except (OperationalError, IntegrityError):
        db.session.rollback()
        return False, "Could not archive that MT5 account right now. Please try again."

    message = (
        "MT5 sync archived. The saved read-only credentials are kept so it can be reactivated later."
    )
    if cleanup_warning:
        message = f"{message} {cleanup_warning}"
    return True, message


def reactivate_mt5_account(*, mt5_account, log_context="reactivate"):
    """
    Queue MT5 terminal setup again for an archived account and clear its
    archived flag so the UI moves back into the setup flow.
    """
    if mt5_account is None:
        return False, "MT5 account not found."
    if mt5_account.is_orphaned:
        return False, "Cleanup-only MT5 records cannot be reactivated."
    if not mt5_account.is_archived:
        return False, "That MT5 account is not archived."
    if not str(mt5_account.investor_password_encrypted or "").strip():
        return (
            False,
            "Saved MT5 credentials are no longer available for this account, so reactivation is not possible.",
        )

    try:
        from celery_workers.mt5_setup_tasks import setup_mt5_terminal

        setup_mt5_terminal.apply_async(
            args=[mt5_account.id],
            queue="mt5_setup",
        )
    except Exception as exc:
        from flask import current_app

        current_app.logger.warning(
            "MT5 reactivation queue failed for %s mt5_account_id=%s: %s",
            log_context,
            getattr(mt5_account, "id", None),
            sanitize_error_message(exc),
        )
        return (
            False,
            "MT5 reactivation could not be queued right now. Please try again shortly.",
        )

    try:
        mt5_account.archived_at = None
        mt5_account.archive_reason = None
        db.session.commit()
    except (OperationalError, IntegrityError):
        db.session.rollback()
        return False, "MT5 reactivation was queued, but the account state could not be updated cleanly."

    return True, "MT5 reactivation started. We'll email you when your sync is ready again."


def unlink_mt5_sync_for_trade_account(*, user_id, trade_account_id):
    """
    Remove MT5 sync for a trade account: delete MT5Account, clear MT5AccessRequest rows,
    release batch slot counters, queue worker terminal cleanup when paths exist.

    Returns (success, message) for user-facing flash text.
    """
    trade_account = db.session.get(TradeAccount, trade_account_id)
    if trade_account is None or trade_account.user_id != user_id:
        return False, "Trade account not found."

    mt5_account = MT5Account.query.filter_by(
        trade_account_id=trade_account_id,
        user_id=user_id,
    ).first()
    if mt5_account is None:
        return False, "This trade account does not have MT5 sync configured."
    if mt5_account.is_orphaned:
        return False, "MT5 sync is not available for this account."

    cleanup_warning = queue_mt5_account_cleanup(
        mt5_account=mt5_account,
        log_context="user unlink",
    )

    try:
        request_rows = MT5AccessRequest.query.filter_by(
            user_id=user_id,
            trade_account_id=trade_account_id,
        ).all()
        batch_ids = {row.batch_id for row in request_rows if row.batch_id is not None}
        batches_by_id = {}
        if batch_ids:
            for batch_row in db.session.query(MT5SyncBatch).filter(MT5SyncBatch.id.in_(batch_ids)):
                batches_by_id[batch_row.id] = batch_row
        for row in request_rows:
            if row.batch_id is not None:
                batch = batches_by_id.get(row.batch_id)
                if batch is not None:
                    batch.total_slots_claimed = max(
                        0,
                        int(batch.total_slots_claimed or 0) - 1,
                    )
            db.session.delete(row)
        db.session.delete(mt5_account)
        db.session.commit()
    except (OperationalError, IntegrityError):
        db.session.rollback()
        return False, "Could not disconnect MT5 right now. Please try again."

    message = "MT5 sync disconnected for this trade account. Your trades stay in the journal."
    if cleanup_warning:
        message = f"{message} {cleanup_warning}"
    return True, message


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


def resolve_active_trade_account(
    user_id,
    requested_account_id=None,
    requested_account_pubkey=None,
    *,
    session_key="active_trade_account_id",
):
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
                session[session_key] = requested_match.id
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
                session[session_key] = requested_match.id
                return requested_match, accounts

    try:
        active_id = int(str(session.get(session_key, "")).strip())
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
        session[session_key] = active_account.id
    return active_account, accounts


def get_active_trade_account_for_user(user_id):
    active_account = getattr(g, "active_trade_account", None)
    if active_account and active_account.user_id == user_id:
        return active_account
    active_account, _accounts = resolve_active_trade_account(user_id)
    return active_account


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
    return (
        Trade.query.filter_by(
            user_id=user_id,
            pubkey=str(trade_pubkey or "").strip(),
        )
        .options(
            selectinload(Trade.interpretation),
            selectinload(Trade.trade_profile),
            selectinload(Trade.trade_profile_version),
            selectinload(Trade.trade_account),
        )
        .first_or_404()
    )


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


def resolve_user_trade_profile_attachment(user_id, profile_pubkey):
    """
    Returns (TradeProfile, TradeProfileVersion) or (None, None) when clearing pubkey.
    Raises ValueError when pubkey is set but the profile is missing or has no version.
    """
    if not str(profile_pubkey or "").strip():
        return None, None
    selected_profile = get_user_trade_profile_by_pubkey(user_id, profile_pubkey)
    if selected_profile is None:
        raise ValueError("Trade profile not found.")
    current_version = get_trade_profile_version_snapshot(selected_profile)
    if current_version is None:
        raise ValueError("Trade profile has no version history.")
    return selected_profile, current_version


def attach_trade_profile_objects(trade, profile, version):
    if profile is None:
        trade.trade_profile = None
        trade.trade_profile_version = None
        trade.trade_profile_id = None
        trade.trade_profile_version_id = None
        return
    trade.trade_profile = profile
    trade.trade_profile_version = version
    trade.trade_profile_id = profile.id
    trade.trade_profile_version_id = version.id


def assign_trade_profile_to_trade(user_id, trade, profile_pubkey):
    selected_profile, current_version = resolve_user_trade_profile_attachment(user_id, profile_pubkey)
    attach_trade_profile_objects(trade, selected_profile, current_version)


def create_trade_profile(user_id, name, short_description=None):
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("Trade profile name is required.")
    normalized_description = str(short_description or "").strip() or None
    profile = TradeProfile(
        pubkey=build_unique_trade_profile_pubkey(),
        user_id=user_id,
        name=normalized_name,
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


def queue_bundle_review_if_split_candidates(*, user_id, trade_account_id):
    """
    If closed trades on the account look like split-fill / same-setup pairs, set
    bundle_review_requested_at so the dashboard can prompt historical bundle review.

    Intended after bulk ingest (file import, MT5 sync) once trades are committed.

    Returns True when bundle_review_requested_at was updated.
    """
    from helpers.trade_analysis import detect_outliers

    if user_id is None or trade_account_id is None:
        return False
    try:
        closed_trades = (
            Trade.query.filter_by(user_id=user_id, trade_account_id=trade_account_id)
            .options(selectinload(Trade.interpretation))
            .filter(Trade.closed_at.isnot(None))
            .order_by(Trade.closed_at.desc(), Trade.id.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        return False

    outliers = detect_outliers(closed_trades)
    if not outliers.get("bundle_candidates"):
        return False

    account = db.session.get(TradeAccount, trade_account_id)
    if account is None or account.user_id != user_id:
        return False
    account.bundle_review_requested_at = utcnow_naive()
    try:
        db.session.commit()
    except OperationalError:
        db.session.rollback()
        return False
    return True


def delete_users_with_related_data(user_ids):
    normalized_ids = sorted({int(uid) for uid in user_ids if uid is not None})
    if not normalized_ids:
        return 0

    users = (
        User.query.options(
            selectinload(User.trades).selectinload(Trade.interpretation),
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
