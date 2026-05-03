"""Deterministic weekly review signals consumed by the AI prompt.

Each detector takes already-serialized trade dicts plus weekly context and
returns a dict ready to drop into the prompt payload. Intent: keep
deterministic computations out of the LLM so risk interpretation, post-loss
sequencing, and dominance flags are stable across runs.
"""

from __future__ import annotations

from helpers.weekly_coaching_hypotheses import build_coaching_hypotheses


_RISK_STABLE_TOLERANCE = 0.15
_DOMINANCE_SHARE_PCT = 40.0
_TP_SHORTFALL_PCT = 80.0

_ISSUE_RANKING = {
    "repeated_revenge_evidence": {
        "severity": 100,
        "lead_hint": "Lead with repeated revenge or reactive post-loss behavior if the concrete trade sequence supports it.",
    },
    "repeated_increased_risk_after_loss": {
        "severity": 95,
        "lead_hint": "Lead with risk increasing after losses when risk authority allows that claim.",
    },
    "repeated_same_symbol_after_loss": {
        "severity": 90,
        "lead_hint": "Lead with same-symbol re-entry after losses; frame it as the next trade not being a fresh decision.",
    },
    "repeated_same_trade_idea_reentry": {
        "severity": 85,
        "lead_hint": "Lead with repeated re-entry into the same trade idea when the sequence, timing, and outcome support it.",
    },
    "unstable_planned_risk": {
        "severity": 70,
        "lead_hint": "Lead with inconsistent planned risk only when risk_authority permits risk judgment.",
    },
    "losing_outlier_risk": {
        "severity": 65,
        "lead_hint": "Lead with one unusually large losing trade only when it explains the week better than behavior sequence.",
    },
    "recurring_winner_exited_before_target": {
        "severity": 60,
        "lead_hint": "Lead with repeated early winner exits when it is the clearest process leak.",
    },
    "multiple_confirmed_reactive_or_corrective_trades": {
        "severity": 55,
        "lead_hint": "Lead with confirmed reactive or corrective behavior when no stronger process leak exists.",
    },
    "single_same_symbol_after_loss": {
        "severity": 45,
        "lead_hint": "Use one same-symbol post-loss re-entry as a representative example, not a repeated pattern.",
    },
    "single_same_trade_idea_reentry": {
        "severity": 42,
        "lead_hint": "Use one same-idea re-entry as a representative example, not a repeated pattern.",
    },
    "isolated_revenge_evidence": {
        "severity": 40,
        "lead_hint": "Treat one revenge signal as isolated unless another signal confirms the same leak.",
    },
    "single_trade_dominance": {
        "severity": 20,
        "lead_hint": "Lead with result concentration only when there is no stronger process issue; do not call it bad execution by itself.",
    },
}


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


def _issue_metadata(reason):
    return _ISSUE_RANKING.get(
        reason,
        {
            "severity": 10,
            "lead_hint": "Use only if it explains the week better than stronger process signals.",
        },
    )


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
    - basis: one of pct_of_account, dollars, lots, or None
    - value: the median used as the anchor
    - stable: True when per-trade risk stays inside +/-15% of median
    - dispersion_pct: max relative deviation from median (None if not enough data)
    - single_sample: True when only one closed trade exists
    - risk_judgment_allowed: True only when basis is pct_of_account or
      dollars. The prompt's R1 forbids any risk claim when this is False
      (basis is lots or no risk data exists).
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
            "risk_judgment_allowed": False,
        }

    risk_judgment_allowed = basis in ("pct_of_account", "dollars")
    populated = [v for v in per_trade_values if v is not None and value > 0]
    if not populated:
        return {
            "basis": basis,
            "value": _round(value, 4),
            "stable": None,
            "dispersion_pct": None,
            "single_sample": len(closed) <= 1,
            "risk_judgment_allowed": risk_judgment_allowed,
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
        "risk_judgment_allowed": risk_judgment_allowed,
    }


def _per_trade_risk_value(trade, basis):
    if basis == "pct_of_account":
        return _safe_float(trade.get("trade_risk_pct"))
    if basis == "dollars":
        return _safe_float(trade.get("planned_risk_dollars"))
    if basis == "lots":
        return _safe_float(trade.get("lot_size"))
    return None


_OUTCOME_PHRASE = {
    "win": "a win",
    "loss": "a loss",
    "breakeven": "breakeven",
    "open": "still open",
}


