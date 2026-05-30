"""Universal weekly AI review payload — one JSON for admin audit and model input."""

from __future__ import annotations

import json
from datetime import datetime

_STRATEGY_DESCRIPTION_MAX_LEN = 1200
_STRATEGY_PRESERVE_SECTION_KEYWORDS = (
    "re-entry",
    "reentry",
    "re entry",
    "risk management",
    "entry",
    "session",
    "setup",
)
_PRIOR_RULE_MAX_LEN = 80
_REENTRY_EXPERIMENT_KEYWORDS = (
    "post-loss",
    "post loss",
    "same symbol",
    "re-entry",
    "reentry",
    "re entry",
    "wait",
    "one trade per symbol",
    "repair",
    "retry",
)

_NARROW_INSTRUCTION = (
    "Review the specific trade sequence directly. Do not call one sequence a repeated pattern."
)
_MODERATE_INSTRUCTION = (
    "Keep claims proportional to this week's evidence. Prefer one clear diagnosis over multiple themes."
)
_BROAD_INSTRUCTION = (
    "You may connect multiple patterns when each is supported by distinct evidence."
)


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


def _outcome(trade):
    pnl = _safe_float(trade.get("pnl"))
    if pnl is None:
        return None
    if pnl > 0:
        return "win"
    if pnl < 0:
        return "loss"
    return "breakeven"


def _closed_trades(trades):
    return [t for t in (trades or []) if t.get("closed_at") or t.get("pnl") is not None]


def _trade_ref(trade):
    return trade.get("review_ref") or trade.get("ref")


def _trade_by_ref(trades):
    return {_trade_ref(t): t for t in trades if _trade_ref(t)}


def _display_symbol(trade):
    raw = str(trade.get("symbol") or "").strip().upper()
    if " (" in raw:
        raw = raw.split(" (", 1)[0].strip()
    return raw or None


def _parse_opened_at(value):
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _trade_date_label(trade):
    opened = _parse_opened_at(trade.get("opened_at"))
    if opened is None:
        return None
    return opened.strftime("%d %b %Y (%a)")


def _strategy_dedupe_key(trade):
    version_id = trade.get("trade_profile_version_id") or trade.get("strategy_version_id")
    if version_id not in (None, ""):
        return ("id", int(version_id))
    return (
        "tuple",
        trade.get("strategy_name"),
        trade.get("strategy_version"),
        trade.get("strategy_description"),
    )


def _safe_strategy_description(description):
    description = (description or "").strip()
    if not description:
        return None, False
    if len(description) <= _STRATEGY_DESCRIPTION_MAX_LEN:
        return description, False
    lower = description.lower()
    tail_start = description[_STRATEGY_DESCRIPTION_MAX_LEN:]
    for keyword in _STRATEGY_PRESERVE_SECTION_KEYWORDS:
        if keyword in lower and keyword in tail_start.lower():
            return description, False
    truncated = description[:_STRATEGY_DESCRIPTION_MAX_LEN].rstrip() + "..."
    return truncated, True


def _build_strategy_context(trades):
    strategies_used = []
    ref_by_key = {}
    for trade in trades:
        key = _strategy_dedupe_key(trade)
        if key in ref_by_key:
            continue
        if not any(
            [
                trade.get("strategy_name"),
                trade.get("strategy_version") is not None,
                trade.get("strategy_description"),
            ]
        ):
            continue
        strategy_ref = f"S{len(strategies_used) + 1}"
        ref_by_key[key] = strategy_ref
        description, truncated = _safe_strategy_description(trade.get("strategy_description"))
        entry = {
            "strategy_ref": strategy_ref,
            "name": trade.get("strategy_name"),
            "version": trade.get("strategy_version"),
            "description": description,
        }
        if truncated:
            entry["description_truncated"] = True
        strategies_used.append(entry)

    trade_strategy_refs = {}
    for trade in trades:
        key = _strategy_dedupe_key(trade)
        trade_strategy_refs[id(trade)] = ref_by_key.get(key)
    return strategies_used, trade_strategy_refs


def _risk_change_label(trade):
    return trade.get("risk_pct_vs_prev_symbol") or trade.get("risk_pct_vs_prev")


