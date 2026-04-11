# MyFXJournal — Project Map

Last updated: 2026-04-12

## What This Product Is

MyFXJournal is a Flask web app for serious retail traders who want more than a trade log: multi-account journaling, imports (MT5 XLSX, Tradovate CSV), analytics, bundle-aware trade normalization, and persisted weekly AI coaching tied to the active trade account. It exists to turn broker history into repeatable review habits without juggling spreadsheets.

## Architecture At A Glance

```
[Browser] → HTTPS → [Render: Gunicorn / Flask app:app]
                          │
                          ├→ [Render: PostgreSQL]     (SQLAlchemy / Alembic)
                          │
                          └→ [Render: Redis]          (Celery broker + backend; rate limit storage when configured)

[Render: Celery worker + Beat]  ←── REDIS ──→  publishes scheduled tasks
        │                                         (weekly cleanup, enqueue MT5 sweep)
        │                                         consumes default `celery` queue (weekly AI, etc.)
        │
        └── does NOT run MetaTrader; MT5 tasks are routed to other queues

[Hyonix Windows VM: Celery workers]  ←── same REDIS ──→
        │   `mt5_sync` queue   → sync deals + bars → HTTP POST → Flask internal MT5 API (secret header)
        │   `mt5_setup` queue  → launch/verify MT5 terminals, encrypted investor passwords
        └── needs its own env file (see below); after reboot, rely on Task Scheduler + scripts in repo

Email (verification, MT5 ready, weekly review): Resend (or placeholder mode) from Flask + worker code paths.
Google OAuth: browser → Flask → Google → session.
OpenAI: weekly AI generation (API key on services that run those tasks).
```

**Read this in 30 seconds:** Users hit Render. App and DB live on Render. Redis ties Render’s web + Celery beat/worker to the Hyonix VM, which is the only place MT5 Python can run. Beat on Render schedules the 5‑minute MT5 sweep into `mt5_sync`; the VM workers drain that queue and push trades into the app over the internal API.

## Infrastructure & Services

Fill in **plan / region / cost** with whatever you actually pay; placeholders below are intentional so nothing is guessed wrong.

