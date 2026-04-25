"""Deterministic weekly review signals consumed by the AI prompt.

Each detector takes already-serialized trade dicts plus weekly context and
returns a dict ready to drop into the prompt payload. Intent: keep
deterministic computations out of the LLM so risk interpretation, post-loss
sequencing, and dominance flags are stable across runs.
"""

from __future__ import annotations


_RISK_STABLE_TOLERANCE = 0.15
_DOMINANCE_SHARE_PCT = 40.0
_TP_SHORTFALL_PCT = 80.0


def _safe_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _classify_change(current, baseline, *, tolerance=_RISK_STABLE_TOLERANCE):
    """Return 'more', 'less', or 'same' relative to baseline within tolerance."""
    if current is None or baseline in (None, 0):
        return None
    ratio = (current - baseline) / abs(baseline)
    if abs(ratio) <= tolerance:
        return "same"
    return "more" if ratio > 0 else "less"


def _closed_trades(serialized_trades):
    return [t for t in (serialized_trades or []) if t.get("closed_at")]


def _outcome(trade):
    if not trade.get("closed_at"):
        return "open"
    pnl = _safe_float(trade.get("pnl"))
    if pnl is None:
        return "open"
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "breakeven"


def build_risk_authority(
    serialized_trades,
    *,
    median_risk_pct_of_account=None,
    median_planned_risk_dollars=None,
    median_lot_size=None,
):
    """Decide which risk basis is authoritative and whether weekly risk was stable.

    Output keys:
    - basis: one of pct_of_account, dollars, lots
    - value: the median used as the anchor
    - stable: True when per-trade risk stays inside +/-15% of median
    - dispersion_pct: max relative deviation from median (None if not enough data)
    - single_sample: True when only one closed trade exists
    """
    closed = _closed_trades(serialized_trades)
    if median_risk_pct_of_account is not None:
        basis = "pct_of_account"
        value = float(median_risk_pct_of_account)
        per_trade_values = [
            _safe_float(t.get("trade_risk_pct")) for t in closed
        ]
    elif median_planned_risk_dollars is not None:
        basis = "dollars"
        value = float(median_planned_risk_dollars)
        per_trade_values = [
            _safe_float(t.get("planned_risk_dollars")) for t in closed
        ]
    elif median_lot_size is not None:
        basis = "lots"
        value = float(median_lot_size)
        per_trade_values = [_safe_float(t.get("lot_size")) for t in closed]
    else:
        return {
            "basis": None,
            "value": None,
            "stable": None,
            "dispersion_pct": None,
            "single_sample": len(closed) <= 1,
        }

    populated = [v for v in per_trade_values if v is not None and value > 0]
    if not populated:
        return {
            "basis": basis,
            "value": _round(value, 4),
            "stable": None,
            "dispersion_pct": None,
            "single_sample": len(closed) <= 1,
        }
    deviations = [abs((v - value) / value) for v in populated]
    dispersion = max(deviations)
    if len(populated) == 1:
        stable = True
    else:
        stable = dispersion <= _RISK_STABLE_TOLERANCE
    return {
        "basis": basis,
        "value": _round(value, 4),
        "stable": bool(stable),
        "dispersion_pct": _round(dispersion * 100.0, 1),
        "single_sample": len(populated) == 1,
    }


def _per_trade_risk_value(trade, basis):
    if basis == "pct_of_account":
        return _safe_float(trade.get("trade_risk_pct"))
    if basis == "dollars":
        return _safe_float(trade.get("planned_risk_dollars"))
    if basis == "lots":
        return _safe_float(trade.get("lot_size"))
    return None