def _build_biggest_loss_phrase(*, risk_judgment_allowed, risk_change, next_outcome, no_followup_reason):
    """Compose a complete English sentence the prompt can quote verbatim.

    Centralising this in code prevents the model from spliceing variable
    names ({risk_change}, {next_outcome}) into prose.
    """
    if no_followup_reason == "biggest_loss_was_last_trade":
        return (
            "Your biggest loss was the last trade of the week, so there is "
            "no follow-up trade to assess."
        )
    outcome_phrase = _OUTCOME_PHRASE.get(next_outcome or "", "an unrecorded outcome")
    if not risk_judgment_allowed:
        return (
            f"After your biggest loss, the next trade was {outcome_phrase}. "
            "Risk direction cannot be compared from available data."
        )
    if risk_change == "more":
        return f"After your biggest loss, the next trade increased risk and was {outcome_phrase}."
    if risk_change == "less":
        return f"After your biggest loss, the next trade reduced risk and was {outcome_phrase}."
    if risk_change == "same":
        return f"After your biggest loss, the next trade kept risk roughly the same and was {outcome_phrase}."
    return (
        f"After your biggest loss, the next trade was {outcome_phrase}. "
        "Risk change could not be confirmed from available data."
    )


def build_post_loss_response(serialized_trades, *, risk_authority):
    """Detect how risk and outcome changed after losses, ordered by sequence.

    Output:
    - basis: same as risk_authority.basis (or null)
    - sequences: list of {loss_ref, next_ref, risk_change, next_outcome, ...}
    - biggest_loss: dedicated entry for the worst loss of the week (or null),
      including a precomputed `phrase` the prompt can quote verbatim
    - pattern_count: len(sequences)
    - repeated_increased_risk: True when 2+ sequences show risk_change == 'more'
    """
    basis = (risk_authority or {}).get("basis")
    risk_judgment_allowed = bool((risk_authority or {}).get("risk_judgment_allowed"))
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
                "phrase": _build_biggest_loss_phrase(
                    risk_judgment_allowed=risk_judgment_allowed,
                    risk_change=None,
                    next_outcome=None,
                    no_followup_reason="biggest_loss_was_last_trade",
                ),
            }
        else:
            prev_risk = _per_trade_risk_value(biggest, basis)
            next_risk = _per_trade_risk_value(next_trade, basis)
            risk_change = _classify_change(next_risk, prev_risk)
            next_outcome = _outcome(next_trade)
            biggest_loss_entry = {
                "loss_ref": biggest.get("review_ref"),
                "loss_pnl": _round(_safe_float(biggest.get("pnl"))),
                "next_ref": next_trade.get("review_ref"),
                "risk_change": risk_change,
                "prev_risk": _round(prev_risk, 4) if prev_risk is not None else None,
                "next_risk": _round(next_risk, 4) if next_risk is not None else None,
                "next_outcome": next_outcome,
                "phrase": _build_biggest_loss_phrase(
                    risk_judgment_allowed=risk_judgment_allowed,
                    risk_change=risk_change,
                    next_outcome=next_outcome,
                    no_followup_reason=None,
                ),
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
    elif confirmed_count >= 2 or len(strong_sequences) >= 2:
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


def build_execution_outcome_archetype(
    serialized_trades,
    *,
    summary,
    risk_authority,
    post_loss_response,
    single_trade_dominance,
    tp_capture_shortfalls,
    revenge_evidence,
):
    """Classify the week before the LLM chooses tone and advice.

    This is not a verdict on strategy quality. It separates the realised
    outcome from observable process leaks so a profitable week can still get a
    warning, and a losing clean week does not get over-corrected.
    """
    closed = _closed_trades(serialized_trades)
    closed_trade_count = len(closed)
    net_pnl = _safe_float((summary or {}).get("net_pnl"))
    if closed_trade_count == 0 or net_pnl is None:
        outcome_class = "unknown"
    elif net_pnl > 0:
        outcome_class = "good"
    elif net_pnl < 0:
        outcome_class = "bad"
    else:
        outcome_class = "flat"

    issue_points = 0
    issue_reasons = []
    issue_entries = []

    def add_issue(reason, points):
        nonlocal issue_points
        if not reason or points <= 0:
            return
        issue_points += int(points)
        issue_reasons.append(reason)
        meta = _issue_metadata(reason)
        issue_entries.append(
            {
                "reason": reason,
                "points": int(points),
                "severity": int(meta.get("severity", 10) or 10),
                "lead_hint": meta.get("lead_hint") or "",
            }
        )

    revenge_class = (revenge_evidence or {}).get("pattern_class")
    if revenge_class == "repeated":
        add_issue("repeated_revenge_evidence", 3)
    elif revenge_class == "isolated":
        add_issue("isolated_revenge_evidence", 1)

    same_symbol_after_loss_count = sum(
        1 for trade in closed if bool(trade.get("is_post_loss_same_symbol_trade"))
    )
    if same_symbol_after_loss_count >= 2:
        add_issue("repeated_same_symbol_after_loss", 2)
    elif same_symbol_after_loss_count == 1:
        add_issue("single_same_symbol_after_loss", 1)

    same_idea_reentry_count = sum(
        1 for trade in closed if bool(trade.get("same_trade_idea_reentry"))
    )
    if same_idea_reentry_count >= 2:
        add_issue("repeated_same_trade_idea_reentry", 2)
    elif same_idea_reentry_count == 1:
        add_issue("single_same_trade_idea_reentry", 1)

    if bool((post_loss_response or {}).get("repeated_increased_risk")):
        add_issue("repeated_increased_risk_after_loss", 2)

    if (
        bool((risk_authority or {}).get("risk_judgment_allowed"))
        and (risk_authority or {}).get("stable") is False
    ):
        add_issue("unstable_planned_risk", 1)

    if bool((tp_capture_shortfalls or {}).get("recurring")):
        add_issue("recurring_winner_exited_before_target", 1)

    losing_outlier_count = sum(
        1
        for trade in closed
        if bool(trade.get("outlier_size")) and (_safe_float(trade.get("pnl")) or 0) < 0
    )
    if losing_outlier_count >= 1:
        add_issue("losing_outlier_risk", 1)

    confirmed_non_revenge_behaviour_count = sum(
        1
        for trade in closed
        if bool(trade.get("is_reactive")) or bool(trade.get("is_corrective"))
    )
    if confirmed_non_revenge_behaviour_count >= 2:
        add_issue("multiple_confirmed_reactive_or_corrective_trades", 1)

    outcome_concentrated = bool(single_trade_dominance)
    if outcome_concentrated and not issue_entries:
        meta = _issue_metadata("single_trade_dominance")
        issue_entries.append(
            {
                "reason": "single_trade_dominance",
                "points": 0,
                "severity": int(meta.get("severity", 20) or 20),
                "lead_hint": meta.get("lead_hint") or "",
            }
        )
    ranked_issues = sorted(
        issue_entries,
        key=lambda item: (
            -int(item.get("severity", 0) or 0),
            -int(item.get("points", 0) or 0),
            str(item.get("reason") or ""),
        ),
    )
    primary_issue = ranked_issues[0] if ranked_issues else None
    if issue_points == 0:
        issue_evidence_level = "none"
    elif issue_points == 1:
        issue_evidence_level = "isolated"
    elif issue_points == 2:
        issue_evidence_level = "moderate"
    else:
        issue_evidence_level = "strong"
    do_not_lead_with = (
        ["single_trade_dominance"]
        if outcome_concentrated
        and primary_issue is not None
        and primary_issue.get("reason") != "single_trade_dominance"
        else []
    )

    if issue_points == 0:
        execution_class = "unclear" if outcome_concentrated else "good"
    elif issue_points >= 3:
        execution_class = "bad"
    else:
        execution_class = "leaky"

    if outcome_class == "unknown" or execution_class == "unclear":
        week_archetype = "random_or_unclear_execution"
        coaching_stance = "measure_first"
        stance_hint = "Do not force a big process claim; explain what is known and what to measure next."
    elif execution_class == "good" and outcome_class == "good":
        week_archetype = "good_execution_good_outcome"
        coaching_stance = "full_praise"
        stance_hint = "Reinforce the process first; add only a small refinement if the evidence supports it."
    elif execution_class == "good" and outcome_class == "flat":
        week_archetype = "good_execution_flat_outcome"
        coaching_stance = "steady_neutral"
        stance_hint = "Treat breakeven as neutral; reinforce controlled process and suggest only a small measurement or refinement."
    elif execution_class == "good" and outcome_class == "bad":
        week_archetype = "good_execution_bad_outcome"
        coaching_stance = "protect_confidence"
        stance_hint = "Protect confidence; do not tell the trader to overhaul clean process because of one result."
    elif execution_class in ("bad", "leaky") and outcome_class == "good":
        week_archetype = f"{execution_class}_execution_good_outcome"
        coaching_stance = "good_week_but_habits_are_leaking"
        stance_hint = "Acknowledge the good result, then show the process leak without overcorrecting."
    elif outcome_class == "flat":
        week_archetype = f"{execution_class}_execution_flat_outcome"
        coaching_stance = "direct_correction" if execution_class == "bad" else "light_correction"
        stance_hint = "Treat breakeven as neutral on results, but still coach the process leak at the right evidence strength."
    elif execution_class == "bad":
        week_archetype = "bad_execution_bad_outcome"
        coaching_stance = "direct_correction"
        stance_hint = "Be direct: connect the process leak to the damage and give one clear corrective rule."
    else:
        week_archetype = "leaky_execution_bad_outcome"
        coaching_stance = "light_correction"
        stance_hint = "Name the leak clearly, but keep the correction proportional to the evidence."

    return {
        "outcome_class": outcome_class,
        "execution_class": execution_class,
        "week_archetype": week_archetype,
        "coaching_stance": coaching_stance,
        "stance_hint": stance_hint,
        "issue_points": issue_points,
        "issue_evidence_level": issue_evidence_level,
        "issue_reasons": issue_reasons,
        "primary_issue": primary_issue.get("reason") if primary_issue else None,
        "primary_issue_hint": primary_issue.get("lead_hint") if primary_issue else None,
        "ranked_issues": ranked_issues[:5],
        "do_not_lead_with": do_not_lead_with,
        "closed_trade_count": closed_trade_count,
        "net_pnl": _round(net_pnl),
        "same_symbol_after_loss_count": same_symbol_after_loss_count,
        "same_trade_idea_reentry_count": same_idea_reentry_count,
        "losing_outlier_count": losing_outlier_count,
        "outcome_concentrated": outcome_concentrated,
    }


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
    post_loss_response = build_post_loss_response(
        serialized_trades, risk_authority=risk_authority
    )
    single_trade_dominance = build_single_trade_dominance(
        largest_trade_symbol=largest_symbol,
        largest_trade_review_ref=largest_ref,
        largest_trade_abs_pnl_share_pct=(summary or {}).get("largest_trade_abs_pnl_share_pct"),
        closed_trade_count=closed_trade_count,
    )
    tp_capture_shortfalls = build_tp_capture_shortfalls(serialized_trades)
    revenge_evidence = build_revenge_evidence(serialized_trades)
    execution_outcome = build_execution_outcome_archetype(
        serialized_trades,
        summary=summary,
        risk_authority=risk_authority,
        post_loss_response=post_loss_response,
        single_trade_dominance=single_trade_dominance,
        tp_capture_shortfalls=tp_capture_shortfalls,
        revenge_evidence=revenge_evidence,
    )
    coaching_hypotheses = build_coaching_hypotheses(
        serialized_trades=serialized_trades,
        summary=summary,
        current_week_breakdowns=current_week_breakdowns,
        execution_outcome=execution_outcome,
        revenge_evidence=revenge_evidence,
        post_loss_response=post_loss_response,
        risk_authority=risk_authority,
        single_trade_dominance=single_trade_dominance,
        tp_capture_shortfalls=tp_capture_shortfalls,
    )
    return {
        "risk_authority": risk_authority,
        "post_loss_response": post_loss_response,
        "single_trade_dominance": single_trade_dominance,
        "tp_capture_shortfalls": tp_capture_shortfalls,
        "session_concentration": build_session_concentration(serialized_trades),
        "revenge_evidence": revenge_evidence,
        "execution_outcome": execution_outcome,
        "coaching_hypotheses": coaching_hypotheses,
        "surface_facts": build_surface_facts(
            summary=summary, current_week_breakdowns=current_week_breakdowns
        ),
        "confidence_envelope": build_confidence_envelope(
            account_age_days=account_age_days,
            closed_trade_count=closed_trade_count,
            notes_confidence=notes_confidence,
        ),
    }
