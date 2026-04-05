import statistics

from helpers.trade_analysis import build_trade_annotations, get_trade_identity
from trading import (
    did_trade_reach_level,
    get_trade_account_type,
    get_trade_level_validation_issues,
    is_extremely_long_duration_minutes,
    merge_bundled_trades,
    resolve_net_pnl,
)

REVENGE_BASE_WEIGHT = 4.5
REVENGE_REPETITION_BONUS_PER_EXTRA = 0.5
REVENGE_REPETITION_BONUS_CAP = 1.5
REVENGE_POINTS_CAP = 6.0
REVENGE_HEURISTIC_SEVERITY_MAX = 3.0

REACTIVE_BASE_WEIGHT = 2.5
REACTIVE_REPETITION_BONUS_PER_EXTRA = 0.35
REACTIVE_REPETITION_BONUS_CAP = 1.0
REACTIVE_POINTS_CAP = 3.5
REACTIVE_HEURISTIC_SEVERITY_MAX = 2.0

CORRECTIVE_BASE_WEIGHT = 1.25
CORRECTIVE_REPETITION_BONUS_PER_EXTRA = 0.25
CORRECTIVE_REPETITION_BONUS_CAP = 0.75
CORRECTIVE_POINTS_CAP = 2.0
CORRECTIVE_HEURISTIC_SEVERITY_MAX = 0.9

SELF_REPORT_MISMATCH_THRESHOLD = 2.5


def _get_value(record, key):
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _subjective_score(weekly_checkin):
    emotional_state_points = {
        "calm": 0.0,
        "slightly_off": 0.75,
        "stressed": 1.5,
    }
    plan_adherence_points = {
        "consistent": 0.0,
        "some_deviations": 0.75,
        "impulsive": 1.5,
    }
    execution_quality_points = {
        "sharp": 0.0,
        "average": 0.25,
        "poor": 0.75,
    }
    return (
        emotional_state_points.get(str(_get_value(weekly_checkin, "emotional_state") or "").strip().lower(), 0.0)
        + plan_adherence_points.get(str(_get_value(weekly_checkin, "plan_adherence") or "").strip().lower(), 0.0)
        + execution_quality_points.get(str(_get_value(weekly_checkin, "execution_quality") or "").strip().lower(), 0.0)
    )


def _score_label(score):
    if score >= 7.5:
        return "very_high"
    if score >= 5.0:
        return "high"
    if score >= 2.5:
        return "moderate"
    return "low"


def _has_calm_controlled_self_report(weekly_checkin):
    emotional_state = str(_get_value(weekly_checkin, "emotional_state") or "").strip().lower()
    plan_adherence = str(_get_value(weekly_checkin, "plan_adherence") or "").strip().lower()
    execution_quality = str(_get_value(weekly_checkin, "execution_quality") or "").strip().lower()
    return (
        emotional_state == "calm"
        and plan_adherence == "consistent"
        and execution_quality in {"sharp", "average"}
    )


def _get_trade_duration_minutes(trade):
    opened_at = getattr(trade, "opened_at", None)
    closed_at = getattr(trade, "closed_at", None)
    if opened_at is None or closed_at is None or closed_at < opened_at:
        return None
    return (closed_at - opened_at).total_seconds() / 60.0


def _get_closed_before_sl(trade, trade_pnl):
    if (
        trade_pnl is None
        or trade_pnl >= 0
        or getattr(trade, "exit_price", None) is None
        or getattr(trade, "stop_loss", None) is None
    ):
        return None

    instrument_type = get_trade_account_type(trade)
    validation_issues = get_trade_level_validation_issues(
        getattr(trade, "entry_price", None),
        getattr(trade, "stop_loss", None),
        getattr(trade, "take_profit", None),
        getattr(trade, "side", None),
        getattr(trade, "symbol", None),
        instrument_type=instrument_type,
        contract_code=getattr(trade, "contract_code", None),
    )
    if validation_issues["stop_loss_too_close"] or validation_issues["invalid_stop_loss_side"]:
        return None

    return not did_trade_reach_level(
        getattr(trade, "exit_price", None),
        getattr(trade, "stop_loss", None),
        getattr(trade, "side", None),
        "sl",
        getattr(trade, "symbol", None),
        instrument_type=instrument_type,
        contract_code=getattr(trade, "contract_code", None),
    )