def build_post_loss_response(serialized_trades, *, risk_authority):
    """Detect how risk and outcome changed after losses, ordered by sequence.

    Output:
    - basis: same as risk_authority.basis (or null)
    - sequences: list of {loss_ref, next_ref, risk_change, next_outcome, ...}
    - biggest_loss: dedicated entry for the worst loss of the week (or null)
    - pattern_count: len(sequences)
    - repeated_increased_risk: True when 2+ sequences show risk_change == 'more'
    """
    basis = (risk_authority or {}).get("basis")
    closed = sorted(
        [t for t in _closed_trades(serialized_trades) if t.get("trade_sequence_number") is not None],
        key=lambda t: t.get("trade_sequence_number"),
    )
    if not closed:
        closed = _closed_trades(serialized_trades)

    sequences = []
    for index, current in enumerate(closed):
        if index == 0:
            continue
        prev = closed[index - 1]
        prev_pnl = _safe_float(prev.get("pnl"))
        if prev_pnl is None or prev_pnl >= 0:
            continue
        prev_risk = _per_trade_risk_value(prev, basis)
        current_risk = _per_trade_risk_value(current, basis)
        sequences.append(
            {
                "loss_ref": prev.get("review_ref"),
                "loss_pnl": _round(prev_pnl),
                "next_ref": current.get("review_ref"),
                "risk_change": _classify_change(current_risk, prev_risk),
                "prev_risk": _round(prev_risk, 4) if prev_risk is not None else None,
                "next_risk": _round(current_risk, 4) if current_risk is not None else None,
                "next_outcome": _outcome(current),
            }
        )

    biggest_loss_entry = None
    losing_trades = [
        (i, t)
        for i, t in enumerate(closed)
        if (_safe_float(t.get("pnl")) or 0) < 0
    ]
    if losing_trades:
        biggest_index, biggest = min(
            losing_trades,
            key=lambda pair: _safe_float(pair[1].get("pnl")) or 0,
        )
        next_trade = closed[biggest_index + 1] if biggest_index + 1 < len(closed) else None
        if next_trade is None:
            biggest_loss_entry = {
                "loss_ref": biggest.get("review_ref"),
                "loss_pnl": _round(_safe_float(biggest.get("pnl"))),
                "next_ref": None,
                "risk_change": None,
                "next_outcome": None,
                "no_followup_reason": "biggest_loss_was_last_trade",
            }
        else:
            prev_risk = _per_trade_risk_value(biggest, basis)
            next_risk = _per_trade_risk_value(next_trade, basis)
            biggest_loss_entry = {
                "loss_ref": biggest.get("review_ref"),
                "loss_pnl": _round(_safe_float(biggest.get("pnl"))),
                "next_ref": next_trade.get("review_ref"),
                "risk_change": _classify_change(next_risk, prev_risk),
                "prev_risk": _round(prev_risk, 4) if prev_risk is not None else None,
                "next_risk": _round(next_risk, 4) if next_risk is not None else None,
                "next_outcome": _outcome(next_trade),
            }

    repeated_increased_risk = (
        sum(1 for seq in sequences if seq.get("risk_change") == "more") >= 2
    )
    return {
        "basis": basis,
        "sequences": sequences,
        "biggest_loss": biggest_loss_entry,
        "pattern_count": len(sequences),
        "repeated_increased_risk": repeated_increased_risk,
    }


def build_single_trade_dominance(
    *,
    largest_trade_symbol,
    largest_trade_review_ref=None,
    largest_trade_abs_pnl_share_pct,
    closed_trade_count,
):
    """Flag when one trade carried the week (>=40% absolute pnl share, >=3 closed trades)."""
    if not largest_trade_abs_pnl_share_pct or closed_trade_count is None or closed_trade_count < 3:
        return None
    share = float(largest_trade_abs_pnl_share_pct)
    if share < _DOMINANCE_SHARE_PCT:
        return None
    return {
        "dominant_symbol": largest_trade_symbol,
        "dominant_ref": largest_trade_review_ref,
        "abs_pnl_share_pct": _round(share, 1),
        "threshold_basis": f"abs_pnl_share>={int(_DOMINANCE_SHARE_PCT)}%",
    }


