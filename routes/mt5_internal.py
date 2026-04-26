import os
import secrets
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func

from celery_workers.cache import CacheUnavailableError, invalidate
from helpers.app_settings import MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY, get_bool_app_setting
from helpers.celery_dispatch import dispatch_celery_task
from helpers.core import (
    build_normalized_trade_insert_batch,
    queue_bundle_review_if_split_candidates,
    sanitize_error_message,
)
from helpers.weekly_ai_queue import queue_weekly_ai_review_after_ingest
from helpers.utils import utcnow_naive
from models import MT5Account, Trade, TradeBars, db
from trading import get_timezone, parse_float_value, parse_mt5_position_value, parse_source_datetime_value

bp = Blueprint("mt5_internal", __name__)


def _normalize_mt5_position_key(value):
    text_value = str(value or "").strip()
    return text_value or None


def _normalize_vm_id(value):
    text_value = str(value or "").strip()
    return text_value[:64] or None


_SKIP_DEBUG_BENIGN_REASON = "existing_already_closed_or_no_state_change"


def _resolve_skip_debug_mode(payload):
    """
    full: collect up to skip_debug_limit rows for any skip reason (legacy include_skip_debug=True).
    worrisome: collect debug rows for non-benign skips only (rolling / beat sync default).
    off: no per-row skip_debug payload (saves CPU and huge worker logs).
    """
    raw = (payload.get("skip_debug_mode") or "").strip().lower()
    if raw in {"full", "worrisome", "off"}:
        return raw
    if bool(payload.get("include_skip_debug")):
        return "full"
    return "off"


def _append_skip_debug_row(skip_debug_rows, skip_debug_limit, skip_debug_mode, reason_key, row_dict):
    if skip_debug_mode == "off":
        return
    if skip_debug_mode == "worrisome" and reason_key == _SKIP_DEBUG_BENIGN_REASON:
        return
    if len(skip_debug_rows) >= skip_debug_limit:
        return
    skip_debug_rows.append(row_dict)


def _trade_has_complete_m5_coverage(trade, coverage_by_trade_id):
    coverage = coverage_by_trade_id.get(trade.id)
    if not coverage or coverage["bar_count"] <= 0:
        return False
    if trade.opened_at is None or trade.closed_at is None:
        return False
    opened_at_epoch = int(trade.opened_at.replace(tzinfo=timezone.utc).timestamp())
    closed_at_epoch = int(trade.closed_at.replace(tzinfo=timezone.utc).timestamp())
    min_bar_time = coverage["min_bar_time"]
    max_bar_time = coverage["max_bar_time"]
    if min_bar_time is None or max_bar_time is None:
        return False
    bar_tolerance_seconds = 15 * 60
    if int(min_bar_time) > opened_at_epoch + bar_tolerance_seconds:
        return False
    if int(max_bar_time) < closed_at_epoch - bar_tolerance_seconds:
        return False
    return True


def _filter_trades_missing_complete_m5_bars(trades):
    closed_trades = [
        trade
        for trade in trades
        if trade.id is not None and trade.closed_at is not None and trade.mt5_position is not None
    ]
    if not closed_trades:
        return []
    trade_ids = [trade.id for trade in closed_trades]
    bar_coverage_rows = (
        db.session.query(
            TradeBars.trade_id,
            func.count(TradeBars.id).label("bar_count"),
            func.min(TradeBars.bar_time).label("min_bar_time"),
            func.max(TradeBars.bar_time).label("max_bar_time"),
        )
        .filter(TradeBars.trade_id.in_(trade_ids), TradeBars.timeframe == "M5")
        .group_by(TradeBars.trade_id)
        .all()
    )
    coverage_by_trade_id = {
        trade_id: {
            "bar_count": int(bar_count or 0),
            "min_bar_time": min_bar_time,
            "max_bar_time": max_bar_time,
        }
        for trade_id, bar_count, min_bar_time, max_bar_time in bar_coverage_rows
    }
    return [
        trade
        for trade in closed_trades
        if not _trade_has_complete_m5_coverage(trade, coverage_by_trade_id)
    ]