def _get_outlier_size_flag(trade, median_lot_size):
    try:
        trade_lot_size = float(getattr(trade, "lot_size", None))
    except (TypeError, ValueError):
        return False
    return bool(
        median_lot_size
        and median_lot_size > 0
        and trade_lot_size > median_lot_size * 3
    )


def _score_heuristic_corrective_signal(
    *,
    annotation,
    trade_pnl,
    duration_minutes,
    median_duration_minutes,
    closed_before_sl,
    outlier_size,
):
    if trade_pnl is None or trade_pnl > 0:
        return 0.0

    score = 0.2
    if closed_before_sl is True:
        score += 0.75

    quick_cutoff_minutes = 15.0
    if median_duration_minutes and median_duration_minutes > 0:
        quick_cutoff_minutes = max(10.0, median_duration_minutes * 0.4)
    if duration_minutes is not None and duration_minutes <= quick_cutoff_minutes:
        score += 0.55

    if bool(annotation.get("is_potential_revenge")) or bool(annotation.get("is_potential_reactive")):
        score += 0.35
    elif bool(annotation.get("is_post_loss_trade")) or bool(annotation.get("same_trade_idea_reentry")):
        score += 0.2

    if outlier_size:
        score += 0.25

    if score < 1.1:
        return 0.0
    return min(score * 0.6, CORRECTIVE_HEURISTIC_SEVERITY_MAX)


def _normalize_signal_strength(value, max_value):
    if value is None or max_value <= 0:
        return 0.0
    return min(max(float(value), 0.0) / float(max_value), 1.0)


def _build_bucket_points(
    *,
    total_closed_trades,
    confirmed_severities,
    heuristic_severities,
    base_weight,
    repetition_bonus_per_extra,
    repetition_bonus_cap,
    bucket_cap,
):
    if total_closed_trades <= 0:
        return {
            "confirmed_points": 0.0,
            "heuristic_points": 0.0,
            "repetition_bonus": 0.0,
            "total_points": 0.0,
            "signal_count": 0,
        }

    combined_severities = [
        max(confirmed_severity, heuristic_severity)
        for confirmed_severity, heuristic_severity in zip(confirmed_severities, heuristic_severities)
    ]
    confirmed_points = (sum(confirmed_severities) / total_closed_trades) * base_weight
    heuristic_points = (sum(heuristic_severities) / total_closed_trades) * base_weight
    signal_count = sum(1 for severity in combined_severities if severity > 0)
    repetition_bonus = min(
        max(signal_count - 1, 0) * repetition_bonus_per_extra,
        repetition_bonus_cap,
    )
    total_points = min(confirmed_points + heuristic_points + repetition_bonus, bucket_cap)
    return {
        "confirmed_points": round(confirmed_points, 2),
        "heuristic_points": round(heuristic_points, 2),
        "repetition_bonus": round(repetition_bonus, 2),
        "total_points": round(total_points, 2),
        "signal_count": signal_count,
    }


def _prepare_closed_trade_signal_inputs(trades):
    closed_trades = [
        trade
        for trade in trades
        if getattr(trade, "closed_at", None) is not None
    ]
    annotations = build_trade_annotations(closed_trades)

    lot_sizes = []
    durations = []
    for trade in closed_trades:
        try:
            lot_size = float(getattr(trade, "lot_size", None))
        except (TypeError, ValueError):
            lot_size = None
        if lot_size is not None:
            lot_sizes.append(lot_size)

        duration_minutes = _get_trade_duration_minutes(trade)
        if duration_minutes is not None and not is_extremely_long_duration_minutes(duration_minutes):
            durations.append(duration_minutes)

    return {
        "closed_trades": closed_trades,
        "annotations": annotations,
        "median_lot_size": statistics.median(lot_sizes) if lot_sizes else None,
        "median_duration_minutes": statistics.median(durations) if durations else None,
    }


