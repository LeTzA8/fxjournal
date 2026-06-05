import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
from datetime import datetime, timezone

from redis import Redis
from redis.exceptions import RedisError

ANALYTICS_TTL = 3600
AI_STATUS_QUEUED_TTL = 900
AI_STATUS_RUNNING_TTL = 900
AI_STATUS_FAILED_TTL = 600
WORKER_MONITOR_TTL = 60 * 60 * 24
ADMIN_MT5_MONITOR_TTL = 20
ADMIN_MT5_MONITOR_CACHE_KEY = "admin_mt5_monitor_snapshot"

# Shared across MT5 setup + sync workers on the VM so only one MT5 runtime
# operation (bootstrap, login, sync, bar fetch, terminal cleanup) runs at a
# time. Token-based release + TTL means a crashed owner can't brick access.
MT5_GLOBAL_LOCK_KEY = "mt5_global_lock"
MT5_GLOBAL_LOCK_SETUP_TTL = 600   # 10 min: copytree + bootstrap + login + verify
MT5_GLOBAL_LOCK_SYNC_TTL = 120    # 2 min: one sync cycle
MT5_GLOBAL_LOCK_CLEANUP_TTL = 180  # 3 min: kill terminal + rmtree
MT5_GLOBAL_LOCK_BAR_FETCH_TTL = 120

