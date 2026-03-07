import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from models import AIGeneratedResponse, AIPromptHistory, Trade, User, db
from trading import build_trade_analytics, format_trade_symbol, resolve_pnl


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_PROMPT_FILE = "dashboard_advice.txt"
DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_MAX_OUTPUT_TOKENS = 220
DEFAULT_TIMEOUT_SECONDS = 30
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
WEEKLY_DASHBOARD_KIND = "weekly_dashboard_advice"
WEEKLY_MARKET_TIMEZONE = ZoneInfo("America/New_York")
WEEKLY_CUTOFF_WEEKDAY = 4
WEEKLY_CUTOFF_HOUR = 17
WEEKLY_CUTOFF_MINUTE = 30
WEEKLY_ACTIVITY_LOOKBACK_DAYS = 3


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


def get_ai_model():
    return os.getenv("AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


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


def build_trade_payload(
    *,
    user_id,
    trade_account_id=None,
    max_trades=None,
    period_start_utc=None,
    period_end_utc=None,
):
    trade_query = Trade.query.filter_by(user_id=user_id)
    if trade_account_id is not None:
        trade_query = trade_query.filter_by(trade_account_id=trade_account_id)
    if period_start_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at >= period_start_utc)
    if period_end_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at < period_end_utc)

    trades = trade_query.order_by(Trade.opened_at.desc(), Trade.id.desc()).all()
    if max_trades is not None and max_trades > 0:
        trades = trades[:max_trades]

    analytics = build_trade_analytics(
        trades,
        display_timezone_name=get_ai_timezone_name(),
    )

    serialized_trades = []
    for trade in trades:
        serialized_trades.append(
            {
                "symbol": format_trade_symbol(trade),
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "lot_size": trade.lot_size,
                "pnl": resolve_pnl(trade),
                "opened_at": format_utc_timestamp(trade.opened_at),
                "closed_at": format_utc_timestamp(trade.closed_at),
                "trade_note": (trade.trade_note or "").strip() or None,
            }
        )

    payload = {
        "generated_at": format_utc_timestamp(datetime.utcnow()),
        "period_start_utc": format_utc_timestamp(period_start_utc),
        "period_end_utc": format_utc_timestamp(period_end_utc),
        "summary": {
            "total_trades": analytics["summary"]["total_trades"],
            "closed_trades": analytics["summary"]["closed_trades"],
            "open_trades": analytics["summary"]["open_trades"],
            "win_rate": analytics["summary"]["win_rate"],
            "net_pnl": analytics["summary"]["net_pnl"],
            "weekly_pnl": analytics["summary"]["weekly_pnl"],
            "monthly_pnl": analytics["summary"]["monthly_pnl"],
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
    return payload


def has_trade_data_for_period(*, user_id, trade_account_id=None, period_start_utc=None, period_end_utc=None):
    trade_query = db.session.query(Trade.id).filter_by(user_id=user_id)
    if trade_account_id is not None:
        trade_query = trade_query.filter_by(trade_account_id=trade_account_id)
    if period_start_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at >= period_start_utc)
    if period_end_utc is not None:
        trade_query = trade_query.filter(Trade.opened_at < period_end_utc)
    return trade_query.first() is not None


def should_generate_weekly_dashboard_advice(
    *,
    user_id,
    trade_account_id=None,
    period_start_utc=None,
    period_end_utc=None,
):
    if not user_id or trade_account_id is None:
        return False
    current_utc = _to_utc_naive(datetime.now(timezone.utc))
    active_cutoff = current_utc - timedelta(days=WEEKLY_ACTIVITY_LOOKBACK_DAYS)
    user_last_login_at = (
        db.session.query(User.last_login_at)
        .filter(User.id == user_id)
        .scalar()
    )
    if user_last_login_at is None or user_last_login_at < active_cutoff:
        return False
    return has_trade_data_for_period(
        user_id=user_id,
        trade_account_id=trade_account_id,
        period_start_utc=period_start_utc,
        period_end_utc=period_end_utc,
    )


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


def _format_percent(value):
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def format_payload_for_prompt(payload):
    summary = payload.get("summary", {})
    trades = payload.get("trades", [])

    lines = [
        "CONTEXT",
        f"- generated_at: {payload.get('generated_at') or '-'}",
        f"- period_start_utc: {payload.get('period_start_utc') or '-'}",
        f"- period_end_utc: {payload.get('period_end_utc') or '-'}",
        "",
        "SUMMARY",
        f"- total_trades: {summary.get('total_trades', 0)}",
        f"- closed_trades: {summary.get('closed_trades', 0)}",
        f"- open_trades: {summary.get('open_trades', 0)}",
        f"- win_rate: {_format_percent(summary.get('win_rate'))}",
        f"- net_pnl: {_format_signed_currency(summary.get('net_pnl'))}",
        f"- weekly_pnl: {_format_signed_currency(summary.get('weekly_pnl'))}",
        f"- monthly_pnl: {_format_signed_currency(summary.get('monthly_pnl'))}",
        f"- best_trade_pnl: {_format_signed_currency(summary.get('best_trade_pnl'))}",
        f"- worst_trade_pnl: {_format_signed_currency(summary.get('worst_trade_pnl'))}",
        f"- max_drawdown: {_format_number(summary.get('max_drawdown'))}",
        "",
        f"TRADES ({len(trades)})",
    ]

    if not trades:
        lines.append("- no trades available")
        return "\n".join(lines)

    for index, trade in enumerate(trades, start=1):
        note = trade.get("trade_note") or "-"
        lines.extend(
            [
                f"{index}. symbol: {trade.get('symbol') or '-'}",
                f"   side: {trade.get('side') or '-'}",
                f"   entry_price: {_format_number(trade.get('entry_price'), digits=5)}",
                f"   exit_price: {_format_number(trade.get('exit_price'), digits=5)}",
                f"   lot_size: {_format_number(trade.get('lot_size'))}",
                f"   pnl: {_format_signed_currency(trade.get('pnl'))}",
                f"   opened_at: {trade.get('opened_at') or '-'}",
                f"   closed_at: {trade.get('closed_at') or '-'}",
                f"   trade_note: {note}",
            ]
        )
    return "\n".join(lines)


def build_dashboard_advice_messages(payload, prompt_filename=None):
    prompt_history = get_or_create_prompt_history(prompt_filename)
    payload_json = serialize_payload(payload)
    prompt_input = format_payload_for_prompt(payload)
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
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": prompt_input,
                }
            ],
        },
    ], payload_json