def _map_risk_change(value):
    if value in ("larger", "more"):
        return "larger"
    if value in ("smaller", "less"):
        return "smaller"
    if value == "same":
        return "same"
    return value


def _session_overlap_label(trade):
    market_context = trade.get("market_context") or {}
    sessions = market_context.get("entry_active_sessions") or []
    if market_context.get("entry_in_session_overlap") and len(sessions) >= 2:
        return f"{'/'.join(sessions)} overlap"
    return None


def _session_fields(trade):
    entry_session = trade.get("entry_session") or trade.get("session")
    exit_session = trade.get("exit_session")
    fields = {}
    if entry_session:
        fields["entry_session"] = entry_session
    if exit_session:
        fields["exit_session"] = exit_session
    entry_context = _session_overlap_label(trade)
    if entry_context:
        fields["entry_session_context"] = entry_context
    exit_context = None
    if exit_session and entry_context and exit_session in (trade.get("market_context") or {}).get(
        "entry_active_sessions", []
    ):
        exit_context = entry_context
    if exit_context and exit_context != entry_context:
        fields["exit_session_context"] = exit_context
    return fields


def _stop_management_summary(trade):
    stop_management = (trade.get("market_context") or {}).get("stop_management") or {}
    confidence = stop_management.get("confidence")
    if confidence not in {"medium", "high"}:
        return None
    if stop_management.get("stop_loss_protects_profit"):
        read = "stored stop was protective"
    elif stop_management.get("stop_loss_breakeven_or_better"):
        read = "stored stop was at or beyond breakeven"
    else:
        read = "stored stop management was adjusted"
    return {"confidence": confidence, "read": read}


def _market_context_summary(trade):
    market_context = trade.get("market_context") or {}
    if not market_context:
        return None

    summary = {}
    bars_status = market_context.get("bars_status")
    if bars_status:
        summary["bars_status"] = bars_status
    for key in ("mfe_r", "mae_r", "post_exit_direction", "post_exit_tp_reached"):
        if market_context.get(key) is not None:
            summary[key] = market_context.get(key)
    entry_context = _session_overlap_label(trade)
    if entry_context:
        summary["entry_session_context"] = entry_context
    stop_summary = _stop_management_summary(trade)
    if stop_summary:
        summary["stop_management_summary"] = stop_summary
    return summary or None


def _resolve_prev_loss_ref(trade, trades_by_ref):
    if not (
        trade.get("is_post_loss_same_symbol_trade")
        or (
            trade.get("same_symbol_reentry")
            and (_safe_float(trade.get("prev_symbol_trade_pnl")) or 0) < 0
        )
    ):
        return None, None

    prev_pnl = (
        trade.get("prev_symbol_trade_pnl")
        if trade.get("prev_symbol_trade_pnl") is not None
        else trade.get("prev_trade_pnl")
    )
    loss_ref = None
    for candidate in trades_by_ref.values():
        if candidate is trade:
            continue
        if _display_symbol(candidate) != _display_symbol(trade):
            continue
        if _safe_float(candidate.get("pnl")) == _safe_float(prev_pnl):
            loss_ref = _trade_ref(candidate)
            break
    return loss_ref, _round(prev_pnl)


