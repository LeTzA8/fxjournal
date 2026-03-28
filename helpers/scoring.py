from helpers.trade_analysis import build_trade_annotations, get_trade_identity
from trading import merge_bundled_trades

CONFIRMED_REACTIVE_RATE_WEIGHT = 2.5
CONFIRMED_REACTIVE_POINTS_CAP = 1.5
CONFIRMED_CORRECTIVE_RATE_WEIGHT = 1.5
CONFIRMED_CORRECTIVE_POINTS_CAP = 1.0
REVENGE_POINTS_PER_TRADE = 1.25
REVENGE_POINTS_CAP = 5.0
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
        "slightly_off": 0.5,
        "stressed": 1.0,
    }
    plan_adherence_points = {
        "consistent": 0.0,
        "some_deviations": 0.5,
        "impulsive": 1.0,
    }
    execution_quality_points = {
        "sharp": 0.0,
        "average": 0.0,
        "poor": 0.5,
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


def compute_emotional_index(*, trades, weekly_checkin):
    merged_trades = merge_bundled_trades(trades)
    closed_trades = [
        trade
        for trade in merged_trades
        if getattr(trade, "closed_at", None) is not None
    ]
    if weekly_checkin is None and not closed_trades:
        return None

    total_closed_trades = len(closed_trades)
    bundle_count = sum(1 for trade in closed_trades if bool(getattr(trade, "_is_bundle", False)))
    confirmed_revenge_trade_count = sum(
        1 for trade in closed_trades if bool(getattr(trade, "is_revenge", False))
    )
    reactive_trade_count = sum(1 for trade in closed_trades if bool(getattr(trade, "is_reactive", False)))
    corrective_trade_count = sum(1 for trade in closed_trades if bool(getattr(trade, "is_corrective", False)))
    confirmed_behavior_trade_count = sum(
        1
        for trade in closed_trades
        if bool(
            getattr(trade, "is_revenge", False)
            or getattr(trade, "is_reactive", False)
            or getattr(trade, "is_corrective", False)
        )
    )
    annotations = build_trade_annotations(closed_trades)
    heuristic_revenge_trade_count = sum(
        1
        for trade in closed_trades
        if bool(annotations.get(get_trade_identity(trade), {}).get("is_potential_revenge"))
    )
    revenge_trade_count = sum(
        1
        for trade in closed_trades
        if bool(getattr(trade, "is_revenge", False))
        or bool(annotations.get(get_trade_identity(trade), {}).get("is_potential_revenge"))
    )

    subjective_points = _subjective_score(weekly_checkin)
    confirmed_reactive_points = (
        min(
            (reactive_trade_count / total_closed_trades) * CONFIRMED_REACTIVE_RATE_WEIGHT,
            CONFIRMED_REACTIVE_POINTS_CAP,
        )
        if total_closed_trades
        else 0.0
    )
    confirmed_corrective_points = (
        min(
            (corrective_trade_count / total_closed_trades) * CONFIRMED_CORRECTIVE_RATE_WEIGHT,
            CONFIRMED_CORRECTIVE_POINTS_CAP,
        )
        if total_closed_trades
        else 0.0
    )
    revenge_points = min(revenge_trade_count * REVENGE_POINTS_PER_TRADE, REVENGE_POINTS_CAP)
    behaviour_signal_points = confirmed_reactive_points + confirmed_corrective_points + revenge_points
    self_report_mismatch = bool(
        _has_calm_controlled_self_report(weekly_checkin)
        and behaviour_signal_points >= SELF_REPORT_MISMATCH_THRESHOLD
    )

    score = min(
        subjective_points
        + confirmed_reactive_points
        + confirmed_corrective_points
        + revenge_points,
        10.0,
    )
    return {
        "score": round(score, 2),
        "label": _score_label(score),
        "self_report_mismatch": self_report_mismatch,
        "components": {
            "subjective_points": round(subjective_points, 2),
            "confirmed_reactive_points": round(confirmed_reactive_points, 2),
            "confirmed_corrective_points": round(confirmed_corrective_points, 2),
            "revenge_points": round(revenge_points, 2),
        },
        "signals": {
            "bundle_count": bundle_count,
            "confirmed_revenge_trade_count": confirmed_revenge_trade_count,
            "heuristic_revenge_trade_count": heuristic_revenge_trade_count,
            "reactive_trade_count": reactive_trade_count,
            "corrective_trade_count": corrective_trade_count,
            "revenge_trade_count": revenge_trade_count,
            "confirmed_behavior_trade_count": confirmed_behavior_trade_count,
            "total_closed_trades": total_closed_trades,
        },
    }