_redis_client = None
_LOCK_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
end
return 0
"""


class CacheUnavailableError(RuntimeError):
    pass


def _get_redis_url():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        raise CacheUnavailableError("REDIS_URL is not configured.")
    return redis_url


def _normalize_redis_url(url):
    # Normalize CERT_* constant names to lowercase values redis-py accepts in URLs.
    import re
    url = re.sub(
        r'(ssl_cert_reqs=)(CERT_NONE|CERT_OPTIONAL|CERT_REQUIRED)',
        lambda m: m.group(1) + m.group(2).replace("CERT_", "").lower(),
        url,
    )
    # rediss:// connections require ssl_cert_reqs; default to "none" so the VM
    # env only needs REDIS_URL without any manual ssl_cert_reqs suffix.
    if url.startswith("rediss://") and "ssl_cert_reqs=" not in url:
        url += ("&" if "?" in url else "?") + "ssl_cert_reqs=none"
    return url


def _client():
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = Redis.from_url(
                _normalize_redis_url(_get_redis_url()), decode_responses=True
            )
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
        cache_key("analytics_v2", user_id, trade_account_id),
        cache_key("analytics_v3", user_id, trade_account_id),
        cache_key("analytics_v4", user_id, trade_account_id),
        cache_key("analytics_v5", user_id, trade_account_id),
        cache_key("rr_summary", user_id, trade_account_id),
        cache_key("rr_summary_v2", user_id, trade_account_id),
        cache_key("rr_summary_v3", user_id, trade_account_id),
        cache_key("rr_summary_v4", user_id, trade_account_id),
        cache_key("rr_summary_v5", user_id, trade_account_id),
        cache_key("dashboard", user_id, trade_account_id),
        cache_key("dashboard_v2", user_id, trade_account_id),
        cache_key("dashboard_v3", user_id, trade_account_id),
        cache_key("dashboard_v4", user_id, trade_account_id),
    ]
    _run_redis(lambda: _client().delete(*keys))


def _stringify_cache_value(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="seconds")
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _write_hash(hash_key, mapping, ttl=WORKER_MONITOR_TTL):
    cleaned_mapping = {
        str(key): _stringify_cache_value(value)
        for key, value in (mapping or {}).items()
        if value is not None
    }
    if not cleaned_mapping:
        return

    def _write():
        client = _client()
        client.hset(str(hash_key), mapping=cleaned_mapping)
        if ttl:
            client.expire(str(hash_key), int(ttl))

    _run_redis(_write)


def _read_hash(hash_key):
    return _run_redis(lambda: _client().hgetall(str(hash_key))) or {}


def _scan_keys(pattern):
    return list(_run_redis(lambda: list(_client().scan_iter(match=str(pattern)))))


def worker_state_key(worker_kind, worker_id):
    return f"worker_state:{worker_kind}:{worker_id}"


def queue_monitor_key(queue_name):
    return f"queue_monitor:{queue_name}"


def set_worker_state(worker_kind, worker_id, mapping, ttl=WORKER_MONITOR_TTL):
    _write_hash(worker_state_key(worker_kind, worker_id), mapping, ttl=ttl)


def get_worker_state(worker_kind, worker_id):
    return _read_hash(worker_state_key(worker_kind, worker_id))


def list_worker_states(worker_kind):
    worker_states = []
    prefix = f"worker_state:{worker_kind}:"
    for key in _scan_keys(f"{prefix}*"):
        state = _read_hash(key)
        if not state:
            continue
        worker_id = str(key)[len(prefix):]
        worker_states.append({"worker_id": worker_id, **state})
    worker_states.sort(key=lambda row: row.get("worker_id", ""))
    return worker_states


def list_mt5_worker_states():
    """Return mt5_sync and mt5_setup worker rows using one Redis SCAN."""
    grouped = {"mt5_sync": [], "mt5_setup": []}
    prefix = "worker_state:"
    for key in _scan_keys(f"{prefix}*"):
        key_text = str(key)
        if not key_text.startswith(prefix):
            continue
        remainder = key_text[len(prefix):]
        worker_kind, _, worker_id = remainder.partition(":")
        if worker_kind not in grouped or not worker_id:
            continue
        state = _read_hash(key)
        if not state:
            continue
        grouped[worker_kind].append({"worker_id": worker_id, **state})
    for rows in grouped.values():
        rows.sort(key=lambda row: row.get("worker_id", ""))
    return grouped


def get_queue_depths(queue_names):
    names = [str(name) for name in (queue_names or []) if str(name or "").strip()]
    if not names:
        return {}

    def _read():
        client = _client()
        pipe = client.pipeline(transaction=False)
        for name in names:
            pipe.llen(name)
        values = pipe.execute()
        return {name: int(value or 0) for name, value in zip(names, values)}

    return _run_redis(_read)


def get_admin_mt5_monitor_cache():
    raw = _run_redis(lambda: _client().get(ADMIN_MT5_MONITOR_CACHE_KEY))
    return json.loads(raw) if raw else None


def set_admin_mt5_monitor_cache(data, ttl=ADMIN_MT5_MONITOR_TTL):
    payload = json.dumps(data, separators=(",", ":"))
    _run_redis(
        lambda: _client().setex(
            ADMIN_MT5_MONITOR_CACHE_KEY,
            int(ttl),
            payload,
        )
    )


def clear_admin_mt5_monitor_cache():
    _run_redis(lambda: _client().delete(ADMIN_MT5_MONITOR_CACHE_KEY))


def set_queue_monitor_state(queue_name, mapping, ttl=WORKER_MONITOR_TTL):
    _write_hash(queue_monitor_key(queue_name), mapping, ttl=ttl)


def get_queue_monitor_state(queue_name):
    return _read_hash(queue_monitor_key(queue_name))


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


def claim_lock(lock_key, token, ttl):
    return bool(
        _run_redis(
            lambda: _client().set(
                str(lock_key),
                str(token),
                ex=int(ttl),
                nx=True,
            )
        )
    )


def release_lock(lock_key, token):
    _run_redis(
        lambda: _client().eval(
            _LOCK_RELEASE_SCRIPT,
            1,
            str(lock_key),
            str(token),
        )
    )


def peek_lock_holder(lock_key):
    """Return the current token stored under *lock_key* (or None)."""
    return _run_redis(lambda: _client().get(str(lock_key)))


def acquire_mt5_global_lock(token, ttl, *, wait_seconds=0.0, poll_interval=0.5):
    """Acquire the shared MT5 runtime lock used across setup + sync workers.

    *token* identifies the owner (e.g. "setup:<task_id>") — release must use
    the same value. *wait_seconds=0* is non-blocking (single attempt). TTL is
    the safety expiry so a crashed owner can't brick the lock.

    Returns True on success; False if the lock stayed busy for the full wait.
    Raises CacheUnavailableError if Redis is down — callers decide whether to
    treat that as fatal or degrade to unlocked operation.
    """
    if claim_lock(MT5_GLOBAL_LOCK_KEY, token, ttl):
        return True
    if wait_seconds <= 0:
        return False
    deadline = time.monotonic() + float(wait_seconds)
    sleep_for = max(float(poll_interval), 0.05)
    while time.monotonic() < deadline:
        time.sleep(sleep_for)
        if claim_lock(MT5_GLOBAL_LOCK_KEY, token, ttl):
            return True
    return False


def release_mt5_global_lock(token):
    """Release the shared MT5 runtime lock iff *token* still owns it."""
    release_lock(MT5_GLOBAL_LOCK_KEY, token)


def peek_mt5_global_lock_holder():
    """Return the token of the current MT5 global lock holder (or None)."""
    return peek_lock_holder(MT5_GLOBAL_LOCK_KEY)


def get_queue_depth(queue_name):
    return int(
        _run_redis(
            lambda: _client().llen(str(queue_name))
        )
        or 0
    )