def build_tp_capture_shortfalls(serialized_trades):
    """List winning trades that closed materially short of TP."""
    items = []
    for trade in _closed_trades(serialized_trades):
        pnl = _safe_float(trade.get("pnl"))
        capture = _safe_float(trade.get("tp_capture_pct"))
        if pnl is None or pnl <= 0 or capture is None:
            continue
        if capture >= _TP_SHORTFALL_PCT:
            continue
        items.append(
            {
                "ref": trade.get("review_ref"),
                "symbol": trade.get("symbol"),
                "tp_capture_pct": _round(capture, 1),
            }
        )
    return {"trades": items, "recurring": len(items) >= 2}


def build_session_concentration(serialized_trades):
    """Describe how the week clustered by session."""
    closed = _closed_trades(serialized_trades)
    if not closed:
        return {
            "dominant_session": None,
            "share_pct": None,
            "single_session": False,
            "mixed_outcome": False,
            "session_distribution": [],
        }
    counts = {}
    outcomes_by_session = {}
    for trade in closed:
        session = trade.get("session") or "unknown"
        counts[session] = counts.get(session, 0) + 1
        outcomes_by_session.setdefault(session, []).append(_outcome(trade))
    dominant_session, dominant_count = max(
        counts.items(), key=lambda item: (item[1], item[0])
    )
    total = len(closed)
    dominant_outcomes = outcomes_by_session.get(dominant_session, [])
    has_win = any(o == "win" for o in dominant_outcomes)
    has_loss = any(o == "loss" for o in dominant_outcomes)
    return {
        "dominant_session": dominant_session,
        "share_pct": _round((dominant_count / total) * 100.0, 1),
        "single_session": len(counts) == 1,
        "mixed_outcome": has_win and has_loss,
        "session_distribution": [
            {"session": s, "count": c} for s, c in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        ],
    }


def build_revenge_evidence(serialized_trades):
    """Summarize confirmed and heuristic revenge evidence at week scope."""
    closed = _closed_trades(serialized_trades)
    confirmed_count = 0
    heuristic_count = 0
    strong_sequences = []
    for trade in closed:
        confirmed = bool(trade.get("is_revenge"))
        heuristic = bool(trade.get("is_potential_revenge")) and not confirmed
        if confirmed:
            confirmed_count += 1
        if heuristic:
            heuristic_count += 1
        if not (confirmed or heuristic):
            continue
        prev_pnl = _safe_float(trade.get("prev_trade_pnl"))
        minutes_since = _safe_float(trade.get("minutes_since_prev_close"))
        size_change = trade.get("size_vs_prev_trade")
        is_strong = (
            confirmed
            or (
                prev_pnl is not None
                and prev_pnl < 0
                and minutes_since is not None
                and minutes_since <= 30
                and size_change == "larger"
            )
        )
        if is_strong:
            strong_sequences.append(
                {
                    "ref": trade.get("review_ref"),
                    "minutes_since_prev_close": minutes_since,
                    "size_vs_prev_trade": size_change,
                    "confirmed": confirmed,
                }
            )

    if confirmed_count == 0 and heuristic_count == 0:
        pattern_class = "none"
    elif confirmed_count >= 1 or len(strong_sequences) >= 2:
        pattern_class = "repeated"
    else:
        pattern_class = "isolated"

    return {
        "confirmed_count": confirmed_count,
        "heuristic_count": heuristic_count,
        "strong_sequences": strong_sequences,
        "pattern_class": pattern_class,
    }