def _build_universal_trade(trade, *, strategy_ref):
    ref = _trade_ref(trade)
    entry = {
        "ref": ref,
        "symbol": _display_symbol(trade),
        "side": trade.get("side"),
        "outcome": _outcome(trade),
        "pnl": _round(trade.get("pnl")),
    }
    entry.update(_session_fields(trade))

    date_label = _trade_date_label(trade)
    if date_label:
        entry["trade_date_label"] = date_label

    if strategy_ref:
        entry["strategy_ref"] = strategy_ref
    if trade.get("realized_rr") is not None:
        entry["realized_rr"] = _round(trade.get("realized_rr"))
    if trade.get("planned_rr") is not None:
        entry["planned_rr"] = _round(trade.get("planned_rr"))
    if trade.get("tp_capture_pct") is not None:
        entry["tp_capture_pct"] = _round(trade.get("tp_capture_pct"))
    if trade.get("closed_before_tp") is not None:
        entry["closed_before_tp"] = bool(trade.get("closed_before_tp"))
    if trade.get("closed_before_sl") is not None:
        entry["closed_before_sl"] = bool(trade.get("closed_before_sl"))
    if trade.get("trade_risk_pct") is not None:
        entry["trade_risk_pct"] = _round(trade.get("trade_risk_pct"), 4)
    if trade.get("duration_minutes") is not None:
        entry["duration_minutes"] = _round(trade.get("duration_minutes"))
    if trade.get("risk_pct_vs_prev"):
        entry["risk_pct_vs_prev"] = trade.get("risk_pct_vs_prev")
    if trade.get("risk_pct_vs_prev_symbol"):
        entry["risk_pct_vs_prev_symbol"] = trade.get("risk_pct_vs_prev_symbol")
    if trade.get("loss_streak_before_trade") is not None:
        entry["loss_streak_before_trade"] = trade.get("loss_streak_before_trade")
    if trade.get("outlier_size"):
        entry["risk_outlier"] = True

    if trade.get("is_bundle"):
        entry["is_bundle"] = True
        bundle_count = trade.get("bundle_trade_count")
        if bundle_count:
            entry["bundle_trade_count"] = int(bundle_count)
            entry["bundle_summary"] = f"{int(bundle_count)} split entries"

    for flag in (
        "is_post_loss_trade",
        "is_post_loss_same_symbol_trade",
        "same_trade_idea_reentry",
        "same_symbol_reentry",
    ):
        if trade.get(flag):
            entry[flag] = True

    if trade.get("trade_note"):
        entry["trade_note"] = trade.get("trade_note")

    market_summary = _market_context_summary(trade)
    if market_summary:
        entry["market_context_summary"] = market_summary

    # Futures proxy replay: annotate and suppress unreliable bar-derived fields.
    # Suppression is idempotent now (proxy trades have no bars in Phase 1) but defines
    # the contract for Phase 2 when proxy bars may be present.
    if trade.get("proxy_replay_status") or trade.get("proxy_replay_symbol"):
        entry["replay_accuracy"] = "approximate"
        if trade.get("proxy_replay_symbol"):
            entry["chart_price_source"] = "cfd_proxy"
        # Suppress fields that are meaningless when chart bars are from a different instrument
        _PROXY_SUPPRESSED = (
            "tp_capture_pct",
            "closed_before_tp",
            "closed_before_sl",
        )
        for _k in _PROXY_SUPPRESSED:
            entry.pop(_k, None)
        # Suppress bar-derived numeric microstructure inside market_context_summary
        if entry.get("market_context_summary"):
            _ms = entry["market_context_summary"]
            for _k in ("mfe_r", "mae_r", "post_exit_direction", "post_exit_tp_reached"):
                _ms.pop(_k, None)
            if not _ms:
                entry.pop("market_context_summary", None)

    return entry


