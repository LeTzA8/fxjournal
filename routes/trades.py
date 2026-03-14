from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import limiter
from celery_workers.cache import CacheUnavailableError, invalidate
from helpers.core import (
    assign_trade_profile_to_trade,
    build_trade_duplicate_key,
    build_trade_import_dedupe_key,
    build_unique_trade_pubkey,
    format_local_datetime_input,
    get_active_trade_account_for_user,
    get_display_timezone_name,
    get_trade_size_label,
    get_user_trade_by_pubkey_or_404,
    get_user_trade_profiles,
    is_local_dev_environment,
    parse_local_datetime_input,
    resolve_trade_profile_form_state,
)
from models import Trade, db
from trading import (
    build_import_signature,
    calc_pnl_values,
    canonicalize_symbol,
    classify_trading_session,
    derive_exit_price,
    detect_trade_import_profile,
    format_duration_minutes,
    format_trade_price,
    format_trade_size,
    format_trade_symbol,
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
    if trade_pnl is None:
        return None
    return float(trade_pnl) + float(commission or 0.0) + float(swap or 0.0)


def _invalidate_trade_caches(user_id, trade_account_id):
    try:
        invalidate(user_id=user_id, trade_account_id=trade_account_id)
    except CacheUnavailableError:
        return


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
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        user_trades = []

    trade_rows = []
    size_label = get_trade_size_label(active_trade_account.account_type)
    timezone_name = get_display_timezone_name()
    for trade in user_trades:
        trade_account_type = get_trade_account_type(trade)
        opened_at_local = to_display_timezone(trade.opened_at, timezone_name)
        trade_profile = getattr(trade, "trade_profile", None)
        trade_profile_version = getattr(trade, "trade_profile_version", None)
        duration_minutes = None
        if trade.opened_at is not None:
            duration_end = trade.closed_at or utcnow_naive()
            if duration_end >= trade.opened_at:
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
                "is_running": trade.exit_price is None,
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
        title="My Trades | FX Journal",
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
        flash("Could not update the selected trade profiles right now.", "error")
        return redirect(url_for("trades.manage_trades"))

    action_label = "cleared" if not selected_profile_pubkey else "updated"
    flash(
        f"Trade profile {action_label} for {len(trades_to_update)} trade"
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
            closed_at = None

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

    return render_template(
        "trade_entry.html",
        title="New Trade | FX Journal",
        username=session.get("username", "User"),
        active_trade_account_name=active_trade_account.name,
        symbol_options=get_symbol_options(active_trade_account.account_type),
        account_type=normalize_account_type(active_trade_account.account_type),
        size_label=get_trade_size_label(active_trade_account.account_type),
        trade=None,
        form_action=url_for("trades.new_trade"),
        form_mode="new",
        opened_at_value="",
        closed_at_value="",
        analytics_timezone=get_display_timezone_name(),
        trade_profile_options=profile_form_state["trade_profile_options"],
        selected_trade_profile_pubkey=profile_form_state[
            "selected_trade_profile_pubkey"
        ],
    )


@bp.route("/dashboard/import", methods=["POST"])
@login_required
def import_trade_file():
    user_id = session["user_id"]
    active_trade_account = get_active_trade_account_for_user(user_id)
    account_type = normalize_account_type(active_trade_account.account_type)
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

        allowed_symbols = set(get_symbol_options(active_trade_account.account_type))
        failed_symbols = set()
        validation_reasons = {
            "missing_mt5_position": 0,
            "invalid_mt5_position": 0,
            "invalid_side": 0,
            "invalid_entry_or_lot": 0,
            "invalid_exit_price": 0,
            "missing_open_time": 0,
        }

        existing_positions = set()
        import_positions = set()
        existing_trade_keys = set()
        import_trade_keys = set()
        if account_type == "FUTURES":
            existing_trades = (
                Trade.query.filter_by(
                    user_id=user_id,
                    trade_account_id=active_trade_account.id,
                )
                .all()
            )
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
                    trade_account_id=active_trade_account.id,
                )
                .filter(Trade.mt5_position.isnot(None))
                .all()
                if pos
            }

        import_stage = "build_insert_batch"
        import_signature = build_import_signature(
            "tradovate" if account_type == "FUTURES" else "mt5"
        )
        insert_batch = []
        reserved_pubkeys = set()

        for current_row_index, row in enumerate(parsed_rows, start=1):
            symbol = canonicalize_symbol(
                row.get("symbol"), active_trade_account.account_type
            )
            side = (row.get("side") or "").upper()
            lot_size = row.get("lot_size")
            entry_price = row.get("entry_price")
            exit_price = row.get("exit_price")
            pnl = row.get("pnl")
            opened_at = row.get("opened_at")
            closed_at = row.get("closed_at")
            contract_code = row.get("contract_code")
            mt5_position = row.get("mt5_position")
            mt5_position_raw = row.get("mt5_position_raw")
            current_row_context = {
                "symbol": symbol,
                "side": side,
                "lot_size": lot_size,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "opened_at": opened_at.isoformat() if opened_at else None,
                "closed_at": closed_at.isoformat() if closed_at else None,
                "contract_code": contract_code,
                "mt5_position": mt5_position,
                "mt5_position_raw": str(mt5_position_raw or "").strip()[:64] or None,
                "source_timezone": row.get("source_timezone"),
            }

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

            if pnl is None and exit_price is not None:
                pnl = calc_pnl_values(
                    symbol,
                    side,
                    entry_price,
                    exit_price,
                    lot_size,
                    instrument_type=active_trade_account.account_type,
                    contract_code=contract_code,
                )

            if account_type == "FUTURES":
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

            insert_batch.append(
                Trade(
                    pubkey=build_unique_trade_pubkey(reserved_pubkeys),
                    user_id=user_id,
                    trade_account_id=active_trade_account.id,
                    source_timezone=(row.get("source_timezone") or None),
                    symbol=symbol,
                    mt5_position=mt5_position,
                    import_signature=import_signature,
                    import_dedupe_key=build_trade_import_dedupe_key(
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
                    trade_note=(
                        "Imported from Tradovate Performance CSV"
                        if account_type == "FUTURES"
                        else "Imported from MT5 Positions"
                    ),
                )
            )

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

    return render_template(
        "trade_detail.html",
        title="Trade Detail | FX Journal",
        username=session.get("username", "User"),
        trade=trade,
        trade_display_symbol=format_trade_symbol(trade),
        trade_pnl=trade_pnl,
        trade_net_pnl=trade_net_pnl,
        trade_pips=trade_pips,
        trade_ticks=trade_ticks,
        planned_rr=planned_rr,
        actual_rr=actual_rr,
        trade_account_type=trade_account_type,
        trade_size_label=get_trade_size_label(trade_account_type),
        trade_opened_at_label=opened_at_local.strftime("%d %b %Y %H:%M")
        if opened_at_local
        else "-",
        trade_closed_at_label=closed_at_local.strftime("%d %b %Y %H:%M")
        if closed_at_local
        else "-",
        trade_source_timezone=trade.source_timezone or "Unknown",
        analytics_timezone=timezone_name,
        trade_profile_name=(
            trade_profile_version.name
            if trade_profile_version is not None
            else (trade_profile.name if trade_profile is not None else "-")
        ),
        trade_profile_version_label=(
            f"v{trade_profile_version.version_number}"
            if trade_profile_version is not None
            else "-"
        ),
        trade_profile_description=(
            trade_profile_version.short_description
            if trade_profile_version is not None
            and trade_profile_version.short_description
            else "No trade profile attached to this trade."
        ),
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
            closed_at = None

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

        trade.symbol = symbol
        trade.contract_code = contract_code
        trade.side = side
        trade.entry_price = entry_price
        trade.exit_price = exit_price
        trade.lot_size = lot_size
        trade.trade_note = trade_note
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

    profile_form_state = resolve_trade_profile_form_state(user_id, trade=trade)

    return render_template(
        "trade_entry.html",
        title="Edit Trade | FX Journal",
        username=session.get("username", "User"),
        active_trade_account_name=trade_account.name,
        symbol_options=get_symbol_options(trade_account.account_type, trade.symbol),
        account_type=normalize_account_type(trade_account.account_type),
        size_label=get_trade_size_label(trade_account.account_type),
        trade=trade,
        form_action=url_for("trades.edit_trade", trade_pubkey=trade.pubkey),
        form_mode="edit",
        opened_at_value=format_local_datetime_input(trade.opened_at),
        closed_at_value=format_local_datetime_input(trade.closed_at),
        analytics_timezone=get_display_timezone_name(),
        trade_profile_options=profile_form_state["trade_profile_options"],
        selected_trade_profile_pubkey=profile_form_state[
            "selected_trade_profile_pubkey"
        ],
    )


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
