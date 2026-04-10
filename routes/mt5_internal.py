import os
import secrets
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request

from celery_workers.cache import CacheUnavailableError, invalidate
from helpers.core import build_normalized_trade_insert_batch, queue_bundle_review_if_split_candidates
from helpers.utils import utcnow_naive
from models import MT5Account, Trade, db
from trading import get_timezone, parse_float_value, parse_mt5_position_value, parse_source_datetime_value

bp = Blueprint("mt5_internal", __name__)


def _normalize_mt5_position_key(value):
    text_value = str(value or "").strip()
    return text_value or None


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


@bp.route("/api/internal/mt5/sync", methods=["POST"])
def sync_mt5_trades():
    sync_secret = os.getenv("MT5_SYNC_SECRET", "").strip()
    header_secret = request.headers.get("X-Sync-Secret", "").strip()
    if not sync_secret or not secrets.compare_digest(header_secret, sync_secret):
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    include_skip_reasons = bool(payload.get("include_skip_reasons"))
    include_skip_debug = bool(payload.get("include_skip_debug"))
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

    try:
        skip_reason_counts = {
            "close_only_without_existing_open": 0,
            "existing_already_closed_or_no_state_change": 0,
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
                    if include_skip_debug and len(skip_debug_rows) < skip_debug_limit:
                        skip_debug_rows.append(
                            {
                                "mt5_position": mt5_position,
                                "reason": "close_only_without_existing_open",
                                "incoming_closed_at": (
                                    row.get("closed_at").isoformat()
                                    if row.get("closed_at") is not None
                                    else None
                                ),
                                "incoming_exit_price": row.get("exit_price"),
                            }
                        )
                    skipped_count += 1
                    continue
                rows_to_insert.append(row)
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
                continue

            skip_reason_counts["existing_already_closed_or_no_state_change"] += 1
            if include_skip_debug and len(skip_debug_rows) < skip_debug_limit:
                skip_debug_rows.append(
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
                    }
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

        if insert_batch:
            db.session.add_all(insert_batch)
        account.last_synced_at = utcnow_naive()
        db.session.commit()
        current_app.logger.info(
            (
                "MT5 internal sync summary mt5_account_id=%s user_id=%s trade_account_id=%s "
                "incoming_rows=%s normalized_rows=%s incoming_positions=%s saved=%s updated=%s skipped=%s errors=%s "
                "skip_reasons=%s"
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
            skip_reason_counts,
        )
        if saved_count or updated_count:
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
        response_payload = {
            "saved": saved_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "errors": error_count,
        }
        if include_skip_reasons:
            response_payload["skip_reasons"] = skip_reason_counts
        if include_skip_debug:
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
