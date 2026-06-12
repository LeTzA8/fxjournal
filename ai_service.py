import copy
import hashlib
import json
import logging
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func
from sqlalchemy.orm import load_only, selectinload

from helpers.ai_market_context import build_trade_market_context
from helpers.journal_context import format_journal_payload_for_prompt
from helpers.risk_comparison import enrich_serialized_trades_with_risk_comparison
from helpers.scoring import compute_emotional_index, prepare_closed_trade_signal_inputs
from helpers.trade_analysis import (
    build_trade_annotations as _build_trade_annotations,
    coerce_float as _coerce_float,
    get_trade_identity as _get_trade_identity,
    get_trade_session as _get_trade_session,
)
from helpers.universal_weekly_payload import build_universal_weekly_payload, format_universal_weekly_payload
from helpers.weekly_signals import build_weekly_signals as _build_weekly_signals
from helpers.weekly_review_ref_rewrite import (
    build_weekly_review_citation_lookup,
    parse_json_blob,
    rewrite_review_text_refs,
)
from models import (
    AIGeneratedResponse,
    AIPromptHistory,
    Trade,
    TradeAccount,
    TradeBars,
    User,
    UserProfile,
    WeeklyCheckin,
    db,
)
from trading import (
    build_trade_analytics,
    classify_trading_session,
    did_trade_reach_level,
    ensure_utc_aware,
    format_trade_symbol,
    get_trade_account_type,
    get_trade_level_validation_issues,
    normalize_account_type,
    risk_size_claim_phrase,
    is_extremely_long_duration_minutes,
    merge_bundled_trades,
    resolve_net_pnl,
    resolve_planned_risk_dollars,
)
from helpers.utils import utcnow_naive


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_PROMPT_FILE = "dashboard_advice.txt"
DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_MAX_OUTPUT_TOKENS = 1500
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_HISTORICAL_CONTEXT_DAYS = 90
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
WEEKLY_DASHBOARD_KIND = "weekly_dashboard_advice"
DEFAULT_REWRITE_PROMPT_FILE = "dashboard_advice_rewrite.txt"
DEFAULT_WEEKLY_REVIEW_CHAT_PROMPT_FILE = "weekly_review_followup.txt"
DEFAULT_JOURNAL_CHAT_PROMPT_FILE = "journal_chat.txt"
WEEKLY_REVIEW_CHAT_PROMPT_VERSION = "weekly_review_followup_v3"
DEFAULT_WEEKLY_REVIEW_CHAT_MAX_OUTPUT_TOKENS = 520
WEEKLY_REVIEW_CHAT_SUGGESTIONS_MARKER = "\n---FXJ_SUGGESTIONS---\n"
WEEKLY_REVIEW_CHAT_STARTER_PROMPT_LIMIT = 3
WEEKLY_REVIEW_CHAT_DYNAMIC_PROMPT_LIMIT = 3
JOURNAL_CHAT_PROMPT_VERSION = "journal_v1"
WEEKLY_MARKET_TIMEZONE = ZoneInfo("America/New_York")
WEEKLY_CUTOFF_WEEKDAY = 4
WEEKLY_CUTOFF_HOUR = 17
WEEKLY_CUTOFF_MINUTE = 30
WEEKLY_ACTIVITY_LOOKBACK_DAYS = 3
MIN_TRADE_IDEAS_FOR_WEEKLY_REVIEW = 1
MIN_TRADE_IDEAS_FOR_EXPERIMENT = MIN_TRADE_IDEAS_FOR_WEEKLY_REVIEW
# Backward-compatible alias used by route imports/templates.
MIN_CLOSED_TRADES_FOR_ADVICE = MIN_TRADE_IDEAS_FOR_WEEKLY_REVIEW
logger = logging.getLogger(__name__)

_DASHBOARD_ADVICE_PREFIX_REPLACEMENTS = (
    ("\u2192 Improve this week:", "Improve this week:"),
    ("\u2192 You're already strong at:", "You're already strong at:"),
)
REVIEW_RESPONSE_FORMAT_VERSION = "weekly_cited_review_v2"
REVIEW_MAX_REFS_PER_ITEM = 3
REVIEW_JSON_OUTPUT_INSTRUCTIONS = """
Return valid JSON only. Do not use markdown fences.
Use this exact shape:
{
  "summary": {
    "text": "2-3 sentence main insight. First sentence starts with a human conclusion and includes a count or concrete trade example.",
    "refs": ["T1", "B1"]
  },
  "takeaways": [
    {
      "text": "One sentence explaining the implication of the evidence, not just the event that happened.",
      "refs": ["T2"]
    }
  ],
  "improvement": {
    "text": "Improve this week: One specific testable action tied to the main insight.",
    "refs": []
  },
  "strength": {
    "text": "You're already strong at: One evidence-backed behavior to keep.",
    "refs": []
  },
  "experiment": {
    "text": "A single measurable experiment to run this week.",
    "refs": []
  }
}

Rules for refs:
- Use only ref values present in trades.
- Prefer 1-2 refs per item, maximum 3.
- Across summary.text and takeaways, include at least one cited representative trade or bundle when any trades[].ref is available and a non-misleading example exists.
- Use a second cited trade only when it creates a useful contrast (best vs worst, before vs after a loss, early exit vs cleaner hold, or session/context contrast).
- Use bundle refs like B1 for bundled trade ideas and trade refs like T1 for solo trade ideas.
- improvement.refs and strength.refs must always be an empty list.
- experiment.refs is optional and may be empty when the experiment is generalized.
- The UI appends ref labels directly after the text. A ref that does not match a symbol explicitly named in the text will appear as an orphaned label. Only add a ref when the text contains the exact symbol name or bundle description that the ref represents.
- Trade references must appear inside sentences, not as trailing fragments. If a sentence cannot naturally name the trade or symbol, leave refs empty.
- If summary.text or a takeaway names a specific symbol (e.g. "GBPJPY") or bundle, include that trade's ref. Do not leave the refs empty in that case.
- If summary.text or a takeaway makes an aggregate or pattern observation ("trades that followed losses", "two London entries", "the session pattern") without naming a specific symbol, refs must be empty — not filled with implied trades.
- Never add a ref for a trade the text does not explicitly name. One named trade = one ref. Two named trades = two refs. Pattern observation = zero refs.
- If two refs would render as the same visible label because they share symbol/date, do not attach both to one short sentence. Either write it as an aggregate pattern with refs empty, or name only the single trade that proves the point.

Rules for text fields:
- summary.text must stay as the single opening paragraph.
- takeaways should contain 1-3 items by default. Use 4 only when the fourth item is genuinely distinct and useful.
- summary.text must identify one dominant diagnosis for the week, not merely restate performance.
- Use week_summary.coaching_stance and week_summary.primary_issue as stance rails before writing. Treat week_summary.primary_issue as the default lead signal when present.
- Use week_summary.issue_scope for intensity. If it is isolated, frame the issue as one watch item, not a repeated habit. If it is repeated or strong, be more direct.
- Use coaching_frame_triggers and coaching_hypotheses as concise writing cues, not as backend-written review copy. Do not invent traps, motives, danger windows, false lessons, or better lessons beyond the trigger facts and instructions.
- If coaching_frame_triggers or coaching_hypotheses includes reward_cost_mislesson / outcome_disguised_habit framing, use Reward -> Cost -> Mislesson -> Better lesson even when coaching_hypotheses is empty or absent.
- When using reward_cost_mislesson, explain the contrast pair: which trade rewarded the habit, which trade exposed the cost or risk, and what the trader may mislearn from the winner. Avoid generic lessons like "a winning retry does not make the habit safe" unless the review section names the rewarded trade or symbol and explains the sequence mechanism.
- For reward_cost_mislesson, use this writing shape across summary/takeaways: Reward -> Cost -> Mislesson -> Better lesson. Do not flatten it into "the problem is the decision after the loss." Name the rewarded trade, say what it reinforced, name the exposed cost or risk, then state the corrected rule.
- Do not restate the same mechanism twice. If the core idea is already stated, deepen it with the contrast, mislesson, or corrected rule instead of repeating it.
- When a flagged post-loss same-symbol or same-idea re-entry has a substantive trade note (sweep/liquidity, HTF thesis unchanged, reclaim, same zone, FVG rejection, better confirmation), mention the note directly; credit what may be legitimate; do not treat the post-trade note or winning outcome as proof; frame the risk as planned re-entry vs post-hoc justification, not confirmed revenge/repair; improvement should require writing the re-entry reason before entering next time.
- A flat clean week is neutral, not a loss; hold the process steady and suggest only a small measurement or refinement.
- When constraints.do_not_claim blocks risk claims, obey them even when the week was profitable.
- Obey risk_authority.risk_judgment_allowed when it blocks account-risk escalation claims.
- Never mention internal labels such as week_archetype, execution_class, coaching_stance, primary_issue, ranked_issues, issue_evidence_level, or do_not_lead_with.
- A profitable week with leaky or bad execution should acknowledge the good result without endorsing the leak; a losing week with good execution should protect confidence and avoid overhauling the process.
- summary.text must start with the human conclusion, then support it with data.
- summary.text must include a count or concrete trade example and, when available, combine at least two signals such as timing, range location, session, volatility, sequence, exit handling, or explicit size/risk context.
- Prefer making summary.text or the first takeaway name the representative trade that proves the diagnosis, so the review has at least one visible trade citation.
- Entry candle fields are supporting evidence only; never make them the whole diagnosis.
- Every takeaway must deepen the same main insight by connecting evidence to a decision or behavior the trader can change.
- A takeaway is not valid if it only says what happened. It must explain what the evidence means for the trader's next decision.
- improvement.text must include the "Improve this week:" prefix exactly once.
- improvement.text must directly address the main insight, be specific and testable, and generalize one level up from the evidence without mentioning a specific trade, bundle, exact date, or weekday.
- strength.text must include the "You're already strong at:" prefix exactly once when the strength key is present.
- Do not force a strength. Omit the strength key entirely when the only notable behavior is the leak, when calm during a questionable re-entry would be praised, or when size stability would validate the re-entry.
- strength.text must be grounded in observed data or consistent execution from this week, not generic praise.
- Prefer behavior, execution, session, sizing, or process language in improvement.text over symbol-specific wording.
- experiment.text must be one clear experiment, specific, measurable, and not repetitive of recent experiments.
- improvement.text and experiment.text must not restate the same main rule; experiment should propose a distinct one-week trial (a different lever than improvement, for example session filter, max trades per day, pause rule, or entry gate).
- If improvement.text blocks same-symbol re-entry after a loss, experiment.text must not be another same-symbol cap with logging added. Use a different lever, such as recording skipped retries, checking whether the next trade is a genuinely fresh decision, or measuring the first post-loss decision across all symbols.
- Use plain English in every text field (summary, takeaways, improvement, strength, experiment), not only for sizing: short sentences, everyday trading words, calm coach tone—never academic or consultant speak.
- Examples: prefer "risked more" / "larger position" over "escalated sizing"; "jumped back in after a loss" over "reactive re-engagement"; "closed before your target" over "suboptimal TP capture"; "one trade drove the week" over "outlier dominance."
- When trades provide explicit risk or size-change fields (trade_risk_pct, risk_pct_vs_prev, risk_pct_vs_prev_symbol), prefer those anchors over lot-size inference.
- Read execution_outcome and coaching_hypotheses before citing trades when framing the review.
- Never mention ref aliases like T1 or B2 inside any text field.
- Do not include any keys other than summary, takeaways, improvement, strength, and experiment.
""".strip()


class AIConfigError(RuntimeError):
    pass


class AIRequestError(RuntimeError):
    pass


def get_ai_timezone_name():
    return os.getenv("APP_TIMEZONE", "Asia/Singapore").strip() or "Asia/Singapore"


def get_openai_api_key():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AIConfigError("OPENAI_API_KEY is not configured.")
    return api_key


def normalize_ai_model_name(value):
    model = str(value or "").strip()
    if len(model) >= 2 and model[0] == model[-1] and model[0] in {"'", '"'}:
        model = model[1:-1].strip()
    model = re.sub(r"\s+", "-", model)
    return model


def get_ai_model():
    return normalize_ai_model_name(os.getenv("AI_MODEL", DEFAULT_MODEL)) or DEFAULT_MODEL


def get_ai_timeout_seconds():
    raw_value = os.getenv("AI_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)).strip()
    try:
        timeout_seconds = int(raw_value)
    except ValueError:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    return max(timeout_seconds, 5)


def get_ai_max_output_tokens():
    raw_value = os.getenv("AI_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS)).strip()
    try:
        max_output_tokens = int(raw_value)
    except ValueError:
        max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    return max(max_output_tokens, 64)


def get_weekly_review_chat_max_output_tokens():
    raw_value = os.getenv(
        "WEEKLY_REVIEW_CHAT_MAX_OUTPUT_TOKENS",
        str(DEFAULT_WEEKLY_REVIEW_CHAT_MAX_OUTPUT_TOKENS),
    ).strip()
    try:
        max_output_tokens = int(raw_value)
    except ValueError:
        max_output_tokens = DEFAULT_WEEKLY_REVIEW_CHAT_MAX_OUTPUT_TOKENS
    return max(max_output_tokens, 128)


def get_prompt_source_path(prompt_filename=None):
    filename = str(prompt_filename or DEFAULT_PROMPT_FILE).strip().replace("\\", "/")
    if not filename:
        raise AIConfigError("Prompt filename is required.")
    if not filename.endswith(".txt"):
        raise AIConfigError("Prompt filename must end with .txt.")
    path = (PROMPTS_DIR / filename).resolve()
    if PROMPTS_DIR.resolve() not in path.parents and path != PROMPTS_DIR.resolve():
        raise AIConfigError("Prompt path must stay within the prompts directory.")
    return path


def load_prompt_text(prompt_filename=None):
    path = get_prompt_source_path(prompt_filename)
    try:
        prompt_text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise AIConfigError(f"Prompt file not found: {path}") from exc
    if not prompt_text:
        raise AIConfigError(f"Prompt file is empty: {path}")
    return {
        "prompt_id": Path(path).stem,
        "prompt_text": prompt_text,
        "source_path": str(path.relative_to(Path(__file__).resolve().parent)),
    }


def load_weekly_review_chat_prompt_text():
    return load_prompt_text(DEFAULT_WEEKLY_REVIEW_CHAT_PROMPT_FILE)["prompt_text"]


def load_journal_chat_prompt_text():
    return load_prompt_text(DEFAULT_JOURNAL_CHAT_PROMPT_FILE)["prompt_text"]


def normalize_dashboard_advice_text(value):
    text = str(value or "").strip()
    if not text:
        return ""

    normalized = text
    for broken_prefix, replacement in _DASHBOARD_ADVICE_PREFIX_REPLACEMENTS:
        normalized = normalized.replace(broken_prefix, replacement)

    normalized = re.sub(r"(?im)^[ \t]*[-*]\s*Improve this week:\s*", "Improve this week: ", normalized)
    normalized = re.sub(r"(?im)^[ \t]*Improve this week:\s*", "Improve this week: ", normalized)
    normalized = re.sub(r"(?im)^[ \t]*[-*]\s*You're already strong at:\s*", "You're already strong at: ", normalized)
    normalized = re.sub(r"(?im)^[ \t]*You're already strong at:\s*", "You're already strong at: ", normalized)
    normalized = re.sub(r"(?im)^(Improve this week:\s*)(?:Improve this week:?\s*)+", r"\1", normalized)
    normalized = re.sub(
        r"(?im)^(You're already strong at:\s*)(?:You're already strong at:?\s*)+",
        r"\1",
        normalized,
    )
    return normalized.strip()


def _extract_response_raw_text(response_payload):
    output_text = str(response_payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    text_chunks = []
    for item in response_payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                text_chunks.append(str(content["text"]).strip())
    return "\n\n".join(chunk for chunk in text_chunks if chunk).strip()


def _strip_json_code_fences(value):
    text = str(value or "").strip()
    if not text.startswith("```"):
        return text

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _load_response_json_object(value):
    text = _strip_json_code_fences(value)
    if not text:
        return None

    candidates = [text]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1].strip())

    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            return loaded
    return None


def _normalize_review_item_text(value):
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"^[\-\*\u2022]\s*", "", text)
    return text.strip()


def _normalize_review_improvement_text(value):
    text = normalize_dashboard_advice_text(value)
    text = _normalize_review_item_text(text)
    if not text:
        return ""
    if not text.lower().startswith("improve this week:"):
        text = f"Improve this week: {text}"
    return normalize_dashboard_advice_text(text)


def _normalize_review_strength_text(value):
    text = normalize_dashboard_advice_text(value)
    text = _normalize_review_item_text(text)
    if not text:
        return ""
    if not text.lower().startswith("you're already strong at:"):
        text = f"You're already strong at: {text}"
    return normalize_dashboard_advice_text(text)


def _sanitize_review_refs(value, allowed_refs):
    if not isinstance(value, (list, tuple)):
        return []
    refs = []
    for item in value:
        ref = str(item or "").strip().upper()
        if not ref or ref not in allowed_refs or ref in refs:
            continue
        refs.append(ref)
        if len(refs) >= REVIEW_MAX_REFS_PER_ITEM:
            break
    return refs


def _render_review_text_from_meta(review_meta):
    summary_text = str(((review_meta or {}).get("summary") or {}).get("text") or "").strip()
    takeaways = (review_meta or {}).get("takeaways") or []
    improvement_text = str(((review_meta or {}).get("improvement") or {}).get("text") or "").strip()
    strength_text = str(((review_meta or {}).get("strength") or {}).get("text") or "").strip()

    lines = []
    if summary_text:
        lines.append(summary_text)
    if takeaways:
        if lines:
            lines.append("")
        lines.append("Key Takeaways")
        for item in takeaways:
            takeaway_text = str((item or {}).get("text") or "").strip()
            if takeaway_text:
                lines.append(f"- {takeaway_text}")
    if improvement_text:
        if lines:
            lines.append("")
        lines.append(_normalize_review_improvement_text(improvement_text))
    if strength_text:
        if lines:
            lines.append("")
        lines.append(_normalize_review_strength_text(strength_text))
    return normalize_dashboard_advice_text("\n".join(lines).strip())


def parse_review_response_meta(value):
    loaded = _load_response_json_object(value)
    if not isinstance(loaded, dict):
        return None
    return loaded