def _queue_auto_trade_bar_sync(account):
    max_tasks_per_sync = 20
    status = {
        "enabled": False,
        "closed_trades": 0,
        "missing_m5": 0,
        "queued": 0,
        "capped": False,
        "max_tasks_per_sync": max_tasks_per_sync,
        "queue": None,
    }
    if not get_bool_app_setting(MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY, False):
        return status
    status["enabled"] = True

    trades = (
        Trade.query.filter(
            Trade.user_id == account.user_id,
            Trade.trade_account_id == account.trade_account_id,
            Trade.closed_at.isnot(None),
            Trade.mt5_position.isnot(None),
        )
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .all()
    )
    status["closed_trades"] = len(trades)
    trades_to_queue = _filter_trades_missing_complete_m5_bars(trades)
    status["missing_m5"] = len(trades_to_queue)
    if not trades_to_queue:
        return status

    from celery_workers.mt5_sync_tasks import MT5_PRIORITY_QUEUE_NAME, fetch_trade_bars

    status["queue"] = MT5_PRIORITY_QUEUE_NAME

    if len(trades_to_queue) > max_tasks_per_sync:
        status["capped"] = True
    for trade in trades_to_queue[:max_tasks_per_sync]:
        dispatch_celery_task(
            fetch_trade_bars,
            args=[account.id, trade.id],
            queue=MT5_PRIORITY_QUEUE_NAME,
            log=current_app.logger,
            label="mt5_auto_bar_sync_after_ingest",
            extra={
                "mt5_account_id": account.id,
                "trade_id": trade.id,
                "user_id": account.user_id,
                "trade_account_id": account.trade_account_id,
            },
        )
        status["queued"] += 1
    return status


def _prefer_existing_mt5_trade(existing_trade, candidate_trade):
    if existing_trade is None:
        return candidate_trade
    if candidate_trade is None:
        return existing_trade

    existing_is_open = existing_trade.closed_at is None
    candidate_is_open = candidate_trade.closed_at is None
    if candidate_is_open != existing_is_open:
        return candidate_trade if candidate_is_open else existing_trade

    existing_opened_at = existing_trade.opened_at or datetime.min
    candidate_opened_at = candidate_trade.opened_at or datetime.min
    if candidate_opened_at != existing_opened_at:
        return candidate_trade if candidate_opened_at > existing_opened_at else existing_trade

    return candidate_trade if (candidate_trade.id or 0) > (existing_trade.id or 0) else existing_trade


def _normalize_sync_trade_rows(raw_rows):
    normalized_rows = []
    invalid_rows = 0

    for row in raw_rows:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue

        source_timezone_name = str(row.get("source_timezone") or "").strip() or None
        source_timezone = get_timezone(source_timezone_name) if source_timezone_name else None
        opened_at, opened_source_timezone = parse_source_datetime_value(
            row.get("opened_at"),
            source_timezone,
            source_timezone_name,
        )
        closed_at, closed_source_timezone = parse_source_datetime_value(
            row.get("closed_at"),
            source_timezone,
            source_timezone_name,
        )
        raw_is_open = row.get("is_open")
        is_open = bool(raw_is_open) if raw_is_open is not None else closed_at is None
        if closed_at is not None:
            is_open = False
        normalized_rows.append(
            {
                "symbol": row.get("symbol"),
                "side": str(row.get("side") or "").strip().upper(),
                "entry_price": parse_float_value(row.get("entry_price")),
                "exit_price": parse_float_value(row.get("exit_price")),
                "lot_size": parse_float_value(row.get("lot_size")),
                "pnl": parse_float_value(row.get("pnl")),
                "commission": parse_float_value(row.get("commission")),
                "swap": parse_float_value(row.get("swap")),
                "stop_loss": parse_float_value(row.get("stop_loss")),
                "take_profit": parse_float_value(row.get("take_profit")),
                "opened_at": opened_at,
                "closed_at": closed_at,
                "mt5_position": parse_mt5_position_value(row.get("mt5_position")),
                "mt5_position_raw": row.get("mt5_position"),
                "system_trade_note": str(row.get("trade_note") or "").strip() or None,
                "source_timezone": (
                    opened_source_timezone
                    or closed_source_timezone
                    or source_timezone_name
                ),
                "is_open": is_open,
            }
        )

    return normalized_rows, invalid_rows