def _build_post_loss_sequences(trades, post_loss_response):
    """Canonical post-loss / re-entry rows — single source for sequence evidence."""
    trades_by_ref = _trade_by_ref(trades)
    sequences = []
    seen = set()

    def add_row(*, loss_ref, next_ref, source_seq=None):
        key = (loss_ref, next_ref)
        if key in seen:
            return
        loss_trade = trades_by_ref.get(loss_ref)
        next_trade = trades_by_ref.get(next_ref)
        if not loss_trade or not next_trade:
            return
        seen.add(key)

        same_symbol = _display_symbol(loss_trade) == _display_symbol(next_trade)
        same_side = (loss_trade.get("side") or "").upper() == (next_trade.get("side") or "").upper()
        row = {
            "loss_ref": loss_ref,
            "next_ref": next_ref,
            "same_symbol": same_symbol,
            "same_side": same_side,
            "same_trade_idea": bool(next_trade.get("same_trade_idea_reentry")),
        }

        minutes = next_trade.get("minutes_since_prev_symbol_close") or next_trade.get(
            "minutes_since_prev_close"
        )
        if minutes is not None:
            row["minutes_after_loss"] = _round(minutes)

        risk_change = None
        if source_seq:
            risk_change = _map_risk_change(source_seq.get("risk_change"))
        if not risk_change:
            risk_change = _map_risk_change(_risk_change_label(next_trade))
        if risk_change:
            row["risk_change"] = risk_change

        prev_loss = None
        if source_seq and source_seq.get("loss_pnl") is not None:
            prev_loss = _round(source_seq.get("loss_pnl"))
        if prev_loss is None:
            prev_loss = _round(loss_trade.get("pnl"))
        if prev_loss is not None:
            row["previous_loss_pnl"] = prev_loss

        next_pnl = _round(next_trade.get("pnl"))
        if next_pnl is not None:
            row["next_pnl"] = next_pnl

        next_outcome = (source_seq or {}).get("next_outcome") or _outcome(next_trade)
        if next_outcome:
            row["next_outcome"] = next_outcome

        if next_outcome == "win" and same_symbol:
            row["sequence_outcome"] = "winning_retry"

        sequences.append(row)

    for trade in trades:
        if not (
            trade.get("is_post_loss_same_symbol_trade")
            or (
                trade.get("same_symbol_reentry")
                and (_safe_float(trade.get("prev_symbol_trade_pnl")) or 0) < 0
            )
        ):
            continue
        next_ref = _trade_ref(trade)
        loss_ref = None
        source_seq = None
        for seq in (post_loss_response or {}).get("sequences") or []:
            if seq.get("next_ref") == next_ref:
                loss_ref = seq.get("loss_ref")
                source_seq = seq
                break
        if loss_ref is None:
            loss_ref, _ = _resolve_prev_loss_ref(trade, trades_by_ref)
        if not loss_ref:
            continue
        add_row(loss_ref=loss_ref, next_ref=next_ref, source_seq=source_seq)

    for seq in (post_loss_response or {}).get("sequences") or []:
        loss_ref = seq.get("loss_ref")
        next_ref = seq.get("next_ref")
        if not loss_ref or not next_ref:
            continue
        add_row(loss_ref=loss_ref, next_ref=next_ref, source_seq=seq)

    return sequences


def _attach_frame_triggers(sequences, coaching_frame_triggers):
    if not sequences or not coaching_frame_triggers:
        return sequences
    for row in sequences:
        refs_set = {row.get("loss_ref"), row.get("next_ref")}
        frame_triggers = []
        for trigger in coaching_frame_triggers:
            trigger_refs = set(trigger.get("refs") or [])
            if trigger_refs & refs_set:
                frame = trigger.get("frame")
                if frame and frame not in frame_triggers:
                    frame_triggers.append(frame)
        if frame_triggers:
            row["frame_triggers"] = frame_triggers
    return sequences


def _claim_scope_level(closed_trades, confidence_envelope):
    if closed_trades < 3:
        return "narrow"
    if (confidence_envelope or {}).get("level") == "strong" and closed_trades >= 5:
        return "broad"
    return "moderate"


def _evidence_boundary(claim_scope):
    review_mode = "focused_sequence" if claim_scope == "narrow" else "standard_review"
    instructions = {
        "narrow": _NARROW_INSTRUCTION,
        "moderate": _MODERATE_INSTRUCTION,
        "broad": _BROAD_INSTRUCTION,
    }
    return {
        "claim_scope": claim_scope,
        "review_mode": review_mode,
        "instruction": instructions.get(claim_scope, _MODERATE_INSTRUCTION),
    }


def _issue_scope(execution_outcome, post_loss_response):
    level = (execution_outcome or {}).get("issue_evidence_level")
    if not level or level == "none":
        return None
    if bool((post_loss_response or {}).get("repeated_increased_risk")):
        return "repeated"
    if level in {"isolated", "moderate", "strong", "repeated"}:
        return level
    return level


def _confirmed_revenge_count(summary):
    return int((summary or {}).get("confirmed_revenge_trade_count") or 0)