def build_dashboard_review_display(response_text, response_meta_json=None):
    review_meta = parse_review_response_meta(response_meta_json) or {}
    summary = review_meta.get("summary") if isinstance(review_meta.get("summary"), dict) else {}
    takeaways = review_meta.get("takeaways") if isinstance(review_meta.get("takeaways"), list) else []
    improvement = review_meta.get("improvement") if isinstance(review_meta.get("improvement"), dict) else {}
    strength = review_meta.get("strength") if isinstance(review_meta.get("strength"), dict) else {}
    experiment = review_meta.get("experiment") if isinstance(review_meta.get("experiment"), dict) else {}

    structured_summary = _normalize_review_item_text(summary.get("text"))
    structured_takeaways = []
    for item in takeaways:
        if not isinstance(item, dict):
            continue
        takeaway_text = _normalize_review_item_text(item.get("text"))
        if not takeaway_text:
            continue
        structured_takeaways.append(
            {
                "text": takeaway_text,
                "refs": [str(ref).strip().upper() for ref in item.get("refs") or [] if str(ref or "").strip()],
            }
        )

    structured_improvement = _normalize_review_improvement_text(improvement.get("text"))
    structured_strength = _normalize_review_strength_text(strength.get("text"))
    structured_experiment = _normalize_review_item_text(experiment.get("text"))
    if structured_summary or structured_takeaways or structured_improvement or structured_strength or structured_experiment:
        summary_refs = [str(ref).strip().upper() for ref in summary.get("refs") or [] if str(ref or "").strip()]
        return {
            "summary": {"text": structured_summary, "refs": summary_refs},
            "takeaways": structured_takeaways,
            "improvement": {"text": structured_improvement, "refs": []},
            "strength": {"text": structured_strength, "refs": []},
            "experiment": {"text": structured_experiment, "refs": []},
            "has_citations": bool(summary_refs or any(item["refs"] for item in structured_takeaways)),
        }

    normalized_text = normalize_dashboard_advice_text(response_text)
    if not normalized_text:
        return {
            "summary": {"text": "", "refs": []},
            "takeaways": [],
            "improvement": {"text": "", "refs": []},
            "strength": {"text": "", "refs": []},
            "experiment": {"text": "", "refs": []},
            "has_citations": False,
        }

    summary_text = normalized_text
    takeaway_lines = []
    improvement_text = ""
    strength_text = ""

    if "Key Takeaways" in normalized_text:
        summary_text, _, remainder = normalized_text.partition("Key Takeaways")
        summary_text = summary_text.strip()
        for raw_line in remainder.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line == "Key Takeaways":
                continue
            if line.lower().startswith("improve this week:"):
                improvement_text = _normalize_review_improvement_text(line)
                continue
            if line.lower().startswith("you're already strong at:"):
                strength_text = _normalize_review_strength_text(line)
                continue
            takeaway_text = _normalize_review_item_text(line)
            if takeaway_text:
                takeaway_lines.append({"text": takeaway_text, "refs": []})
    else:
        lines = [line.strip() for line in normalized_text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line.lower().startswith("improve this week:"):
                improvement_text = _normalize_review_improvement_text(line)
                summary_text = " ".join(lines[:index]).strip()
                break

    return {
        "summary": {"text": summary_text, "refs": []},
        "takeaways": takeaway_lines,
        "improvement": {"text": improvement_text, "refs": []},
        "strength": {"text": strength_text, "refs": []},
        "experiment": {"text": "", "refs": []},
        "has_citations": False,
    }


def _extract_structured_review(response_payload, allowed_refs, *, experiment_eligible=False):
    raw_text = _extract_response_raw_text(response_payload)
    loaded = _load_response_json_object(raw_text)
    if not isinstance(loaded, dict):
        return None

    summary = loaded.get("summary") if isinstance(loaded.get("summary"), dict) else {}
    takeaways = loaded.get("takeaways") if isinstance(loaded.get("takeaways"), list) else []
    improvement = loaded.get("improvement") if isinstance(loaded.get("improvement"), dict) else {}
    strength = loaded.get("strength") if isinstance(loaded.get("strength"), dict) else {}
    experiment = loaded.get("experiment") if isinstance(loaded.get("experiment"), dict) else {}

    summary_text = _normalize_review_item_text(summary.get("text"))
    if not summary_text:
        return None

    structured_takeaways = []
    for item in takeaways[:4]:
        if not isinstance(item, dict):
            continue
        takeaway_text = _normalize_review_item_text(item.get("text"))
        if not takeaway_text:
            continue
        structured_takeaways.append(
            {
                "text": takeaway_text,
                "refs": _sanitize_review_refs(item.get("refs"), allowed_refs),
            }
        )
    if not structured_takeaways:
        return None

    improvement_text = _normalize_review_improvement_text(improvement.get("text"))
    if not improvement_text:
        return None

    strength_text = _normalize_review_strength_text(strength.get("text"))
    experiment_text = _normalize_review_item_text(experiment.get("text"))

    review_meta = {
        "format": REVIEW_RESPONSE_FORMAT_VERSION,
        "summary": {
            "text": summary_text,
            "refs": _sanitize_review_refs(summary.get("refs"), allowed_refs),
        },
        "takeaways": structured_takeaways,
        "improvement": {
            "text": improvement_text,
            "refs": [],
        },
        "strength": {
            "text": strength_text,
            "refs": [],
        },
    }
    if experiment_eligible and experiment_text:
        review_meta["experiment"] = {
            "text": experiment_text,
            "refs": _sanitize_review_refs(experiment.get("refs"), allowed_refs),
        }
    return {
        "response_text": _render_review_text_from_meta(review_meta),
        "response_meta": review_meta,
        "response_meta_json": json.dumps(review_meta, sort_keys=True),
    }


def hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def get_or_create_prompt_history(prompt_filename=None):
    prompt_data = load_prompt_text(prompt_filename)
    prompt_sha256 = hash_text(prompt_data["prompt_text"])
    prompt_history = AIPromptHistory.query.filter_by(prompt_sha256=prompt_sha256).first()
    if prompt_history:
        return prompt_history

    prompt_history = AIPromptHistory(
        prompt_id=prompt_data["prompt_id"],
        prompt_sha256=prompt_sha256,
        prompt_text=prompt_data["prompt_text"],
        source_path=prompt_data["source_path"],
    )
    db.session.add(prompt_history)
    db.session.flush()
    return prompt_history


def format_utc_timestamp(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        normalized = value.astimezone(timezone.utc)
    else:
        normalized = value.replace(tzinfo=timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _to_utc_naive(value):
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=timezone.utc)


def _normalize_optional_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _get_record_value(record, field_name):
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get(field_name)
    return getattr(record, field_name, None)


def get_weekly_dashboard_period(now_utc=None):
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    else:
        current_utc = current_utc.astimezone(timezone.utc)

    market_now = current_utc.astimezone(WEEKLY_MARKET_TIMEZONE)
    current_week_start_market = (market_now - timedelta(days=market_now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    current_cutoff_market = current_week_start_market + timedelta(
        days=WEEKLY_CUTOFF_WEEKDAY,
        hours=WEEKLY_CUTOFF_HOUR,
        minutes=WEEKLY_CUTOFF_MINUTE,
    )

    if market_now >= current_cutoff_market:
        period_start_market = current_week_start_market
        eligible_at_market = current_cutoff_market
    else:
        period_start_market = current_week_start_market - timedelta(days=7)
        eligible_at_market = period_start_market + timedelta(
            days=WEEKLY_CUTOFF_WEEKDAY,
            hours=WEEKLY_CUTOFF_HOUR,
            minutes=WEEKLY_CUTOFF_MINUTE,
        )

    period_end_market = period_start_market + timedelta(days=7)
    next_eligible_at_market = eligible_at_market + timedelta(days=7)

    return {
        "period_start_utc": _to_utc_naive(period_start_market.astimezone(timezone.utc)),
        "period_end_utc": _to_utc_naive(period_end_market.astimezone(timezone.utc)),
        "eligible_at_utc": _to_utc_naive(eligible_at_market.astimezone(timezone.utc)),
        "next_eligible_at_utc": _to_utc_naive(next_eligible_at_market.astimezone(timezone.utc)),
        "market_cutoff_label": "Friday 5:30 PM New York time",
    }


def get_current_market_week_period(now_utc=None):
    current_utc = now_utc or datetime.now(timezone.utc)
    if current_utc.tzinfo is None:
        current_utc = current_utc.replace(tzinfo=timezone.utc)
    else:
        current_utc = current_utc.astimezone(timezone.utc)

    market_now = current_utc.astimezone(WEEKLY_MARKET_TIMEZONE)
    period_start_market = (market_now - timedelta(days=market_now.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    period_end_market = period_start_market + timedelta(days=7)
    return {
        "period_start_utc": _to_utc_naive(period_start_market.astimezone(timezone.utc)),
        "period_end_utc": _to_utc_naive(period_end_market.astimezone(timezone.utc)),
        "market_cutoff_label": "Current market week",
    }


def get_latest_trade_week_period(*, user_id, trade_account_id=None, now_utc=None):
    current_period = get_weekly_dashboard_period(now_utc=now_utc)
    latest_trade_query = Trade.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        latest_trade_query = latest_trade_query.filter_by(trade_account_id=trade_account_id)
    # Prefer trades that fall before the dashboard week's end boundary so we align
    # with the historical "completed week" window. When every closed trade is newer
    # than that boundary (common right after onboarding / mid-week imports), fall
    # back to the latest closed trade so a review week can be resolved for the payload.
    latest_trade = (
        latest_trade_query
        .filter(Trade.closed_at.isnot(None))
        .filter(Trade.closed_at < current_period["period_end_utc"])
        .order_by(Trade.closed_at.desc(), Trade.id.desc())
        .first()
    )
    if latest_trade is None:
        latest_trade = (
            latest_trade_query
            .filter(Trade.closed_at.isnot(None))
            .order_by(Trade.closed_at.desc(), Trade.id.desc())
            .first()
        )
    if latest_trade is None or latest_trade.closed_at is None:
        return None

    latest_trade_utc = latest_trade.closed_at
    if latest_trade_utc.tzinfo is None:
        latest_trade_utc = latest_trade_utc.replace(tzinfo=timezone.utc)
    else:
        latest_trade_utc = latest_trade_utc.astimezone(timezone.utc)
    latest_trade_market = latest_trade_utc.astimezone(WEEKLY_MARKET_TIMEZONE)
    period_start_market = (latest_trade_market - timedelta(days=latest_trade_market.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    period_end_market = period_start_market + timedelta(days=7)
    eligible_at_market = period_start_market + timedelta(
        days=WEEKLY_CUTOFF_WEEKDAY,
        hours=WEEKLY_CUTOFF_HOUR,
        minutes=WEEKLY_CUTOFF_MINUTE,
    )

    return {
        "period_start_utc": _to_utc_naive(period_start_market.astimezone(timezone.utc)),
        "period_end_utc": _to_utc_naive(period_end_market.astimezone(timezone.utc)),
        "eligible_at_utc": _to_utc_naive(eligible_at_market.astimezone(timezone.utc)),
        "next_eligible_at_utc": None,
        "market_cutoff_label": "Latest eligible trade week",
    }


def trade_account_has_prior_weekly_dashboard_ai(*, user_id, trade_account_id):
    if not user_id or trade_account_id is None:
        return False
    return (
        db.session.query(AIGeneratedResponse.id)
        .filter(
            AIGeneratedResponse.user_id == user_id,
            AIGeneratedResponse.trade_account_id == trade_account_id,
            AIGeneratedResponse.kind == WEEKLY_DASHBOARD_KIND,
        )
        .limit(1)
        .first()
        is not None
    )


def weekly_review_generation_past_market_week_cutoff(
    *, user_id, trade_account_id, period, now_utc=None
):
    """
    After the first weekly dashboard AI on a trade account, only allow automatic
    generation once NY Friday 5:30 PM has passed for that review week
    (period["eligible_at_utc"]). The first review may run anytime (onboarding).
    """
    if trade_account_id is None or period is None:
        return False
    if not trade_account_has_prior_weekly_dashboard_ai(
        user_id=user_id, trade_account_id=trade_account_id
    ):
        return True
    eligible_at = period.get("eligible_at_utc")
    if eligible_at is None:
        return False
    if now_utc is None:
        current = datetime.now(timezone.utc)
    else:
        current = now_utc
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
    return _to_utc_naive(current) >= eligible_at


def _query_trades_for_payload(
    *,
    user_id,
    trade_account_id=None,
    period_start_utc=None,
    period_end_utc=None,
    closed_trades_only=False,
):
    trade_query = Trade.query.filter_by(user_id=user_id).options(
        selectinload(Trade.trade_account),
        selectinload(Trade.interpretation),
        selectinload(Trade.trade_profile),
        selectinload(Trade.trade_profile_version),
    )
    if trade_account_id is not None:
        trade_query = trade_query.filter_by(trade_account_id=trade_account_id)
    if closed_trades_only:
        trade_query = trade_query.filter(Trade.closed_at.isnot(None))
        period_filters = []
        if period_start_utc is not None:
            period_filters.append(Trade.closed_at >= period_start_utc)
        if period_end_utc is not None:
            period_filters.append(Trade.closed_at < period_end_utc)
        if period_filters:
            trade_query = trade_query.filter(and_(*period_filters))
        return trade_query.order_by(Trade.closed_at.desc(), Trade.id.desc()).all()

    if period_start_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at >= period_start_utc)
    if period_end_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at < period_end_utc)
    return trade_query.order_by(Trade.opened_at.desc(), Trade.id.desc()).all()


def _serialize_user_profile(user_profile):
    return {
        "trading_style": _normalize_optional_text(_get_record_value(user_profile, "trading_style")),
        "instruments": _normalize_optional_text(_get_record_value(user_profile, "instruments")),
        "experience_level": _normalize_optional_text(_get_record_value(user_profile, "experience_level")),
    }


def _serialize_weekly_checkin(weekly_checkin):
    return {
        "emotional_state": _normalize_optional_text(_get_record_value(weekly_checkin, "emotional_state")),
        "plan_adherence": _normalize_optional_text(_get_record_value(weekly_checkin, "plan_adherence")),
        "execution_quality": _normalize_optional_text(_get_record_value(weekly_checkin, "execution_quality")),
        "additional_context": _normalize_optional_text(_get_record_value(weekly_checkin, "additional_context")),
    }


def _serialize_trade_strategy(trade):
    profile = getattr(trade, "trade_profile", None)
    version = getattr(trade, "trade_profile_version", None)
    version_number = _get_record_value(version, "version_number")
    return {
        "strategy_name": (
            _normalize_optional_text(_get_record_value(version, "name"))
            or _normalize_optional_text(_get_record_value(profile, "name"))
        ),
        "strategy_version": int(version_number) if version_number is not None else None,
        "strategy_description": _normalize_optional_text(
            _get_record_value(version, "short_description")
        ),
    }


def _get_user_profile_for_prompt(user_id):
    if not user_id:
        return None
    return UserProfile.query.filter_by(user_id=user_id).first()


def _get_weekly_checkin_for_prompt(*, user_id, trade_account_id=None, period_start_utc=None):
    if not user_id:
        return None
    checkin_query = WeeklyCheckin.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        checkin_query = checkin_query.filter_by(trade_account_id=trade_account_id)
    if period_start_utc is not None:
        checkin_query = checkin_query.filter_by(week_start_utc=period_start_utc)
    return checkin_query.order_by(WeeklyCheckin.week_start_utc.desc(), WeeklyCheckin.id.desc()).first()


def build_profile_instructions(
    trading_style,
    experience_level,
    instruments,
    emotional_state,
    plan_adherence,
    execution_quality,
    *,
    emotional_index_label=None,
    emotional_index_mismatch=False,
):
    trading_style = _normalize_optional_text(trading_style)
    experience_level = _normalize_optional_text(experience_level)
    instruments = _normalize_optional_text(instruments)
    emotional_state = _normalize_optional_text(emotional_state)
    plan_adherence = _normalize_optional_text(plan_adherence)
    execution_quality = _normalize_optional_text(execution_quality)
    instructions = []

    if experience_level == "beginner":
        instructions.append("Use plain language. Explain any jargon.")
        instructions.append("Focus on maximum 2-3 observations only.")
        instructions.append("Rule must be simple and immediately actionable.")
        instructions.append("Never reference ICT, SMC, or orderflow terms.")
        instructions.append("Encouraging tone always, even on bad weeks.")
    elif experience_level == "intermediate":
        instructions.append("Assume basic trading knowledge, but keep language plain.")
        instructions.append("Balance foundational feedback with deeper pattern context.")
        instructions.append("If you use RR or expectancy, explain it in plain English briefly.")
    elif experience_level == "experienced":
        instructions.append("Focus on subtle patterns, not obvious mistakes.")
        instructions.append("Use concise language without jargon-heavy phrasing.")
        instructions.append("Depth should increase, but wording should stay clear and direct.")

    if trading_style == "scalper":
        instructions.append("Duration analysis in minutes not hours.")
        instructions.append("Flag any trade held over 30 mins as outside style.")
        instructions.append("Overtrading threshold is 5 or more trades per session.")
        instructions.append("Session timing is critical - flag off-session entries.")
    elif trading_style in ("swing", "position"):
        instructions.append("Cross-week trades are expected, never flag as unusual.")
        instructions.append("Holding time vs planned duration is primary focus.")
        instructions.append("Weekly PnL less relevant than per-trade RR.")
        instructions.append("Premature exit analysis weighted more heavily.")
        instructions.append("Acknowledge single week is thin sample for swing traders.")
    elif trading_style == "intraday":
        instructions.append("Same-session open and close is expected.")
        instructions.append("Treat overnight holds as style exceptions, not automatic mistakes.")
        instructions.append("Only flag overnight holding when it is repeated, clearly unplanned, or concentrated the week's risk.")

    if instruments == "indices":
        instructions.append("US market hours dominate - flag trades outside 13:30-20:00 UTC.")
        instructions.append("Session analysis scoped to US session primarily.")
    elif instruments == "forex":
        instructions.append("London/NY overlap is prime session - weight it accordingly.")
    elif instruments == "gold":
        instructions.append("Gold trades across all sessions - session analysis less critical.")
    elif instruments == "mixed":
        instructions.append("Scope session relevance per instrument, not globally.")

    if emotional_state == "stressed":
        instructions.append("Emotional week detected - prioritise BEHAVIOUR section.")
        instructions.append("Acknowledge emotional context in tone, not explicitly.")
    if plan_adherence == "impulsive":
        instructions.append("Flag impulsive trading in BEHAVIOUR section.")
        instructions.append("Reference plan adherence directly in the Rule.")
    if execution_quality == "poor":
        instructions.append("Cross-reference poor execution with actual trade data.")
        instructions.append("Find specific examples of poor execution in the trades.")
    if emotional_index_label in {"high", "very_high"}:
        instructions.append("Objective emotional index is elevated - prioritise BEHAVIOUR section.")
        instructions.append("Treat revenge sequences as the strongest behaviour signal, and use reactive/corrective signals as supporting context with less weight.")
        instructions.append("Describe the week as emotionally pressured or less composed only when the trade evidence supports it, and never mention internal scores or labels.")
    if emotional_index_label == "very_high":
        instructions.append("Lead with behavioural observations before performance metrics.")
    if emotional_index_mismatch:
        instructions.append("Self-report sounded calm or controlled, but observed behaviour signals were elevated - note that mismatch gently and ground it in trade evidence.")
    if emotional_index_label == "moderate" and emotional_state not in {"stressed"}:
        instructions.append("Mild behavioural signals detected - note briefly, don't over-weight.")
    if emotional_index_label == "low" and not emotional_index_mismatch:
        instructions.append("If the trade evidence looks orderly, explicitly acknowledge that emotions looked in check this week without mentioning any internal score.")

    if not instructions:
        return ""

    lines = "\n".join(f"- {instruction}" for instruction in instructions)
    return f"\nTRADER PROFILE ADJUSTMENTS\nApply all of the following:\n{lines}"


def _build_profile_adjustments_for_prompt(user_profile, weekly_checkin, emotional_index=None):
    return build_profile_instructions(
        _get_record_value(user_profile, "trading_style"),
        _get_record_value(user_profile, "experience_level"),
        _get_record_value(user_profile, "instruments"),
        _get_record_value(weekly_checkin, "emotional_state"),
        _get_record_value(weekly_checkin, "plan_adherence"),
        _get_record_value(weekly_checkin, "execution_quality"),
        emotional_index_label=_get_record_value(emotional_index, "label"),
        emotional_index_mismatch=bool(_get_record_value(emotional_index, "self_report_mismatch")),
    )


def _round_metric(value, digits=2):
    if value is None:
        return None
    return round(float(value), digits)


def _first_positive_account_size_from_trades(trades):
    for trade in trades or []:
        account = getattr(trade, "trade_account", None)
        if account is None:
            continue
        raw = getattr(account, "account_size", None)
        if raw in {None, ""}:
            continue
        try:
            size = float(raw)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    return None


def _median_planned_risk_from_closed_trades(trades):
    """
    Median planned risk at stop for closed trades in the payload cohort.
    When account_size is set on the trade account, also return median risk as % of that balance.
    """
    account_size = _first_positive_account_size_from_trades(trades)
    risk_dollars = []
    risk_pcts = []
    for trade in trades or []:
        if getattr(trade, "closed_at", None) is None:
            continue
        dollars = resolve_planned_risk_dollars(trade)
        if dollars is None:
            continue
        risk_dollars.append(float(dollars))
        if account_size:
            risk_pcts.append((float(dollars) / account_size) * 100.0)
    med_dollars = _round_metric(statistics.median(risk_dollars)) if risk_dollars else None
    med_pct = _round_metric(statistics.median(risk_pcts)) if risk_pcts else None
    return med_dollars, med_pct


def _serialize_breakdown_rows(rows, *, key_name, top_n=5):
    serialized = []
    for row in (rows or [])[:top_n]:
        key_value = row.get(key_name)
        if not key_value:
            continue
        serialized.append(
            {
                key_name: key_value,
                "count": int(row.get("count", 0) or 0),
                "win_rate": _round_metric(row.get("win_rate")),
                "net_pnl": _round_metric(row.get("net_pnl")),
            }
        )
    return serialized


def _build_strategy_breakdown(serialized_trades, *, top_n=5):
    strategy_rows = {}
    closed_trade_count = 0
    trades_with_strategy = 0
    for trade in serialized_trades or []:
        if not trade.get("closed_at"):
            continue
        closed_trade_count += 1
        strategy_name = _normalize_optional_text(trade.get("strategy_name"))
        if strategy_name:
            trades_with_strategy += 1
        else:
            strategy_name = "No strategy attached"
        row = strategy_rows.setdefault(
            strategy_name,
            {"strategy_name": strategy_name, "count": 0, "wins": 0, "net_pnl": 0.0},
        )
        pnl = _coerce_float(trade.get("pnl"))
        row["count"] += 1
        if pnl is not None:
            row["net_pnl"] += pnl
            if pnl > 0:
                row["wins"] += 1

    rows = []
    for row in strategy_rows.values():
        count = int(row["count"] or 0)
        rows.append(
            {
                "strategy_name": row["strategy_name"],
                "count": count,
                "win_rate": _round_metric((row["wins"] / count * 100.0) if count else None),
                "net_pnl": _round_metric(row["net_pnl"]),
            }
        )
    rows = sorted(
        rows,
        key=lambda item: (
            -item["count"],
            -abs(item["net_pnl"] or 0.0),
            item["strategy_name"],
        ),
    )[:top_n]
    return {
        "strategies": rows,
        "strategy_coverage": {
            "trades_with_strategy": trades_with_strategy,
            "strategy_coverage_pct": _round_metric(
                (trades_with_strategy / closed_trade_count * 100.0)
                if closed_trade_count
                else None
            ),
        },
    }


def _build_market_context_lookup(trades):
    trade_ids = [
        getattr(trade, "id", None)
        for trade in (trades or [])
        if getattr(trade, "id", None) is not None
    ]
    bars_by_trade_id = {trade_id: [] for trade_id in trade_ids}
    if trade_ids:
        bar_rows = (
            TradeBars.query.filter(
                TradeBars.trade_id.in_(trade_ids),
                TradeBars.timeframe == "M5",
            )
            .order_by(TradeBars.trade_id.asc(), TradeBars.bar_time.asc())
            .all()
        )
        for row in bar_rows:
            bars_by_trade_id.setdefault(row.trade_id, []).append(row)

    return {
        _get_trade_identity(trade): build_trade_market_context(
            trade,
            bars_by_trade_id.get(getattr(trade, "id", None), []),
        )
        for trade in (trades or [])
    }


def _build_market_context_breakdown(serialized_trades):
    closed_trades = [trade for trade in serialized_trades or [] if trade.get("closed_at")]
    market_contexts = [trade.get("market_context") or {} for trade in closed_trades]
    stop_contexts = [
        (context.get("stop_management") or {})
        for context in market_contexts
    ]
    trades_with_bars = sum(
        1 for context in market_contexts if context.get("bars_status") == "ready"
    )
    post_exit_tp_reached_count = sum(
        1 for context in market_contexts if context.get("post_exit_tp_reached") is True
    )
    protective_stop_count = sum(
        1 for context in stop_contexts if context.get("stop_loss_protects_profit") is True
    )
    trailing_or_breakeven_stop_count = sum(
        1
        for context in stop_contexts
        if context.get("possible_trailing_or_breakeven_stop") is True
    )
    large_candle_entry_count = sum(
        1 for context in market_contexts if context.get("large_candle_before_entry") is True
    )
    entry_bar_against_direction_count = sum(
        1 for context in market_contexts
        if context.get("entry_bar_closes_in_trade_direction") is False
    )
    post_exit_continued_count = sum(
        1 for context in market_contexts if context.get("post_exit_direction") == "continued"
    )
    post_exit_reversed_count = sum(
        1 for context in market_contexts if context.get("post_exit_direction") == "reversed"
    )
    return {
        "trades_with_bars": trades_with_bars,
        "bar_coverage_pct": _round_metric(
            (trades_with_bars / len(closed_trades) * 100.0)
            if closed_trades
            else None
        ),
        "post_exit_tp_reached_count": post_exit_tp_reached_count,
        "protective_stop_count": protective_stop_count,
        "trailing_or_breakeven_stop_count": trailing_or_breakeven_stop_count,
        "large_candle_entry_count": large_candle_entry_count,
        "entry_bar_against_direction_count": entry_bar_against_direction_count,
        "post_exit_continued_count": post_exit_continued_count,
        "post_exit_reversed_count": post_exit_reversed_count,
    }


def _build_current_week_breakdowns(
    *,
    analytics,
    serialized_trades,
    median_lot_size,
    median_planned_risk_dollars=None,
    median_risk_pct_of_account=None,
):
    closed_trade_count = sum(1 for trade in serialized_trades if trade.get("closed_at"))
    tp_capture_values = [trade.get("tp_capture_pct") for trade in serialized_trades if trade.get("tp_capture_pct") is not None]
    closed_before_tp_count = sum(1 for trade in serialized_trades if trade.get("closed_before_tp") is True)
    closed_before_sl_count = sum(1 for trade in serialized_trades if trade.get("closed_before_sl") is True)
    outlier_size_count = sum(1 for trade in serialized_trades if trade.get("outlier_size"))

    session_counts = {row.get("name"): int(row.get("count", 0) or 0) for row in analytics.get("session_stats", []) if row.get("name")}
    busiest_session = max(session_counts.items(), key=lambda item: item[1])[0] if session_counts else None

    open_days = {
        (trade.get("opened_at") or "")[:10]
        for trade in serialized_trades
        if trade.get("opened_at")
    }
    active_day_count = len(open_days)
    trade_ideas_per_active_day = (
        _round_metric(closed_trade_count / active_day_count)
        if active_day_count > 0
        else None
    )
    strategy_breakdown = _build_strategy_breakdown(serialized_trades)
    market_context_breakdown = _build_market_context_breakdown(serialized_trades)
    return {
        "sessions": _serialize_breakdown_rows(analytics.get("session_stats"), key_name="name"),
        "symbols": _serialize_breakdown_rows(analytics.get("pair_stats"), key_name="symbol"),
        "weekdays": _serialize_breakdown_rows(
            [row for row in analytics.get("weekday_stats", []) if int(row.get("count", 0) or 0) > 0],
            key_name="name",
        ),
        "strategies": strategy_breakdown["strategies"],
        "strategy_coverage": strategy_breakdown["strategy_coverage"],
        "market_context": market_context_breakdown,
        "sizing": {
            "median_lot_size": _round_metric(median_lot_size),
            "median_planned_risk_dollars": median_planned_risk_dollars,
            "median_risk_pct_of_account": median_risk_pct_of_account,
            "outlier_size_count": outlier_size_count,
            "outlier_size_share_pct": _round_metric((outlier_size_count / closed_trade_count * 100.0) if closed_trade_count else None),
        },
        "frequency": {
            "trade_idea_count": closed_trade_count,
            "active_day_count": active_day_count,
            "trade_ideas_per_active_day": trade_ideas_per_active_day,
            "busiest_session": busiest_session,
        },
        "exit_quality": {
            "closed_before_tp_count": closed_before_tp_count,
            "closed_before_sl_count": closed_before_sl_count,
            "avg_tp_capture_pct": _round_metric(
                (sum(tp_capture_values) / len(tp_capture_values))
                if tp_capture_values
                else None
            ),
        },
    }


def _build_tone_context(*, weekly_checkin, emotional_index, analytics_summary):
    emotional_state = _normalize_optional_text(_get_record_value(weekly_checkin, "emotional_state"))
    plan_adherence = _normalize_optional_text(_get_record_value(weekly_checkin, "plan_adherence"))
    execution_quality = _normalize_optional_text(_get_record_value(weekly_checkin, "execution_quality"))
    ei_label = _normalize_optional_text(_get_record_value(emotional_index, "label"))
    checkin_submitted = any(
        _normalize_optional_text(_get_record_value(weekly_checkin, field_name))
        for field_name in ("emotional_state", "plan_adherence", "execution_quality", "additional_context")
    )

    reasons = []
    if emotional_state:
        reasons.append(f"checkin_emotional_state:{emotional_state}")
    if plan_adherence:
        reasons.append(f"checkin_plan_adherence:{plan_adherence}")
    if execution_quality:
        reasons.append(f"checkin_execution_quality:{execution_quality}")
    if ei_label:
        reasons.append(f"behaviour_pressure:{ei_label}")

    net_pnl = _coerce_float((analytics_summary or {}).get("net_pnl"))
    if net_pnl is not None:
        if net_pnl < 0:
            reasons.append("week_result:losing")
        elif net_pnl > 0:
            reasons.append("week_result:profitable")
        else:
            reasons.append("week_result:breakeven")

    if not checkin_submitted:
        mode = "neutral_no_checkin"
    elif emotional_state == "stressed" or execution_quality == "poor" or ei_label in {"high", "very_high"}:
        mode = "stressed"
    elif emotional_state in {"calm", "confident"} and plan_adherence in {"disciplined", "focused"} and ei_label in {None, "low", "moderate"}:
        mode = "calm_sharp"
    else:
        mode = "balanced"
    return {
        "checkin_submitted": checkin_submitted,
        "mode": mode,
        "reasons": reasons,
    }


def _build_week_behaviour_patterns(merged_trades):
    trade_annotations = _build_trade_annotations(merged_trades)
    closed_trades = [
        trade
        for trade in merged_trades
        if getattr(trade, "closed_at", None) is not None and resolve_net_pnl(trade) is not None
    ]
    closed_count = len(closed_trades)
    if closed_count <= 0:
        return {
            "post_loss_reentry_count": 0,
            "same_symbol_post_loss_reentry_count": 0,
            "larger_size_after_loss_count": 0,
            "potential_revenge_count": 0,
            "post_loss_reentry_share_pct": None,
            "larger_size_after_loss_share_pct": None,
        }

    post_loss_reentry_count = 0
    same_symbol_post_loss_reentry_count = 0
    larger_size_after_loss_count = 0
    potential_revenge_count = 0
    chronological_closed = sorted(closed_trades, key=lambda item: (
        getattr(item, "opened_at", None) or datetime.min,
        getattr(item, "closed_at", None) or datetime.min,
        getattr(item, "id", 0) or 0,
    ))
    previous_trade = None
    for trade in chronological_closed:
        identity = _get_trade_identity(trade)
        annotation = trade_annotations.get(identity, {})
        if bool(annotation.get("is_post_loss_trade")):
            post_loss_reentry_count += 1
        if bool(annotation.get("is_post_loss_same_symbol_trade")):
            same_symbol_post_loss_reentry_count += 1
        if bool(annotation.get("is_potential_revenge")) or bool(getattr(trade, "is_revenge", False)):
            potential_revenge_count += 1
        if previous_trade is not None:
            prev_pnl = resolve_net_pnl(previous_trade)
            if prev_pnl is not None and prev_pnl < 0:
                prev_risk = resolve_planned_risk_dollars(previous_trade)
                current_risk = resolve_planned_risk_dollars(trade)
                if (
                    prev_risk not in (None, 0)
                    and current_risk is not None
                    and (current_risk - prev_risk) / abs(prev_risk) > 0.15
                ):
                    larger_size_after_loss_count += 1
        previous_trade = trade
    return {
        "post_loss_reentry_count": post_loss_reentry_count,
        "same_symbol_post_loss_reentry_count": same_symbol_post_loss_reentry_count,
        "larger_size_after_loss_count": larger_size_after_loss_count,
        "potential_revenge_count": potential_revenge_count,
        "post_loss_reentry_share_pct": _round_metric(post_loss_reentry_count / closed_count * 100.0),
        "larger_size_after_loss_share_pct": _round_metric(larger_size_after_loss_count / closed_count * 100.0),
    }


def _build_four_week_patterns(*, user_id, trade_account_id, period_start_utc, weeks=4):
    if period_start_utc is None:
        return {
            "weeks_considered": 0,
            "session_patterns": [],
            "symbol_patterns": [],
            "weekday_patterns": [],
            "behaviour_patterns": {},
            "weekly_series": [],
        }

    weekly_rows = []
    session_totals = {}
    symbol_totals = {}
    weekday_totals = {}
    behaviour_rows = []
    for offset in range(1, weeks + 1):
        week_end = period_start_utc - timedelta(days=(offset - 1) * 7)
        week_start = week_end - timedelta(days=7)
        raw_trades = _query_trades_for_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=week_start,
            period_end_utc=week_end,
            closed_trades_only=True,
        )
        merged_trades = merge_bundled_trades(raw_trades)
        analytics = build_trade_analytics(
            merged_trades,
            display_timezone_name=get_ai_timezone_name(),
        )
        summary = analytics.get("summary", {})
        week_behaviour = _build_week_behaviour_patterns(merged_trades)
        behaviour_rows.append(week_behaviour)

        week_payload = {
            "period_start_utc": format_utc_timestamp(week_start),
            "period_end_utc": format_utc_timestamp(week_end),
            "trade_idea_count": int(summary.get("closed_trades", 0) or 0),
            "win_rate": _round_metric(summary.get("win_rate")),
            "net_pnl": _round_metric(summary.get("net_pnl")),
            "top_session": ((analytics.get("session_stats") or [{}])[0]).get("name"),
            "top_symbol": ((analytics.get("pair_stats") or [{}])[0]).get("symbol"),
            "top_weekday": next((row.get("name") for row in analytics.get("weekday_stats", []) if int(row.get("count", 0) or 0) > 0), None),
            "post_loss_reentry_count": week_behaviour.get("post_loss_reentry_count"),
            "larger_size_after_loss_count": week_behaviour.get("larger_size_after_loss_count"),
        }
        weekly_rows.append(week_payload)

        for row in analytics.get("session_stats", [])[:3]:
            name = row.get("name")
            if not name:
                continue
            session_totals[name] = session_totals.get(name, 0) + int(row.get("count", 0) or 0)
        for row in analytics.get("pair_stats", [])[:3]:
            symbol = row.get("symbol")
            if not symbol:
                continue
            symbol_totals[symbol] = symbol_totals.get(symbol, 0) + int(row.get("count", 0) or 0)
        for row in analytics.get("weekday_stats", []):
            name = row.get("name")
            count = int(row.get("count", 0) or 0)
            if not name or count <= 0:
                continue
            weekday_totals[name] = weekday_totals.get(name, 0) + count

    session_patterns = [{"name": name, "count": count} for name, count in sorted(session_totals.items(), key=lambda item: (-item[1], item[0]))[:5]]
    symbol_patterns = [{"symbol": symbol, "count": count} for symbol, count in sorted(symbol_totals.items(), key=lambda item: (-item[1], item[0]))[:5]]
    weekday_patterns = [{"name": name, "count": count} for name, count in sorted(weekday_totals.items(), key=lambda item: (-item[1], item[0]))[:5]]

    weeks_with_trades = [row for row in weekly_rows if (row.get("trade_idea_count") or 0) > 0]
    behaviour_patterns = {
        "weeks_with_trades": len(weeks_with_trades),
        "avg_post_loss_reentry_count": _round_metric(
            sum((row.get("post_loss_reentry_count") or 0) for row in behaviour_rows) / len(behaviour_rows)
            if behaviour_rows
            else None
        ),
        "avg_larger_size_after_loss_count": _round_metric(
            sum((row.get("larger_size_after_loss_count") or 0) for row in behaviour_rows) / len(behaviour_rows)
            if behaviour_rows
            else None
        ),
    }
    return {
        "weeks_considered": weeks,
        "session_patterns": session_patterns,
        "symbol_patterns": symbol_patterns,
        "weekday_patterns": weekday_patterns,
        "behaviour_patterns": behaviour_patterns,
        "weekly_series": weekly_rows,
    }


def _build_recent_experiments(*, user_id, trade_account_id, period_start_utc, limit=6):
    if not user_id:
        return []
    query = AIGeneratedResponse.query.filter_by(
        user_id=user_id,
        trade_account_id=trade_account_id,
        kind=WEEKLY_DASHBOARD_KIND,
    )
    if period_start_utc is not None:
        query = query.filter(AIGeneratedResponse.period_start_utc < period_start_utc)
    rows = (
        query.options(load_only(AIGeneratedResponse.response_meta_json, AIGeneratedResponse.period_start_utc))
        .order_by(AIGeneratedResponse.period_start_utc.desc(), AIGeneratedResponse.id.desc())
        .limit(max(int(limit), 1))
        .all()
    )
    recent = []
    for row in rows:
        meta = parse_review_response_meta(getattr(row, "response_meta_json", None)) or {}
        experiment = meta.get("experiment") if isinstance(meta.get("experiment"), dict) else {}
        experiment_text = _normalize_review_item_text(experiment.get("text"))
        if not experiment_text:
            continue
        recent.append(
            {
                "period_start_utc": format_utc_timestamp(getattr(row, "period_start_utc", None)),
                "text": experiment_text,
            }
        )
    return recent


def _build_historical_context(
    *,
    user_id,
    trade_account_id=None,
    period_start_utc=None,
    period_end_utc=None,
    lookback_days=DEFAULT_HISTORICAL_CONTEXT_DAYS,
    closed_trades_only=False,
):
    historical_end_utc = period_start_utc or period_end_utc
    if historical_end_utc is None:
        return None

    historical_start_utc = historical_end_utc - timedelta(days=max(int(lookback_days), 1))
    historical_trades = _query_trades_for_payload(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=historical_start_utc,
        period_end_utc=historical_end_utc,
        closed_trades_only=closed_trades_only,
    )
    if not historical_trades:
        return None

    analytics = build_trade_analytics(
        historical_trades,
        display_timezone_name=get_ai_timezone_name(),
    )

    pair_stats = []
    for item in analytics.get("pair_stats", [])[:3]:
        pair_stats.append(
            {
                "symbol": item.get("symbol"),
                "count": item.get("count", 0),
                "win_rate": _round_metric(item.get("win_rate")),
                "net_pnl": _round_metric(item.get("net_pnl")),
            }
        )

    session_stats = []
    for item in analytics.get("session_stats", [])[:3]:
        session_stats.append(
            {
                "name": item.get("name"),
                "count": item.get("count", 0),
                "win_rate": _round_metric(item.get("win_rate")),
                "net_pnl": _round_metric(item.get("net_pnl")),
            }
        )

    weekday_stats = []
    for item in analytics.get("weekday_stats", []):
        if item.get("count", 0) <= 0:
            continue
        weekday_stats.append(
            {
                "name": item.get("name"),
                "count": item.get("count", 0),
                "win_rate": _round_metric(item.get("win_rate")),
                "net_pnl": _round_metric(item.get("net_pnl")),
            }
        )
    weekday_stats = sorted(
        weekday_stats,
        key=lambda item: (-item["count"], -(item["net_pnl"] or 0.0), item["name"]),
    )[:3]

    summary = analytics.get("summary", {})
    return {
        "comparison_scope": (
            "history_before_review_period_only"
            if period_start_utc is not None
            else "history_up_to_period_end"
        ),
        "window_start_utc": format_utc_timestamp(historical_start_utc),
        "window_end_utc": format_utc_timestamp(historical_end_utc),
        "window_days": max(int(lookback_days), 1),
        "summary": {
            "total_trades": summary.get("total_trades", 0),
            "closed_trades": summary.get("closed_trades", 0),
            "win_rate": _round_metric(summary.get("win_rate")),
            "expectancy": _round_metric(summary.get("expectancy")),
            "net_pnl": _round_metric(summary.get("net_pnl")),
            "max_drawdown": _round_metric(summary.get("max_drawdown")),
        },
        "top_pairs": pair_stats,
        "top_sessions": session_stats,
        "top_weekdays": weekday_stats,
    }


def _get_trade_duration_minutes(trade):
    if trade.opened_at is None or trade.closed_at is None:
        return None
    if trade.closed_at < trade.opened_at:
        return None
    return _round_metric((trade.closed_at - trade.opened_at).total_seconds() / 60.0)


def _classify_trade_session(timestamp):
    timestamp_utc = ensure_utc_aware(timestamp)
    if timestamp_utc is None:
        return None
    return classify_trading_session(timestamp_utc)


def _format_bool(value):
    return "true" if bool(value) else "false"


def _format_optional_bool(value):
    if value is None:
        return "-"
    return _format_bool(value)


def _calculate_trade_rr(target_price, entry_price, stop_loss, side=None, *, signed=False):
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
        return _round_metric(move_amount / risk_amount)
    if move_amount <= 0:
        return None
    return _round_metric(move_amount / risk_amount)


def _build_trade_exit_quality(trade, trade_pnl):
    result = {
        "planned_rr": None,
        "realized_rr": None,
        "tp_capture_pct": None,
        "closed_before_tp": None,
        "closed_before_sl": None,
    }
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
    stop_loss_is_valid = not (
        validation_issues["stop_loss_too_close"] or validation_issues["invalid_stop_loss_side"]
    )
    take_profit_is_valid = not (
        validation_issues["take_profit_too_close"] or validation_issues["invalid_take_profit_side"]
    )

    if stop_loss_is_valid and take_profit_is_valid:
        result["planned_rr"] = _calculate_trade_rr(
            getattr(trade, "take_profit", None),
            getattr(trade, "entry_price", None),
            getattr(trade, "stop_loss", None),
            getattr(trade, "side", None),
        )

    if stop_loss_is_valid:
        result["realized_rr"] = _calculate_trade_rr(
            getattr(trade, "exit_price", None),
            getattr(trade, "entry_price", None),
            getattr(trade, "stop_loss", None),
            getattr(trade, "side", None),
            signed=True,
        )

    if result["planned_rr"] is not None and result["realized_rr"] is not None and result["realized_rr"] > 0:
        result["tp_capture_pct"] = _round_metric((result["realized_rr"] / result["planned_rr"]) * 100.0)

    if (
        trade_pnl is not None
        and trade_pnl > 0
        and getattr(trade, "exit_price", None) is not None
        and getattr(trade, "take_profit", None) is not None
        and take_profit_is_valid
    ):
        result["closed_before_tp"] = not did_trade_reach_level(
            getattr(trade, "exit_price", None),
            getattr(trade, "take_profit", None),
            getattr(trade, "side", None),
            "tp",
            getattr(trade, "symbol", None),
            instrument_type=instrument_type,
            contract_code=getattr(trade, "contract_code", None),
        )
    elif (
        trade_pnl is not None
        and trade_pnl < 0
        and getattr(trade, "exit_price", None) is not None
        and getattr(trade, "stop_loss", None) is not None
        and stop_loss_is_valid
    ):
        result["closed_before_sl"] = not did_trade_reach_level(
            getattr(trade, "exit_price", None),
            getattr(trade, "stop_loss", None),
            getattr(trade, "side", None),
            "sl",
            getattr(trade, "symbol", None),
            instrument_type=instrument_type,
            contract_code=getattr(trade, "contract_code", None),
        )

    return result


def build_trade_payload(
    *,
    user_id,
    trade_account_id=None,
    max_trades=None,
    period_start_utc=None,
    period_end_utc=None,
    closed_trades_only=False,
    user_profile=None,
    weekly_checkin=None,
):
    raw_trades = _query_trades_for_payload(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        closed_trades_only=closed_trades_only,
    )
    trades = merge_bundled_trades(raw_trades)
    account_type = get_trade_account_type(trades[0]) if trades else "CFD"
    if not trades and trade_account_id is not None:
        account = TradeAccount.query.get(trade_account_id)
        if account is not None:
            account_type = account.account_type
    if max_trades is not None and max_trades > 0:
        trades = trades[:max_trades]

    analytics = build_trade_analytics(
        trades,
        display_timezone_name=get_ai_timezone_name(),
    )
    trade_annotations = _build_trade_annotations(trades)
    emotional_index = compute_emotional_index(trades=raw_trades, weekly_checkin=weekly_checkin)
    signals = (emotional_index or {}).get("signals", {})

    notes_with_content = 0
    durations = []
    closed_trade_count = 0
    total_absolute_trade_pnl = 0.0
    largest_trade_symbol = None
    largest_trade_abs_pnl = 0.0
    symbol_trade_counts = {}
    symbol_net_pnl = {}
    bundle_count = 0
    for trade in trades:
        note = (trade.trade_note or "").strip()
        if note:
            notes_with_content += 1
        if bool(getattr(trade, "_is_bundle", False)):
            bundle_count += 1
        duration_minutes = _get_trade_duration_minutes(trade)
        if duration_minutes is not None and not is_extremely_long_duration_minutes(duration_minutes):
            durations.append(duration_minutes)
        trade_pnl = resolve_net_pnl(trade)
        if getattr(trade, "closed_at", None) is None or trade_pnl is None:
            continue
        closed_trade_count += 1
        absolute_trade_pnl = abs(float(trade_pnl))
        total_absolute_trade_pnl += absolute_trade_pnl
        trade_symbol = format_trade_symbol(trade) or "-"
        symbol_trade_counts[trade_symbol] = symbol_trade_counts.get(trade_symbol, 0) + 1
        symbol_net_pnl[trade_symbol] = symbol_net_pnl.get(trade_symbol, 0.0) + float(trade_pnl)
        if absolute_trade_pnl > largest_trade_abs_pnl:
            largest_trade_abs_pnl = absolute_trade_pnl
            largest_trade_symbol = trade_symbol

    sig_inputs = prepare_closed_trade_signal_inputs(trades)
    median_lot_size = sig_inputs["median_lot_size"]
    outlier_context = sig_inputs["outlier_context"]
    median_duration_minutes = statistics.median(durations) if durations else None
    notes_coverage = round(notes_with_content / len(trades), 2) if trades else 0.0
    notes_missing = max(len(trades) - notes_with_content, 0)
    notes_confidence = _get_notes_confidence_label(notes_coverage, len(trades))
    first_trade_query = db.session.query(func.min(Trade.opened_at)).filter(Trade.user_id == user_id)
    if trade_account_id is not None:
        first_trade_query = first_trade_query.filter(Trade.trade_account_id == trade_account_id)
    first_trade_opened_at = first_trade_query.scalar()
    account_age_days = None
    if first_trade_opened_at is not None:
        account_age_days = max((utcnow_naive() - first_trade_opened_at).days, 0)

    top_symbol_by_trade_count = None
    top_symbol_trade_count = 0
    top_symbol_trade_share_pct = None
    if symbol_trade_counts and closed_trade_count > 0:
        top_symbol_by_trade_count, top_symbol_trade_count = max(
            symbol_trade_counts.items(),
            key=lambda item: (item[1], abs(symbol_net_pnl.get(item[0], 0.0)), item[0]),
        )
        top_symbol_trade_share_pct = _round_metric((top_symbol_trade_count / closed_trade_count) * 100.0)

    top_symbol_by_abs_pnl = None
    top_symbol_abs_pnl_share_pct = None
    total_absolute_symbol_pnl = sum(abs(value) for value in symbol_net_pnl.values())
    if symbol_net_pnl and total_absolute_symbol_pnl > 0:
        top_symbol_by_abs_pnl, dominant_symbol_pnl = max(
            symbol_net_pnl.items(),
            key=lambda item: (abs(item[1]), symbol_trade_counts.get(item[0], 0), item[0]),
        )
        top_symbol_abs_pnl_share_pct = _round_metric(
            (abs(dominant_symbol_pnl) / total_absolute_symbol_pnl) * 100.0
        )

    largest_trade_abs_pnl_share_pct = None
    if total_absolute_trade_pnl > 0 and largest_trade_abs_pnl > 0:
        largest_trade_abs_pnl_share_pct = _round_metric(
            (largest_trade_abs_pnl / total_absolute_trade_pnl) * 100.0
        )

    account_size_for_risk = _first_positive_account_size_from_trades(trades)
    market_context_lookup = _build_market_context_lookup(trades)
    serialized_trades = []
    next_trade_ref = 1
    next_bundle_ref = 1
    for trade in trades:
        identity = _get_trade_identity(trade)
        annotation = trade_annotations.get(identity, {})
        trade_pnl = resolve_net_pnl(trade)
        duration_minutes = _get_trade_duration_minutes(trade)
        exit_quality = _build_trade_exit_quality(trade, trade_pnl)
        is_bundle_trade = bool(getattr(trade, "_is_bundle", False))
        review_ref = f"B{next_bundle_ref}" if is_bundle_trade else f"T{next_trade_ref}"
        if is_bundle_trade:
            next_bundle_ref += 1
        else:
            next_trade_ref += 1
        planned_risk_dollars = resolve_planned_risk_dollars(trade)
        strategy_context = _serialize_trade_strategy(trade)
        trade_risk_pct = None
        if planned_risk_dollars is not None and account_size_for_risk:
            trade_risk_pct = _round_metric(
                (float(planned_risk_dollars) / account_size_for_risk) * 100.0, digits=4
            )
        serialized_trades.append(
            {
                "review_ref": review_ref,
                "trade_id": getattr(trade, "id", None),
                "trade_pubkey": (getattr(trade, "pubkey", None) or None),
                "symbol": format_trade_symbol(trade),
                "contract_code": (trade.contract_code or "").strip() or None,
                "strategy_name": strategy_context["strategy_name"],
                "strategy_version": strategy_context["strategy_version"],
                "strategy_description": strategy_context["strategy_description"],
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "lot_size": trade.lot_size,
                "pnl": trade_pnl,
                "planned_risk_dollars": _round_metric(planned_risk_dollars) if planned_risk_dollars is not None else None,
                "trade_risk_pct": trade_risk_pct,
                "entry_session": _classify_trade_session(trade.opened_at),
                "exit_session": _classify_trade_session(trade.closed_at),
                "session": _get_trade_session(trade),
                "duration_minutes": duration_minutes,
                "opened_at": format_utc_timestamp(trade.opened_at),
                "closed_at": format_utc_timestamp(trade.closed_at),
                "trade_sequence_number": annotation.get("trade_sequence_number"),
                "trade_number_in_session": annotation.get("trade_number_in_session"),
                "prev_trade_pnl": annotation.get("prev_trade_pnl"),
                "minutes_since_prev_close": annotation.get("minutes_since_prev_close"),
                "prev_symbol_trade_pnl": annotation.get("prev_symbol_trade_pnl"),
                "minutes_since_prev_symbol_close": annotation.get("minutes_since_prev_symbol_close"),
                "loss_streak_before_trade": annotation.get("loss_streak_before_trade"),
                "is_post_loss_trade": bool(annotation.get("is_post_loss_trade")),
                "same_symbol_reentry": bool(annotation.get("same_symbol_reentry")),
                "is_post_loss_same_symbol_trade": bool(annotation.get("is_post_loss_same_symbol_trade")),
                "same_trade_idea_reentry": bool(annotation.get("same_trade_idea_reentry")),
                "is_potential_revenge": bool(annotation.get("is_potential_revenge")),
                "is_potential_reactive": bool(annotation.get("is_potential_reactive")),
                "trade_note": (trade.trade_note or "").strip() or None,
                "is_revenge": bool(getattr(trade, "is_revenge", False)),
                "is_reactive": bool(getattr(trade, "is_reactive", False)),
                "is_corrective": bool(getattr(trade, "is_corrective", False)),
                "bundle_pubkey": (getattr(trade, "bundle_pubkey", None) or None),
                "is_bundle": is_bundle_trade,
                "bundle_trade_count": int(getattr(trade, "_bundle_trade_count", 1) or 1),
                "planned_rr": exit_quality["planned_rr"],
                "realized_rr": exit_quality["realized_rr"],
                "tp_capture_pct": exit_quality["tp_capture_pct"],
                "closed_before_tp": exit_quality["closed_before_tp"],
                "closed_before_sl": exit_quality["closed_before_sl"],
                "outlier_size": bool(outlier_context.get(identity, {}).get("outlier_risk")),
                "outlier_lot_spike": bool(outlier_context.get(identity, {}).get("outlier_lot_spike")),
                "possible_split_order": bool(annotation.get("possible_split_order")),
                "split_group_size": annotation.get("split_group_size", 1),
                "split_group_index": annotation.get("split_group_index", 1),
                "split_group_role": annotation.get("split_group_role") or "solo",
                "is_likely_corrective": bool(
                    median_duration_minutes
                    and median_duration_minutes > 0
                    and duration_minutes is not None
                    and duration_minutes < median_duration_minutes * 0.2
                ),
                "market_context": market_context_lookup.get(identity, {}),
            }
        )

    enrich_serialized_trades_with_risk_comparison(serialized_trades)

    historical_ctx = _build_historical_context(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        closed_trades_only=closed_trades_only,
    )
    historical_summary = (historical_ctx or {}).get("summary") or {}

    ei_trend_direction = None
    if period_start_utc is not None:
        try:
            past_reviews = (
                AIGeneratedResponse.query.filter_by(
                    user_id=user_id,
                    trade_account_id=trade_account_id,
                    kind=WEEKLY_DASHBOARD_KIND,
                )
                .filter(AIGeneratedResponse.period_start_utc < period_start_utc)
                .options(load_only(AIGeneratedResponse.payload_json, AIGeneratedResponse.period_start_utc))
                .order_by(AIGeneratedResponse.period_start_utc.desc())
                .limit(3)
                .all()
            )
            past_ei_scores = []
            for row in past_reviews:
                try:
                    stored = json.loads(row.payload_json or "")
                    score = stored.get("emotional_index", {}).get("score")
                    if score is not None:
                        past_ei_scores.append(float(score))
                except (TypeError, ValueError):
                    pass
            current_ei_score = (emotional_index or {}).get("score")
            if current_ei_score is not None:
                current_ei_score = float(current_ei_score)
            all_ei_scores = (
                [current_ei_score] if current_ei_score is not None else []
            ) + past_ei_scores
            if len(all_ei_scores) >= 2:
                recent = all_ei_scores[0]
                older_avg = sum(all_ei_scores[1:]) / len(all_ei_scores[1:])
                delta = recent - older_avg
                if abs(delta) < 0.3:
                    ei_trend_direction = "flat"
                elif delta < 0:
                    ei_trend_direction = "improving"
                else:
                    ei_trend_direction = "declining"
        except Exception:
            ei_trend_direction = None

    performance_trends = {
        "comparison_basis": "reviewed_period_vs_prior_pre_period_history",
        "prior_history_window_days": (historical_ctx or {}).get("window_days"),
        "win_rate_current": _round_metric(analytics["summary"].get("win_rate")),
        "win_rate_historical": historical_summary.get("win_rate"),
        "expectancy_current": _round_metric(analytics["summary"].get("expectancy")),
        "expectancy_historical": historical_summary.get("expectancy"),
        "ei_trend": ei_trend_direction,
    }
    trade_idea_count = sum(1 for trade in serialized_trades if trade.get("closed_at"))
    experiment_context = {
        "eligible": trade_idea_count >= MIN_TRADE_IDEAS_FOR_EXPERIMENT,
        "trade_idea_count": trade_idea_count,
        "min_required": MIN_TRADE_IDEAS_FOR_EXPERIMENT,
    }
    med_planned_risk_d, med_risk_pct = _median_planned_risk_from_closed_trades(trades)
    current_week_breakdowns = _build_current_week_breakdowns(
        analytics=analytics,
        serialized_trades=serialized_trades,
        median_lot_size=median_lot_size,
        median_planned_risk_dollars=med_planned_risk_d,
        median_risk_pct_of_account=med_risk_pct,
    )
    _summary_for_signals = {
        "net_pnl": _round_metric(analytics["summary"].get("net_pnl")),
        "win_rate": _round_metric(analytics["summary"].get("win_rate")),
        "closed_trades": analytics["summary"].get("closed_trades"),
        "open_trades": analytics["summary"].get("open_trades"),
        "top_symbol_by_trade_count": top_symbol_by_trade_count,
        "top_symbol_by_abs_pnl": top_symbol_by_abs_pnl,
        "largest_trade_symbol": largest_trade_symbol,
        "largest_trade_abs_pnl_share_pct": largest_trade_abs_pnl_share_pct,
    }
    weekly_signals = _build_weekly_signals(
        serialized_trades=serialized_trades,
        summary=_summary_for_signals,
        current_week_breakdowns=current_week_breakdowns,
        median_risk_pct_of_account=med_risk_pct,
        median_planned_risk_dollars=med_planned_risk_d,
        median_lot_size=median_lot_size,
        account_type=account_type,
        account_age_days=account_age_days,
        notes_confidence=notes_confidence,
    )
    current_week_breakdowns["risk_authority"] = weekly_signals["risk_authority"]
    current_week_breakdowns["post_loss_response"] = weekly_signals["post_loss_response"]
    current_week_breakdowns["single_trade_dominance"] = weekly_signals["single_trade_dominance"]
    current_week_breakdowns["tp_capture_shortfalls"] = weekly_signals["tp_capture_shortfalls"]
    current_week_breakdowns["session_concentration"] = weekly_signals["session_concentration"]
    current_week_breakdowns["revenge_evidence"] = weekly_signals["revenge_evidence"]
    current_week_breakdowns["execution_outcome"] = weekly_signals["execution_outcome"]
    current_week_breakdowns["coaching_hypotheses"] = weekly_signals["coaching_hypotheses"]
    tone_context = _build_tone_context(
        weekly_checkin=weekly_checkin,
        emotional_index=emotional_index,
        analytics_summary=analytics.get("summary"),
    )
    four_week_patterns = _build_four_week_patterns(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        weeks=4,
    )
    recent_experiments = _build_recent_experiments(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        limit=6,
    )

    payload = {
        "account_type": account_type,
        "generated_at": format_utc_timestamp(utcnow_naive()),
        "period_start_utc": format_utc_timestamp(period_start_utc),
        "period_end_utc": format_utc_timestamp(period_end_utc),
        "notes_coverage": notes_coverage,
        "notes_with_content": notes_with_content,
        "notes_missing": notes_missing,
        "notes_confidence": notes_confidence,
        "notes_basis": "Per weekly trade idea after bundle merging; counts non-empty user-authored trade_note text only.",
        "account_age_days": account_age_days,
        "user_profile": _serialize_user_profile(user_profile),
        "weekly_checkin": _serialize_weekly_checkin(weekly_checkin),
        "emotional_index": emotional_index,
        "performance_trends": performance_trends,
        "historical_context": historical_ctx,
        "tone_context": tone_context,
        "surface_facts": weekly_signals["surface_facts"],
        "confidence_envelope": weekly_signals["confidence_envelope"],
        "current_week_breakdowns": current_week_breakdowns,
        "four_week_patterns": four_week_patterns,
        "experiment_context": experiment_context,
        "recent_experiments": recent_experiments,
        "summary": {
            "total_trades": analytics["summary"]["total_trades"],
            "closed_trades": analytics["summary"]["closed_trades"],
            "open_trades": analytics["summary"]["open_trades"],
            "win_rate": analytics["summary"]["win_rate"],
            "net_pnl": analytics["summary"]["net_pnl"],
            "weekly_pnl": analytics["summary"]["weekly_pnl"],
            "monthly_pnl": analytics["summary"]["monthly_pnl"],
            "closed_before_tp_count": analytics["summary"].get("closed_before_tp_count", 0),
            "closed_before_sl_count": analytics["summary"].get("closed_before_sl_count", 0),
            "top_symbol_by_trade_count": top_symbol_by_trade_count,
            "top_symbol_trade_count": top_symbol_trade_count,
            "top_symbol_trade_share_pct": top_symbol_trade_share_pct,
            "top_symbol_by_abs_pnl": top_symbol_by_abs_pnl,
            "top_symbol_abs_pnl_share_pct": top_symbol_abs_pnl_share_pct,
            "largest_trade_symbol": largest_trade_symbol,
            "largest_trade_abs_pnl_share_pct": largest_trade_abs_pnl_share_pct,
            "pair_sample_is_diverse": analytics["summary"].get("pair_sample_is_diverse", False),
            "equity_has_outlier_dominance": analytics["summary"].get("equity_has_outlier_dominance", False),
            "bundle_count": bundle_count,
            "confirmed_revenge_trade_count": signals.get("confirmed_revenge_trade_count", 0),
            "revenge_trade_count": signals.get("revenge_trade_count", 0),
            "reactive_trade_count": signals.get("reactive_trade_count", 0),
            "corrective_trade_count": signals.get("corrective_trade_count", 0),
            "best_trade_pnl": (
                analytics["summary"]["best_trade"]["pnl"]
                if analytics["summary"]["best_trade"]
                else None
            ),
            "worst_trade_pnl": (
                analytics["summary"]["worst_trade"]["pnl"]
                if analytics["summary"]["worst_trade"]
                else None
            ),
            "max_drawdown": analytics["summary"]["max_drawdown"],
        },
        "trades": serialized_trades,
    }
    return build_universal_weekly_payload(payload)


def _trade_citation_ref(trade):
    return str((trade or {}).get("ref") or (trade or {}).get("review_ref") or "").strip().upper()


def has_trade_data_for_period(*, user_id, trade_account_id=None, period_start_utc=None, period_end_utc=None):
    trade_query = db.session.query(Trade.id).filter_by(user_id=user_id)
    if trade_account_id is not None:
        trade_query = trade_query.filter_by(trade_account_id=trade_account_id)
    trade_query = trade_query.filter(Trade.closed_at.isnot(None))
    if period_start_utc is not None:
        trade_query = trade_query.filter(Trade.closed_at >= period_start_utc)
    if period_end_utc is not None:
        trade_query = trade_query.filter(Trade.closed_at < period_end_utc)
    return trade_query.first() is not None


def count_closed_trade_ideas_in_period(*, user_id, trade_account_id=None, period_start_utc=None, period_end_utc=None):
    if not user_id:
        return 0
    raw_trades = _query_trades_for_payload(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
        closed_trades_only=True,
    )
    merged_trades = merge_bundled_trades(raw_trades)
    return sum(
        1
        for trade in merged_trades
        if getattr(trade, "closed_at", None) is not None and resolve_net_pnl(trade) is not None
    )


def should_generate_weekly_dashboard_advice(
    *,
    user_id,
    trade_account_id=None,
    period_start_utc=None,
    period_end_utc=None,
    require_recent_login=False,
):
    if not user_id or trade_account_id is None:
        return False
    if require_recent_login:
        current_utc = _to_utc_naive(datetime.now(timezone.utc))
        active_cutoff = current_utc - timedelta(days=WEEKLY_ACTIVITY_LOOKBACK_DAYS)
        last_active_at, last_login_at = (
            db.session.query(User.last_active_at, User.last_login_at)
            .filter(User.id == user_id)
            .one()
        )
        recent_activity_at = last_active_at or last_login_at
        if recent_activity_at is None or recent_activity_at < active_cutoff:
            return False
    return True


def serialize_payload(payload):
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _format_number(value, digits=2):
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _format_signed_currency(value):
    if value is None:
        return "-"
    amount = float(value)
    return f"{amount:+.2f}"


def _format_currency_magnitude(value):
    if value is None:
        return "-"
    return f"{abs(float(value)):.2f}"


def _format_percent(value):
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def _format_hypothesis_fact_value(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return _format_bool(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "-"
        return ", ".join(_format_hypothesis_fact_value(item) for item in value)
    if isinstance(value, dict):
        if not value:
            return "-"
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    if isinstance(value, (int, float)):
        return _format_number(value)
    return str(value)


def _payload_section_has_values(section):
    return any(value is not None for value in (section or {}).values())


def _get_notes_confidence_label(notes_coverage, trade_count):
    if trade_count <= 0:
        return None
    if notes_coverage < 0.5:
        return "low"
    if notes_coverage < 0.8:
        return "medium"
    return "high"


def _format_notes_coverage(value, notes_with_content=None, notes_missing=None):
    ratio_text = _format_number(value)
    if notes_with_content is None or notes_missing is None:
        return ratio_text
    total = notes_with_content + notes_missing
    if total <= 0:
        return ratio_text
    ticket_label = "trade ticket" if total == 1 else "trade tickets"
    verb = "has" if notes_with_content == 1 else "have"
    return (
        f"{ratio_text} "
        f"({notes_with_content} of {total} weekly {ticket_label} {verb} non-empty notes)"
    )


def _trade_size_field_label(account_type):
    if normalize_account_type(account_type) == "FUTURES":
        return "contract_count"
    return "lot_size"


def _futures_terminology_adjustments(payload):
    account_type = (payload or {}).get("account_type")
    if normalize_account_type(account_type) != "FUTURES":
        return ""
    return (
        "\n\nFUTURES TERMINOLOGY\n"
        "- This is a futures account. Position size is measured in contracts.\n"
        '- Say "contracts" or "contract count" in review text. Never say "lots" or "lot size".\n'
        "- Trade field lot_size and sizing.median_lot_size mean contract count.\n"
    )


def format_payload_for_prompt(payload):
    account_type = payload.get("account_type") or "CFD"
    size_field_label = _trade_size_field_label(account_type)
    median_size_field_label = (
        "median_contract_count"
        if normalize_account_type(account_type) == "FUTURES"
        else "median_lot_size"
    )
    summary = payload.get("summary", {})
    user_profile = payload.get("user_profile") or {}
    weekly_checkin = payload.get("weekly_checkin") or {}
    emotional_index = payload.get("emotional_index") or {}
    historical_context = payload.get("historical_context") or {}
    tone_context = payload.get("tone_context") or {}
    current_week_breakdowns = payload.get("current_week_breakdowns") or {}
    four_week_patterns = payload.get("four_week_patterns") or {}
    experiment_context = payload.get("experiment_context") or {}
    recent_experiments = payload.get("recent_experiments") or []
    surface_facts = payload.get("surface_facts") or []
    confidence_envelope = payload.get("confidence_envelope") or {}
    trades = payload.get("trades", [])

    lines = [
        "CONTEXT",
        f"- generated_at: {payload.get('generated_at') or '-'}",
        f"- period_start_utc: {payload.get('period_start_utc') or '-'}",
        f"- period_end_utc: {payload.get('period_end_utc') or '-'}",
        "- payload_scope: one completed review period for one trade account",
        "- period_bounds: period_start_utc inclusive, period_end_utc ending boundary",
        "- summary_scope: SUMMARY metrics describe the completed review period only",
        "- historical_scope: historical sections are prior account context, not this week's pair/session/weekday breakdown",
        "- count_semantics: total_trades, closed_trades, open_trades, closed_before_tp_count, closed_before_sl_count, and bundle_count are counts",
        "- percent_semantics: win_rate, historical_win_rate, tp_capture_pct, and *_share_pct fields are already percentages",
        "- currency_semantics: *_pnl fields are signed currency values for this account; *_drawdown_amount fields are drawdown magnitudes",
        "- realized_result_semantics: SUMMARY.net_pnl is the reviewed-period result; weekly_pnl/monthly_pnl are rolling calendar aggregates relative to generated_at",
        "- open_trade_semantics: total_trades includes open and closed trades; closed_trades is the realized-performance denominator",
        f"- notes_coverage: {_format_notes_coverage(payload.get('notes_coverage'), payload.get('notes_with_content'), payload.get('notes_missing'))}",
        f"- notes_confidence: {payload.get('notes_confidence') or '-'}",
        f"- notes_basis: {payload.get('notes_basis') or '-'}",
        f"- account_age_days: {payload.get('account_age_days') if payload.get('account_age_days') is not None else '-'}",
    ]

    if _payload_section_has_values(user_profile):
        lines.extend(
            [
                "",
                "USER PROFILE",
                f"- trading_style: {user_profile.get('trading_style') or '-'}",
                f"- instruments: {user_profile.get('instruments') or '-'}",
                f"- experience_level: {user_profile.get('experience_level') or '-'}",
            ]
        )

    if _payload_section_has_values(weekly_checkin):
        lines.extend(
            [
                "",
                "WEEKLY CHECKIN",
                f"- emotional_state: {weekly_checkin.get('emotional_state') or '-'}",
                f"- plan_adherence: {weekly_checkin.get('plan_adherence') or '-'}",
                f"- execution_quality: {weekly_checkin.get('execution_quality') or '-'}",
                f"- additional_context: {weekly_checkin.get('additional_context') or '-'}",
            ]
        )

    lines.extend(
        [
            "",
            "SUMMARY",
            f"- total_trades: {summary.get('total_trades', 0)}",
            f"- closed_trades: {summary.get('closed_trades', 0)}",
            f"- open_trades: {summary.get('open_trades', 0)}",
            f"- win_rate: {_format_percent(summary.get('win_rate'))}",
            f"- net_pnl: {_format_signed_currency(summary.get('net_pnl'))}",
            f"- weekly_pnl: {_format_signed_currency(summary.get('weekly_pnl'))}",
            f"- monthly_pnl: {_format_signed_currency(summary.get('monthly_pnl'))}",
            f"- closed_before_tp_count: {summary.get('closed_before_tp_count', 0)}",
            f"- closed_before_sl_count: {summary.get('closed_before_sl_count', 0)}",
            f"- pair_sample_is_diverse: {_format_bool(summary.get('pair_sample_is_diverse'))}",
            f"- equity_has_outlier_dominance: {_format_bool(summary.get('equity_has_outlier_dominance'))}",
            f"- top_symbol_by_trade_count: {summary.get('top_symbol_by_trade_count') or '-'}",
            f"- top_symbol_trade_share_pct: {_format_percent(summary.get('top_symbol_trade_share_pct'))}",
            f"- top_symbol_by_abs_pnl: {summary.get('top_symbol_by_abs_pnl') or '-'}",
            f"- top_symbol_abs_pnl_share_pct: {_format_percent(summary.get('top_symbol_abs_pnl_share_pct'))}",
            f"- largest_trade_symbol: {summary.get('largest_trade_symbol') or '-'}",
            f"- largest_trade_abs_pnl_share_pct: {_format_percent(summary.get('largest_trade_abs_pnl_share_pct'))}",
            f"- bundle_count: {summary.get('bundle_count', 0)}",
            f"- confirmed_revenge_trade_count: {summary.get('confirmed_revenge_trade_count', 0)}",
            f"- revenge_trade_count: {summary.get('revenge_trade_count', 0)}",
            f"- reactive_trade_count: {summary.get('reactive_trade_count', 0)}",
            f"- corrective_trade_count: {summary.get('corrective_trade_count', 0)}",
            f"- best_trade_pnl: {_format_signed_currency(summary.get('best_trade_pnl'))}",
            f"- worst_trade_pnl: {_format_signed_currency(summary.get('worst_trade_pnl'))}",
            f"- max_drawdown_amount: {_format_currency_magnitude(summary.get('max_drawdown'))}",
        ]
    )

    if _payload_section_has_values(tone_context):
        lines.extend(
            [
                "",
                "TONE_CONTEXT",
                f"- checkin_submitted: {_format_bool(tone_context.get('checkin_submitted'))}",
                f"- mode: {tone_context.get('mode') or '-'}",
                f"- reasons: {', '.join(tone_context.get('reasons') or []) or '-'}",
            ]
        )

    if confidence_envelope:
        lines.extend(
            [
                "",
                "CONFIDENCE_ENVELOPE",
                f"- level: {confidence_envelope.get('level') or '-'}",
                f"- reasons: {', '.join(confidence_envelope.get('reasons') or []) or '-'}",
            ]
        )

    if surface_facts:
        lines.extend(
            [
                "",
                "SURFACE_FACTS",
                "- semantics: facts the dashboard already shows; takeaways must add information beyond echoing these strings",
            ]
        )
        for fact in surface_facts:
            lines.append(f"- {fact}")

    if _payload_section_has_values(emotional_index):
        signals = emotional_index.get("signals") or {}
        components = emotional_index.get("components") or {}
        lines.extend(
            [
                "",
                "EMOTIONAL INDEX",
                "- score_range: 0.00 to 10.00 (higher = stronger objective behavioural pressure)",
                "- label_interpretation: low=quiet, moderate=mild, high=elevated, very_high=strong objective behavioural pressure",
                "- component_semantics: *_points and *_repetition_bonus fields are normalized weighted components, not counts",
                "- denominator_semantics: total_closed_trades is the denominator used for normalized behaviour scoring",
                f"- score: {_format_number(emotional_index.get('score'))}",
                f"- label: {emotional_index.get('label') or '-'}",
                f"- self_report_mismatch: {_format_bool(emotional_index.get('self_report_mismatch'))}",
                f"- subjective_points: {_format_number(components.get('subjective_points'))}",
                f"- confirmed_revenge_points: {_format_number(components.get('confirmed_revenge_points'))}",
                f"- heuristic_revenge_points: {_format_number(components.get('heuristic_revenge_points'))}",
                f"- revenge_points: {_format_number(components.get('revenge_points'))}",
                f"- confirmed_reactive_points: {_format_number(components.get('confirmed_reactive_points'))}",
                f"- heuristic_reactive_points: {_format_number(components.get('heuristic_reactive_points'))}",
                f"- reactive_points: {_format_number(components.get('reactive_points'))}",
                f"- confirmed_corrective_points: {_format_number(components.get('confirmed_corrective_points'))}",
                f"- heuristic_corrective_points: {_format_number(components.get('heuristic_corrective_points'))}",
                f"- corrective_points: {_format_number(components.get('corrective_points'))}",
                f"- bundle_count: {signals.get('bundle_count', 0)}",
                f"- total_closed_trades: {signals.get('total_closed_trades', 0)}",
                f"- confirmed_behavior_trade_count: {signals.get('confirmed_behavior_trade_count', 0)}",
                f"- confirmed_revenge_trade_count: {signals.get('confirmed_revenge_trade_count', 0)}",
                f"- heuristic_revenge_trade_count: {signals.get('heuristic_revenge_trade_count', 0)}",
                f"- confirmed_reactive_trade_count: {signals.get('confirmed_reactive_trade_count', 0)}",
                f"- heuristic_reactive_trade_count: {signals.get('heuristic_reactive_trade_count', 0)}",
                f"- reactive_trade_count: {signals.get('reactive_trade_count', 0)}",
                f"- reactive_signal_trade_count: {signals.get('reactive_signal_trade_count', 0)}",
                f"- confirmed_corrective_trade_count: {signals.get('confirmed_corrective_trade_count', 0)}",
                f"- heuristic_corrective_trade_count: {signals.get('heuristic_corrective_trade_count', 0)}",
                f"- corrective_trade_count: {signals.get('corrective_trade_count', 0)}",
                f"- corrective_signal_trade_count: {signals.get('corrective_signal_trade_count', 0)}",
                f"- revenge_trade_count: {signals.get('revenge_trade_count', 0)}",
            ]
        )

    performance_trends = payload.get("performance_trends") or {}
    if any(
        performance_trends.get(key) is not None
        for key in (
            "win_rate_current",
            "win_rate_historical",
            "expectancy_current",
            "expectancy_historical",
            "ei_trend",
            "prior_history_window_days",
        )
    ):
        lines.extend(
            [
                "",
                "PERFORMANCE_TRENDS",
                "- scope_note: these compare the completed review period to prior account history before that period (see comparison_basis and prior_history_window_days), not the dashboard four-week rolling panel",
                f"- comparison_basis: {performance_trends.get('comparison_basis') or '-'}",
                f"- prior_history_window_days: {performance_trends.get('prior_history_window_days') if performance_trends.get('prior_history_window_days') is not None else '-'}",
                f"- win_rate_current: {_format_percent(performance_trends.get('win_rate_current'))}",
                f"- win_rate_historical: {_format_percent(performance_trends.get('win_rate_historical'))}",
                f"- expectancy_current: {_format_signed_currency(performance_trends.get('expectancy_current'))}",
                f"- expectancy_historical: {_format_signed_currency(performance_trends.get('expectancy_historical'))}",
                f"- ei_trend: {performance_trends.get('ei_trend') or '-'}",
            ]
        )

    if current_week_breakdowns:
        lines.extend(
            [
                "",
                "CURRENT_WEEK_BREAKDOWNS",
            ]
        )
        for item in current_week_breakdowns.get("sessions") or []:
            lines.append(
                f"- session {item.get('name') or '-'}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
        for item in current_week_breakdowns.get("symbols") or []:
            lines.append(
                f"- symbol {item.get('symbol') or '-'}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
        for item in current_week_breakdowns.get("weekdays") or []:
            lines.append(
                f"- weekday {item.get('name') or '-'}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
        for item in current_week_breakdowns.get("strategies") or []:
            lines.append(
                f"- strategy {item.get('strategy_name') or '-'}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
        sizing = current_week_breakdowns.get("sizing") or {}
        frequency = current_week_breakdowns.get("frequency") or {}
        exit_quality = current_week_breakdowns.get("exit_quality") or {}
        strategy_coverage = current_week_breakdowns.get("strategy_coverage") or {}
        market_context = current_week_breakdowns.get("market_context") or {}
        lines.extend(
            [
                f"- sizing.median_planned_risk_dollars: {_format_currency_magnitude(sizing.get('median_planned_risk_dollars'))}",
                f"- sizing.median_risk_pct_of_account: {_format_percent(sizing.get('median_risk_pct_of_account'))}",
                f"- sizing.{median_size_field_label}: {_format_number(sizing.get('median_lot_size'))}",
                f"- sizing.outlier_size_count: {sizing.get('outlier_size_count', 0)}",
                f"- sizing.outlier_size_share_pct: {_format_percent(sizing.get('outlier_size_share_pct'))}",
                f"- frequency.trade_idea_count: {frequency.get('trade_idea_count', 0)}",
                f"- frequency.active_day_count: {frequency.get('active_day_count', 0)}",
                f"- frequency.trade_ideas_per_active_day: {_format_number(frequency.get('trade_ideas_per_active_day'))}",
                f"- frequency.busiest_session: {frequency.get('busiest_session') or '-'}",
                f"- strategy_coverage.trades_with_strategy: {strategy_coverage.get('trades_with_strategy', 0)}",
                f"- strategy_coverage.strategy_coverage_pct: {_format_percent(strategy_coverage.get('strategy_coverage_pct'))}",
                f"- market_context.trades_with_bars: {market_context.get('trades_with_bars', 0)}",
                f"- market_context.bar_coverage_pct: {_format_percent(market_context.get('bar_coverage_pct'))}",
                f"- market_context.post_exit_tp_reached_count: {market_context.get('post_exit_tp_reached_count', 0)}",
                f"- market_context.protective_stop_count: {market_context.get('protective_stop_count', 0)}",
                f"- market_context.trailing_or_breakeven_stop_count: {market_context.get('trailing_or_breakeven_stop_count', 0)}",
                f"- market_context.large_candle_entry_count: {market_context.get('large_candle_entry_count', 0)}",
                f"- market_context.entry_bar_against_direction_count: {market_context.get('entry_bar_against_direction_count', 0)}",
                f"- market_context.post_exit_continued_count: {market_context.get('post_exit_continued_count', 0)}",
                f"- market_context.post_exit_reversed_count: {market_context.get('post_exit_reversed_count', 0)}",
                f"- exit_quality.closed_before_tp_count: {exit_quality.get('closed_before_tp_count', 0)}",
                f"- exit_quality.closed_before_sl_count: {exit_quality.get('closed_before_sl_count', 0)}",
                f"- exit_quality.avg_tp_capture_pct: {_format_percent(exit_quality.get('avg_tp_capture_pct'))}",
            ]
        )

        risk_authority = current_week_breakdowns.get("risk_authority") or {}
        execution_outcome = current_week_breakdowns.get("execution_outcome") or {}
        if execution_outcome:
            lines.extend(
                [
                    f"- execution_outcome.outcome_class: {execution_outcome.get('outcome_class') or '-'}",
                    f"- execution_outcome.execution_class: {execution_outcome.get('execution_class') or '-'}",
                    f"- execution_outcome.week_archetype: {execution_outcome.get('week_archetype') or '-'}",
                    f"- execution_outcome.coaching_stance: {execution_outcome.get('coaching_stance') or '-'}",
                    f"- execution_outcome.stance_hint: {execution_outcome.get('stance_hint') or '-'}",
                    f"- execution_outcome.issue_points: {execution_outcome.get('issue_points', 0)}",
                    f"- execution_outcome.issue_evidence_level: {execution_outcome.get('issue_evidence_level') or '-'}",
                    f"- execution_outcome.issue_reasons: {', '.join(execution_outcome.get('issue_reasons') or []) or '-'}",
                    f"- execution_outcome.primary_issue: {execution_outcome.get('primary_issue') or '-'}",
                    f"- execution_outcome.primary_issue_hint: {execution_outcome.get('primary_issue_hint') or '-'}",
                    f"- execution_outcome.do_not_lead_with: {', '.join(execution_outcome.get('do_not_lead_with') or []) or '-'}",
                    f"- execution_outcome.same_symbol_after_loss_count: {execution_outcome.get('same_symbol_after_loss_count', 0)}",
                    f"- execution_outcome.same_trade_idea_reentry_count: {execution_outcome.get('same_trade_idea_reentry_count', 0)}",
                    f"- execution_outcome.losing_outlier_count: {execution_outcome.get('losing_outlier_count', 0)}",
                    f"- execution_outcome.outcome_concentrated: {_format_bool(execution_outcome.get('outcome_concentrated'))}",
                ]
            )
            for index, issue in enumerate(execution_outcome.get("ranked_issues") or [], start=1):
                lines.append(
                    f"- execution_outcome.ranked_issues[{index}]: "
                    f"reason={issue.get('reason') or '-'}, "
                    f"severity={issue.get('severity', '-')}, "
                    f"points={issue.get('points', '-')}, "
                    f"lead_hint={issue.get('lead_hint') or '-'}"
                )

        coaching_hypotheses = current_week_breakdowns.get("coaching_hypotheses") or []
        if coaching_hypotheses:
            lines.append(f"- coaching_hypotheses.count: {len(coaching_hypotheses)}")
            for index, hypothesis in enumerate(coaching_hypotheses, start=1):
                refs = ", ".join(hypothesis.get("evidence_refs") or []) or "-"
                lines.append(
                    f"- coaching_hypotheses[{index}]: "
                    f"type={hypothesis.get('type') or '-'}, "
                    f"rank={hypothesis.get('rank') or '-'}, "
                    f"severity={hypothesis.get('severity') or '-'}, "
                    f"confidence={hypothesis.get('confidence') or '-'}, "
                    f"evidence_refs={refs}"
                )
                if hypothesis.get("human_trap_hint"):
                    lines.append(
                        f"- coaching_hypotheses[{index}].human_trap_hint: {hypothesis.get('human_trap_hint')}"
                    )
                if hypothesis.get("false_lesson_hint"):
                    lines.append(
                        f"- coaching_hypotheses[{index}].false_lesson_hint: {hypothesis.get('false_lesson_hint')}"
                    )
                if hypothesis.get("better_lesson_hint"):
                    lines.append(
                        f"- coaching_hypotheses[{index}].better_lesson_hint: {hypothesis.get('better_lesson_hint')}"
                    )
                for key in (
                    "habit_rewarded_by_ref",
                    "habit_rewarded_by_symbol",
                    "habit_exposed_by_ref",
                    "habit_exposed_by_symbol",
                    "mechanism_hint",
                    "what_the_trader_may_have_mislearned",
                    "contrast_instruction",
                    "writing_shape",
                ):
                    if hypothesis.get(key):
                        lines.append(
                            f"- coaching_hypotheses[{index}].{key}: "
                            f"{_format_hypothesis_fact_value(hypothesis.get(key))}"
                        )
                if hypothesis.get("prompt_instruction"):
                    lines.append(
                        f"- coaching_hypotheses[{index}].prompt_instruction: {hypothesis.get('prompt_instruction')}"
                    )
                facts = hypothesis.get("facts") or {}
                for key in sorted(facts):
                    lines.append(
                        f"- coaching_hypotheses[{index}].facts.{key}: "
                        f"{_format_hypothesis_fact_value(facts.get(key))}"
                    )

        if risk_authority:
            lines.extend(
                [
                    f"- risk_authority.basis: {risk_authority.get('basis') or '-'}",
                    f"- risk_authority.value: {_format_number(risk_authority.get('value'))}",
                    f"- risk_authority.stable: {_format_bool(risk_authority.get('stable'))}",
                    f"- risk_authority.dispersion_pct: {_format_percent(risk_authority.get('dispersion_pct'))}",
                    f"- risk_authority.single_sample: {_format_bool(risk_authority.get('single_sample'))}",
                    f"- risk_authority.risk_judgment_allowed: {_format_bool(risk_authority.get('risk_judgment_allowed'))}",
                ]
            )

        post_loss = current_week_breakdowns.get("post_loss_response") or {}
        if post_loss:
            lines.append(f"- post_loss_response.basis: {post_loss.get('basis') or '-'}")
            lines.append(f"- post_loss_response.pattern_count: {post_loss.get('pattern_count', 0)}")
            lines.append(
                f"- post_loss_response.repeated_increased_risk: {_format_bool(post_loss.get('repeated_increased_risk'))}"
            )
            biggest = post_loss.get("biggest_loss") or {}
            if biggest:
                lines.append(
                    f"- post_loss_response.biggest_loss: loss_ref={biggest.get('loss_ref') or '-'}, "
                    f"next_ref={biggest.get('next_ref') or '-'}, risk_change={biggest.get('risk_change') or '-'}, "
                    f"next_outcome={biggest.get('next_outcome') or '-'}"
                )
                if biggest.get("phrase"):
                    lines.append(
                        f"- post_loss_response.biggest_loss.phrase: {biggest.get('phrase')}"
                    )
            for index, seq in enumerate(post_loss.get("sequences") or [], start=1):
                lines.append(
                    f"- post_loss_response.sequences[{index}]: loss_ref={seq.get('loss_ref') or '-'}, "
                    f"next_ref={seq.get('next_ref') or '-'}, risk_change={seq.get('risk_change') or '-'}, "
                    f"next_outcome={seq.get('next_outcome') or '-'}"
                )

        dominance = current_week_breakdowns.get("single_trade_dominance")
        if dominance:
            lines.append(
                f"- single_trade_dominance: dominant_symbol={dominance.get('dominant_symbol') or '-'}, "
                f"dominant_ref={dominance.get('dominant_ref') or '-'}, "
                f"abs_pnl_share_pct={_format_percent(dominance.get('abs_pnl_share_pct'))}, "
                f"threshold={dominance.get('threshold_basis') or '-'}"
            )
        else:
            lines.append("- single_trade_dominance: -")

        tp_shortfalls = current_week_breakdowns.get("tp_capture_shortfalls") or {}
        lines.append(
            f"- tp_capture_shortfalls.recurring: {_format_bool(tp_shortfalls.get('recurring'))}"
        )
        for index, item in enumerate(tp_shortfalls.get("trades") or [], start=1):
            lines.append(
                f"- tp_capture_shortfalls.trades[{index}]: ref={item.get('ref') or '-'}, "
                f"symbol={item.get('symbol') or '-'}, capture_pct={_format_percent(item.get('tp_capture_pct'))}"
            )

        session_concentration = current_week_breakdowns.get("session_concentration") or {}
        if session_concentration:
            lines.extend(
                [
                    f"- session_concentration.dominant_session: {session_concentration.get('dominant_session') or '-'}",
                    f"- session_concentration.share_pct: {_format_percent(session_concentration.get('share_pct'))}",
                    f"- session_concentration.single_session: {_format_bool(session_concentration.get('single_session'))}",
                    f"- session_concentration.mixed_outcome: {_format_bool(session_concentration.get('mixed_outcome'))}",
                ]
            )

        revenge_evidence = current_week_breakdowns.get("revenge_evidence") or {}
        if revenge_evidence:
            lines.extend(
                [
                    f"- revenge_evidence.pattern_class: {revenge_evidence.get('pattern_class') or '-'}",
                    f"- revenge_evidence.confirmed_count: {revenge_evidence.get('confirmed_count', 0)}",
                    f"- revenge_evidence.heuristic_count: {revenge_evidence.get('heuristic_count', 0)}",
                ]
            )
            for index, seq in enumerate(revenge_evidence.get("strong_sequences") or [], start=1):
                lines.append(
                    f"- revenge_evidence.strong_sequences[{index}]: ref={seq.get('ref') or '-'}, "
                    f"minutes_since_prev_close={_format_number(seq.get('minutes_since_prev_close'))}, "
                    f"risk_pct_vs_prev={seq.get('risk_pct_vs_prev') or '-'}, confirmed={_format_bool(seq.get('confirmed'))}"
                )

    if four_week_patterns:
        lines.extend(
            [
                "",
                "FOUR_WEEK_PATTERNS",
                f"- weeks_considered: {four_week_patterns.get('weeks_considered', 0)}",
            ]
        )
        for index, row in enumerate(four_week_patterns.get("weekly_series") or [], start=1):
            lines.append(
                f"- weekly_series[{index}]: period={row.get('period_start_utc') or '-'} -> {row.get('period_end_utc') or '-'}, "
                f"trade_ideas={row.get('trade_idea_count', 0)}, win_rate={_format_percent(row.get('win_rate'))}, "
                f"net_pnl={_format_signed_currency(row.get('net_pnl'))}, top_session={row.get('top_session') or '-'}, "
                f"top_symbol={row.get('top_symbol') or '-'}, top_weekday={row.get('top_weekday') or '-'}"
            )
        for row in four_week_patterns.get("session_patterns") or []:
            lines.append(f"- session_patterns: {row.get('name') or '-'} count={row.get('count', 0)}")
        for row in four_week_patterns.get("symbol_patterns") or []:
            lines.append(f"- symbol_patterns: {row.get('symbol') or '-'} count={row.get('count', 0)}")
        for row in four_week_patterns.get("weekday_patterns") or []:
            lines.append(f"- weekday_patterns: {row.get('name') or '-'} count={row.get('count', 0)}")
        behaviour_patterns = four_week_patterns.get("behaviour_patterns") or {}
        lines.extend(
            [
                f"- behaviour_patterns.weeks_with_trades: {behaviour_patterns.get('weeks_with_trades', 0)}",
                f"- behaviour_patterns.avg_post_loss_reentry_count: {_format_number(behaviour_patterns.get('avg_post_loss_reentry_count'))}",
                f"- behaviour_patterns.avg_larger_size_after_loss_count: {_format_number(behaviour_patterns.get('avg_larger_size_after_loss_count'))}",
            ]
        )

    if _payload_section_has_values(experiment_context):
        lines.extend(
            [
                "",
                "EXPERIMENT_CONTEXT",
                f"- eligible: {_format_bool(experiment_context.get('eligible'))}",
                f"- trade_idea_count: {experiment_context.get('trade_idea_count', 0)}",
                f"- min_required: {experiment_context.get('min_required', MIN_TRADE_IDEAS_FOR_EXPERIMENT)}",
            ]
        )

    lines.extend(
        [
            "",
            "RECENT_EXPERIMENTS",
        ]
    )
    if recent_experiments:
        for index, item in enumerate(recent_experiments, start=1):
            lines.append(
                f"- {index}. period_start_utc={item.get('period_start_utc') or '-'} text={item.get('text') or '-'}"
            )
    else:
        lines.append("- no recent experiments")

    lines.extend(
        [
            "",
            "HISTORICAL_CONTEXT",
            f"- comparison_scope: {historical_context.get('comparison_scope') or '-'}",
            "- comparison_scope_note: history sections are comparison-only context and may exclude the current review period",
            f"- window_start_utc: {historical_context.get('window_start_utc') or '-'}",
            f"- window_end_utc: {historical_context.get('window_end_utc') or '-'}",
            f"- window_days: {historical_context.get('window_days') or '-'}",
            f"- historical_total_trades: {(historical_context.get('summary') or {}).get('total_trades', 0)}",
            f"- historical_closed_trades: {(historical_context.get('summary') or {}).get('closed_trades', 0)}",
            f"- historical_win_rate: {_format_percent((historical_context.get('summary') or {}).get('win_rate'))}",
            f"- historical_expectancy: {_format_signed_currency((historical_context.get('summary') or {}).get('expectancy'))}",
            f"- historical_net_pnl: {_format_signed_currency((historical_context.get('summary') or {}).get('net_pnl'))}",
            f"- historical_max_drawdown_amount: {_format_currency_magnitude((historical_context.get('summary') or {}).get('max_drawdown'))}",
            "",
            "HISTORICAL_TOP_PAIRS",
        ]
    )

    historical_pairs = historical_context.get("top_pairs") or []
    if historical_pairs:
        for index, item in enumerate(historical_pairs, start=1):
            lines.append(
                f"- {index}. {(item.get('symbol') or '-')}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
    else:
        lines.append("- no pair history available")

    lines.extend(
        [
            "",
            "HISTORICAL_TOP_SESSIONS",
        ]
    )
    historical_sessions = historical_context.get("top_sessions") or []
    if historical_sessions:
        for index, item in enumerate(historical_sessions, start=1):
            lines.append(
                f"- {index}. {(item.get('name') or '-')}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
    else:
        lines.append("- no session history available")

    lines.extend(
        [
            "",
            "HISTORICAL_TOP_WEEKDAYS",
        ]
    )
    historical_weekdays = historical_context.get("top_weekdays") or []
    if historical_weekdays:
        for index, item in enumerate(historical_weekdays, start=1):
            lines.append(
                f"- {index}. {(item.get('name') or '-')}: count={item.get('count', 0)}, "
                f"win_rate={_format_percent(item.get('win_rate'))}, net_pnl={_format_signed_currency(item.get('net_pnl'))}"
            )
    else:
        lines.append("- no weekday history available")

    lines.extend(
        [
            "",
        f"TRADES ({len(trades)})",
        ]
    )

    if not trades:
        lines.append("- no trades available")
        return "\n".join(lines)

    for index, trade in enumerate(trades, start=1):
        note = trade.get("trade_note") or "-"
        market_context = trade.get("market_context") or {}
        stop_management = market_context.get("stop_management") or {}
        lines.extend(
            [
                f"{index}. review_ref: {trade.get('review_ref') or '-'}",
                f"   symbol: {trade.get('symbol') or '-'}",
                f"   contract_code: {trade.get('contract_code') or '-'}",
                f"   strategy_name: {trade.get('strategy_name') or '-'}",
                f"   strategy_version: {trade.get('strategy_version') if trade.get('strategy_version') is not None else '-'}",
                f"   strategy_description: {trade.get('strategy_description') or '-'}",
                f"   side: {trade.get('side') or '-'}",
                f"   entry_price: {_format_number(trade.get('entry_price'), digits=5)}",
                f"   exit_price: {_format_number(trade.get('exit_price'), digits=5)}",
                f"   stop_loss: {_format_number(trade.get('stop_loss'), digits=5)}",
                f"   take_profit: {_format_number(trade.get('take_profit'), digits=5)}",
                f"   {size_field_label}: {_format_number(trade.get('lot_size'))}",
                f"   planned_risk_dollars: {_format_currency_magnitude(trade.get('planned_risk_dollars'))}",
                f"   trade_risk_pct: {_format_percent(trade.get('trade_risk_pct'))}",
                f"   pnl: {_format_signed_currency(trade.get('pnl'))}",
                f"   entry_session: {trade.get('entry_session') or '-'}",
                f"   exit_session: {trade.get('exit_session') or '-'}",
                f"   session: {trade.get('session') or '-'}",
                f"   duration_minutes: {_format_number(trade.get('duration_minutes'))}",
                f"   opened_at: {trade.get('opened_at') or '-'}",
                f"   closed_at: {trade.get('closed_at') or '-'}",
                f"   trade_sequence_number: {trade.get('trade_sequence_number') if trade.get('trade_sequence_number') is not None else '-'}",
                f"   trade_number_in_session: {trade.get('trade_number_in_session') if trade.get('trade_number_in_session') is not None else '-'}",
                f"   prev_trade_pnl: {_format_signed_currency(trade.get('prev_trade_pnl'))}",
                f"   minutes_since_prev_close: {_format_number(trade.get('minutes_since_prev_close'))}",
                f"   risk_pct_vs_prev: {trade.get('risk_pct_vs_prev') or '-'}",
                f"   prev_symbol_trade_pnl: {_format_signed_currency(trade.get('prev_symbol_trade_pnl'))}",
                f"   minutes_since_prev_symbol_close: {_format_number(trade.get('minutes_since_prev_symbol_close'))}",
                f"   risk_pct_vs_prev_symbol: {trade.get('risk_pct_vs_prev_symbol') or '-'}",
                f"   loss_streak_before_trade: {trade.get('loss_streak_before_trade') if trade.get('loss_streak_before_trade') is not None else '-'}",
                f"   is_post_loss_trade: {_format_bool(trade.get('is_post_loss_trade'))}",
                f"   same_symbol_reentry: {_format_bool(trade.get('same_symbol_reentry'))}",
                f"   is_post_loss_same_symbol_trade: {_format_bool(trade.get('is_post_loss_same_symbol_trade'))}",
                f"   same_trade_idea_reentry: {_format_bool(trade.get('same_trade_idea_reentry'))}",
                f"   is_potential_revenge: {_format_bool(trade.get('is_potential_revenge'))}",
                f"   is_potential_reactive: {_format_bool(trade.get('is_potential_reactive'))}",
                f"   is_revenge: {_format_bool(trade.get('is_revenge'))}",
                f"   is_reactive: {_format_bool(trade.get('is_reactive'))}",
                f"   is_corrective: {_format_bool(trade.get('is_corrective'))}",
                f"   is_bundle: {_format_bool(trade.get('is_bundle'))}",
                f"   bundle_trade_count: {trade.get('bundle_trade_count') if trade.get('bundle_trade_count') is not None else '-'}",
                f"   planned_rr: {_format_number(trade.get('planned_rr'))}",
                f"   realized_rr: {_format_number(trade.get('realized_rr'))}",
                f"   tp_capture_pct: {_format_percent(trade.get('tp_capture_pct'))}",
                f"   closed_before_tp: {_format_optional_bool(trade.get('closed_before_tp'))}",
                f"   closed_before_sl: {_format_optional_bool(trade.get('closed_before_sl'))}",
                f"   market_context.bars_status: {market_context.get('bars_status') or '-'}",
                f"   market_context.timeframe: {market_context.get('timeframe') or '-'}",
                f"   market_context.in_trade_bars: {market_context.get('in_trade_bars', 0)}",
                f"   market_context.post_exit_bars: {market_context.get('post_exit_bars', 0)}",
                f"   market_context.mfe_price_move: {_format_number(market_context.get('mfe_price_move'), digits=5)}",
                f"   market_context.mae_price_move: {_format_number(market_context.get('mae_price_move'), digits=5)}",
                f"   market_context.mfe_r: {_format_number(market_context.get('mfe_r'))}",
                f"   market_context.mae_r: {_format_number(market_context.get('mae_r'))}",
                f"   market_context.entry_range_vs_prior_median: {_format_number(market_context.get('entry_range_vs_prior_median'))}",
                f"   market_context.entry_location_in_prior_range_pct: {_format_percent(market_context.get('entry_location_in_prior_range_pct'))}",
                f"   market_context.entry_near_prior_high: {_format_optional_bool(market_context.get('entry_near_prior_high'))}",
                f"   market_context.entry_near_prior_low: {_format_optional_bool(market_context.get('entry_near_prior_low'))}",
                f"   market_context.post_exit_tp_reached: {_format_optional_bool(market_context.get('post_exit_tp_reached'))}",
                f"   market_context.minutes_after_exit_to_tp: {_format_number(market_context.get('minutes_after_exit_to_tp'))}",
                f"   market_context.post_exit_sl_reached: {_format_optional_bool(market_context.get('post_exit_sl_reached'))}",
                f"   market_context.minutes_after_exit_to_sl: {_format_number(market_context.get('minutes_after_exit_to_sl'))}",
                f"   market_context.entry_active_sessions: {', '.join(market_context.get('entry_active_sessions') or []) or '-'}",
                f"   market_context.entry_in_session_overlap: {_format_optional_bool(market_context.get('entry_in_session_overlap'))}",
                f"   market_context.large_candle_before_entry: {_format_optional_bool(market_context.get('large_candle_before_entry'))}",
                f"   market_context.entry_bar_body_ratio: {_format_number(market_context.get('entry_bar_body_ratio'))}",
                f"   market_context.entry_bar_closes_in_trade_direction: {_format_optional_bool(market_context.get('entry_bar_closes_in_trade_direction'))}",
                f"   market_context.pre_entry_bars_in_trade_direction: {market_context.get('pre_entry_bars_in_trade_direction') if market_context.get('pre_entry_bars_in_trade_direction') is not None else '-'}",
                f"   market_context.entry_tick_volume_vs_median: {_format_number(market_context.get('entry_tick_volume_vs_median'))}",
                f"   market_context.post_exit_direction: {market_context.get('post_exit_direction') or '-'}",
                f"   market_context.post_exit_price_move: {_format_number(market_context.get('post_exit_price_move'), digits=5)}",
                f"   stop_management.stop_loss_breakeven_or_better: {_format_bool(stop_management.get('stop_loss_breakeven_or_better'))}",
                f"   stop_management.stop_loss_protects_profit: {_format_bool(stop_management.get('stop_loss_protects_profit'))}",
                f"   stop_management.possible_trailing_or_breakeven_stop: {_format_bool(stop_management.get('possible_trailing_or_breakeven_stop'))}",
                f"   stop_management.confidence: {stop_management.get('confidence') or '-'}",
                f"   stop_management.evidence: {', '.join(stop_management.get('evidence') or []) or '-'}",
                f"   outlier_size: {_format_bool(trade.get('outlier_size'))}",
                f"   outlier_lot_spike: {_format_bool(trade.get('outlier_lot_spike'))}",
                f"   possible_split_order: {_format_bool(trade.get('possible_split_order'))}",
                f"   split_group_size: {trade.get('split_group_size') if trade.get('split_group_size') is not None else '-'}",
                f"   split_group_index: {trade.get('split_group_index') if trade.get('split_group_index') is not None else '-'}",
                f"   split_group_role: {trade.get('split_group_role') or '-'}",
                f"   is_likely_corrective: {_format_bool(trade.get('is_likely_corrective'))}",
                f"   trade_note: {note}",
            ]
        )
    return "\n".join(lines)


def build_dashboard_advice_messages(payload, prompt_filename=None, profile_adjustments=""):
    prompt_history = get_or_create_prompt_history(prompt_filename)
    payload_json = serialize_payload(payload)
    prompt_input = format_universal_weekly_payload(payload)
    futures_adjustments = _futures_terminology_adjustments(payload)
    combined_adjustments = f"{profile_adjustments}{futures_adjustments}".strip()
    if combined_adjustments:
        prompt_input = f"{prompt_input}\n{combined_adjustments}"
    return prompt_history, [
        {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": prompt_history.prompt_text,
                }
            ],
        },
        {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": REVIEW_JSON_OUTPUT_INSTRUCTIONS,
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": prompt_input,
                }
            ],
        },
    ], payload_json


def build_weekly_rewrite_messages(pass_1_output, prompt_filename=DEFAULT_REWRITE_PROMPT_FILE):
    prompt_data = load_prompt_text(prompt_filename)
    prompt_text = prompt_data["prompt_text"]
    if "{PASS_1_OUTPUT}" not in prompt_text:
        raise AIConfigError("Weekly rewrite prompt must include {PASS_1_OUTPUT}.")
    return prompt_data, [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": prompt_text.replace("{PASS_1_OUTPUT}", pass_1_output),
                }
            ],
        }
    ]


def extract_response_text(response_payload):
    return normalize_dashboard_advice_text(_extract_response_raw_text(response_payload))


def extract_rewrite_response_text(response_payload):
    structured_review = _extract_structured_review(
        response_payload,
        set(),
        experiment_eligible=True,
    )
    if structured_review is not None:
        return structured_review["response_text"]
    return extract_response_text(response_payload)


def weekly_review_text_preserves_display_format(response_text):
    display = build_dashboard_review_display(response_text)
    return bool(
        (display.get("summary") or {}).get("text")
        and display.get("takeaways")
        and (display.get("improvement") or {}).get("text")
    )


def rewrite_weekly_dashboard_review(pass_1_output, *, model=None):
    prompt_data, messages = build_weekly_rewrite_messages(pass_1_output)
    response_payload = request_openai_response(
        messages,
        model=model or get_ai_model(),
    )
    response_text = extract_rewrite_response_text(response_payload)
    if not response_text:
        raise AIRequestError(describe_empty_response(response_payload))
    if not weekly_review_text_preserves_display_format(response_text):
        raise AIRequestError("Weekly rewrite response did not preserve the dashboard review format.")
    return response_text, response_payload, prompt_data


def rewrite_weekly_dashboard_review_or_fallback(pass_1_output, *, user_id, trade_account_id, period, model=None):
    try:
        return rewrite_weekly_dashboard_review(pass_1_output, model=model)
    except Exception as exc:
        logger.warning(
            "Weekly AI rewrite pass failed; falling back to pass 1. user_id=%s trade_account_id=%s "
            "period_start_utc=%s period_end_utc=%s error=%s",
            user_id,
            trade_account_id,
            (period or {}).get("period_start_utc"),
            (period or {}).get("period_end_utc"),
            exc,
        )
        return pass_1_output, None, None


def _compact_json_for_prompt(value):
    if value is None:
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return text
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


_WEEKLY_REVIEW_REF_TOKEN_RE = re.compile(r"\b[BT]\d+\b", re.IGNORECASE)
_ISO_DATE_T_TIME_SEPARATOR_RE = re.compile(r"(?<=\d{4}-\d{2}-\d{2})T(?=\d{2}:)")


def _spacify_iso_datetime_t_separator(obj):
    """Avoid literal 'T' between calendar date and clock time (e.g. 2026-04-02T10:00:00Z) in chat prompts."""
    if isinstance(obj, dict):
        return {key: _spacify_iso_datetime_t_separator(val) for key, val in obj.items()}
    if isinstance(obj, list):
        return [_spacify_iso_datetime_t_separator(item) for item in obj]
    if isinstance(obj, str):
        return _ISO_DATE_T_TIME_SEPARATOR_RE.sub(" ", obj)
    return obj


def _strip_residual_weekly_review_ref_codes(text):
    """Remove internal T1/B1-style tokens the user never sees on the dashboard."""
    normalized = str(text or "").strip()
    if not normalized:
        return ""

    def _ref_repl(match):
        # Do not strip the "T" in ISO-8601 fragments like 2026-04-02T10:00:00Z
        if normalized[match.end() : match.end() + 1] == ":":
            return match.group(0)
        return ""

    cleaned = _WEEKLY_REVIEW_REF_TOKEN_RE.sub(_ref_repl, normalized)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned.strip()


def _chat_context_rewrite_review_text(text, citation_lookup):
    normalized = normalize_dashboard_advice_text(text or "")
    if not normalized:
        return ""
    rewritten = rewrite_review_text_refs(normalized, citation_lookup)
    return _strip_residual_weekly_review_ref_codes(rewritten)


def _drop_refs_keys_recursive(obj):
    if isinstance(obj, dict):
        return {key: _drop_refs_keys_recursive(val) for key, val in obj.items() if key != "refs"}
    if isinstance(obj, list):
        return [_drop_refs_keys_recursive(item) for item in obj]
    return obj


def _rewrite_strings_in_structure(obj, citation_lookup):
    if isinstance(obj, dict):
        return {key: _rewrite_strings_in_structure(val, citation_lookup) for key, val in obj.items()}
    if isinstance(obj, list):
        return [_rewrite_strings_in_structure(item, citation_lookup) for item in obj]
    if isinstance(obj, str):
        return _chat_context_rewrite_review_text(obj, citation_lookup)
    return obj


def _sanitize_response_meta_json_for_chat(meta_json_text, citation_lookup):
    parsed = parse_json_blob(meta_json_text)
    if not isinstance(parsed, dict):
        return _compact_json_for_prompt(meta_json_text)
    without_refs = _drop_refs_keys_recursive(parsed)
    rewritten = _rewrite_strings_in_structure(without_refs, citation_lookup)
    spacified = _spacify_iso_datetime_t_separator(rewritten)
    return json.dumps(spacified, ensure_ascii=False, separators=(",", ":"))


def _sanitize_payload_json_for_chat(payload_json_text):
    parsed = parse_json_blob(payload_json_text)
    if not isinstance(parsed, dict):
        return _compact_json_for_prompt(payload_json_text)
    stripped = copy.deepcopy(parsed)
    spacified = _spacify_iso_datetime_t_separator(stripped)
    return json.dumps(spacified, ensure_ascii=False, separators=(",", ":"))


def _format_weekly_review_chat_ref_map(citation_lookup):
    if not citation_lookup:
        return ""

    lines = [
        "Use these refs only in square brackets after a plain-language trade phrase; the UI hides the ref and links the phrase."
    ]
    for ref, citation in citation_lookup.items():
        if not ref or not isinstance(citation, dict):
            continue
        label = str(citation.get("label") or citation.get("inline_label") or "").strip()
        citation_type = str(citation.get("type") or "").strip()
        if not label:
            continue
        type_label = "bundle" if citation_type == "bundle" else "trade"
        lines.append(f"{ref}: {label} ({type_label})")
    return "\n".join(lines)


def normalize_weekly_review_chat_suggested_prompts(raw_prompts, *, limit=WEEKLY_REVIEW_CHAT_DYNAMIC_PROMPT_LIMIT):
    normalized = []
    seen = set()
    items = raw_prompts if isinstance(raw_prompts, list) else []
    for raw_prompt in items:
        text = re.sub(r"\s+", " ", str(raw_prompt or "")).strip()
        if not text:
            continue
        if not text.endswith("?"):
            text = f"{text}?"
        key = text.lower()
        if key in seen:
            continue
        normalized.append(text)
        seen.add(key)
        if len(normalized) >= limit:
            break
    return normalized


def _extract_weekly_review_chat_json_candidate(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return ""
    if text.startswith("{") and text.endswith("}"):
        return text
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        return str(fence_match.group(1) or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def parse_weekly_review_chat_model_output(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return "", []

    for candidate in (text, _extract_weekly_review_chat_json_candidate(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        reply = str(parsed.get("reply") or "").strip()
        suggested_prompts = normalize_weekly_review_chat_suggested_prompts(
            parsed.get("suggested_prompts"),
        )
        if reply:
            return reply, suggested_prompts

    return text, []


def pack_weekly_review_chat_assistant_content(reply, suggested_prompts):
    base = str(reply or "").strip()
    prompts = normalize_weekly_review_chat_suggested_prompts(suggested_prompts)
    if not base:
        return ""
    if not prompts:
        return base
    return (
        base
        + WEEKLY_REVIEW_CHAT_SUGGESTIONS_MARKER
        + json.dumps(prompts, ensure_ascii=False)
    )


def unpack_weekly_review_chat_assistant_content(content):
    text = str(content or "")
    if WEEKLY_REVIEW_CHAT_SUGGESTIONS_MARKER not in text:
        return text.strip(), []
    reply, _, raw_json = text.partition(WEEKLY_REVIEW_CHAT_SUGGESTIONS_MARKER)
    try:
        parsed = json.loads(str(raw_json or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return reply.strip(), []
    if isinstance(parsed, list):
        return reply.strip(), normalize_weekly_review_chat_suggested_prompts(parsed)
    return reply.strip(), []


def build_weekly_review_chat_messages(
    review_record,
    user_message,
    chat_history=None,
    *,
    timezone_name="UTC",
):
    tz = str(timezone_name or "").strip() or "UTC"
    citation_lookup = build_weekly_review_citation_lookup(
        getattr(review_record, "payload_json", None),
        tz,
    )

    final_review_text = _chat_context_rewrite_review_text(
        getattr(review_record, "response_text", None) or "",
        citation_lookup,
    )
    pass_1_rw = _chat_context_rewrite_review_text(
        getattr(review_record, "pass_1_output", None) or "",
        citation_lookup,
    )
    raw_review_text = pass_1_rw if pass_1_rw and pass_1_rw != final_review_text else ""

    payload_json = _sanitize_payload_json_for_chat(getattr(review_record, "payload_json", None))
    response_meta_json = _sanitize_response_meta_json_for_chat(
        getattr(review_record, "response_meta_json", None),
        citation_lookup,
    )
    chat_ref_map = _format_weekly_review_chat_ref_map(citation_lookup)

    history_lines = []
    for message in chat_history or []:
        role = str(getattr(message, "role", "") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        raw_content = getattr(message, "content", "") or ""
        if role == "assistant":
            raw_content, _ = unpack_weekly_review_chat_assistant_content(raw_content)
        content = _chat_context_rewrite_review_text(raw_content, citation_lookup)
        if not content:
            continue
        history_lines.append(f"{role}: {content}")

    user_context = "\n\n".join(
        part
        for part in [
            "FINAL_WEEKLY_REVIEW_SHOWN_TO_USER\n" + (final_review_text or "-"),
            "RAW_PASS_1_REVIEW_IF_AVAILABLE\n" + (raw_review_text or "-"),
            "STRUCTURED_REVIEW_META_IF_AVAILABLE\n" + (response_meta_json or "-"),
            "TRADE_LINK_REFS_AVAILABLE_FOR_OUTPUT\n" + (chat_ref_map or "-"),
            "ORIGINAL_WEEKLY_REVIEW_PAYLOAD_AND_TRADE_CONTEXT_IF_AVAILABLE\n" + (payload_json or "-"),
            "RECENT_CHAT_HISTORY_FOR_THIS_REVIEW\n" + ("\n".join(history_lines) if history_lines else "-"),
            "USER_QUESTION\n" + _chat_context_rewrite_review_text(user_message, citation_lookup),
        ]
    )

    return [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": load_weekly_review_chat_prompt_text()}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": user_context}],
        },
    ]


def generate_weekly_review_chat_reply(
    review_record,
    user_message,
    chat_history=None,
    *,
    model=None,
    timezone_name="UTC",
):
    resolved_model = model or get_ai_model()
    messages = build_weekly_review_chat_messages(
        review_record,
        user_message,
        chat_history=chat_history,
        timezone_name=timezone_name,
    )
    response_payload = request_openai_response(
        messages,
        model=resolved_model,
        max_output_tokens=get_weekly_review_chat_max_output_tokens(),
    )
    raw_reply = extract_response_text(response_payload)
    reply, suggested_prompts = parse_weekly_review_chat_model_output(raw_reply)
    if not reply:
        raise AIRequestError(describe_empty_response(response_payload))
    return reply, suggested_prompts, response_payload, resolved_model


def _format_journal_chat_ref_map(payload):
    trades = payload.get("trades") if isinstance(payload, dict) else []
    if not isinstance(trades, list):
        return ""
    lines = [
        "Use only these refs in square brackets after a plain-language trade phrase; the UI hides the ref and links the phrase."
    ]
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        ref = str(trade.get("ref") or "").strip().upper()
        symbol = str(trade.get("symbol") or "").strip()
        closed_at = str(trade.get("closed_at") or "").strip()
        if not ref or not symbol:
            continue
        label = symbol
        if closed_at:
            label = f"{symbol} closed {closed_at[:10]}"
        lines.append(f"{ref}: {label} (trade)")
    return "\n".join(lines)


def build_journal_chat_messages(session, payload, user_message, chat_history=None):
    formatted_payload = format_journal_payload_for_prompt(payload)
    ref_map = _format_journal_chat_ref_map(payload if isinstance(payload, dict) else {})
    scope_type = str(getattr(session, "scope_type", "") or "").strip().lower()

    history_lines = []
    for message in chat_history or []:
        role = str(getattr(message, "role", "") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if not content:
            continue
        history_lines.append(f"{role}: {content}")

    user_context = "\n\n".join(
        [
            f"SESSION_SCOPE_TYPE\n{scope_type or '-'}",
            "TRADE_LINK_REFS_AVAILABLE_FOR_OUTPUT\n" + (ref_map or "-"),
            "STRUCTURED_SCOPE_PAYLOAD\n" + (formatted_payload or "-"),
            "RECENT_CHAT_HISTORY_FOR_THIS_SESSION\n" + ("\n".join(history_lines) if history_lines else "-"),
            "USER_MESSAGE\n" + str(user_message or "").strip(),
        ]
    )

    return [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": load_journal_chat_prompt_text()}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": user_context}],
        },
    ]


def generate_journal_chat_reply(
    session,
    payload,
    user_message,
    chat_history=None,
    *,
    model=None,
):
    resolved_model = model or get_ai_model()
    messages = build_journal_chat_messages(
        session,
        payload,
        user_message,
        chat_history=chat_history,
    )
    response_payload = request_openai_response(messages, model=resolved_model)
    reply = extract_response_text(response_payload)
    if not reply:
        raise AIRequestError(describe_empty_response(response_payload))
    return reply, response_payload, resolved_model


def describe_empty_response(response_payload):
    status = str(response_payload.get("status") or "").strip() or "unknown"
    incomplete_details = response_payload.get("incomplete_details") or {}
    reason = str(incomplete_details.get("reason") or "").strip()
    if reason:
        return f"OpenAI response did not include any text output (status={status}, reason={reason})."
    return f"OpenAI response did not include any text output (status={status})."


def summarize_response_payload(response_payload):
    usage = response_payload.get("usage") or {}
    incomplete_details = response_payload.get("incomplete_details") or {}
    output_items = response_payload.get("output") or []
    content_types = []
    for item in output_items:
        for content in item.get("content", []):
            content_type = str(content.get("type") or "").strip()
            if content_type:
                content_types.append(content_type)
    summary = {
        "id": response_payload.get("id"),
        "model": response_payload.get("model"),
        "status": response_payload.get("status"),
        "incomplete_reason": incomplete_details.get("reason"),
        "output_item_count": len(output_items),
        "content_types": content_types[:10],
        "has_output_text": bool(str(response_payload.get("output_text") or "").strip()),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
    }
    return summary


def request_openai_response(messages, *, model=None, timeout_seconds=None, max_output_tokens=None):
    resolved_model = model or get_ai_model()
    resolved_max_output_tokens = (
        max_output_tokens
        if max_output_tokens is not None
        else get_ai_max_output_tokens()
    )
    resolved_timeout_seconds = timeout_seconds or get_ai_timeout_seconds()
    api_key = get_openai_api_key()
    request_body = {
        "model": resolved_model,
        "input": messages,
        "max_output_tokens": resolved_max_output_tokens,
        "reasoning": {"effort": "medium"},
        "text": {"verbosity": "low"},
    }
    logger.info(
        "OpenAI request starting. model=%s max_output_tokens=%s timeout_seconds=%s",
        resolved_model,
        resolved_max_output_tokens,
        resolved_timeout_seconds,
    )
    encoded_body = json.dumps(request_body).encode("utf-8")
    request = Request(
        OPENAI_RESPONSES_URL,
        data=encoded_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=resolved_timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
            usage = response_payload.get("usage") or {}
            logger.info(
                "OpenAI response received. model=%s max_output_tokens=%s timeout_seconds=%s input_tokens=%s output_tokens=%s cached_input_tokens=%s summary=%s",
                resolved_model,
                resolved_max_output_tokens,
                resolved_timeout_seconds,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                (usage.get("input_tokens_details") or {}).get("cached_tokens"),
                summarize_response_payload(response_payload),
            )
            return response_payload
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise AIRequestError(
            f"OpenAI request failed for model {resolved_model} with HTTP {exc.code}: {details}"
        ) from exc
    except URLError as exc:
        raise AIRequestError(f"OpenAI request failed: {exc.reason}") from exc


def save_ai_response(
    *,
    user_id,
    trade_account_id,
    prompt_history,
    model,
    response_text,
    response_meta_json,
    payload_json,
    trade_count_used,
    source_last_trade_id,
    kind,
    period_start_utc=None,
    period_end_utc=None,
    pass_1_output=None,
    pass_2_output=None,
    prompt_version_pass_1=None,
    prompt_version_pass_2=None,
    model_used=None,
):
    ai_response = AIGeneratedResponse(
        user_id=user_id,
        trade_account_id=trade_account_id,
        prompt_history_id=prompt_history.id,
        kind=kind,
        model=model,
        response_text=response_text,
        pass_1_output=pass_1_output,
        pass_2_output=pass_2_output,
        prompt_version_pass_1=prompt_version_pass_1,
        prompt_version_pass_2=prompt_version_pass_2,
        model_used=model_used,
        response_meta_json=response_meta_json,
        payload_json=payload_json,
        payload_hash=hash_text(payload_json),
        trade_count_used=trade_count_used,
        source_last_trade_id=source_last_trade_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
    )
    db.session.add(ai_response)
    db.session.flush()
    return ai_response


def generate_dashboard_advice(*, user_id, trade_account_id=None, prompt_filename=None, max_trades=None):
    try:
        user_profile = _get_user_profile_for_prompt(user_id)
        weekly_checkin = _get_weekly_checkin_for_prompt(
            user_id=user_id,
            trade_account_id=trade_account_id,
        )
        payload = build_trade_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            max_trades=max_trades,
            user_profile=user_profile,
            weekly_checkin=weekly_checkin,
        )
        profile_adjustments = _build_profile_adjustments_for_prompt(
            user_profile,
            weekly_checkin,
            payload.get("emotional_index"),
        )
        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
            profile_adjustments=profile_adjustments,
        )
        response_payload = request_openai_response(messages, model=get_ai_model())
        allowed_refs = {
            _trade_citation_ref(trade)
            for trade in payload.get("trades", [])
            if _trade_citation_ref(trade)
        }
        experiment_context = payload.get("experiment_context") if isinstance(payload.get("experiment_context"), dict) else {}
        structured_review = _extract_structured_review(
            response_payload,
            allowed_refs,
            experiment_eligible=bool(experiment_context.get("eligible")),
        )
        response_text = (
            structured_review["response_text"]
            if structured_review is not None
            else extract_response_text(response_payload)
        )
        if not response_text:
            logger.warning(
                "AI dashboard advice returned no text. user_id=%s trade_account_id=%s kind=%s summary=%s",
                user_id,
                trade_account_id,
                prompt_history.prompt_id,
                summarize_response_payload(response_payload),
            )
            raise AIRequestError(describe_empty_response(response_payload))

        latest_trade = (
            Trade.query.filter_by(user_id=user_id, trade_account_id=trade_account_id)
            .order_by(Trade.id.desc())
            .first()
            if trade_account_id is not None
            else Trade.query.filter_by(user_id=user_id).order_by(Trade.id.desc()).first()
        )

        ai_response = save_ai_response(
            user_id=user_id,
            trade_account_id=trade_account_id,
            prompt_history=prompt_history,
            model=str(response_payload.get("model") or get_ai_model()),
            response_text=response_text,
            response_meta_json=(
                structured_review["response_meta_json"]
                if structured_review is not None
                else None
            ),
            payload_json=payload_json,
            trade_count_used=len(payload["trades"]),
            source_last_trade_id=latest_trade.id if latest_trade else None,
            kind=prompt_history.prompt_id,
        )
        db.session.commit()
        return {
            "record": ai_response,
            "payload": payload,
            "response_payload": response_payload,
            "response_text": response_text,
            "response_meta": (
                structured_review["response_meta"]
                if structured_review is not None
                else None
            ),
        }
    except Exception:
        db.session.rollback()
        raise


def get_latest_ai_response(*, user_id, trade_account_id=None, kind=None):
    query = AIGeneratedResponse.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        query = query.filter_by(trade_account_id=trade_account_id)
    if kind:
        query = query.filter_by(kind=kind)
    return query.order_by(AIGeneratedResponse.generated_at.desc(), AIGeneratedResponse.id.desc()).first()


def get_latest_weekly_dashboard_advice(*, user_id, trade_account_id=None, period_start_utc=None):
    query = AIGeneratedResponse.query.filter_by(
        user_id=user_id,
        trade_account_id=trade_account_id,
        kind=WEEKLY_DASHBOARD_KIND,
    )
    if period_start_utc is not None:
        query = query.filter_by(period_start_utc=period_start_utc)
    return query.order_by(AIGeneratedResponse.generated_at.desc(), AIGeneratedResponse.id.desc()).first()


def maybe_generate_weekly_dashboard_advice(
    *,
    user_id,
    trade_account_id=None,
    prompt_filename=None,
    max_trades=None,
    now_utc=None,
    require_recent_login=False,
    force_regenerate=False,
):
    period = get_latest_trade_week_period(
        user_id=user_id,
        trade_account_id=trade_account_id,
        now_utc=now_utc,
    )
    if period is None:
        return {
            "record": None,
            "generated": False,
            "period": get_weekly_dashboard_period(now_utc=now_utc),
            "skip_reason": "no_trades",
        }
    try:
        existing = get_latest_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
        )
        if existing is not None and not force_regenerate:
            return {"record": existing, "generated": False, "period": period}

        if not should_generate_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
            require_recent_login=require_recent_login,
        ):
            return {"record": None, "generated": False, "period": period}

        if not force_regenerate and not weekly_review_generation_past_market_week_cutoff(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period=period,
            now_utc=now_utc,
        ):
            return {
                "record": None,
                "generated": False,
                "period": period,
                "skip_reason": "before_weekly_cutoff",
            }

        user_profile = _get_user_profile_for_prompt(user_id)
        weekly_checkin = _get_weekly_checkin_for_prompt(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
        )
        payload = build_trade_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            max_trades=max_trades,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
            closed_trades_only=True,
            user_profile=user_profile,
            weekly_checkin=weekly_checkin,
        )
        if not payload["trades"]:
            return {"record": None, "generated": False, "period": period, "skip_reason": "no_trades"}
        payload_json = serialize_payload(payload)
        payload_hash = hash_text(payload_json)
        if existing is not None and not force_regenerate and existing.payload_hash == payload_hash:
            return {"record": existing, "generated": False, "period": period}

        profile_adjustments = _build_profile_adjustments_for_prompt(
            user_profile,
            weekly_checkin,
            payload.get("emotional_index"),
        )
        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
            profile_adjustments=profile_adjustments,
        )
        response_payload = request_openai_response(messages, model=get_ai_model())
        allowed_refs = {
            _trade_citation_ref(trade)
            for trade in payload.get("trades", [])
            if _trade_citation_ref(trade)
        }
        experiment_context = payload.get("experiment_context") if isinstance(payload.get("experiment_context"), dict) else {}
        structured_review = _extract_structured_review(
            response_payload,
            allowed_refs,
            experiment_eligible=bool(experiment_context.get("eligible")),
        )
        response_text = (
            structured_review["response_text"]
            if structured_review is not None
            else extract_response_text(response_payload)
        )
        if not response_text:
            logger.warning(
                "Weekly AI dashboard advice returned no text. user_id=%s trade_account_id=%s kind=%s "
                "period_start_utc=%s period_end_utc=%s summary=%s",
                user_id,
                trade_account_id,
                WEEKLY_DASHBOARD_KIND,
                period["period_start_utc"],
                period["period_end_utc"],
                summarize_response_payload(response_payload),
            )
            raise AIRequestError(describe_empty_response(response_payload))

        pass_1_output = response_text
        pass_1_model = str(response_payload.get("model") or get_ai_model())
        pass_2_output, rewrite_response_payload, rewrite_prompt_data = rewrite_weekly_dashboard_review_or_fallback(
            pass_1_output,
            user_id=user_id,
            trade_account_id=trade_account_id,
            period=period,
            model=get_ai_model(),
        )
        final_response_text = pass_2_output
        model_used = str(
            (rewrite_response_payload or {}).get("model")
            or response_payload.get("model")
            or get_ai_model()
        )

        latest_trade = (
            Trade.query.filter_by(user_id=user_id, trade_account_id=trade_account_id)
            .order_by(Trade.id.desc())
            .first()
            if trade_account_id is not None
            else Trade.query.filter_by(user_id=user_id).order_by(Trade.id.desc()).first()
        )

        ai_response = save_ai_response(
            user_id=user_id,
            trade_account_id=trade_account_id,
            prompt_history=prompt_history,
            model=pass_1_model,
            response_text=final_response_text,
            response_meta_json=(
                structured_review["response_meta_json"]
                if structured_review is not None
                else None
            ),
            payload_json=payload_json,
            trade_count_used=len(payload["trades"]),
            source_last_trade_id=latest_trade.id if latest_trade else None,
            kind=WEEKLY_DASHBOARD_KIND,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
            pass_1_output=pass_1_output,
            pass_2_output=pass_2_output,
            prompt_version_pass_1=prompt_history.prompt_sha256,
            prompt_version_pass_2=(
                hash_text(rewrite_prompt_data["prompt_text"])
                if rewrite_prompt_data is not None
                else None
            ),
            model_used=model_used,
        )
        db.session.commit()
        return {
            "record": ai_response,
            "generated": True,
            "period": period,
            "payload": payload,
            "response_payload": response_payload,
            "rewrite_response_payload": rewrite_response_payload,
            "response_text": final_response_text,
            "pass_1_output": pass_1_output,
            "pass_2_output": pass_2_output,
            "response_meta": (
                structured_review["response_meta"]
                if structured_review is not None
                else None
            ),
        }
    except Exception:
        db.session.rollback()
        raise
