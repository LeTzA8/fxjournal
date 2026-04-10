# CURRENT_STATE

Last Updated: 2026-04-05

Short-term operational memory.

## Current Focus

- Trades hub UX: shared app-page hero, `static/css/app_pages.css`, corrected nav `request.endpoint` names, scoped trades CSS under `.trades-page`
- MT5 request/setup flow polish
- Dashboard MT5 state and weekly AI display
- Test coverage around MT5 request/admin paths and ready-email behavior

## Active Areas

- MT5 timestamp interpretation tightened: MT5 sync marks explicit UTC epoch interpretation and logs VM timezone context, while MT5 file imports no longer assume timezone for naive timestamps (`trading.py`, `routes/mt5_internal.py`, `celery_workers/mt5_sync.py`, `templates/trade_entry.html`, `tests/test_trading_import.py`)
- MT5 request flow work in `routes/trade_accounts.py`, `templates/trade_accounts.html`, and `static/js/mt5_request_form.js`
- MT5 receipt confirmation now uses the shared HTML email pattern in `routes/trade_accounts.py` and `templates/emails/mt5-request-received.html`
- Admin MT5 tab now focuses on submitted MT5 accounts and setup actions instead of legacy request-review/manual-add panels (`auth_account.py`, `templates/admin_signup_access.html`)
- Legacy MT5 `approved before details` path is being removed so dashboard/trade-account states now stay aligned with the one-step submit-then-review flow
- Admin MT5 table now uses explicit workflow statuses: Requested, Setting Up, Active, Inactive, while preserving Cleanup Pending for orphaned records
- Dashboard MT5 messaging, rolling performance/behaviour trend panel, and weekly AI presentation work in `routes/dashboard.py`, `templates/index.html`, and `helpers/trends.py`
- Worker/admin MT5 flow work in `auth_account.py`, `helpers/core.py`, and `celery_workers/mt5_setup.py`
- MT5 sync diagnostics improved: internal ingest now logs explicit skip-reason counters and sync worker warns when a run is all-skipped (`routes/mt5_internal.py`, `celery_workers/mt5_sync.py`)
- MT5 rolling/full-history sync now shifts the `history_deals_get` request window into broker/server time before normalizing returned deal timestamps back to UTC, to avoid recent MT5 deals arriving 1-3 hours late on non-UTC brokers (`celery_workers/mt5_sync.py`, `tests/test_mt5_sync.py`)
- Celery worker logging now uses a shared ASCII-table formatter across MT5 sync, MT5 setup/cleanup, weekly AI generation, and weekly checkin cleanup so task logs read as consistent summaries instead of mixed plain lines (`celery_workers/logging_utils.py`, worker modules, `tests/test_celery_worker_logging.py`)
- Bundle review and weekly check-in outlier cards now render trade timestamps in the user display timezone instead of raw stored UTC (`routes/trades.py`, `routes/checkin.py`, `templates/bundle_review.html`, `templates/checkin.html`, route tests)
- After file import or MT5 sync, `queue_bundle_review_if_split_candidates` in `helpers/core.py` may set `bundle_review_requested_at` when `detect_outliers` finds split candidates (`routes/trades.py`, `routes/mt5_internal.py`, `tests/test_bundle_review_queue.py`)
- Optional per trade account: `default_trade_profile_id` on `TradeAccount` (null by default) tags **new** import/MT5-sync rows via `resolve_import_default_trade_profile_ids` + `build_normalized_trade_insert_batch` (`migrations/versions/20260405_0037_trade_account_default_strategy.py`, Trade Accounts dialog + `trade_accounts_page.js`, `tests/test_trading_import.py`)
- Strategies page quick action can set the active trade account default strategy directly from a strategy card (`routes/trade_profiles.py`, `templates/trade_profiles.html`, `tests/test_trade_profiles_routes.py`)
- App layout: centered narrow hero band (`--app-hero-max-width` on `body.app-layout`, `dash-head`, `app-page-hero`, `analytics-hero`) with full-width panels below
- Dashboard and analytics UX pass: grouped KPI sections (at-a-glance net PnL + edge + execution), collapsible session/pair/weekday breakdowns with chart render on open, trade and journal deep links from analytics, `?pair=` / `?session=` auto-filter on dashboard and trades tables (`trade_filters_shared.js`, `dashboard_page.js`, `trades_table.js`), dashboard primary-story line and small-sample win-rate note, calmer onboarding (pulse animation removed), analytics cache prefix bump to `analytics_v5` (`routes/dashboard.py`, `helpers/behavior_labels.py`, templates, tests)
- AI role/taste guidance refinement in `context/ROLES.md`, `AGENTS.md`, and `CLAUDE.md`
- Public acquisition messaging polish across `auth_account.py`, `templates/landing.html`, `templates/seo_page.html`, `templates/register.html`, `templates/login.html`, and `templates/base.html`
- Second-pass landing/auth copy tightening for clearer review-first positioning and lower perceived signup friction

## Next Priorities

- [ ] Finish MT5 request/setup work
- [ ] Verify dashboard MT5 messaging against actual account state
- [ ] Verify ready-email trigger path
- [ ] Review live conversion response to the landing/auth messaging refresh