def _build_coaching_frame_triggers(
    *,
    trades,
    post_loss_sequences,
    summary,
    execution_outcome,
    single_trade_dominance,
):
    triggers = []
    trades_by_ref = _trade_by_ref(trades)
    confirmed_count = _confirmed_revenge_count(summary)

    for seq in post_loss_sequences:
        if seq.get("sequence_outcome") != "winning_retry":
            continue
        next_trade = trades_by_ref.get(seq.get("next_ref"))
        if not next_trade:
            continue
        if next_trade.get("is_revenge"):
            continue
        if _risk_change_label(next_trade) == "larger":
            continue
        triggers.append(
            {
                "frame": "reward_cost_mislesson",
                "trigger": "winning_same_symbol_retry_after_loss",
                "refs": [seq.get("loss_ref"), seq.get("next_ref")],
            }
        )
        break

    reentry_trades = [
        t
        for t in trades
        if t.get("is_post_loss_same_symbol_trade")
        or t.get("same_trade_idea_reentry")
        or t.get("same_symbol_reentry")
    ]
    if len(reentry_trades) >= 2:
        risk_changes = [_risk_change_label(t) for t in reentry_trades]
        net_reentry = sum(_safe_float(t.get("pnl")) or 0.0 for t in reentry_trades)
        stable_risk = all(change in (None, "same") for change in risk_changes)
        if stable_risk and net_reentry <= 0 and confirmed_count == 0:
            triggers.append(
                {
                    "frame": "calm_decision_leak",
                    "trigger": "controlled_repetition_after_loss",
                    "refs": [_trade_ref(t) for t in reentry_trades[:3] if _trade_ref(t)],
                }
            )

    net_pnl = _safe_float((summary or {}).get("net_pnl"))
    closed_count = int((summary or {}).get("closed_trades") or len(_closed_trades(trades)))
    largest_share = _safe_float((summary or {}).get("largest_trade_abs_pnl_share_pct"))
    if net_pnl is not None and net_pnl > 0 and closed_count >= 3:
        masked = bool(single_trade_dominance)
        if not masked and largest_share is not None and largest_share >= 60:
            masked = True
        if not masked:
            winners = sorted((_safe_float(t.get("pnl")) or 0.0 for t in trades), reverse=True)
            if winners and (net_pnl - winners[0]) < 0:
                masked = True
        if masked:
            dominant_ref = (single_trade_dominance or {}).get("dominant_ref")
            triggers.append(
                {
                    "frame": "single_trade_masked_week",
                    "trigger": "one_winner_changed_week_result",
                    "refs": [dominant_ref] if dominant_ref else [],
                }
            )

    coaching_stance = (execution_outcome or {}).get("coaching_stance")
    if coaching_stance == "good_week_but_habits_are_leaking":
        primary_issue = (execution_outcome or {}).get("primary_issue")
        if primary_issue and not any(item.get("frame") == "reward_cost_mislesson" for item in triggers):
            refs = []
            for seq in post_loss_sequences[:1]:
                refs.extend([seq.get("loss_ref"), seq.get("next_ref")])
            triggers.append(
                {
                    "frame": "acknowledge_profit_focus_leak",
                    "trigger": primary_issue,
                    "refs": [ref for ref in refs if ref],
                }
            )

    return triggers


def _build_constraints(
    *,
    closed_trades,
    summary,
    execution_outcome,
    risk_authority,
):
    do_not_claim = []
    confirmed_count = _confirmed_revenge_count(summary)
    if confirmed_count == 0:
        do_not_claim.append("Do not call this confirmed revenge.")
    risk_authority = risk_authority or {}
    if not risk_authority.get("risk_judgment_allowed"):
        do_not_claim.append("Do not make account-risk claims from lot size.")
    elif risk_authority.get("stable") is True:
        do_not_claim.append("Do not claim risk escalation.")
    if closed_trades < 3:
        do_not_claim.extend(
            [
                "Do not call one sequence a repeated pattern.",
                "Do not overstate broad improvement from a 1-2 trade sample.",
                "Do not dismiss the week as low-value or low-confidence.",
            ]
        )
    coaching_stance = (execution_outcome or {}).get("coaching_stance")
    if coaching_stance == "good_week_but_habits_are_leaking":
        do_not_claim.append("Do not let a profitable week validate the process leak.")
    return {"do_not_claim": do_not_claim}


