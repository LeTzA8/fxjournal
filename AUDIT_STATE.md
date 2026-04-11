# MyFXJournal — Audit State
Last updated: 2026-04-11

## Category 1 — Polish
| Issue | Page/Area | Severity |
|-------|-----------|----------|
| Dead constant `CONTACT_EMAIL_RE` in `app.py:166` — never used, and uses broken raw-string regex (`\\s` instead of `\s`). The real validation lives in `routes/contact.py` and is correct. Safe to delete the dead definition. | `app.py` | Low |
| Email template `templates/emails/_base.html:125` hardcodes `support@myfxjournal.com` as a Jinja default fallback for support email — not driven by an env variable. If the live support address ever changes this silently sticks. | Email templates | Low |
| `DEFAULT_MODEL = "gpt-5-mini"` in `ai_service.py:48` and model default `"gpt-5-mini"` in `models.py:733` — non-standard model name (likely `gpt-4o-mini`). If this is intentional for a custom deployment it is fine; if not, stored AI response rows will carry the wrong model label. | `ai_service.py`, `models.py` | Low |
| CSS cache-bust version `v='13'` in `base.html:43` is manually managed — forgetting to bump it after a CSS change means users see stale styles. | `templates/base.html` | Low |

## Category 2 — Bugs
| Issue | Area | Severity |
|-------|------|----------|
| **`invalidate()` in `cache.py` is missing `analytics_v4` and `analytics_v5`** — the live analytics cache prefix is `ANALYTICS_CACHE_PREFIX = "analytics_v5"` (`routes/dashboard.py:70`) but `invalidate()` only deletes up to `analytics_v3`. When a trade is added, edited, or deleted, the analytics page will keep serving the stale `analytics_v5` cached payload for up to 1 hour (ANALYTICS_TTL = 3600). Confirmed: neither key appears in the invalidate list at `cache.py:96–110`. Needs `cache_key("analytics_v4", ...)` and `cache_key("analytics_v5", ...)` added to that list. | `celery_workers/cache.py` | High |

## Category 3 — Architecture
| Observation | Area | Act At |
|-------------|------|--------|
| `PENDING_REGISTRATIONS = {}` in `auth_account.py:44` is a process-local dict — lost on worker restart and invisible across multiple web processes. Fine on a single-worker deployment, breaks silently with horizontal scaling. | `auth_account.py` | 100 users / multi-process |
| `User.query.filter_by(id=user_id).first()` runs on every authenticated request inside `inject_trade_account_context()` in `app.py:229`. That is one extra DB hit per page load for all logged-in users. | `app.py` | 500 users |
| `invalidate()` in `cache.py` maintains a manually-curated list of every versioned cache prefix. A prefix bump (e.g. v3 → v5) requires remembering to update this list — the current v4/v5 bug is a direct consequence. Structural smell; consider a glob-style key pattern or an explicit registry. | `celery_workers/cache.py` | 100 users |
| `_redis_client` module-level singleton in `cache.py` is never reset after a `RedisError`. A stale connection persists until the process restarts. | `celery_workers/cache.py` | 500 users |

## Recently Resolved
| Issue | Resolved | How |
|-------|----------|-----|
| MT5 sync fetching recent deals late on non-UTC brokers | 2026-03-31 | Shift history_deals_get window into broker time before normalising back to UTC |
| Celery built-in task lifecycle logs polluting worker output | 2026-04-03 | SuppressCeleryTraceTaskLogs filter in `celery_app.py` |
| MT5 chart bars misaligned with entry/exit markers | 2026-04-11 | Apply same `_adjust_mt5_unix_epoch` delta to bar times as to deal times |
| Weekly review weekly pills showing wrong citation label | 2026-04-03 | Always render full dated citation label |
| MT5 setup failing on transient auth hiccup | 2026-03-29 | Single in-task retry with explicit `mt5.shutdown()` between attempts |