def build_surface_facts(*, summary, current_week_breakdowns):
    """Plain-text facts already visible on the dashboard.

    The prompt instructs takeaways must add information beyond these strings.
    """
    facts = []
    summary = summary or {}
    breakdowns = current_week_breakdowns or {}
    sizing = breakdowns.get("sizing") or {}
    frequency = breakdowns.get("frequency") or {}

    def add(label, value, suffix=""):
        if value in (None, ""):
            return
        facts.append(f"{label}: {value}{suffix}")

    add("net_pnl", summary.get("net_pnl"))
    add("win_rate", summary.get("win_rate"), "%")
    add("closed_trades", summary.get("closed_trades"))
    add("open_trades", summary.get("open_trades"))
    add("top_symbol_by_trade_count", summary.get("top_symbol_by_trade_count"))
    add("top_symbol_by_abs_pnl", summary.get("top_symbol_by_abs_pnl"))
    add("largest_trade_symbol", summary.get("largest_trade_symbol"))
    add(
        "largest_trade_abs_pnl_share_pct",
        summary.get("largest_trade_abs_pnl_share_pct"),
        "%",
    )
    add("busiest_session", frequency.get("busiest_session"))
    add("median_risk_pct_of_account", sizing.get("median_risk_pct_of_account"), "%")
    add("median_planned_risk_dollars", sizing.get("median_planned_risk_dollars"))
    add("median_lot_size", sizing.get("median_lot_size"))
    return facts


def build_confidence_envelope(*, account_age_days, closed_trade_count, notes_confidence):
    """Decay confidence based on age, sample size, and notes coverage.

    Levels: limited (2+ triggers), moderate (1 trigger), strong (none).
    """
    reasons = []
    if account_age_days is not None and account_age_days < 30:
        reasons.append("account_age_days_below_30")
    if closed_trade_count is not None and closed_trade_count < 3:
        reasons.append("closed_trade_count_below_3")
    if (notes_confidence or "").lower() == "low":
        reasons.append("notes_confidence_low")

    if len(reasons) >= 2:
        level = "limited"
    elif len(reasons) == 1:
        level = "moderate"
    else:
        level = "strong"
    return {"level": level, "reasons": reasons}


def build_weekly_signals(
    *,
    serialized_trades,
    summary,
    current_week_breakdowns,
    median_risk_pct_of_account,
    median_planned_risk_dollars,
    median_lot_size,
    account_age_days,
    notes_confidence,
):
    """Compose every detector output into one dict for prompt consumption."""
    closed_trade_count = sum(1 for t in (serialized_trades or []) if t.get("closed_at"))
    risk_authority = build_risk_authority(
        serialized_trades,
        median_risk_pct_of_account=median_risk_pct_of_account,
        median_planned_risk_dollars=median_planned_risk_dollars,
        median_lot_size=median_lot_size,
    )
    largest_ref = None
    largest_symbol = (summary or {}).get("largest_trade_symbol")
    if largest_symbol:
        for trade in (serialized_trades or []):
            if (
                trade.get("symbol") == largest_symbol
                and abs(_safe_float(trade.get("pnl")) or 0)
                == max(abs(_safe_float(t.get("pnl")) or 0) for t in (serialized_trades or []))
            ):
                largest_ref = trade.get("review_ref")
                break
    return {
        "risk_authority": risk_authority,
        "post_loss_response": build_post_loss_response(
            serialized_trades, risk_authority=risk_authority
        ),
        "single_trade_dominance": build_single_trade_dominance(
            largest_trade_symbol=largest_symbol,
            largest_trade_review_ref=largest_ref,
            largest_trade_abs_pnl_share_pct=(summary or {}).get("largest_trade_abs_pnl_share_pct"),
            closed_trade_count=closed_trade_count,
        ),
        "tp_capture_shortfalls": build_tp_capture_shortfalls(serialized_trades),
        "session_concentration": build_session_concentration(serialized_trades),
        "revenge_evidence": build_revenge_evidence(serialized_trades),
        "surface_facts": build_surface_facts(
            summary=summary, current_week_breakdowns=current_week_breakdowns
        ),
        "confidence_envelope": build_confidence_envelope(
            account_age_days=account_age_days,
            closed_trade_count=closed_trade_count,
            notes_confidence=notes_confidence,
        ),
    }