| Piece | Provider & plan (you fill) | Region (you fill) | ~Monthly (you fill) | What it does | If it dies | Dashboard |
|-------|----------------------------|-------------------|----------------------|--------------|------------|-----------|
| Web app (Gunicorn) | Render Web Service | | | Serves Flask, migrations on deploy, user-facing HTTP | Site down | [Render Dashboard](https://dashboard.render.com) |
| PostgreSQL | Render Postgres | | | All durable data | App errors / data unavailable | Render → your DB instance |
| Redis | Render Redis | | | Celery broker+result; optional rate-limit backend | No async tasks; possible rate-limit fallback to memory per process | Render → your Redis instance |
| Celery + Beat (“AI / housekeeping”) | Render Background Worker | | | Runs weekly AI pipeline, check-in cleanup **and publishes** beat schedule (including MT5 sync sweep messages) | No weekly AI emails/generation; **MT5 sync not triggered** unless you move beat elsewhere | Render → worker service |
| Windows VM | Hyonix | | | MT5 setup + sync Celery workers only | MT5 linking and 5‑min sync stop; web may still run | Hyonix control panel (your provider URL) |

**Login URLs:** Render — https://dashboard.render.com · Hyonix — your hosting account URL.

## Start / Restart Commands

Run everything from the **deployed repo root** unless noted. On Render, these usually match your service **Start Command** (or equivalent).

**Render — web service (migrations then Gunicorn):**

```bash
flask db upgrade && gunicorn app:app --timeout 90
```

**Render — Celery worker + Beat (weekly AI, scheduled tasks; shared Redis):**

```bash
celery -A celery_app.celery worker --beat --loglevel=info --concurrency=4
```

*(Equivalent: `python -m celery -A celery_app.celery worker --beat --loglevel=info --concurrency=4` if `celery` is not on PATH.)*

**Render — redeploy / restart:** use Render Dashboard → Manual Deploy or restart service. No SSH command line required for normal operation.

**Hyonix VM — MT5 sync worker (restart loop, solo pool, required for MT5):**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_mt5_sync_worker.ps1
```

**Hyonix VM — MT5 setup worker (restart loop, terminal bootstrap):**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_mt5_setup_worker.ps1
```

**VM after reboot:** ensure Task Scheduler (or your Hyonix panel scheduled tasks) starts the two PowerShell launchers above with working directory = repo root. Keep any **extra** maintenance scripts (e.g. git pull) alongside `manual VM scripts\` in the repo — your machine-specific schedule config should stay documented there, not only in chat history.

**Local dev (optional):** `.venv\Scripts\python.exe -m flask --app app run` — you are **not** maintaining a canonical local `.env` anymore; treat Render + VM env as source of truth.

## Environment Variables

**Rule of thumb:** secrets and URLs for **production** live in Render (web + worker + Redis + Postgres) and in a **separate env file on the Hyonix VM** (point workers at it with `FXJ_ENV_FILE` if the file is not named `.env`). Do not rely on the old checked-in or stale local `.env`.

| Name | Where it matters | Notes |
|------|------------------|--------|
| `DATABASE_URL` | Render web, Render Celery worker | Postgres connection string |
| `REDIS_URL` | Render web (limits/cache), Render worker, **VM workers** | Must be the **same** Redis for all Celery processes |
| `SECRET_KEY` | Render web | Session signing |
| `TOKEN_SALT` | Render web | Email tokens, password reset, etc. |
| `ENCRYPTION_KEY` | Render web, **VM** (MT5 tasks decrypt) | Fernet key for MT5 investor password storage |
| `MT5_SYNC_SECRET` | Render web (internal API), **VM** (sync task HTTP client) | Shared secret header for `/…` internal ingest |
| `FLASK_API_URL` | **VM** | Public base URL of the live site (sync task posts here) |
| `OPENAI_API_KEY` | Render worker (weekly AI) | Omit only if AI disabled in practice |
| `AI_MODEL`, `AI_REQUEST_TIMEOUT_SECONDS`, `AI_MAX_OUTPUT_TOKENS` | Render worker | Optional tuning |
| `RESEND_API_KEY` / `EMAIL_API_KEY` | Render web + worker paths that send mail | Plus `EMAIL_FROM`, `EMAIL_FROM_NAME`, `EMAIL_PROVIDER`, `EMAIL_SEND_ENABLED` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Render web | OAuth |
| `ADMIN_USER_EMAILS` | Render web | Root admin access |
| `PUBLIC_BASE_URL` | Render web | Canonical links and emails |
| `FEEDBACK_TO_EMAIL` | Render web | Contact routing |
| `APP_ENV` / `FLASK_ENV` | Render web | Environment classification |
| `MAX_UPLOAD_MB` | Render web | Upload cap |
| `RATELIMIT_STORAGE_URI` | Render web | Defaults from `REDIS_URL` then `memory://` |
| `FXJ_ENV_FILE` | **VM** (optional) | Explicit path to env file for Celery bootstrap |
| `MT5_BASE_PATH`, `MT5_TERMINALS_DIR` | **VM** | MT5 install + per-account terminal roots |
| `CELERY_POOL` | optional | `solo` / `threads` override |
| `FXJ_WORKER_FILE_LOG`, `FXJ_WORKER_LOG_DIR`, `FXJ_ASCII_LOG_MAX_WIDTH` | **VM** | Logging noise and file rotation |
| `FXJ_ASCII_LOG_LAYOUT` (`narrow` / `table`), `FXJ_ASCII_LOG_LINE_MAX` | **VM** (defaults set in `scripts/windows/run_mt5_*.ps1`) | `narrow` = one metric per line for windowed consoles; `table` = wide box (e.g. full-screen); line max ~100 |

*(See `README.md` for additional optional vars; names only here.)*

## Core User Flows

Each flow: what happens, what breaks, how you notice.

### Signup and Onboarding

1. User registers (possibly signup gating / email verification).
2. Flask creates `User`, session established.
3. **Wrong:** email not configured → verification or notices fail (Resend dashboard errors). **Wrong:** `SECRET_KEY`/`TOKEN_SALT` missing → unstable sessions/tokens.
4. **Looks broken if:** cannot log in, no verification email, onboarding banner never advances.

### MT5 Sync (every 5 minutes)

1. Celery Beat (on **Render** worker) fires `sync-all-mt5-accounts` on a 300s schedule.
2. Task is routed to queue `mt5_sync` (not the default `celery` queue).
3. **Hyonix** `mt5_sync` worker runs MetaTrader5 API, then POSTs normalized trades to Flask internal routes with `MT5_SYNC_SECRET`.
4. **Wrong:** VM worker down → queue grows, users see stale MT5 data. **Wrong:** `REDIS_URL` mismatch → beat fires but nobody consumes. **Wrong:** secret/url mismatch → 401/403 on internal API, logged on web and worker.
5. **Looks broken if:** Render beat logs show schedule but VM logs show no consumption; or internal API skip counters spike in logs.

### MT5 Terminal Setup (one time per user)

1. User submits MT5 request + details; admin/batch workflow queues setup on `mt5_setup`.
2. **Hyonix** setup worker launches MT5 terminal, verifies account, stores encrypted password.
3. **Wrong:** setup worker stopped → status stuck in “Setting Up”. **Wrong:** `ENCRYPTION_KEY` differs between web and VM → decrypt failures.
4. **Looks broken if:** Trade Accounts page never reaches “Sync Active”; setup worker logs show retry loops.

### AI Weekly Coaching Generation

1. Scheduled / triggered tasks on default Celery queue run on **Render** worker (with app context).
2. `OPENAI_API_KEY` used; output persisted; email may send.
3. **Wrong:** worker down → no new weekly reviews. **Wrong:** no API key → failures in worker logs, dashboard AI cards empty or stale.
4. **Looks broken if:** Redis OK but no `celery` consumer; or OpenAI errors in Render logs.

### Weekly Checkin

1. User completes check-in in UI; data stored per account.
2. Cleanup task runs on Beat schedule (Monday 03:00 app timezone logic in code).
3. **Wrong:** worker down → old check-ins not pruned (usually low user impact until scale).
4. **Looks broken if:** check-in page errors (web) vs silent cleanup lag (worker).

### Trade Import (CSV / XLSX)

1. User uploads file; Flask parses in `trading.py`, writes `Trade` rows, dedupe keys.
2. **Wrong:** `MAX_UPLOAD_MB` too low → 413 / flash error. **Wrong:** parser regression → bad PnL (tests should catch).
3. **Looks broken if:** import flash errors, zero rows imported, or obvious math wrong on dashboard.

## Key Files and What They Do

| File / area | Purpose | Risk if changed |
|-------------|---------|-----------------|
| `app.py` | Flask app, config, blueprints, per-request context | Breaks entire site |
| `celery_app.py` | Celery app, Beat schedule, queue routes, Redis resolution | Wrong queue = MT5 or AI stuck |
| `celery_workers/mt5_sync_tasks.py` | Deal + bar sync, HTTP ingest | Cross-account or data corruption if botched |
| `celery_workers/mt5_setup_tasks.py` | Terminal lifecycle | Security + broker credentials |
| `routes/mt5_internal.py` | Internal ingest API | Exposes trade pipeline if mis-gated |
| `auth_account.py` | Auth, admin, email sends, OAuth | Auth bypass, spam, leaked data |
| `ai_service.py` | Weekly AI payloads + persistence | Wrong coaching or leaked context in prompts |
| `models.py` | Schema | Migrations required; data loss if careless |
| `trading.py` | Imports, PnL, symbol normalization | Silent wrong analytics |
| `helpers/trade_analysis.py`, `helpers/scoring.py` | Bundles, behavior scoring | False coaching signals |
| `routes/dashboard.py`, `routes/trades.py`, `routes/checkin.py` | Primary UX surfaces | Broken flows |
| `migrations/` | Alembic history | Deploy mismatch against DB |
| `scripts/windows/run_mt5_*.ps1` | VM worker entrypoints | MT5 stops if paths/commands wrong |
| `manual VM scripts/` | Operator-only helpers (e.g. `gitpull.bat`) | Not run automatically — document schedules yourself |

**Sensitive:** `auth_account.py`, `routes/trade_accounts.py`, `routes/mt5_internal.py`, MT5 worker modules, `models.py`, `ai_service.py`.

## Third Party Dependencies

| Service | Purpose | If it goes down | Dashboard |
|---------|---------|-----------------|-----------|
| Render (web, worker, PG, Redis) | Hosting stack | Site or async features dead | https://dashboard.render.com |
| Hyonix VM | MT5 workers | MT5 setup/sync stop | Provider panel |
| Resend (email) | Transactional mail | No verification / MT5 / weekly emails | Resend dashboard |
| Google OAuth | Login with Google | OAuth path fails | Google Cloud Console |
| OpenAI | Weekly AI | No new AI output | OpenAI platform |
| MetaTrader 5 / brokers | Source of truth for MT5 sync | Sync returns empty / errors | Broker side |

## Deployment Workflow

1. **Push** to the connected Git branch (GitHub/GitLab — public vs private does not change Render mechanics; keep secrets only in Render/VM).
2. **Render web service:** build + `flask db upgrade && gunicorn app:app --timeout 90` (or your equivalent) on deploy.
3. **Render worker:** redeploy when `celery_app.py`, `celery_workers/`, or task dependencies change.
4. **Hyonix VM:** pull new code (`manual VM scripts\gitpull.bat` or git pull in repo root), restart MT5 PowerShell workers if code affects them.
5. **Migrations:** must run before new code relies on new columns — usually handled by web start command.

## Known Weak Points

- **Single-process assumptions:** e.g. in-memory structures for some signup flows — fine until you scale web horizontally; then confirmations could get lost across instances (see `AUDIT_STATE.md` Watching).
- **Beat lives on Render, MT5 on VM:** if Redis or the VM is wrong, beat still runs but MT5 queue stalls — symptom is “quiet” VM logs while Redis queue depth grows.
- **Curated cache invalidation:** versioned analytics cache prefixes need manual list updates when bumped (`celery_workers/cache.py`) or stale dashboard numbers persist.
- **MT5 concurrency:** MT5 Python API is process-global; VM **must** stay `--pool=solo` and effective concurrency 1 for sync (scripts enforce this).

## Planned Changes

- See `context/CURRENT_STATE.md` → **Next Priorities** (browser passes, legacy MT5 admin paths, live email verification, messaging review).

## Tech Stack (short)

Python, Flask, SQLAlchemy, Alembic, Jinja, vanilla JS, Celery, PostgreSQL (deployed), Redis, Windows + MetaTrader5 on Hyonix only.

## Essential Local Commands (reference)

```bash
.venv\Scripts\python.exe -m flask --app app db migrate -m "describe change"
.venv\Scripts\python.exe -m flask --app app db upgrade
.venv\Scripts\pytest.exe -q
```

---

*Written as a personal whiteboard: if something here disagrees with Render/Hyonix reality, update the table rows and commands — this file is the canonical “how it runs” cheat sheet.*
