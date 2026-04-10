import hashlib
from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from extensions import limiter
from celery_workers.cache import CacheUnavailableError, invalidate
from helpers.behavior_labels import build_trade_behavior_badge_map
from helpers.core import (
    assign_trade_profile_to_trade,
    build_trade_duplicate_key,
    build_normalized_trade_insert_batch,
    build_unique_trade_pubkey,
    queue_bundle_review_if_split_candidates,
    format_local_datetime_input,
    get_active_trade_account_for_user,
    get_display_timezone_name,
    get_trade_size_label,
    get_user_trade_by_pubkey_or_404,
    get_user_trade_profiles,
    is_trade_running,
    is_local_dev_environment,
    parse_local_datetime_input,
    resolve_trade_profile_form_state,
)
from helpers.trade_analysis import detect_outliers, get_trade_identity
from auth_account import user_has_admin_access
from models import Trade, TradeBars, User, db
from trading import (
    aggregate_ohlc_bars,
    calculate_trade_net_pnl,
    build_import_signature,
    calc_pnl_values,
    canonicalize_symbol,
    classify_trading_session,
    chart_timeframe_bar_seconds,
    derive_exit_price,
    detect_trade_import_profile,
    format_duration_minutes,
    format_trade_price,
    format_trade_size,
    format_trade_symbol,
    get_trade_level_validation_issues,
    get_symbol_options,
    get_trade_account_type,
    normalize_account_type,
    parse_futures_contract_code,
    parse_import_signature_datetime,
    parse_mt5_xlsx_stream,
    parse_tradovate_csv_stream,
    resolve_pips,
    resolve_pnl,
    resolve_ticks,
    ensure_utc_aware,
    to_display_timezone,
)
from helpers.utils import login_required, utcnow_naive

bp = Blueprint("trades", __name__)


def _calculate_trade_risk_reward(target_price, entry_price, stop_loss, side=None, *, signed=False):
    if target_price is None or entry_price is None or stop_loss is None:
        return None
    normalized_side = str(side or "BUY").strip().upper()
    if normalized_side == "SELL":
        if stop_loss <= entry_price:
            return None
        move_amount = entry_price - target_price
    else:
        if stop_loss >= entry_price:
            return None
        move_amount = target_price - entry_price
    risk_amount = abs(entry_price - stop_loss)
    if risk_amount == 0:
        return None
    if signed:
        return move_amount / risk_amount
    if move_amount <= 0:
        return None
    return move_amount / risk_amount


def _calculate_trade_net_pnl(trade_pnl, commission=None, swap=None):
    return calculate_trade_net_pnl(trade_pnl, commission, swap)


def _build_trade_entry_context(user_id, trade):
    trade_account = trade.trade_account or get_active_trade_account_for_user(user_id)
    profile_form_state = resolve_trade_profile_form_state(user_id, trade=trade)
    return {
        "username": session.get("username", "User"),
        "active_trade_account_name": trade_account.name,
        "symbol_options": get_symbol_options(trade_account.account_type, trade.symbol),
        "account_type": normalize_account_type(trade_account.account_type),
        "size_label": get_trade_size_label(trade_account.account_type),
        "trade": trade,
        "opened_at_value": format_local_datetime_input(trade.opened_at),
        "closed_at_value": format_local_datetime_input(trade.closed_at),
        "trade_is_running": is_trade_running(trade),
        "analytics_timezone": get_display_timezone_name(),
        "trade_profile_options": profile_form_state["trade_profile_options"],
        "selected_trade_profile_pubkey": profile_form_state[
            "selected_trade_profile_pubkey"
        ],
    }


def _validate_trade_submission(
    *,
    symbol,
    contract_code,
    account_type,
    side,
    entry_price,
    exit_price,
    lot_size,
    stop_loss,
    take_profit,
    opened_at,
    closed_at,
):
    if lot_size is None or lot_size <= 0:
        return "Lot size must be greater than zero."
    if exit_price is not None and exit_price <= 0:
        return "Exit price must be greater than zero."
    if closed_at is not None and opened_at is not None and closed_at < opened_at:
        return "Close time cannot be earlier than open time."

    validation_issues = get_trade_level_validation_issues(
        entry_price,
        stop_loss,
        take_profit,
        side,
        symbol,
        instrument_type=account_type,
        contract_code=contract_code,
    )
    if validation_issues["stop_loss_too_close"]:
        return "Stop loss cannot be at or effectively equal to entry."
    if validation_issues["take_profit_too_close"]:
        return "Take profit cannot be at or effectively equal to entry."
    if validation_issues["invalid_stop_loss_side"]:
        return "Stop loss must be below entry for buys and above entry for sells."
    if validation_issues["invalid_take_profit_side"]:
        return "Take profit must be above entry for buys and below entry for sells."

    return None