def _build_relevant_continuity(full_payload, primary_issue, post_loss_sequences):
    recent_experiments = full_payload.get("recent_experiments") or []
    issue_text = (primary_issue or "").lower()
    if not any(
        token in issue_text for token in ("loss", "reentry", "re-entry", "same_symbol", "retry", "symbol")
    ):
        if not post_loss_sequences:
            return {}

    matched_rules = []
    for item in recent_experiments:
        text = (item.get("text") or "").lower()
        if any(keyword in text for keyword in _REENTRY_EXPERIMENT_KEYWORDS):
            rule_text = (item.get("text") or "").strip()
            if len(rule_text) > _PRIOR_RULE_MAX_LEN:
                rule_text = rule_text[:_PRIOR_RULE_MAX_LEN].rstrip() + "..."
            matched_rules.append(rule_text)

    if not matched_rules:
        return {}

    return {
        "theme": "post-loss re-entry control",
        "prior_rules": matched_rules[:3],
    }


def _build_emotional_context(emotional_index):
    emotional_index = emotional_index or {}
    signals = emotional_index.get("signals") or {}
    return {
        "label": emotional_index.get("label"),
        "confirmed_revenge_count": signals.get("confirmed_revenge_trade_count", 0),
        "heuristic_revenge_count": signals.get("heuristic_revenge_trade_count", 0),
        "confirmed_reactive_count": signals.get("confirmed_reactive_trade_count", 0),
        "heuristic_reactive_count": signals.get("heuristic_reactive_trade_count", 0),
        "confirmed_corrective_count": signals.get("confirmed_corrective_trade_count", 0),
        "heuristic_corrective_count": signals.get("heuristic_corrective_trade_count", 0),
        "self_report_mismatch": bool(emotional_index.get("self_report_mismatch")),
    }


def _build_historical_context_summary(historical_context):
    historical_context = historical_context or {}
    summary = historical_context.get("summary") or {}
    if not summary and not historical_context.get("window_days"):
        return None
    return {
        "window_days": historical_context.get("window_days"),
        "historical_win_rate": _round(summary.get("win_rate")),
        "historical_expectancy": _round(summary.get("expectancy")),
    }


def _build_four_week_summary(four_week_patterns, primary_issue):
    patterns = four_week_patterns or {}
    behaviour = patterns.get("behaviour_patterns") or {}
    if not behaviour:
        return None
    issue = (primary_issue or "").lower()
    if not any(token in issue for token in ("loss", "reentry", "re-entry", "revenge", "reactive")):
        return None
    return {
        "avg_post_loss_reentry_count": _round(behaviour.get("avg_post_loss_reentry_count")),
        "avg_larger_size_after_loss_count": _round(behaviour.get("avg_larger_size_after_loss_count")),
    }


def _slim_coaching_hypotheses(hypotheses):
    slim = []
    for item in (hypotheses or [])[:3]:
        if not item:
            continue
        entry = {
            "type": item.get("type"),
            "rank": item.get("rank"),
            "confidence": item.get("confidence"),
            "evidence_refs": item.get("evidence_refs") or [],
        }
        if item.get("writing_shape"):
            entry["writing_shape"] = item.get("writing_shape")
        if item.get("better_lesson_hint"):
            entry["better_lesson_hint"] = item.get("better_lesson_hint")
        slim.append(entry)
    return slim


def _compress_revenge_evidence(revenge_evidence):
    revenge_evidence = revenge_evidence or {}
    if not revenge_evidence:
        return None
    return {
        "pattern_class": revenge_evidence.get("pattern_class"),
        "confirmed_count": revenge_evidence.get("confirmed_count", 0),
        "heuristic_count": revenge_evidence.get("heuristic_count", 0),
    }


def _slim_risk_authority(risk_authority):
    risk_authority = risk_authority or {}
    if not risk_authority:
        return None
    slim = {}
    for key in ("basis", "stable", "risk_judgment_allowed"):
        if risk_authority.get(key) is not None:
            slim[key] = risk_authority[key]
    return slim or None


def _slim_single_trade_dominance(single_trade_dominance):
    single_trade_dominance = single_trade_dominance or {}
    if not single_trade_dominance:
        return None
    slim = {}
    for key in ("dominant_symbol", "dominant_ref", "abs_pnl_share_pct"):
        if single_trade_dominance.get(key) is not None:
            slim[key] = single_trade_dominance[key]
    return slim or None


