# PROJECT_MAP

Stable system blueprint.

## Product Purpose

- Flask trading journal for multi-account trade review
- Supports CFD/FX and futures workflows
- Converts trade history into weekly review, not just storage
- Key edge: account-scoped review, bundle-aware trade normalization, behavior-aware weekly AI

## Tech Stack

- Python / Flask
- SQLAlchemy / Alembic
- Jinja templates
- Vanilla JS
- Celery
- SQLite local fallback
- PostgreSQL in deployed environments
- Windows-only MT5 setup/sync workers

## High-Level Structure

- `app.py`: app bootstrap, config, blueprint registration
- `models.py`: schema and relationships
- `auth_account.py`: auth, admin, landing/SEO pages
- `ai_service.py`: weekly AI payloads, prompts, storage, rendering helpers
- `trading.py`: calculations, analytics helpers, import parsing
- `routes/`: feature routes
- `helpers/`: shared state, behavior analysis, scoring, utilities
- `celery_workers/`: MT5 setup/sync and cache tasks
- `templates/`, `static/js/`: UI
- `tests/`: focused pytest coverage

## Key Modules And Features

- Dashboard and weekly AI:
  `routes/dashboard.py`, `templates/index.html`
- Weekly AI engine:
  `ai_service.py`, `prompts/dashboard_advice.txt`
- Weekly check-in and bundle review:
  `routes/checkin.py`, `helpers/trade_analysis.py`, `helpers/scoring.py`, `routes/trades.py`
- Trade CRUD and imports:
  `routes/trades.py`, `trading.py`
- Trade accounts and MT5 user flow:
  `routes/trade_accounts.py`
- Account and trader profile:
  `routes/account.py`
- Trade profiles / strategies:
  `routes/trade_profiles.py`
- MT5 workers and internal sync:
  `celery_workers/mt5_setup.py`, `celery_workers/mt5_sync.py`, `routes/mt5_internal.py`

## Lean Ownership Map

- Dashboard home / banners:
  `routes/dashboard.py`, `templates/index.html`, `tests/test_dashboard_weekly_ai.py`
- Weekly AI payloads / refs / citations:
  `ai_service.py`, `prompts/dashboard_advice.txt`, `tests/test_ai_service.py`
- Weekly check-in:
  `routes/checkin.py`, `templates/checkin.html`, `tests/test_checkin_routes.py`
- Bundle grouping:
  `helpers/trade_analysis.py`, `routes/trades.py`, `templates/bundle_review.html`
- Emotional index and scoring:
  `helpers/scoring.py`, `helpers/trade_analysis.py`
- Trade forms / table / detail:
  `routes/trades.py`, `templates/trades.html`, `templates/trade_entry.html`, `templates/trade_detail.html`
- Import pipeline / trade math:
  `routes/trades.py`, `trading.py`, `tests/test_trading_import.py`, `tests/test_trading_math.py`
- Analytics page:
  `routes/dashboard.py`, `templates/analytics.html`, `static/js/analytics_page.js`
- Trade accounts / MT5 request-access:
  `routes/trade_accounts.py`, `templates/trade_accounts.html`, `static/js/mt5_request_form.js`, `tests/test_mt5_access_requests.py`
- Admin MT5 actions:
  `auth_account.py`, `templates/admin_signup_access.html`
- Account / onboarding:
  `routes/account.py`, `templates/account.html`, `templates/onboarding.html`
- Auth / signup gating:
  `auth_account.py`, `templates/login.html`, `templates/register.html`
- Landing / SEO:
  `auth_account.py`, `templates/landing.html`, `templates/seo_page.html`
- Contact:
  `routes/contact.py`, `templates/contact.html`
- MT5 workers:
  `celery_app.py`, `celery_workers/mt5_setup.py`, `celery_workers/mt5_sync.py`, `routes/mt5_internal.py`

## Data Flow

- Trade input/import -> `Trade` rows -> trade analysis / bundle detection -> weekly check-in -> emotional index -> AI payload -> stored AI response -> dashboard/admin display
- MT5 request -> `MT5AccessRequest` / `MT5Account` -> admin approval -> worker setup/sync -> internal sync endpoint -> trades

## Sensitive Areas

- `auth_account.py`: auth, admin access, signup gating, public/admin route boundaries
- `routes/trade_accounts.py`: ownership checks, MT5 request flow, investor-password handling
- `celery_workers/mt5_setup.py`, `celery_workers/mt5_sync.py`: account isolation, worker safety, credential-sensitive flows
- `models.py`: deletes, relationships, uniqueness, lifecycle invariants
- `ai_service.py`: payload semantics, citations, persisted AI output

## Essential Commands

- Run app:
  `.venv\Scripts\python.exe -m flask --app app run`
- Run migrations:
  `.venv\Scripts\python.exe -m flask db upgrade`
- Run all tests:
  `.venv\Scripts\pytest.exe -q`
- Run one test file:
  `.venv\Scripts\pytest.exe tests\test_dashboard_weekly_ai.py -q`
- MT5 setup worker:
  `powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_mt5_setup_worker.ps1`
- MT5 sync worker:
  `powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_mt5_sync_worker.ps1`