def _find_duplicate_trade(
    *,
    user_id,
    trade_account_id,
    symbol,
    contract_code,
    side,
    entry_price,
    exit_price,
    lot_size,
    opened_at,
    closed_at,
    pnl,
    exclude_trade_id=None,
):
    candidate_key = build_trade_duplicate_key(
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
    existing_trades = Trade.query.filter_by(
        user_id=user_id,
        trade_account_id=trade_account_id,
    ).filter(
        Trade.symbol == symbol,
        Trade.opened_at == opened_at,
    ).all()
    for existing_trade in existing_trades:
        if exclude_trade_id is not None and existing_trade.id == exclude_trade_id:
            continue
        existing_key = build_trade_duplicate_key(
            symbol=existing_trade.symbol,
            contract_code=existing_trade.contract_code,
            side=existing_trade.side,
            entry_price=existing_trade.entry_price,
            exit_price=existing_trade.exit_price,
            lot_size=existing_trade.lot_size,
            opened_at=existing_trade.opened_at,
            closed_at=existing_trade.closed_at,
            pnl=resolve_pnl(existing_trade),
        )
        if existing_key == candidate_key:
            return existing_trade
    return None


def _invalidate_trade_caches(user_id, trade_account_id):
    try:
        invalidate(user_id=user_id, trade_account_id=trade_account_id)
    except CacheUnavailableError:
        return


def _is_bundle_review_pending(trade_account):
    if trade_account is None:
        return False
    requested_at = getattr(trade_account, "bundle_review_requested_at", None)
    completed_at = getattr(trade_account, "bundle_review_completed_at", None)
    return requested_at is not None and (completed_at is None or completed_at < requested_at)


def _bundle_group_hash(pubkeys):
    normalized_pubkeys = sorted(
        {
            str(pubkey or "").strip()
            for pubkey in pubkeys
            if str(pubkey or "").strip()
        }
    )
    if not normalized_pubkeys:
        return ""
    return hashlib.md5(",".join(normalized_pubkeys).encode("utf-8")).hexdigest()[:8]


def _bundle_group_value(pubkeys):
    normalized_pubkeys = sorted(
        {
            str(pubkey or "").strip()
            for pubkey in pubkeys
            if str(pubkey or "").strip()
        }
    )
    return ",".join(normalized_pubkeys)


def _build_bundle_review_candidates(bundle_candidates):
    review_rows = []
    for candidate in bundle_candidates:
        trades = candidate.get("trades") or []
        trade_pubkeys = [
            str(getattr(trade, "pubkey", "") or "").strip()
            for trade in trades
            if str(getattr(trade, "pubkey", "") or "").strip()
        ]
        review_rows.append(
            {
                **candidate,
                "group_hash": _bundle_group_hash(trade_pubkeys),
                "group_value": _bundle_group_value(trade_pubkeys),
            }
        )
    return review_rows


def render_trades_page(*, manage_mode=False):
    username = session.get("username", "User")
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    try:
        user_trades = (
            Trade.query.filter_by(
                user_id=user_id,
                trade_account_id=active_trade_account.id,
            )
            .options(
                selectinload(Trade.trade_profile),
                selectinload(Trade.trade_profile_version),
            )
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        user_trades = []

    trade_rows = []
    size_label = get_trade_size_label(active_trade_account.account_type)
    timezone_name = get_display_timezone_name()
    behavior_badge_map = build_trade_behavior_badge_map(user_trades)
    for trade in user_trades:
        trade_is_running = is_trade_running(trade)
        trade_account_type = get_trade_account_type(trade)
        opened_at_local = to_display_timezone(trade.opened_at, timezone_name)
        trade_profile = getattr(trade, "trade_profile", None)
        trade_profile_version = getattr(trade, "trade_profile_version", None)
        duration_minutes = None
        if trade.opened_at is not None:
            if trade.closed_at is not None:
                duration_end = trade.closed_at
            elif trade_is_running:
                duration_end = utcnow_naive()
            else:
                duration_end = None
            if duration_end is not None and duration_end >= trade.opened_at:
                duration_minutes = (
                    duration_end - trade.opened_at
                ).total_seconds() / 60.0
        trade_rows.append(
            {
                "id": trade.id,
                "pubkey": trade.pubkey,
                "date_label": opened_at_local.strftime("%d %b %Y")
                if opened_at_local
                else "-",
                "symbol": format_trade_symbol(trade),
                "trade_profile_label": (
                    trade_profile_version.name
                    if trade_profile_version is not None
                    else (trade_profile.name if trade_profile is not None else "-")
                ),
                "side": trade.side,
                "entry_price": trade.entry_price,
                "entry_price_display": format_trade_price(
                    trade.entry_price,
                    trade.symbol,
                    instrument_type=trade_account_type,
                    contract_code=trade.contract_code,
                ),
                "exit_price": trade.exit_price,
                "exit_price_display": format_trade_price(
                    trade.exit_price,
                    trade.symbol,
                    instrument_type=trade_account_type,
                    contract_code=trade.contract_code,
                ),
                "lot_size": trade.lot_size,
                "size_display": format_trade_size(trade.lot_size, trade_account_type),
                "pnl": resolve_pnl(trade),
                "pips": resolve_pips(trade),
                "ticks": resolve_ticks(trade),
                "opened_at": trade.opened_at,
                "opened_at_value": opened_at_local.isoformat() if opened_at_local else "",
                "opened_date_value": opened_at_local.strftime("%Y-%m-%d")
                if opened_at_local
                else "",
                "closed_at": trade.closed_at,
                "session_label": classify_trading_session(trade.opened_at)
                if trade.opened_at
                else "-",
                "duration_label": format_duration_minutes(duration_minutes),
                "is_running": trade_is_running,
                "is_revenge": bool(getattr(trade, "is_revenge", False)),
                "is_reactive": bool(getattr(trade, "is_reactive", False)),
                "is_corrective": bool(getattr(trade, "is_corrective", False)),
                "bundle_pubkey": getattr(trade, "bundle_pubkey", None),
                "behavior_badges": behavior_badge_map.get(get_trade_identity(trade), []),
            }
        )

    import_batch_map = {}
    for trade in user_trades:
        signature = (trade.import_signature or "").strip()
        if not signature:
            continue
        if signature not in import_batch_map:
            imported_at = parse_import_signature_datetime(signature)
            import_batch_map[signature] = {
                "signature": signature,
                "trade_count": 0,
                "imported_at": imported_at,
                "imported_at_label": (
                    imported_at.strftime("%Y-%m-%d %H:%M UTC")
                    if imported_at
                    else "Unknown import time"
                ),
            }
        import_batch_map[signature]["trade_count"] += 1

    import_batches = sorted(
        import_batch_map.values(),
        key=lambda batch: batch["imported_at"] or datetime.min,
        reverse=True,
    )

    return render_template(
        "trades.html",
        title="MyFXJournal | My Trades",
        username=username,
        trades=trade_rows,
        size_label=size_label,
        import_batches=import_batches,
        manage_mode=manage_mode,
        trade_profile_options=get_user_trade_profiles(user_id),
    )


@bp.route("/dashboard/trades")
@login_required
def trades():
    return render_trades_page(manage_mode=False)


@bp.route("/dashboard/trades/manage")
@login_required
def manage_trades():
    return render_trades_page(manage_mode=True)


@bp.route("/dashboard/trades/bulk-delete", methods=["POST"])
@limiter.limit(
    "6 per minute",
    methods=["POST"],
    error_message="Too many trade deletion attempts. Please wait and try again.",
)
@login_required
def bulk_delete_trades():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    selected_pubkeys = [
        value.strip()
        for value in request.form.getlist("trade_pubkeys")
        if value and value.strip()
    ]

    if not selected_pubkeys:
        flash("Please select at least one trade to delete.", "error")
        return redirect(url_for("trades.manage_trades"))

    try:
        trades_to_delete = (
            Trade.query.filter(
                Trade.user_id == user_id,
                Trade.trade_account_id == active_trade_account.id,
                Trade.pubkey.in_(selected_pubkeys),
            ).all()
        )
        deleted = len(trades_to_delete)
        for trade in trades_to_delete:
            db.session.delete(trade)
        db.session.commit()
        _invalidate_trade_caches(user_id, active_trade_account.id)
    except (OperationalError, IntegrityError):
        db.session.rollback()
        flash("Could not delete the selected trades right now. Please try again.", "error")
        return redirect(url_for("trades.manage_trades"))

    status = "success" if deleted > 0 else "info"
    message = (
        f"Deleted {deleted} selected trade{'s' if deleted != 1 else ''}."
        if deleted > 0
        else "No matching trades were found in the selected rows."
    )
    flash(message, status)
    return redirect(url_for("trades.manage_trades"))


@bp.route("/dashboard/trades/batch-profile", methods=["POST"])
@limiter.limit(
    "8 per minute",
    methods=["POST"],
    error_message="Too many batch profile updates. Please wait and try again.",
)
@login_required
def batch_update_trade_profile():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    selected_pubkeys = [
        value.strip()
        for value in request.form.getlist("trade_pubkeys")
        if value and value.strip()
    ]
    selected_profile_pubkey = request.form.get("trade_profile_pubkey", "").strip()

    if not selected_pubkeys:
        flash("Please select at least one trade to update.", "error")
        return redirect(url_for("trades.manage_trades"))

    try:
        trades_to_update = (
            Trade.query.filter(
                Trade.user_id == user_id,
                Trade.trade_account_id == active_trade_account.id,
                Trade.pubkey.in_(selected_pubkeys),
            ).all()
        )
        if not trades_to_update:
            flash("No trades matched the selected rows.", "info")
            return redirect(url_for("trades.manage_trades"))

        for trade in trades_to_update:
            assign_trade_profile_to_trade(user_id, trade, selected_profile_pubkey)

        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("trades.manage_trades"))
    except (OperationalError, IntegrityError):
        db.session.rollback()
        flash("Could not update the selected strategies right now.", "error")
        return redirect(url_for("trades.manage_trades"))

    action_label = "cleared" if not selected_profile_pubkey else "updated"
    flash(
        f"Strategy {action_label} for {len(trades_to_update)} trade"
        f"{'s' if len(trades_to_update) != 1 else ''}.",
        "success",
    )
    return redirect(url_for("trades.manage_trades"))


@bp.route("/dashboard/trades/new", methods=["GET", "POST"])
@login_required
def new_trade():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)

    if request.method == "POST":
        input_timezone_name = get_display_timezone_name()
        account_type = normalize_account_type(active_trade_account.account_type)
        allowed_symbols = set(get_symbol_options(account_type))
        symbol = canonicalize_symbol(request.form.get("symbol", ""), account_type)
        contract_code = (
            request.form.get("contract_code", "") or ""
        ).strip().upper() or None
        if symbol not in allowed_symbols:
            return redirect(url_for("trades.new_trade"))
        if account_type == "FUTURES" and contract_code:
            parsed_contract = parse_futures_contract_code(
                contract_code, expected_root=symbol
            )
            if parsed_contract is None:
                return redirect(url_for("trades.new_trade"))
            contract_code = parsed_contract["contract_code"]
        elif account_type != "FUTURES":
            contract_code = None
        side = request.form.get("side", "BUY").strip().upper()
        status = request.form.get("status", "Running").strip().lower()
        entry_price = float(request.form.get("entry_price", 0.0))
        exit_price = request.form.get("exit_price", "").strip()
        exit_price = float(exit_price) if exit_price else None
        lot_size = float(request.form.get("lot_size", 0.01))
        trade_note = request.form.get("trade_note", "").strip()
        is_revenge = request.form.get("is_revenge") == "on"
        is_corrective = request.form.get("is_corrective") == "on"
        is_reactive = request.form.get("is_reactive") == "on"
        pnl = request.form.get("pnl", "").strip()
        pnl = float(pnl) if pnl else None
        stop_loss = request.form.get("stop_loss", "").strip()
        stop_loss = float(stop_loss) if stop_loss else None
        take_profit = request.form.get("take_profit", "").strip()
        take_profit = float(take_profit) if take_profit else None
        commission = request.form.get("commission", "").strip()
        commission = float(commission) if commission else None
        swap = request.form.get("swap", "").strip()
        swap = float(swap) if swap else None
        opened_at = parse_local_datetime_input(request.form.get("opened_at", "").strip())
        if opened_at is None:
            opened_at = parse_local_datetime_input(
                request.form.get("trade_date", "").strip()
            )
        if opened_at is None:
            opened_at = utcnow_naive()
        closed_at = parse_local_datetime_input(request.form.get("closed_at", "").strip())
        if status != "closed":
            closed_at = None
        if closed_at is not None and closed_at < opened_at:
            flash("Close time cannot be earlier than open time.", "error")
            return redirect(url_for("trades.new_trade"))

        if pnl is not None and exit_price is None:
            exit_price = derive_exit_price(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                lot_size=lot_size,
                pnl_value=pnl,
                instrument_type=account_type,
                contract_code=contract_code,
            )
        if pnl is None and exit_price is not None:
            pnl = calc_pnl_values(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                lot_size=lot_size,
                instrument_type=account_type,
                contract_code=contract_code,
            )
        validation_message = _validate_trade_submission(
            symbol=symbol,
            contract_code=contract_code,
            account_type=account_type,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            lot_size=lot_size,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        if validation_message:
            flash(validation_message, "error")
            return redirect(url_for("trades.new_trade"))
        duplicate_trade = _find_duplicate_trade(
            user_id=user_id,
            trade_account_id=active_trade_account.id,
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
        if duplicate_trade is not None:
            flash("A matching trade already exists on this account.", "error")
            return redirect(url_for("trades.new_trade"))
        trade = Trade(
            pubkey=build_unique_trade_pubkey(),
            user_id=user_id,
            trade_account_id=active_trade_account.id,
            source_timezone=input_timezone_name,
            symbol=symbol,
            contract_code=contract_code,
            side=side,
            entry_price=entry_price,
            lot_size=lot_size,
            trade_note=trade_note,
            is_revenge=is_revenge,
            is_corrective=is_corrective,
            is_reactive=is_reactive,
            pnl=pnl,
            stop_loss=stop_loss,
            take_profit=take_profit,
            commission=commission,
            swap=swap,
            exit_price=exit_price,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        try:
            assign_trade_profile_to_trade(
                user_id,
                trade,
                request.form.get("trade_profile_pubkey", ""),
            )
        except ValueError:
            return redirect(url_for("trades.new_trade"))
        db.session.add(trade)
        db.session.commit()
        _invalidate_trade_caches(user_id, active_trade_account.id)
        return redirect(url_for("trades.trades"))

    profile_form_state = resolve_trade_profile_form_state(user_id)
    first_import_nudge = session.pop("first_import_nudge", None)

    return render_template(
        "trade_entry.html",
        title="MyFXJournal | New Trade",
        username=session.get("username", "User"),
        active_trade_account_name=active_trade_account.name,
        symbol_options=get_symbol_options(active_trade_account.account_type),
        account_type=normalize_account_type(active_trade_account.account_type),
        size_label=get_trade_size_label(active_trade_account.account_type),
        trade=None,
        trade_display_symbol="",
        form_action=url_for("trades.new_trade"),
        form_mode="new",
        opened_at_value="",
        closed_at_value="",
        trade_is_running=True,
        analytics_timezone=get_display_timezone_name(),
        trade_profile_options=profile_form_state["trade_profile_options"],
        selected_trade_profile_pubkey=profile_form_state[
            "selected_trade_profile_pubkey"
        ],
        first_import_nudge=first_import_nudge,
    )


@bp.route("/dashboard/import", methods=["POST"])
@login_required
def import_trade_file():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_type = normalize_account_type(active_trade_account.account_type)
    existing_import_count = (
        Trade.query.filter_by(user_id=user_id)
        .filter(Trade.import_signature.isnot(None))
        .count()
    )
    import_stage = "request_validation"
    detected_profile = None
    parser_name = None
    total_rows = 0
    skipped_rows = 0
    parsed_rows = []
    validation_skipped = 0
    duplicate_count = 0
    import_signature = None
    current_row_index = None
    current_row_context = None

    uploaded_file = request.files.get("mt5_file")
    if not uploaded_file or not uploaded_file.filename:
        flash(
            "Please choose a Tradovate CSV file to upload."
            if account_type == "FUTURES"
            else "Please choose an MT5 XLSX file to upload.",
            "error",
        )
        return redirect(url_for("trades.new_trade"))

    try:
        import_stage = "detect_profile"
        uploaded_file.stream.seek(0)
        detected_profile = detect_trade_import_profile(uploaded_file.stream)
        if detected_profile is None:
            flash(
                "We could not recognize that import file. Please use an MT5 Positions workbook or a Tradovate Performance CSV.",
                "error",
            )
            return redirect(url_for("trades.new_trade"))

        detected_account_type = normalize_account_type(
            detected_profile.get("account_type")
        )
        if detected_account_type != account_type:
            flash(
                f"This file was identified as a {detected_profile.get('platform', 'unknown')} "
                f"{detected_profile.get('market_type', 'import')} import, "
                f"but the active account is set to {account_type.title()}.",
                "error",
            )
            return redirect(url_for("trades.new_trade"))

        uploaded_file.stream.seek(0)
        parser_name = detected_profile.get("parser")
        import_stage = f"parse_{parser_name or 'unknown'}"
        if parser_name == "tradovate_csv":
            parsed_rows, total_rows, skipped_rows = parse_tradovate_csv_stream(
                uploaded_file.stream
            )
        else:
            parsed_rows, total_rows, skipped_rows = parse_mt5_xlsx_stream(
                uploaded_file.stream
            )
        if not parsed_rows:
            flash(
                "No valid trade rows were found in this Tradovate CSV."
                if account_type == "FUTURES"
                else "No valid trade rows were found in this MT5 file.",
                "error",
            )
            return redirect(url_for("trades.new_trade"))

        import_stage = "build_insert_batch"
        import_signature = build_import_signature(
            "tradovate" if account_type == "FUTURES" else "mt5"
        )
        batch_result = build_normalized_trade_insert_batch(
            user_id=user_id,
            trade_account=active_trade_account,
            rows=parsed_rows,
            import_signature=import_signature,
            use_import_dedupe_key=True,
            dedupe_by_mt5_position_only=False,
            default_system_trade_note=(
                "Imported from Tradovate Performance CSV"
                if account_type == "FUTURES"
                else "Imported from MT5 Positions"
            ),
        )
        insert_batch = batch_result["insert_batch"]
        validation_skipped = batch_result["validation_skipped"]
        duplicate_count = batch_result["duplicate_count"]
        failed_symbols = batch_result["failed_symbols"]
        validation_reasons = batch_result["validation_reasons"]

        if not insert_batch:
            symbol_msg = ""
            if failed_symbols:
                failed_list = sorted(failed_symbols)
                shown = ", ".join(failed_list[:8])
                extra = len(failed_list) - 8
                if extra > 0:
                    shown = f"{shown}, +{extra} more"
                symbol_msg = f" Unrecognized symbols: {shown}."
            flash(
                f"No trades were imported. Parsed {len(parsed_rows)} rows and "
                f"skipped {skipped_rows + validation_skipped + duplicate_count}."
                f"{symbol_msg}",
                "error",
            )
            return redirect(url_for("trades.new_trade"))

        import_stage = "commit_import"
        db.session.add_all(insert_batch)
        db.session.commit()
        _invalidate_trade_caches(user_id, active_trade_account.id)
        if queue_bundle_review_if_split_candidates(
            user_id=user_id,
            trade_account_id=active_trade_account.id,
        ):
            flash(
                "Possible split fills detected. Use Bundle Review on the dashboard to confirm them "
                "so stats and weekly review stay accurate.",
                "info",
            )
        if existing_import_count == 0:
            session["first_import_nudge"] = {
                "imported_count": len(insert_batch),
                "has_closed_trades": any(trade.closed_at is not None for trade in insert_batch),
            }

        status = "success"
        if failed_symbols:
            status = "error"
        elif skipped_rows or validation_skipped or duplicate_count:
            status = "info"

        symbol_msg = ""
        if failed_symbols:
            failed_list = sorted(failed_symbols)
            shown = ", ".join(failed_list[:8])
            extra = len(failed_list) - 8
            if extra > 0:
                shown = f"{shown}, +{extra} more"
            symbol_msg = f" Unrecognized symbols skipped: {shown}."

        uncertain_bits = []
        if validation_reasons["missing_mt5_position"]:
            uncertain_bits.append(
                f"missing MT5 position {validation_reasons['missing_mt5_position']}"
            )
        if validation_reasons["invalid_mt5_position"]:
            uncertain_bits.append(
                f"invalid MT5 position {validation_reasons['invalid_mt5_position']}"
            )
        if validation_reasons["invalid_side"]:
            uncertain_bits.append(f"invalid side {validation_reasons['invalid_side']}")
        if validation_reasons["invalid_entry_or_lot"]:
            uncertain_bits.append(
                f"invalid entry/lot {validation_reasons['invalid_entry_or_lot']}"
            )
        if validation_reasons["invalid_exit_price"]:
            uncertain_bits.append(
                f"invalid exit price {validation_reasons['invalid_exit_price']}"
            )
        if validation_reasons["missing_open_time"]:
            uncertain_bits.append(
                f"missing open time {validation_reasons['missing_open_time']}"
            )
        if validation_reasons["negative_holding_time"]:
            uncertain_bits.append(
                f"negative holding time {validation_reasons['negative_holding_time']}"
            )
        if validation_reasons["invalid_stop_distance"]:
            uncertain_bits.append(
                f"stop loss too close {validation_reasons['invalid_stop_distance']}"
            )
        if validation_reasons["invalid_stop_loss"]:
            uncertain_bits.append(
                f"stop loss wrong side {validation_reasons['invalid_stop_loss']}"
            )
        if validation_reasons["invalid_take_profit"]:
            uncertain_bits.append(
                f"take profit invalid {validation_reasons['invalid_take_profit']}"
            )
        uncertain_msg = ""
        if uncertain_bits:
            uncertain_msg = " Validation details: " + ", ".join(uncertain_bits) + "."

        flash(
            f"Imported {len(insert_batch)} trade{'s' if len(insert_batch) != 1 else ''} from "
            f"{'Tradovate CSV' if account_type == 'FUTURES' else 'Positions'}. "
            f"Parsed rows: {len(parsed_rows)}/{total_rows}. "
            f"Skipped: {skipped_rows + validation_skipped}. "
            f"{'Duplicate trades' if account_type == 'FUTURES' else 'Duplicate positions'}: {duplicate_count}."
            f" Import signature: {import_signature}."
            f"{symbol_msg}"
            f"{uncertain_msg}",
            status,
        )
        return redirect(url_for("trades.new_trade"))
    except IntegrityError as exc:
        db.session.rollback()
        current_app.logger.warning("Trade import blocked by integrity constraint: %s", exc)
        flash(
            "Some trades in this upload already exist on the active account. The import was stopped to avoid creating duplicates.",
            "info",
        )
        return redirect(url_for("trades.new_trade"))
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "Trade import failed. stage=%s user_id=%s trade_account_id=%s account_type=%s "
            "filename=%r parser=%r detected_profile=%r parsed_rows=%s total_rows=%s skipped_rows=%s "
            "validation_skipped=%s duplicate_count=%s import_signature=%r current_row_index=%r "
            "current_row_context=%r",
            import_stage,
            user_id,
            getattr(active_trade_account, "id", None),
            account_type,
            getattr(uploaded_file, "filename", None),
            parser_name,
            detected_profile,
            len(parsed_rows) if parsed_rows is not None else None,
            total_rows,
            skipped_rows,
            validation_skipped,
            duplicate_count,
            import_signature,
            current_row_index,
            current_row_context,
            exc_info=exc,
        )
        extra_detail = ""
        if is_local_dev_environment():
            extra_detail = f" Details: {str(exc).strip()[:240]}"
        flash(
            (
                "We could not process that upload. Please check the Tradovate CSV format and try again."
                if account_type == "FUTURES"
                else "We could not process that upload. Please check the MT5 export format and try again."
            )
            + extra_detail,
            "error",
        )
        return redirect(url_for("trades.new_trade"))


@bp.route("/dashboard/trades/<string:trade_pubkey>")
@login_required
def trade_detail(trade_pubkey):
    user_id = session["user_id"]

    trade = get_user_trade_by_pubkey_or_404(user_id, trade_pubkey)
    trade_pnl = resolve_pnl(trade)
    trade_pips = resolve_pips(trade)
    trade_ticks = resolve_ticks(trade)
    planned_rr = _calculate_trade_risk_reward(
        trade.take_profit,
        trade.entry_price,
        trade.stop_loss,
        trade.side,
    )
    actual_rr = _calculate_trade_risk_reward(
        trade.exit_price,
        trade.entry_price,
        trade.stop_loss,
        trade.side,
        signed=True,
    )
    trade_net_pnl = _calculate_trade_net_pnl(
        trade_pnl,
        trade.commission,
        trade.swap,
    )
    trade_account_type = get_trade_account_type(trade)
    timezone_name = get_display_timezone_name()
    opened_at_local = to_display_timezone(trade.opened_at, timezone_name)
    closed_at_local = to_display_timezone(trade.closed_at, timezone_name)
    trade_profile = getattr(trade, "trade_profile", None)
    trade_profile_version = getattr(trade, "trade_profile_version", None)

    base = _build_trade_entry_context(user_id, trade)
    return render_template(
        "trade_entry.html",
        title="MyFXJournal | Trade Detail",
        form_mode="view",
        form_action="",
        trade_display_symbol=format_trade_symbol(trade),
        trade_pnl=trade_pnl,
        trade_net_pnl=trade_net_pnl,
        trade_pips=trade_pips,
        trade_ticks=trade_ticks,
        show_trade_pips=trade_account_type != "FUTURES",
        show_trade_ticks=trade_account_type == "FUTURES",
        planned_rr=planned_rr,
        actual_rr=actual_rr,
        trade_opened_at_label=opened_at_local.strftime("%d %b %Y %H:%M")
        if opened_at_local
        else "-",
        trade_closed_at_label=closed_at_local.strftime("%d %b %Y %H:%M")
        if closed_at_local
        else "-",
        trade_source_timezone=trade.source_timezone or "Unknown",
        has_trade_source_timezone=bool(trade.source_timezone),
        trade_profile_description=(
            trade_profile_version.short_description
            if trade_profile_version is not None
            and trade_profile_version.short_description
            else "No strategy attached to this trade."
        ),
        has_trade_profile_description=bool(
            trade_profile_version is not None and trade_profile_version.short_description
        ),
        **base,
    )


@bp.route("/dashboard/trades/<string:trade_pubkey>/edit", methods=["GET", "POST"])
@login_required
def edit_trade(trade_pubkey):
    user_id = session["user_id"]

    trade = get_user_trade_by_pubkey_or_404(user_id, trade_pubkey)
    trade_account = trade.trade_account or get_active_trade_account_for_user(user_id)

    if request.method == "POST":
        input_timezone_name = get_display_timezone_name()
        account_type = normalize_account_type(trade_account.account_type)
        allowed_symbols = set(get_symbol_options(account_type, trade.symbol))
        symbol = canonicalize_symbol(request.form.get("symbol", ""), account_type)
        contract_code = (
            request.form.get("contract_code", "") or ""
        ).strip().upper() or None
        if symbol not in allowed_symbols:
            return redirect(url_for("trades.edit_trade", trade_pubkey=trade.pubkey))
        if account_type == "FUTURES" and contract_code:
            parsed_contract = parse_futures_contract_code(
                contract_code, expected_root=symbol
            )
            if parsed_contract is None:
                return redirect(url_for("trades.edit_trade", trade_pubkey=trade.pubkey))
            contract_code = parsed_contract["contract_code"]
        elif account_type != "FUTURES":
            contract_code = None
        side = request.form.get("side", "BUY").strip().upper()
        status = request.form.get("status", "Running").strip().lower()
        entry_price = float(request.form.get("entry_price", 0.0))
        exit_price = request.form.get("exit_price", "").strip()
        exit_price = float(exit_price) if exit_price else None
        lot_size = float(request.form.get("lot_size", 0.01))
        trade_note = request.form.get("trade_note", "").strip()
        is_revenge = request.form.get("is_revenge") == "on"
        is_corrective = request.form.get("is_corrective") == "on"
        is_reactive = request.form.get("is_reactive") == "on"
        pnl = request.form.get("pnl", "").strip()
        pnl = float(pnl) if pnl else None
        stop_loss = request.form.get("stop_loss", "").strip()
        stop_loss = float(stop_loss) if stop_loss else None
        take_profit = request.form.get("take_profit", "").strip()
        take_profit = float(take_profit) if take_profit else None
        commission = request.form.get("commission", "").strip()
        commission = float(commission) if commission else None
        swap = request.form.get("swap", "").strip()
        swap = float(swap) if swap else None
        opened_at = parse_local_datetime_input(request.form.get("opened_at", "").strip())
        if opened_at is None:
            opened_at = parse_local_datetime_input(
                request.form.get("trade_date", "").strip()
            )
        if opened_at is None:
            opened_at = trade.opened_at
        closed_at = parse_local_datetime_input(request.form.get("closed_at", "").strip())
        if status != "closed":
            closed_at = None
        elif closed_at is None:
            closed_at = trade.closed_at
        if closed_at is not None and opened_at is not None and closed_at < opened_at:
            flash("Close time cannot be earlier than open time.", "error")
            return redirect(url_for("trades.edit_trade", trade_pubkey=trade.pubkey))

        if pnl is not None and exit_price is None:
            exit_price = derive_exit_price(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                lot_size=lot_size,
                pnl_value=pnl,
                instrument_type=account_type,
                contract_code=contract_code,
            )
        if pnl is None and exit_price is not None:
            pnl = calc_pnl_values(
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                lot_size=lot_size,
                instrument_type=account_type,
                contract_code=contract_code,
            )
        validation_message = _validate_trade_submission(
            symbol=symbol,
            contract_code=contract_code,
            account_type=account_type,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            lot_size=lot_size,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        if validation_message:
            flash(validation_message, "error")
            return redirect(url_for("trades.edit_trade", trade_pubkey=trade.pubkey))
        duplicate_trade = _find_duplicate_trade(
            user_id=user_id,
            trade_account_id=trade_account.id,
            symbol=symbol,
            contract_code=contract_code,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            lot_size=lot_size,
            opened_at=opened_at,
            closed_at=closed_at,
            pnl=pnl,
            exclude_trade_id=trade.id,
        )
        if duplicate_trade is not None:
            flash("A matching trade already exists on this account.", "error")
            return redirect(url_for("trades.edit_trade", trade_pubkey=trade.pubkey))

        trade.symbol = symbol
        trade.contract_code = contract_code
        trade.side = side
        trade.entry_price = entry_price
        trade.exit_price = exit_price
        trade.lot_size = lot_size
        trade.trade_note = trade_note
        trade.is_revenge = is_revenge
        trade.is_corrective = is_corrective
        trade.is_reactive = is_reactive
        trade.pnl = pnl
        trade.stop_loss = stop_loss
        trade.take_profit = take_profit
        trade.commission = commission
        trade.swap = swap
        trade.opened_at = opened_at
        trade.closed_at = closed_at
        trade.source_timezone = input_timezone_name
        try:
            assign_trade_profile_to_trade(
                user_id,
                trade,
                request.form.get("trade_profile_pubkey", ""),
            )
        except ValueError:
            return redirect(url_for("trades.edit_trade", trade_pubkey=trade.pubkey))

        db.session.commit()
        _invalidate_trade_caches(user_id, trade_account.id)
        return redirect(url_for("trades.trades"))

    trade_account_type = get_trade_account_type(trade)
    trade_pnl = resolve_pnl(trade)
    trade_pips = resolve_pips(trade)
    trade_ticks = resolve_ticks(trade)
    planned_rr = _calculate_trade_risk_reward(
        trade.take_profit,
        trade.entry_price,
        trade.stop_loss,
        trade.side,
    )
    actual_rr = _calculate_trade_risk_reward(
        trade.exit_price,
        trade.entry_price,
        trade.stop_loss,
        trade.side,
        signed=True,
    )
    trade_net_pnl = _calculate_trade_net_pnl(
        trade_pnl,
        trade.commission,
        trade.swap,
    )
    timezone_name = get_display_timezone_name()
    opened_at_local = to_display_timezone(trade.opened_at, timezone_name)
    closed_at_local = to_display_timezone(trade.closed_at, timezone_name)
    trade_opened_at_label = (
        opened_at_local.strftime("%d %b %Y %H:%M") if opened_at_local else "-"
    )
    trade_closed_at_label = (
        closed_at_local.strftime("%d %b %Y %H:%M") if closed_at_local else "-"
    )
    base = _build_trade_entry_context(user_id, trade)
    return render_template(
        "trade_entry.html",
        title="MyFXJournal | Edit Trade",
        form_action=url_for("trades.edit_trade", trade_pubkey=trade.pubkey),
        form_mode="edit",
        trade_display_symbol=format_trade_symbol(trade),
        trade_pnl=trade_pnl,
        trade_net_pnl=trade_net_pnl,
        trade_pips=trade_pips,
        trade_ticks=trade_ticks,
        show_trade_pips=trade_account_type != "FUTURES",
        show_trade_ticks=trade_account_type == "FUTURES",
        planned_rr=planned_rr,
        actual_rr=actual_rr,
        trade_opened_at_label=trade_opened_at_label,
        trade_closed_at_label=trade_closed_at_label,
        **base,
    )


@bp.route("/dashboard/trades/bundle-review")
@login_required
def bundle_review():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        return redirect(url_for("dashboard.home"))

    closed_trades = (
        Trade.query.filter_by(
            user_id=user_id,
            trade_account_id=active_trade_account.id,
        )
        .filter(Trade.closed_at.isnot(None))
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .all()
    )
    outliers = detect_outliers(closed_trades)
    bundle_candidates = _build_bundle_review_candidates(outliers["bundle_candidates"])
    review_pending = _is_bundle_review_pending(active_trade_account)
    if review_pending and not bundle_candidates:
        active_trade_account.bundle_review_completed_at = utcnow_naive()
        db.session.commit()
        flash("Bundle review is already up to date for this account.", "info")
        return redirect(url_for("dashboard.home"))

    return render_template(
        "bundle_review.html",
        title="MyFXJournal | Bundle Review",
        username=session.get("username", "User"),
        bundle_candidates=bundle_candidates,
        complete_action=url_for("trades.bundle_review_complete") if review_pending else None,
        complete_label="Done Reviewing",
        display_timezone_name=get_display_timezone_name(),
        to_display_timezone=to_display_timezone,
    )


@bp.route("/dashboard/trades/bundle-confirm", methods=["POST"])
@login_required
def bundle_confirm():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        return redirect(url_for("dashboard.home"))

    selected_groups = [
        value.strip()
        for value in request.form.getlist("bundle_group")
        if value and value.strip()
    ]
    if not selected_groups:
        flash("No bundle candidates were selected. Click Done Reviewing if you want to clear this prompt.", "info")
        return redirect(url_for("trades.bundle_review"))

    updated_group_count = 0
    for group_value in selected_groups:
        trade_pubkeys = [
            value.strip()
            for value in group_value.split(",")
            if value and value.strip()
        ]
        if len(trade_pubkeys) < 2:
            continue
        trades = (
            Trade.query.filter(
                Trade.user_id == user_id,
                Trade.trade_account_id == active_trade_account.id,
                Trade.pubkey.in_(trade_pubkeys),
                Trade.closed_at.isnot(None),
            )
            .all()
        )
        if len(trades) < 2:
            continue
        bundle_pubkey = build_unique_trade_pubkey()
        for trade in trades:
            if not trade.bundle_pubkey:
                trade.bundle_pubkey = bundle_pubkey
        updated_group_count += 1

    active_trade_account.bundle_review_completed_at = utcnow_naive()
    db.session.commit()
    _invalidate_trade_caches(user_id, active_trade_account.id)
    flash(
        f"Saved {updated_group_count} confirmed bundle"
        f"{'s' if updated_group_count != 1 else ''}.",
        "success" if updated_group_count else "info",
    )
    return redirect(url_for("dashboard.home"))


@bp.route("/dashboard/trades/bundle-review/complete", methods=["POST"])
@login_required
def bundle_review_complete():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    if active_trade_account is None:
        return redirect(url_for("dashboard.home"))

    active_trade_account.bundle_review_completed_at = utcnow_naive()
    db.session.commit()
    flash("Bundle review marked as complete.", "info")
    return redirect(url_for("dashboard.home"))


_CHART_UI_TIMEFRAMES = ("M5", "M15")


@bp.route("/api/trades/<string:trade_pubkey>/chart-data")
@login_required
def trade_chart_data(trade_pubkey):
    user_id = session["user_id"]
    user = db.session.get(User, user_id)
    if not user_has_admin_access(user):
        return current_app.response_class(status=404)
    trade = get_user_trade_by_pubkey_or_404(user_id, trade_pubkey)

    if not trade.mt5_position or trade.closed_at is None:
        return current_app.response_class(
            response='{"status":"unavailable"}',
            status=200,
            mimetype="application/json",
        )

    from flask import jsonify

    any_bar = TradeBars.query.filter_by(trade_id=trade.id).first()
    if any_bar is None:
        return current_app.response_class(
            response='{"status":"pending"}',
            status=200,
            mimetype="application/json",
        )

    tf_rows = (
        TradeBars.query.with_entities(TradeBars.timeframe)
        .filter_by(trade_id=trade.id)
        .distinct()
        .all()
    )
    stored_ui_tfs = {row[0] for row in tf_rows if row[0] in _CHART_UI_TIMEFRAMES}
    has_m5 = "M5" in stored_ui_tfs
    has_m15_native = "M15" in stored_ui_tfs
    # M15 is always offered when we have M5 (aggregated); native M15 if stored alone or with M5.
    if has_m5:
        available_timeframes = ["M5", "M15"]
    elif has_m15_native:
        available_timeframes = ["M15"]
    else:
        available_timeframes = sorted(stored_ui_tfs)

    requested_tf = (request.args.get("timeframe") or "M5").strip().upper()
    if requested_tf not in _CHART_UI_TIMEFRAMES:
        requested_tf = "M5"

    bars_derived_from_m5 = False
    bars_rows = (
        TradeBars.query.filter_by(trade_id=trade.id, timeframe=requested_tf)
        .order_by(TradeBars.bar_time.asc())
        .all()
    )

    if requested_tf == "M15" and not bars_rows and has_m5:
        m5_rows = (
            TradeBars.query.filter_by(trade_id=trade.id, timeframe="M5")
            .order_by(TradeBars.bar_time.asc())
            .all()
        )
        m5_dicts = [
            {"time": b.bar_time, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for b in m5_rows
        ]
        bars = aggregate_ohlc_bars(m5_dicts, chart_timeframe_bar_seconds("M15"))
        bars_derived_from_m5 = bool(bars)
    else:
        bars = [
            {"time": b.bar_time, "open": b.open, "high": b.high, "low": b.low, "close": b.close}
            for b in bars_rows
        ]

    entry_dt_utc = ensure_utc_aware(trade.opened_at) if trade.opened_at else None
    exit_dt_utc = ensure_utc_aware(trade.closed_at) if trade.closed_at else None
    entry_time = int(entry_dt_utc.timestamp()) if entry_dt_utc else None
    exit_time = int(exit_dt_utc.timestamp()) if exit_dt_utc else None

    markers = {
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "side": trade.side,
    }

    payload = {
        "status": "ready",
        "timeframe": requested_tf,
        "available_timeframes": available_timeframes,
        "bars": bars,
        "markers": markers,
    }
    if bars_derived_from_m5:
        payload["bars_source"] = "aggregated_from_m5"
    return jsonify(payload)


@bp.route("/dashboard/trades/<string:trade_pubkey>/delete", methods=["POST"])
@login_required
def delete_trade(trade_pubkey):
    user_id = session["user_id"]

    trade = get_user_trade_by_pubkey_or_404(user_id, trade_pubkey)
    trade_account_id = trade.trade_account_id
    db.session.delete(trade)
    db.session.commit()
    _invalidate_trade_caches(user_id, trade_account_id)
    return redirect(url_for("trades.trades"))


@bp.route("/dashboard/imports/delete", methods=["POST"])
@login_required
def delete_import_batch():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)

    import_signature = request.form.get("import_signature", "").strip()
    if not import_signature:
        flash("Please select an import batch to delete.", "error")
        return redirect(url_for("trades.manage_trades"))

    try:
        trades_to_delete = (
            Trade.query.filter_by(
                user_id=user_id,
                trade_account_id=active_trade_account.id,
                import_signature=import_signature,
            ).all()
        )
        deleted = len(trades_to_delete)
        for trade in trades_to_delete:
            db.session.delete(trade)
        db.session.commit()
        _invalidate_trade_caches(user_id, active_trade_account.id)
    except (OperationalError, IntegrityError):
        db.session.rollback()
        flash(
            "Could not delete the selected import batch right now. Please try again.",
            "error",
        )
        return redirect(url_for("trades.manage_trades"))

    status = "success" if deleted > 0 else "info"
    message = (
        f"Deleted {deleted} imported trade{'s' if deleted != 1 else ''} from the selected batch."
        if deleted > 0
        else "No trades were found for the selected import batch."
    )
    flash(message, status)
    return redirect(url_for("trades.manage_trades"))