def _positive_float_from_sync_payload(payload, key):
    raw = payload.get(key)
    if raw in {None, ""}:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _account_size_from_mt5_sync_payload(payload):
    """
    Live account value for risk-% analytics: prefer equity (includes open PnL),
    else balance. Both come from MT5 account_info on each sync.
    """
    equity = _positive_float_from_sync_payload(payload, "broker_equity")
    if equity is not None:
        return equity
    return _positive_float_from_sync_payload(payload, "broker_balance")


@bp.route("/api/internal/mt5/sync", methods=["POST"])
def sync_mt5_trades():
    sync_secret = os.getenv("MT5_SYNC_SECRET", "").strip()
    header_secret = request.headers.get("X-Sync-Secret", "").strip()
    if not sync_secret or not secrets.compare_digest(header_secret, sync_secret):
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    include_skip_reasons = bool(payload.get("include_skip_reasons"))
    skip_debug_mode = _resolve_skip_debug_mode(payload)
    refresh_trade_timestamps = bool(payload.get("refresh_closed_trade_timestamps"))
    sync_vm_id = _normalize_vm_id(payload.get("vm_id"))
    history_scope = str(payload.get("history_scope") or "").strip().lower()
    stamp_full_history = history_scope == "full"
    try:
        mt5_account_id = int(payload.get("mt5_account_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid mt5_account_id"}), 400

    raw_rows = payload.get("trades")
    if not isinstance(raw_rows, list):
        return jsonify({"error": "trades must be a list"}), 400

    account = MT5Account.query.filter_by(id=mt5_account_id).first()
    if account is None:
        return jsonify({"error": "mt5 account not found"}), 404
    if not account.is_active:
        return jsonify({"error": "mt5 account is inactive"}), 409
    if not account.trade_account or str(account.trade_account.account_type).strip().upper() != "CFD":
        return jsonify({"error": "mt5 sync requires a CFD trade account"}), 400

    normalized_rows, invalid_rows = _normalize_sync_trade_rows(raw_rows)
    if invalid_rows:
        current_app.logger.warning(
            "MT5 sync normalize rejected rows mt5_account_id=%s invalid=%s of %s",
            mt5_account_id,
            invalid_rows,
            len(raw_rows),
        )

    try:
        skip_reason_counts = {
            "close_only_without_existing_open": 0,
            "existing_already_closed_or_no_state_change": 0,
            "incoming_close_validation_failed": 0,
            "batch_duplicate_mt5_position": 0,
            "batch_validation_skipped": 0,
            "batch_symbol_validation_failed": 0,
        }
        skip_debug_rows = []
        skip_debug_limit = 12
        incoming_positions = {
            _normalize_mt5_position_key(row.get("mt5_position"))
            for row in normalized_rows
            if row.get("mt5_position") is not None
        }
        incoming_positions.discard(None)
        existing_trades = {}
        if incoming_positions:
            for trade in (
                Trade.query.filter_by(
                    user_id=account.user_id,
                    trade_account_id=account.trade_account_id,
                )
                .filter(Trade.mt5_position.in_(incoming_positions))
                .all()
            ):
                position_key = _normalize_mt5_position_key(trade.mt5_position)
                if position_key is None:
                    continue
                existing_trades[position_key] = _prefer_existing_mt5_trade(
                    existing_trades.get(position_key),
                    trade,
                )

        rows_to_insert = []
        updated_count = 0
        skipped_count = 0
        error_count = invalid_rows
        timestamp_refresh_count = 0
        auto_bar_sync_candidate_trade_ids = set()

        for row in normalized_rows:
            mt5_position = _normalize_mt5_position_key(row.get("mt5_position"))
            existing_trade = existing_trades.get(mt5_position) if mt5_position is not None else None
            is_close_only_row = (
                row.get("closed_at") is not None
                and row.get("opened_at") is None
                and row.get("entry_price") is None
            )
            if existing_trade is None:
                if is_close_only_row:
                    skip_reason_counts["close_only_without_existing_open"] += 1
                    _append_skip_debug_row(
                        skip_debug_rows,
                        skip_debug_limit,
                        skip_debug_mode,
                        "close_only_without_existing_open",
                        {
                            "mt5_position": mt5_position,
                            "reason": "close_only_without_existing_open",
                            "incoming_closed_at": (
                                row.get("closed_at").isoformat()
                                if row.get("closed_at") is not None
                                else None
                            ),
                            "incoming_exit_price": row.get("exit_price"),
                        },
                    )
                    skipped_count += 1
                    continue
                rows_to_insert.append(row)
                continue

            if refresh_trade_timestamps:
                row_opened = row.get("opened_at")
                row_closed = row.get("closed_at")
                if existing_trade.closed_at is not None:
                    if (
                        row_opened is not None
                        and row_closed is not None
                        and row_closed >= row_opened
                    ):
                        if (
                            existing_trade.opened_at != row_opened
                            or existing_trade.closed_at != row_closed
                        ):
                            existing_trade.opened_at = row_opened
                            existing_trade.closed_at = row_closed
                            timestamp_refresh_count += 1
                            auto_bar_sync_candidate_trade_ids.add(existing_trade.id)
                    elif (
                        row_opened is None
                        and row_closed is not None
                        and existing_trade.opened_at is not None
                        and row_closed >= existing_trade.opened_at
                        and existing_trade.closed_at != row_closed
                    ):
                        # Some MT5 history windows can emit close-only rows when
                        # entry details are outside the current slice; still refresh
                        # closed_at during explicit recalibration.
                        existing_trade.closed_at = row_closed
                        timestamp_refresh_count += 1
                        auto_bar_sync_candidate_trade_ids.add(existing_trade.id)
                    # Recalibration pass: never treat already-closed rows as generic skips.
                    continue
                if row_opened is not None:
                    if existing_trade.opened_at != row_opened:
                        existing_trade.opened_at = row_opened
                        timestamp_refresh_count += 1
                    if row_closed is None:
                        continue
                    # Open in DB but broker row includes exit — close here; do not fall through
                    # to the normal close branch (same validations, single code path intent).
                    closed_at = row_closed
                    exit_price = row.get("exit_price")
                    if (
                        exit_price is None
                        or exit_price <= 0
                        or (
                            existing_trade.opened_at is not None
                            and closed_at < existing_trade.opened_at
                        )
                    ):
                        error_count += 1
                        skip_reason_counts["incoming_close_validation_failed"] += 1
                        current_app.logger.warning(
                            "MT5 sync rejected incoming close mt5_account_id=%s mt5_position=%s "
                            "exit_price=%s closed_at=%s opened_at=%s",
                            mt5_account_id,
                            mt5_position,
                            exit_price,
                            closed_at,
                            existing_trade.opened_at,
                        )
                        _append_skip_debug_row(
                            skip_debug_rows,
                            skip_debug_limit,
                            skip_debug_mode,
                            "incoming_close_validation_failed",
                            {
                                "reason": "incoming_close_validation_failed",
                                "mt5_position": mt5_position,
                                "detail": "exit_price missing/non-positive or closed_at before opened_at",
                                "incoming_exit_price": exit_price,
                                "incoming_closed_at": closed_at.isoformat()
                                if closed_at is not None
                                else None,
                                "existing_opened_at": (
                                    existing_trade.opened_at.isoformat()
                                    if existing_trade.opened_at is not None
                                    else None
                                ),
                            },
                        )
                        continue
                    existing_trade.exit_price = float(exit_price)
                    existing_trade.pnl = float(row.get("pnl")) if row.get("pnl") is not None else None
                    existing_trade.closed_at = closed_at
                    existing_trade.commission = (
                        float(row.get("commission"))
                        if row.get("commission") is not None
                        else None
                    )
                    existing_trade.swap = (
                        float(row.get("swap"))
                        if row.get("swap") is not None
                        else None
                    )
                    if row.get("system_trade_note"):
                        existing_trade.system_trade_note = row.get("system_trade_note")
                    updated_count += 1
                    auto_bar_sync_candidate_trade_ids.add(existing_trade.id)
                    continue

            if existing_trade.closed_at is None and row.get("closed_at") is None:
                row_pnl = row.get("pnl")
                if row_pnl is not None:
                    previous_pnl = existing_trade.pnl
                    existing_trade.pnl = float(row_pnl)
                    updated_count += 1
                    current_app.logger.info(
                        "MT5 sync DB open running pnl mt5_account_id=%s mt5_position=%s "
                        "written_pnl=%s previous_pnl=%s",
                        mt5_account_id,
                        mt5_position,
                        existing_trade.pnl,
                        previous_pnl,
                    )
                else:
                    current_app.logger.warning(
                        "MT5 sync DB open row skipped no pnl mt5_account_id=%s mt5_position=%s "
                        "symbol=%s",
                        mt5_account_id,
                        mt5_position,
                        row.get("symbol"),
                    )
                    skipped_count += 1
                continue

            if existing_trade.closed_at is None and row.get("closed_at") is not None:
                closed_at = row.get("closed_at")
                exit_price = row.get("exit_price")
                if (
                    exit_price is None
                    or exit_price <= 0
                    or (
                        existing_trade.opened_at is not None
                        and closed_at < existing_trade.opened_at
                    )
                ):
                    error_count += 1
                    skip_reason_counts["incoming_close_validation_failed"] += 1
                    current_app.logger.warning(
                        "MT5 sync rejected incoming close mt5_account_id=%s mt5_position=%s "
                        "exit_price=%s closed_at=%s opened_at=%s",
                        mt5_account_id,
                        mt5_position,
                        exit_price,
                        closed_at,
                        existing_trade.opened_at,
                    )
                    _append_skip_debug_row(
                        skip_debug_rows,
                        skip_debug_limit,
                        skip_debug_mode,
                        "incoming_close_validation_failed",
                        {
                            "reason": "incoming_close_validation_failed",
                            "mt5_position": mt5_position,
                            "detail": "exit_price missing/non-positive or closed_at before opened_at",
                            "incoming_exit_price": exit_price,
                            "incoming_closed_at": closed_at.isoformat()
                            if closed_at is not None
                            else None,
                            "existing_opened_at": (
                                existing_trade.opened_at.isoformat()
                                if existing_trade.opened_at is not None
                                else None
                            ),
                        },
                    )
                    continue

                existing_trade.exit_price = float(exit_price)
                existing_trade.pnl = float(row.get("pnl")) if row.get("pnl") is not None else None
                existing_trade.closed_at = closed_at
                existing_trade.commission = (
                    float(row.get("commission"))
                    if row.get("commission") is not None
                    else None
                )
                existing_trade.swap = (
                    float(row.get("swap"))
                    if row.get("swap") is not None
                    else None
                )
                if row.get("system_trade_note"):
                    existing_trade.system_trade_note = row.get("system_trade_note")
                updated_count += 1
                auto_bar_sync_candidate_trade_ids.add(existing_trade.id)
                continue

            if existing_trade.closed_at is not None and row.get("closed_at") is not None:
                auto_bar_sync_candidate_trade_ids.add(existing_trade.id)

            skip_reason_counts["existing_already_closed_or_no_state_change"] += 1
            _append_skip_debug_row(
                skip_debug_rows,
                skip_debug_limit,
                skip_debug_mode,
                _SKIP_DEBUG_BENIGN_REASON,
                {
                    "mt5_position": mt5_position,
                    "reason": "existing_already_closed_or_no_state_change",
                    "incoming_is_open": row.get("closed_at") is None,
                    "incoming_opened_at": (
                        row.get("opened_at").isoformat()
                        if row.get("opened_at") is not None
                        else None
                    ),
                    "incoming_closed_at": (
                        row.get("closed_at").isoformat()
                        if row.get("closed_at") is not None
                        else None
                    ),
                    "incoming_entry_price": row.get("entry_price"),
                    "incoming_exit_price": row.get("exit_price"),
                    "existing_is_open": existing_trade.closed_at is None,
                    "existing_opened_at": (
                        existing_trade.opened_at.isoformat()
                        if existing_trade.opened_at is not None
                        else None
                    ),
                    "existing_closed_at": (
                        existing_trade.closed_at.isoformat()
                        if existing_trade.closed_at is not None
                        else None
                    ),
                    "existing_entry_price": existing_trade.entry_price,
                    "existing_exit_price": existing_trade.exit_price,
                },
            )
            skipped_count += 1

        batch_result = build_normalized_trade_insert_batch(
            user_id=account.user_id,
            trade_account=account.trade_account,
            rows=rows_to_insert,
            import_signature=None,
            use_import_dedupe_key=False,
            dedupe_by_mt5_position_only=True,
            default_system_trade_note="Auto-imported via MT5 sync",
            fallback_source_timezone=None,
        )
        insert_batch = batch_result["insert_batch"]
        saved_count = len(insert_batch)
        skipped_count += batch_result["duplicate_count"]
        error_count += batch_result["validation_skipped"]
        skip_reason_counts["batch_duplicate_mt5_position"] += int(batch_result["duplicate_count"] or 0)
        skip_reason_counts["batch_validation_skipped"] += int(batch_result["validation_skipped"] or 0)
        skip_reason_counts["batch_symbol_validation_failed"] += len(batch_result["failed_symbols"] or [])

        if int(batch_result.get("validation_skipped") or 0) > 0:
            current_app.logger.warning(
                "MT5 sync insert batch validation: mt5_account_id=%s rows_queued=%s validation_skipped=%s "
                "reasons=%s failed_symbols=%s",
                mt5_account_id,
                len(rows_to_insert),
                batch_result["validation_skipped"],
                batch_result.get("validation_reasons") or {},
                sorted(batch_result.get("failed_symbols") or []),
            )

        if insert_batch:
            db.session.add_all(insert_batch)
        stamp = utcnow_naive()
        account.last_synced_at = stamp
        if stamp_full_history:
            account.last_full_history_sync_at = stamp
        if sync_vm_id is not None:
            account.vm_id = sync_vm_id
        # Reaching this point means MT5 connected and the sync HTTP call succeeded.
        # Clear any stale "failed" connection state so the dashboard/admin don't show
        # a false error label after the account has recovered.
        if account.connection_status != MT5Account.CONNECTION_STATUS_CONNECTED or account.connection_error_message:
            account.connection_status = MT5Account.CONNECTION_STATUS_CONNECTED
            account.connection_error_message = None
        broker_account_size = _account_size_from_mt5_sync_payload(payload)
        if broker_account_size is not None:
            account.trade_account.account_size = broker_account_size
        db.session.commit()
        for inserted_trade in insert_batch:
            if inserted_trade.closed_at is not None and inserted_trade.mt5_position is not None:
                auto_bar_sync_candidate_trade_ids.add(inserted_trade.id)
        auto_bar_sync_status = {
            "enabled": False,
            "closed_trades": 0,
            "missing_m5": 0,
            "queued": 0,
            "capped": False,
            "max_tasks_per_sync": 20,
            "queue": None,
        }
        try:
            auto_bar_sync_status = _queue_auto_trade_bar_sync(account)
        except Exception as exc:
            current_app.logger.warning(
                "MT5 automatic bar sync queue failed mt5_account_id=%s touched_candidates=%s: %s",
                mt5_account_id,
                len(auto_bar_sync_candidate_trade_ids),
                sanitize_error_message(exc),
            )
        auto_bar_sync_queued = int(auto_bar_sync_status.get("queued") or 0)
        current_app.logger.info(
            (
                "MT5 internal sync summary mt5_account_id=%s user_id=%s trade_account_id=%s "
                "incoming_rows=%s normalized_rows=%s incoming_positions=%s saved=%s updated=%s skipped=%s errors=%s "
                "timestamp_refreshes=%s vm_id=%s auto_bar_sync=%s skip_reasons=%s"
            ),
            mt5_account_id,
            account.user_id,
            account.trade_account_id,
            len(raw_rows),
            len(normalized_rows),
            len(incoming_positions),
            saved_count,
            updated_count,
            skipped_count,
            error_count,
            timestamp_refresh_count,
            account.vm_id or "unknown",
            auto_bar_sync_status,
            skip_reason_counts,
        )
        if saved_count or updated_count or timestamp_refresh_count:
            try:
                invalidate(user_id=account.user_id, trade_account_id=account.trade_account_id)
            except CacheUnavailableError as exc:
                current_app.logger.warning(
                    "MT5 sync cache invalidation unavailable for mt5_account_id=%s: %s",
                    mt5_account_id,
                    exc,
                )
            try:
                queue_bundle_review_if_split_candidates(
                    user_id=account.user_id,
                    trade_account_id=account.trade_account_id,
                )
            except Exception as exc:
                current_app.logger.warning(
                    "Bundle review queue after MT5 sync failed for mt5_account_id=%s: %s",
                    mt5_account_id,
                    exc,
                )
            if saved_count or updated_count:
                try:
                    queue_weekly_ai_review_after_ingest(
                        user_id=account.user_id,
                        trade_account_id=account.trade_account_id,
                        log=current_app.logger,
                    )
                except Exception as exc:
                    current_app.logger.warning(
                        "Weekly AI queue after MT5 sync failed for mt5_account_id=%s: %s",
                        mt5_account_id,
                        exc,
                    )

        response_payload = {
            "saved": saved_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "errors": error_count,
        }
        if include_skip_reasons or auto_bar_sync_status.get("enabled"):
            response_payload["auto_bar_sync_queued"] = auto_bar_sync_queued
            response_payload["auto_bar_sync"] = auto_bar_sync_status
        if timestamp_refresh_count:
            response_payload["timestamp_refreshes"] = timestamp_refresh_count
        if include_skip_reasons:
            response_payload["skip_reasons"] = skip_reason_counts
            if int(batch_result.get("validation_skipped") or 0) > 0:
                response_payload["insert_validation_reasons"] = batch_result.get("validation_reasons") or {}
        if skip_debug_rows:
            response_payload["skip_debug"] = skip_debug_rows
        return jsonify(response_payload)
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "MT5 internal sync failed for mt5_account_id=%s.",
            mt5_account_id,
            exc_info=exc,
        )
        return jsonify({"saved": 0, "updated": 0, "skipped": 0, "errors": len(raw_rows) + invalid_rows}), 500