def _maybe_session_concentration(session_concentration, closed_trades):
    session_concentration = session_concentration or {}
    if closed_trades < 3:
        return None
    share = _safe_float(session_concentration.get("share_pct"))
    if share is None or share < 60:
        return None
    return {
        "dominant_session": session_concentration.get("dominant_session"),
        "share_pct": session_concentration.get("share_pct"),
    }


def _maybe_exit_quality(breakdowns, execution_outcome):
    exit_quality = breakdowns.get("exit_quality") or {}
    primary_issue = (execution_outcome or {}).get("primary_issue") or ""
    if "tp" not in primary_issue and "exit" not in primary_issue and "target" not in primary_issue:
        tp_shortfalls = breakdowns.get("tp_capture_shortfalls") or {}
        if not tp_shortfalls.get("recurring"):
            return None
    return {
        "closed_before_tp_count": exit_quality.get("closed_before_tp_count", 0),
        "avg_tp_capture_pct": _round(exit_quality.get("avg_tp_capture_pct")),
    }


def build_universal_weekly_payload(full_payload: dict) -> dict:
    """Transform the internal weekly computation payload into the universal audit/model JSON."""
    trades = list(full_payload.get("trades") or [])
    closed = _closed_trades(trades)
    closed_trades = int((full_payload.get("summary") or {}).get("closed_trades") or len(closed))
    trade_idea_count = sum(1 for trade in trades if trade.get("closed_at")) or closed_trades
    breakdowns = full_payload.get("current_week_breakdowns") or {}
    execution_outcome = breakdowns.get("execution_outcome") or {}
    risk_authority = breakdowns.get("risk_authority") or {}
    post_loss_response = breakdowns.get("post_loss_response") or {}
    single_trade_dominance = breakdowns.get("single_trade_dominance")
    revenge_evidence = breakdowns.get("revenge_evidence") or {}
    tp_capture_shortfalls = breakdowns.get("tp_capture_shortfalls") or {}
    session_concentration = breakdowns.get("session_concentration") or {}
    summary = full_payload.get("summary") or {}
    primary_issue = execution_outcome.get("primary_issue")
    confidence_envelope = full_payload.get("confidence_envelope") or {}

    claim_scope = _claim_scope_level(closed_trades, confidence_envelope)
    strategies_used, strategy_ref_map = _build_strategy_context(trades)
    universal_trades = []
    for trade in closed or trades:
        universal_trades.append(
            _build_universal_trade(
                trade,
                strategy_ref=strategy_ref_map.get(id(trade)),
            )
        )

    post_loss_sequences = _build_post_loss_sequences(trades, post_loss_response)
    coaching_frame_triggers = _build_coaching_frame_triggers(
        trades=trades,
        post_loss_sequences=post_loss_sequences,
        summary=summary,
        execution_outcome=execution_outcome,
        single_trade_dominance=single_trade_dominance,
    )
    post_loss_sequences = _attach_frame_triggers(post_loss_sequences, coaching_frame_triggers)

    week_summary = {
        "closed_trades": closed_trades,
        "trade_idea_count": trade_idea_count,
        "net_pnl": _round(summary.get("net_pnl")),
        "win_rate": _round(summary.get("win_rate")),
        "best_trade_pnl": _round(summary.get("best_trade_pnl")),
        "worst_trade_pnl": _round(summary.get("worst_trade_pnl")),
        "max_drawdown": _round(summary.get("max_drawdown")),
        "largest_trade_symbol": _display_symbol({"symbol": summary.get("largest_trade_symbol")})
        if summary.get("largest_trade_symbol")
        else None,
        "largest_trade_abs_pnl_share_pct": _round(summary.get("largest_trade_abs_pnl_share_pct")),
        "top_symbol_by_trade_count": summary.get("top_symbol_by_trade_count"),
        "top_symbol_trade_share_pct": _round(summary.get("top_symbol_trade_share_pct")),
        "coaching_stance": execution_outcome.get("coaching_stance"),
        "primary_issue": primary_issue,
    }
    issue_scope = _issue_scope(execution_outcome, post_loss_response)
    if issue_scope:
        week_summary["issue_scope"] = issue_scope

    payload = {
        "evidence_boundary": _evidence_boundary(claim_scope),
        "week_summary": week_summary,
        "strategy_context": {"strategies_used": strategies_used},
        "trades": universal_trades,
        "post_loss_sequences": post_loss_sequences,
        "constraints": _build_constraints(
            closed_trades=closed_trades,
            summary=summary,
            execution_outcome=execution_outcome,
            risk_authority=risk_authority,
        ),
        "coaching_frame_triggers": coaching_frame_triggers,
    }

    slim_risk = _slim_risk_authority(risk_authority)
    if slim_risk:
        payload["risk_authority"] = slim_risk

    if post_loss_response or post_loss_sequences:
        payload["pattern_count"] = post_loss_response.get("pattern_count", len(post_loss_sequences))
        payload["repeated_increased_risk"] = bool(post_loss_response.get("repeated_increased_risk"))
        biggest = post_loss_response.get("biggest_loss") or {}
        if biggest.get("loss_ref"):
            payload["biggest_loss_ref"] = biggest.get("loss_ref")

    user_profile = full_payload.get("user_profile") or {}
    if any(user_profile.get(key) for key in ("trading_style", "instruments", "experience_level")):
        payload["user_profile"] = {
            key: user_profile.get(key)
            for key in ("trading_style", "instruments", "experience_level")
            if user_profile.get(key)
        }

    weekly_checkin = full_payload.get("weekly_checkin") or {}
    if any(
        weekly_checkin.get(key)
        for key in ("emotional_state", "plan_adherence", "execution_quality", "additional_context")
    ):
        payload["weekly_checkin"] = {
            key: weekly_checkin.get(key)
            for key in (
                "emotional_state",
                "plan_adherence",
                "execution_quality",
                "additional_context",
            )
            if weekly_checkin.get(key)
        }

    emotional_context = _build_emotional_context(full_payload.get("emotional_index"))
    if any(v not in (None, 0, False) for v in emotional_context.values()):
        payload["emotional_context"] = emotional_context

    tone_context = full_payload.get("tone_context") or {}
    if tone_context.get("mode"):
        payload["tone_context"] = {"mode": tone_context.get("mode")}

    experiment_context = full_payload.get("experiment_context") or {}
    if experiment_context:
        payload["experiment_context"] = {
            "eligible": bool(experiment_context.get("eligible")),
            "trade_idea_count": experiment_context.get("trade_idea_count"),
            "min_required": experiment_context.get("min_required"),
        }

    compressed_revenge = _compress_revenge_evidence(revenge_evidence)
    if compressed_revenge:
        payload["revenge_evidence"] = compressed_revenge

    slim_dominance = _slim_single_trade_dominance(single_trade_dominance)
    if slim_dominance:
        payload["single_trade_dominance"] = slim_dominance

    if tp_capture_shortfalls.get("recurring"):
        payload["tp_capture_shortfalls"] = tp_capture_shortfalls

    session_block = _maybe_session_concentration(session_concentration, closed_trades)
    if session_block:
        payload["session_concentration"] = session_block

    exit_quality = _maybe_exit_quality(breakdowns, execution_outcome)
    if exit_quality:
        payload["exit_quality"] = exit_quality

    hypotheses = _slim_coaching_hypotheses(breakdowns.get("coaching_hypotheses"))
    if hypotheses:
        payload["coaching_hypotheses"] = hypotheses

    historical_summary = _build_historical_context_summary(full_payload.get("historical_context"))
    if historical_summary:
        payload["historical_context_summary"] = historical_summary

    four_week_summary = _build_four_week_summary(
        full_payload.get("four_week_patterns"),
        primary_issue,
    )
    if four_week_summary:
        payload["four_week_behaviour_summary"] = four_week_summary

    relevant_continuity = _build_relevant_continuity(full_payload, primary_issue, post_loss_sequences)
    if relevant_continuity:
        payload["relevant_continuity"] = relevant_continuity

    performance_trends = full_payload.get("performance_trends") or {}
    if performance_trends.get("ei_trend"):
        payload["performance_trends"] = {"ei_trend": performance_trends.get("ei_trend")}

    return payload


def format_universal_weekly_payload(payload: dict) -> str:
    """Format universal payload JSON for the model user message."""
    return "WEEKLY REVIEW DATA\n\n" + json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)