def extract_response_text(response_payload):
    output_text = str(response_payload.get("output_text") or "").strip()
    if output_text:
        return output_text

    text_chunks = []
    for item in response_payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                text_chunks.append(str(content["text"]).strip())
    return "\n\n".join(chunk for chunk in text_chunks if chunk).strip()


def request_openai_response(messages, *, model=None, max_output_tokens=None, timeout_seconds=None):
    api_key = get_openai_api_key()
    request_body = {
        "model": model or get_ai_model(),
        "input": messages,
        "max_output_tokens": max_output_tokens or get_ai_max_output_tokens(),
    }
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
        with urlopen(request, timeout=timeout_seconds or get_ai_timeout_seconds()) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise AIRequestError(f"OpenAI request failed with HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise AIRequestError(f"OpenAI request failed: {exc.reason}") from exc


def save_ai_response(
    *,
    user_id,
    trade_account_id,
    prompt_history,
    model,
    response_text,
    payload_json,
    trade_count_used,
    source_last_trade_id,
    kind,
    period_start_utc=None,
    period_end_utc=None,
):
    ai_response = AIGeneratedResponse(
        user_id=user_id,
        trade_account_id=trade_account_id,
        prompt_history_id=prompt_history.id,
        kind=kind,
        model=model,
        response_text=response_text,
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
        payload = build_trade_payload(
            user_id=user_id,
            trade_account_id=trade_account_id,
            max_trades=max_trades,
        )
        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
        )
        response_payload = request_openai_response(messages, model=get_ai_model())
        response_text = extract_response_text(response_payload)
        if not response_text:
            raise AIRequestError("OpenAI response did not include any text output.")

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
):
    period = get_weekly_dashboard_period(now_utc=now_utc)
    try:
        if not should_generate_weekly_dashboard_advice(
            user_id=user_id,
            trade_account_id=trade_account_id,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
        ):
            return {"record": None, "generated": False, "period": period}

        existing = get_latest_weekly_dashboard_advice(
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
        )
        if not payload["trades"]:
            return {"record": None, "generated": False, "period": period}

        payload_json = serialize_payload(payload)
        payload_hash = hash_text(payload_json)
        if existing is not None and existing.payload_hash == payload_hash:
            return {"record": existing, "generated": False, "period": period}

        prompt_history, messages, payload_json = build_dashboard_advice_messages(
            payload,
            prompt_filename=prompt_filename,
        )
        response_payload = request_openai_response(messages, model=get_ai_model())
        response_text = extract_response_text(response_payload)
        if not response_text:
            raise AIRequestError("OpenAI response did not include any text output.")

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
            payload_json=payload_json,
            trade_count_used=len(payload["trades"]),
            source_last_trade_id=latest_trade.id if latest_trade else None,
            kind=WEEKLY_DASHBOARD_KIND,
            period_start_utc=period["period_start_utc"],
            period_end_utc=period["period_end_utc"],
        )
        db.session.commit()
        return {
            "record": ai_response,
            "generated": True,
            "period": period,
            "payload": payload,
            "response_payload": response_payload,
            "response_text": response_text,
        }
    except Exception:
        db.session.rollback()
        raise