def build_trade_behavior_signal_map(trades):
    prepared = _prepare_closed_trade_signal_inputs(trades)
    annotations = prepared["annotations"]
    median_lot_size = prepared["median_lot_size"]
    median_duration_minutes = prepared["median_duration_minutes"]

    signal_map = {}
    for trade in trades:
        identity = get_trade_identity(trade)
        trade_is_closed = getattr(trade, "closed_at", None) is not None
        annotation = annotations.get(identity, {}) if trade_is_closed else {}
        trade_pnl = resolve_net_pnl(trade) if trade_is_closed else None
        duration_minutes = _get_trade_duration_minutes(trade) if trade_is_closed else None
        closed_before_sl = _get_closed_before_sl(trade, trade_pnl) if trade_is_closed else None
        outlier_size = _get_outlier_size_flag(trade, median_lot_size) if trade_is_closed else False
        heuristic_corrective_strength = (
            _normalize_signal_strength(
                _score_heuristic_corrective_signal(
                    annotation=annotation,
                    trade_pnl=trade_pnl,
                    duration_minutes=duration_minutes,
                    median_duration_minutes=median_duration_minutes,
                    closed_before_sl=closed_before_sl,
                    outlier_size=outlier_size,
                ),
                CORRECTIVE_HEURISTIC_SEVERITY_MAX,
            )
            if trade_is_closed
            else 0.0
        )
        quick_cutoff_minutes = 15.0
        if median_duration_minutes and median_duration_minutes > 0:
            quick_cutoff_minutes = max(10.0, median_duration_minutes * 0.4)
        quick_duration = bool(
            trade_is_closed
            and duration_minutes is not None
            and duration_minutes <= quick_cutoff_minutes
        )

        confirmed = {
            "revenge": bool(getattr(trade, "is_revenge", False)),
            "reactive": bool(getattr(trade, "is_reactive", False)),
            "corrective": bool(getattr(trade, "is_corrective", False)),
        }
        has_any_confirmed = any(confirmed.values())
        suppress_possible = bool(
            has_any_confirmed
            or getattr(trade, "bundle_pubkey", None)
            or annotation.get("possible_split_order")
        )
        possible = {
            "revenge": bool(
                trade_is_closed
                and not suppress_possible
                and annotation.get("is_potential_revenge")
            ),
            "reactive": bool(
                trade_is_closed
                and not suppress_possible
                and annotation.get("is_potential_reactive")
            ),
            "corrective": bool(
                trade_is_closed
                and not suppress_possible
                and heuristic_corrective_strength > 0
            ),
        }

        signal_map[identity] = {
            "is_closed": trade_is_closed,
            "confirmed": confirmed,
            "possible": possible,
            "strength": {
                "revenge": (
                    _normalize_signal_strength(
                        annotation.get("revenge_signal_strength"),
                        REVENGE_HEURISTIC_SEVERITY_MAX,
                    )
                    if possible["revenge"]
                    else 0.0
                ),
                "reactive": (
                    _normalize_signal_strength(
                        annotation.get("reactive_signal_strength"),
                        REACTIVE_HEURISTIC_SEVERITY_MAX,
                    )
                    if possible["reactive"]
                    else 0.0
                ),
                "corrective": heuristic_corrective_strength if possible["corrective"] else 0.0,
            },
            "context": {
                "is_post_loss_trade": bool(annotation.get("is_post_loss_trade")),
                "is_post_loss_same_symbol_trade": bool(annotation.get("is_post_loss_same_symbol_trade")),
                "same_symbol_reentry": bool(annotation.get("same_symbol_reentry")),
                "same_trade_idea_reentry": bool(annotation.get("same_trade_idea_reentry")),
                "minutes_since_prev_close": annotation.get("minutes_since_prev_close"),
                "minutes_since_prev_symbol_close": annotation.get("minutes_since_prev_symbol_close"),
                "size_vs_prev_trade": annotation.get("size_vs_prev_trade"),
                "size_vs_prev_symbol_trade": annotation.get("size_vs_prev_symbol_trade"),
                "loss_streak_before_trade": annotation.get("loss_streak_before_trade") or 0,
                "closed_before_sl": closed_before_sl,
                "quick_duration": quick_duration,
                "outlier_size": outlier_size,
                "possible_split_order": bool(annotation.get("possible_split_order")),
            },
        }

    return signal_map