@bp.route("/api/internal/mt5/trade-bars", methods=["POST"])
def ingest_trade_bars():
    sync_secret = os.getenv("MT5_SYNC_SECRET", "").strip()
    header_secret = request.headers.get("X-Sync-Secret", "").strip()
    if not sync_secret or not secrets.compare_digest(header_secret, sync_secret):
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    try:
        mt5_account_id = int(payload.get("mt5_account_id"))
        trade_id = int(payload.get("trade_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid mt5_account_id or trade_id"}), 400

    timeframe = str(payload.get("timeframe") or "").strip()
    if not timeframe:
        return jsonify({"error": "timeframe required"}), 400

    bars = payload.get("bars")
    if not isinstance(bars, list):
        return jsonify({"error": "bars must be a list"}), 400

    account = MT5Account.query.filter_by(id=mt5_account_id).first()
    if account is None:
        return jsonify({"error": "mt5 account not found"}), 404

    trade = Trade.query.filter_by(id=trade_id).first()
    if trade is None:
        return jsonify({"error": "trade not found"}), 404

    if trade.user_id != account.user_id:
        return jsonify({"error": "trade does not belong to this account's user"}), 403

    try:
        TradeBars.query.filter_by(trade_id=trade_id, timeframe=timeframe).delete()
        now = utcnow_naive()
        bar_rows = [
            TradeBars(
                trade_id=trade_id,
                timeframe=timeframe,
                bar_time=int(bar["time"]),
                open=float(bar["open"]),
                high=float(bar["high"]),
                low=float(bar["low"]),
                close=float(bar["close"]),
                tick_volume=bar.get("tick_volume"),
                fetched_at=now,
            )
            for bar in bars
            if isinstance(bar, dict) and bar.get("time") is not None
        ]
        db.session.add_all(bar_rows)
        db.session.commit()
        current_app.logger.info(
            "MT5 trade bars ingested trade_id=%s mt5_account_id=%s timeframe=%s saved=%s",
            trade_id,
            mt5_account_id,
            timeframe,
            len(bar_rows),
        )
        return jsonify({"saved": len(bar_rows), "timeframe": timeframe})
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "trade-bars ingest failed for trade_id=%s mt5_account_id=%s: %s",
            trade_id,
            mt5_account_id,
            exc,
        )
        return jsonify({"error": "internal error"}), 500
