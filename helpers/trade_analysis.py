import math
from datetime import datetime, timezone

from trading import classify_trading_session, ensure_utc_aware, format_trade_symbol, resolve_net_pnl

SPLIT_BUCKET_MINUTES = 5
REVENGE_REENTRY_WINDOW_MINUTES = 30
SAME_TRADE_IDEA_REENTRY_WINDOW_MINUTES = 60
BUNDLE_TP_SL_TOLERANCE_PCT = 0.002
CORRECTIVE_REENTRY_WINDOW_MINUTES = 240


def floor_timestamp_to_bucket(timestamp, bucket_minutes=SPLIT_BUCKET_MINUTES):
    timestamp_utc = ensure_utc_aware(timestamp)
    if timestamp_utc is None:
        return None
    minute_floor = (timestamp_utc.minute // max(int(bucket_minutes), 1)) * max(int(bucket_minutes), 1)
    return timestamp_utc.replace(minute=minute_floor, second=0, microsecond=0)


def coerce_float(value):
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def minutes_between(earlier, later):
    earlier_utc = ensure_utc_aware(earlier)
    later_utc = ensure_utc_aware(later)
    if earlier_utc is None or later_utc is None or later_utc < earlier_utc:
        return None
    return round((later_utc - earlier_utc).total_seconds() / 60.0, 2)


def get_trade_session(trade):
    timestamp_utc = ensure_utc_aware(getattr(trade, "opened_at", None))
    if timestamp_utc is None:
        return None
    return classify_trading_session(timestamp_utc)


def get_trade_identity(trade):
    trade_id = getattr(trade, "id", None)
    if trade_id is not None:
        return f"id:{trade_id}"
    trade_pubkey = str(getattr(trade, "pubkey", "") or "").strip()
    if trade_pubkey:
        return f"pubkey:{trade_pubkey}"
    return f"obj:{id(trade)}"


def get_trade_sort_key(trade):
    opened_at = ensure_utc_aware(getattr(trade, "opened_at", None)) or datetime.min.replace(tzinfo=timezone.utc)
    closed_at = ensure_utc_aware(getattr(trade, "closed_at", None)) or datetime.min.replace(tzinfo=timezone.utc)
    return (opened_at, closed_at, getattr(trade, "id", 0) or 0)


def get_split_group_key(trade):
    split_bucket = floor_timestamp_to_bucket(getattr(trade, "opened_at", None))
    if split_bucket is None:
        return None
    return (
        (format_trade_symbol(trade) or "").strip().upper(),
        (getattr(trade, "side", "") or "").strip().upper(),
        split_bucket,
    )


def _lot_sizes_match(first_size, second_size):
    if first_size is None or second_size is None:
        return False
    return math.isclose(first_size, second_size, rel_tol=0.05, abs_tol=0.01)


def _describe_size_change(current_size, previous_size):
    if current_size is None or previous_size is None:
        return None
    if _lot_sizes_match(current_size, previous_size):
        return "same"
    return "larger" if current_size > previous_size else "smaller"


def _prices_match(first_price, second_price):
    first_value = coerce_float(first_price)
    second_value = coerce_float(second_price)
    if first_value is None or second_value is None:
        return False
    baseline = max(abs(first_value), abs(second_value), 1.0)
    return abs(first_value - second_value) <= baseline * BUNDLE_TP_SL_TOLERANCE_PCT


def build_trade_annotations(trades):
    chronological_trades = sorted(trades, key=get_trade_sort_key)
    split_group_members = {}
    for trade in chronological_trades:
        group_key = get_split_group_key(trade)
        if group_key is None:
            continue
        split_group_members.setdefault(group_key, []).append(get_trade_identity(trade))

    split_group_lookup = {}
    for trade in chronological_trades:
        identity = get_trade_identity(trade)
        split_group_lookup[identity] = {
            "split_group_size": 1,
            "split_group_index": 1,
            "split_group_role": "solo",
            "possible_split_order": False,
            "group_key": get_split_group_key(trade),
        }

    for group_key, member_ids in split_group_members.items():
        group_size = len(member_ids)
        for index, member_id in enumerate(member_ids, start=1):
            split_group_lookup[member_id] = {
                "split_group_size": group_size,
                "split_group_index": index,
                "split_group_role": "solo" if group_size == 1 else ("lead" if index == 1 else "add_on"),
                "possible_split_order": group_size >= 2,
                "group_key": group_key,
            }

    annotations = {}
    previous_trade = None
    previous_trade_pnl = None
    current_loss_streak = 0
    session_counts = {}
    last_same_idea_trade = {}

    for sequence_number, trade in enumerate(chronological_trades, start=1):
        identity = get_trade_identity(trade)
        current_symbol = (format_trade_symbol(trade) or "").strip().upper()
        current_side = (getattr(trade, "side", "") or "").strip().upper()
        current_session = get_trade_session(trade)
        session_key = current_session or "__unknown__"
        session_counts[session_key] = session_counts.get(session_key, 0) + 1
        current_group = split_group_lookup[identity]
        trade_lot_size = coerce_float(getattr(trade, "lot_size", None))

        previous_trade_identity = get_trade_identity(previous_trade) if previous_trade is not None else None
        minutes_since_prev_close = None
        size_vs_prev_trade = None
        same_symbol_reentry = False
        is_post_loss_trade = False
        if previous_trade is not None:
            minutes_since_prev_close = minutes_between(
                getattr(previous_trade, "closed_at", None),
                getattr(trade, "opened_at", None),
            )
            previous_lot_size = coerce_float(getattr(previous_trade, "lot_size", None))
            size_vs_prev_trade = _describe_size_change(trade_lot_size, previous_lot_size)
            previous_symbol = (format_trade_symbol(previous_trade) or "").strip().upper()
            same_symbol_reentry = bool(
                minutes_since_prev_close is not None and previous_symbol == current_symbol
            )
            is_post_loss_trade = previous_trade_pnl is not None and previous_trade_pnl < 0

        same_trade_idea_reentry = False
        same_idea_key = (current_symbol, current_side)
        previous_same_idea = last_same_idea_trade.get(same_idea_key)
        if previous_same_idea is not None and previous_same_idea["group_key"] != current_group["group_key"]:
            same_idea_gap_minutes = minutes_between(
                getattr(previous_same_idea["trade"], "closed_at", None),
                getattr(trade, "opened_at", None),
            )
            if (
                same_idea_gap_minutes is not None
                and same_idea_gap_minutes <= SAME_TRADE_IDEA_REENTRY_WINDOW_MINUTES
            ):
                same_trade_idea_reentry = True

        is_potential_revenge = bool(
            is_post_loss_trade
            and same_trade_idea_reentry
            and minutes_since_prev_close is not None
            and minutes_since_prev_close <= REVENGE_REENTRY_WINDOW_MINUTES
            and size_vs_prev_trade in {"same", "larger"}
            and not current_group["possible_split_order"]
        )

        annotations[identity] = {
            "trade_sequence_number": sequence_number,
            "trade_number_in_session": session_counts[session_key],
            "prev_trade_identity": previous_trade_identity,
            "prev_trade_pnl": previous_trade_pnl,
            "minutes_since_prev_close": minutes_since_prev_close,
            "size_vs_prev_trade": size_vs_prev_trade,
            "loss_streak_before_trade": current_loss_streak,
            "is_post_loss_trade": is_post_loss_trade,
            "same_symbol_reentry": same_symbol_reentry,
            "same_trade_idea_reentry": same_trade_idea_reentry,
            "is_potential_revenge": is_potential_revenge,
            "split_group_size": current_group["split_group_size"],
            "split_group_index": current_group["split_group_index"],
            "split_group_role": current_group["split_group_role"],
            "possible_split_order": current_group["possible_split_order"],
        }

        current_trade_pnl = resolve_net_pnl(trade)
        if current_trade_pnl is not None and current_trade_pnl < 0:
            current_loss_streak += 1
        else:
            current_loss_streak = 0

        previous_trade = trade
        previous_trade_pnl = current_trade_pnl
        last_same_idea_trade[same_idea_key] = {
            "trade": trade,
            "group_key": current_group["group_key"],
        }

    return annotations


def _classify_annotation(annotation):
    minutes_since_prev_close = annotation.get("minutes_since_prev_close")
    if (
        annotation.get("is_post_loss_trade")
        and annotation.get("same_symbol_reentry")
        and minutes_since_prev_close is not None
        and minutes_since_prev_close <= REVENGE_REENTRY_WINDOW_MINUTES
    ):
        return "reactive"
    if (
        annotation.get("is_post_loss_trade")
        and annotation.get("same_symbol_reentry")
        and minutes_since_prev_close is not None
        and minutes_since_prev_close <= CORRECTIVE_REENTRY_WINDOW_MINUTES
    ):
        return "corrective"
    return "neutral"


def detect_outliers(trades):
    closed_trades = [
        trade
        for trade in trades
        if getattr(trade, "closed_at", None) is not None
    ]
    if not closed_trades:
        return {"bundle_candidates": [], "standalone_candidates": []}

    annotations = build_trade_annotations(closed_trades)
    identity_to_trade = {get_trade_identity(trade): trade for trade in closed_trades}
    eligible_trades = [
        trade
        for trade in closed_trades
        if not getattr(trade, "bundle_pubkey", None)
        and not bool(getattr(trade, "is_reactive", False))
        and not bool(getattr(trade, "is_corrective", False))
    ]
    if not eligible_trades:
        return {"bundle_candidates": [], "standalone_candidates": []}

    eligible_trades = sorted(eligible_trades, key=get_trade_sort_key)
    eligible_ids = [get_trade_identity(trade) for trade in eligible_trades]
    split_groups = {}
    for trade in eligible_trades:
        identity = get_trade_identity(trade)
        group_key = annotations.get(identity, {}).get("possible_split_order") and get_split_group_key(trade)
        if group_key is None:
            continue
        split_groups.setdefault(group_key, []).append(identity)

    bundle_candidates = []
    bundled_trade_ids = set()
    for index, trade in enumerate(eligible_trades):
        identity = eligible_ids[index]
        if identity in bundled_trade_ids:
            continue
        member_ids = [identity]
        reason_keys = set()
        group_key = annotations.get(identity, {}).get("possible_split_order") and get_split_group_key(trade)
        if group_key is not None:
            split_member_ids = split_groups.get(group_key, [])
            if len(split_member_ids) >= 2:
                member_ids = [member_id for member_id in split_member_ids if member_id not in bundled_trade_ids]
                if len(member_ids) >= 2:
                    reason_keys.add("split_bucket")

        representative = identity_to_trade[member_ids[0]]
        representative_symbol = (format_trade_symbol(representative) or "").strip().upper()
        representative_side = (getattr(representative, "side", "") or "").strip().upper()
        representative_opened_at = ensure_utc_aware(getattr(representative, "opened_at", None))

        if representative_opened_at is not None:
            for offset in range(index + 1, len(eligible_trades)):
                candidate = eligible_trades[offset]
                candidate_identity = eligible_ids[offset]
                if candidate_identity in bundled_trade_ids or candidate_identity in member_ids:
                    continue

                candidate_opened_at = ensure_utc_aware(getattr(candidate, "opened_at", None))
                if candidate_opened_at is None:
                    continue
                gap_minutes = minutes_between(representative_opened_at, candidate_opened_at)
                if gap_minutes is None or gap_minutes > CORRECTIVE_REENTRY_WINDOW_MINUTES:
                    break

                if (format_trade_symbol(candidate) or "").strip().upper() != representative_symbol:
                    continue
                if (getattr(candidate, "side", "") or "").strip().upper() != representative_side:
                    continue

                same_tp = _prices_match(
                    getattr(representative, "take_profit", None),
                    getattr(candidate, "take_profit", None),
                )
                same_sl = _prices_match(
                    getattr(representative, "stop_loss", None),
                    getattr(candidate, "stop_loss", None),
                )
                if not same_tp and not same_sl:
                    continue

                member_ids.append(candidate_identity)
                if same_tp:
                    reason_keys.add("same_tp")
                if same_sl:
                    reason_keys.add("same_sl")

        if len(member_ids) < 2:
            continue

        member_trades = sorted(
            (identity_to_trade[member_id] for member_id in member_ids),
            key=get_trade_sort_key,
        )
        representative = member_trades[0]
        representative_annotation = annotations.get(get_trade_identity(representative), {})
        sub_type = _classify_annotation(representative_annotation)
        trigger = identity_to_trade.get(representative_annotation.get("prev_trade_identity"))
        if "split_bucket" in reason_keys:
            match_reason = "split_bucket"
        elif {"same_tp", "same_sl"}.issubset(reason_keys):
            match_reason = "same_tp_and_sl"
        elif "same_tp" in reason_keys:
            match_reason = "same_tp"
        elif "same_sl" in reason_keys:
            match_reason = "same_sl"
        else:
            match_reason = "split_bucket"
        bundle_candidates.append(
            {
                "trades": member_trades,
                "match_reason": match_reason,
                "sub_type": sub_type,
                "trigger": trigger if sub_type != "neutral" else None,
            }
        )
        bundled_trade_ids.update(member_ids)

    standalone_candidates = []
    for trade in eligible_trades:
        identity = get_trade_identity(trade)
        if identity in bundled_trade_ids:
            continue
        annotation = annotations.get(identity, {})
        sub_type = _classify_annotation(annotation)
        if sub_type == "neutral":
            continue
        standalone_candidates.append(
            {
                "trade": trade,
                "sub_type": sub_type,
                "trigger": identity_to_trade.get(annotation.get("prev_trade_identity")),
            }
        )

    return {
        "bundle_candidates": bundle_candidates,
        "standalone_candidates": standalone_candidates,
    }