def compute_emotional_index(*, trades, weekly_checkin):
    merged_trades = merge_bundled_trades(trades)
    prepared = _prepare_closed_trade_signal_inputs(merged_trades)
    closed_trades = prepared["closed_trades"]
    if weekly_checkin is None and not closed_trades:
        return None

    total_closed_trades = len(closed_trades)
    bundle_count = sum(1 for trade in closed_trades if bool(getattr(trade, "_is_bundle", False)))
    confirmed_revenge_trade_count = sum(
        1 for trade in closed_trades if bool(getattr(trade, "is_revenge", False))
    )
    confirmed_reactive_trade_count = sum(
        1 for trade in closed_trades if bool(getattr(trade, "is_reactive", False))
    )
    confirmed_corrective_trade_count = sum(
        1 for trade in closed_trades if bool(getattr(trade, "is_corrective", False))
    )
    confirmed_behavior_trade_count = sum(
        1
        for trade in closed_trades
        if bool(
            getattr(trade, "is_revenge", False)
            or getattr(trade, "is_reactive", False)
            or getattr(trade, "is_corrective", False)
        )
    )

    annotations = prepared["annotations"]
    median_lot_size = prepared["median_lot_size"]
    median_duration_minutes = prepared["median_duration_minutes"]

    heuristic_revenge_trade_count = 0
    heuristic_reactive_trade_count = 0
    heuristic_corrective_trade_count = 0
    revenge_trade_count = 0

    revenge_confirmed_severities = []
    revenge_heuristic_severities = []
    reactive_confirmed_severities = []
    reactive_heuristic_severities = []
    corrective_confirmed_severities = []
    corrective_heuristic_severities = []

    for trade in closed_trades:
        identity = get_trade_identity(trade)
        annotation = annotations.get(identity, {})
        trade_pnl = resolve_net_pnl(trade)
        duration_minutes = _get_trade_duration_minutes(trade)
        outlier_size = _get_outlier_size_flag(trade, median_lot_size)
        closed_before_sl = _get_closed_before_sl(trade, trade_pnl)

        has_confirmed_revenge = bool(getattr(trade, "is_revenge", False))
        has_confirmed_reactive = bool(getattr(trade, "is_reactive", False))
        has_confirmed_corrective = bool(getattr(trade, "is_corrective", False))
        heuristic_revenge_strength = _normalize_signal_strength(
            annotation.get("revenge_signal_strength"),
            REVENGE_HEURISTIC_SEVERITY_MAX,
        ) if bool(annotation.get("is_potential_revenge")) else 0.0
        heuristic_reactive_strength = _normalize_signal_strength(
            annotation.get("reactive_signal_strength"),
            REACTIVE_HEURISTIC_SEVERITY_MAX,
        ) if bool(annotation.get("is_potential_reactive")) else 0.0
        heuristic_corrective_strength = _normalize_signal_strength(
            _score_heuristic_corrective_signal(
                annotation=annotation,
                trade_pnl=trade_pnl,
                duration_minutes=duration_minutes,
                median_duration_minutes=median_duration_minutes,
                closed_before_sl=closed_before_sl,
                outlier_size=outlier_size,
            ),
            CORRECTIVE_HEURISTIC_SEVERITY_MAX,
        )

        if heuristic_revenge_strength > 0:
            heuristic_revenge_trade_count += 1
        if heuristic_reactive_strength > 0:
            heuristic_reactive_trade_count += 1
        if heuristic_corrective_strength > 0:
            heuristic_corrective_trade_count += 1

        revenge_confirmed_severities.append(1.0 if has_confirmed_revenge else 0.0)
        revenge_heuristic_severities.append(
            0.0 if has_confirmed_revenge else heuristic_revenge_strength
        )
        reactive_confirmed_severities.append(1.0 if has_confirmed_reactive else 0.0)
        reactive_heuristic_severities.append(
            0.0 if has_confirmed_reactive else heuristic_reactive_strength
        )
        corrective_confirmed_severities.append(1.0 if has_confirmed_corrective else 0.0)
        corrective_heuristic_severities.append(
            0.0 if has_confirmed_corrective else heuristic_corrective_strength
        )

        if has_confirmed_revenge or heuristic_revenge_strength > 0:
            revenge_trade_count += 1

    revenge_bucket = _build_bucket_points(
        total_closed_trades=total_closed_trades,
        confirmed_severities=revenge_confirmed_severities,
        heuristic_severities=revenge_heuristic_severities,
        base_weight=REVENGE_BASE_WEIGHT,
        repetition_bonus_per_extra=REVENGE_REPETITION_BONUS_PER_EXTRA,
        repetition_bonus_cap=REVENGE_REPETITION_BONUS_CAP,
        bucket_cap=REVENGE_POINTS_CAP,
    )
    reactive_bucket = _build_bucket_points(
        total_closed_trades=total_closed_trades,
        confirmed_severities=reactive_confirmed_severities,
        heuristic_severities=reactive_heuristic_severities,
        base_weight=REACTIVE_BASE_WEIGHT,
        repetition_bonus_per_extra=REACTIVE_REPETITION_BONUS_PER_EXTRA,
        repetition_bonus_cap=REACTIVE_REPETITION_BONUS_CAP,
        bucket_cap=REACTIVE_POINTS_CAP,
    )
    corrective_bucket = _build_bucket_points(
        total_closed_trades=total_closed_trades,
        confirmed_severities=corrective_confirmed_severities,
        heuristic_severities=corrective_heuristic_severities,
        base_weight=CORRECTIVE_BASE_WEIGHT,
        repetition_bonus_per_extra=CORRECTIVE_REPETITION_BONUS_PER_EXTRA,
        repetition_bonus_cap=CORRECTIVE_REPETITION_BONUS_CAP,
        bucket_cap=CORRECTIVE_POINTS_CAP,
    )

    subjective_points = _subjective_score(weekly_checkin)
    revenge_points = revenge_bucket["total_points"]
    reactive_points = reactive_bucket["total_points"]
    corrective_points = corrective_bucket["total_points"]
    behaviour_signal_points = revenge_points + reactive_points + corrective_points
    self_report_mismatch = bool(
        _has_calm_controlled_self_report(weekly_checkin)
        and behaviour_signal_points >= SELF_REPORT_MISMATCH_THRESHOLD
    )

    score = min(
        subjective_points
        + revenge_points
        + reactive_points
        + corrective_points,
        10.0,
    )
    return {
        "score": round(score, 2),
        "label": _score_label(score),
        "self_report_mismatch": self_report_mismatch,
        "components": {
            "subjective_points": round(subjective_points, 2),
            "confirmed_revenge_points": revenge_bucket["confirmed_points"],
            "heuristic_revenge_points": revenge_bucket["heuristic_points"],
            "revenge_repetition_bonus": revenge_bucket["repetition_bonus"],
            "revenge_points": revenge_points,
            "confirmed_reactive_points": reactive_bucket["confirmed_points"],
            "heuristic_reactive_points": reactive_bucket["heuristic_points"],
            "reactive_repetition_bonus": reactive_bucket["repetition_bonus"],
            "reactive_points": reactive_points,
            "confirmed_corrective_points": corrective_bucket["confirmed_points"],
            "heuristic_corrective_points": corrective_bucket["heuristic_points"],
            "corrective_repetition_bonus": corrective_bucket["repetition_bonus"],
            "corrective_points": corrective_points,
        },
        "signals": {
            "bundle_count": bundle_count,
            "confirmed_revenge_trade_count": confirmed_revenge_trade_count,
            "heuristic_revenge_trade_count": heuristic_revenge_trade_count,
            "revenge_trade_count": revenge_trade_count,
            "confirmed_reactive_trade_count": confirmed_reactive_trade_count,
            "heuristic_reactive_trade_count": heuristic_reactive_trade_count,
            "reactive_trade_count": confirmed_reactive_trade_count + heuristic_reactive_trade_count,
            "reactive_signal_trade_count": reactive_bucket["signal_count"],
            "confirmed_corrective_trade_count": confirmed_corrective_trade_count,
            "heuristic_corrective_trade_count": heuristic_corrective_trade_count,
            "corrective_trade_count": confirmed_corrective_trade_count + heuristic_corrective_trade_count,
            "corrective_signal_trade_count": corrective_bucket["signal_count"],
            "confirmed_behavior_trade_count": confirmed_behavior_trade_count,
            "total_closed_trades": total_closed_trades,
        },
    }
