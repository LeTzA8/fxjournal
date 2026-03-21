import os
from datetime import timezone

from flask import Blueprint, current_app, jsonify, request

from helpers.core import build_normalized_trade_insert_batch
from helpers.utils import utcnow_naive
from models import MT5Account, db
from trading import parse_float_value, parse_mt5_position_value, parse_source_datetime_value

bp = Blueprint("mt5_internal", __name__)


def _normalize_sync_trade_rows(raw_rows):
    normalized_rows = []
    invalid_rows = 0

    for row in raw_rows:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue

        source_timezone_name = str(row.get("source_timezone") or "UTC").strip() or "UTC"
        opened_at, opened_source_timezone = parse_source_datetime_value(
            row.get("opened_at"),
            timezone.utc,
            source_timezone_name,
        )
        closed_at, closed_source_timezone = parse_source_datetime_value(
            row.get("closed_at"),
            timezone.utc,
            source_timezone_name,
        )
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
                "trade_note": str(row.get("trade_note") or "").strip() or None,
                "source_timezone": (
                    opened_source_timezone
                    or closed_source_timezone
                    or source_timezone_name
                ),
            }
        )

    return normalized_rows, invalid_rows


@bp.route("/api/internal/mt5/sync", methods=["POST"])
def sync_mt5_trades():
    sync_secret = os.getenv("MT5_SYNC_SECRET", "").strip()
    header_secret = request.headers.get("X-Sync-Secret", "").strip()
    if not sync_secret or header_secret != sync_secret:
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
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
        batch_result = build_normalized_trade_insert_batch(
            user_id=account.user_id,
            trade_account=account.trade_account,
            rows=normalized_rows,
            import_signature=None,
            use_import_dedupe_key=False,
            dedupe_by_mt5_position_only=True,
            default_trade_note="Auto-imported via MT5 sync",
            fallback_source_timezone="UTC",
        )
        insert_batch = batch_result["insert_batch"]
        saved_count = len(insert_batch)
        skipped_count = batch_result["duplicate_count"]
        error_count = invalid_rows + batch_result["validation_skipped"]

        if insert_batch:
            db.session.add_all(insert_batch)
        account.last_synced_at = utcnow_naive()
        db.session.commit()
        return jsonify(
            {
                "saved": saved_count,
                "skipped": skipped_count,
                "errors": error_count,
            }
        )
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "MT5 internal sync failed for mt5_account_id=%s.",
            mt5_account_id,
            exc_info=exc,
        )
        return jsonify({"saved": 0, "skipped": 0, "errors": len(raw_rows) + invalid_rows}), 500
