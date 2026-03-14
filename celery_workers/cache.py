import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from redis import Redis
from redis.exceptions import RedisError

ANALYTICS_TTL = 3600
AI_STATUS_QUEUED_TTL = 900
AI_STATUS_RUNNING_TTL = 900
AI_STATUS_FAILED_TTL = 600

_redis_client = None


class CacheUnavailableError(RuntimeError):
    pass


def _get_redis_url():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        raise CacheUnavailableError("REDIS_URL is not configured.")
    return redis_url


def _client():
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = Redis.from_url(_get_redis_url(), decode_responses=True)
        except RedisError as exc:
            raise CacheUnavailableError(f"Redis unavailable: {exc}") from exc
    return _redis_client


def _run_redis(callable_obj):
    try:
        return callable_obj()
    except CacheUnavailableError:
        raise
    except RedisError as exc:
        raise CacheUnavailableError(f"Redis unavailable: {exc}") from exc


def _period_token(period_start_utc=None):
    if period_start_utc is None:
        return "none"
    if hasattr(period_start_utc, "isoformat"):
        return period_start_utc.isoformat()
    return str(period_start_utc)


def cache_key(prefix, user_id, trade_account_id=None):
    account_token = trade_account_id if trade_account_id is not None else "none"
    return f"{prefix}:u{user_id}:a{account_token}"


def ai_status_key(user_id, trade_account_id=None, period_start_utc=None):
    account_token = trade_account_id if trade_account_id is not None else "none"
    return (
        f"ai_status:u{user_id}:a{account_token}:"
        f"p{_period_token(period_start_utc=period_start_utc)}"
    )


def get_cached(prefix, user_id, trade_account_id=None):
    raw = _run_redis(lambda: _client().get(cache_key(prefix, user_id, trade_account_id)))
    return json.loads(raw) if raw else None


def set_cached(prefix, user_id, data, ttl, trade_account_id=None):
    payload = json.dumps(data, separators=(",", ":"))
    _run_redis(
        lambda: _client().setex(
            cache_key(prefix, user_id, trade_account_id),
            int(ttl),
            payload,
        )
    )


def delete_cached(prefix, user_id, trade_account_id=None):
    _run_redis(lambda: _client().delete(cache_key(prefix, user_id, trade_account_id)))


def invalidate(user_id, trade_account_id=None):
    keys = [
        cache_key("analytics", user_id, trade_account_id),
        cache_key("rr_summary", user_id, trade_account_id),
        cache_key("dashboard", user_id, trade_account_id),
    ]
    _run_redis(lambda: _client().delete(*keys))


def get_ai_status(user_id, trade_account_id=None, period_start_utc=None):
    return _run_redis(
        lambda: _client().get(
            ai_status_key(
                user_id,
                trade_account_id=trade_account_id,
                period_start_utc=period_start_utc,
            )
        )
    )


def claim_ai_status(user_id, trade_account_id=None, period_start_utc=None, status="queued", ttl=AI_STATUS_QUEUED_TTL):
    return bool(
        _run_redis(
            lambda: _client().set(
                ai_status_key(
                    user_id,
                    trade_account_id=trade_account_id,
                    period_start_utc=period_start_utc,
                ),
                status,
                ex=int(ttl),
                nx=True,
            )
        )
    )


def set_ai_status(user_id, trade_account_id=None, period_start_utc=None, status="queued", ttl=AI_STATUS_QUEUED_TTL):
    _run_redis(
        lambda: _client().setex(
            ai_status_key(
                user_id,
                trade_account_id=trade_account_id,
                period_start_utc=period_start_utc,
            ),
            int(ttl),
            status,
        )
    )


def clear_ai_status(user_id, trade_account_id=None, period_start_utc=None):
    _run_redis(
        lambda: _client().delete(
            ai_status_key(
                user_id,
                trade_account_id=trade_account_id,
                period_start_utc=period_start_utc,
            )
        )
    )
