# CURRENT_STATE

Last Updated: 2026-04-12

Short-term operational memory.

## Current Focus

- Trades hub UX: shared app-page hero, `static/css/app_pages.css`, corrected nav `request.endpoint` names, scoped trades CSS under `.trades-page`
- MT5 batch-gated immediate setup flow
- Dashboard MT5 state and weekly AI display
- Test coverage around MT5 batch/admin paths and ready-email behavior
- Activation copy pass: import-first onboarding, clearer first weekly-review unlock rule, MT5 sync framed as optional after import, and customer-facing brand copy standardized to `MyFXJournal`

## Active Areas

- MT5 timestamp interpretation tightened: MT5 sync marks explicit UTC epoch interpretation and logs VM timezone context, while MT5 file imports no longer assume timezone for naive timestamps (`trading.py`, `routes/mt5_internal.py`, `celery_workers/mt5_sync.py`, `templates/trade_entry.html`, `tests/test_trading_import.py`)
- MT5 `copy_rates_range` bar `time` values are normalized with the same `_adjust_mt5_unix_epoch` delta as deal times before persisting to `trade_bars`, so chart candles line up with entry/exit markers (re-backfill after deploy if bars were stored raw)
- Closed MT5 trade detail includes Lightweight Charts with admin-gated chart API, 5m/15m toggle (M15 aggregated from stored M5), price-scale labels for entry/exit/SL/TP; entry, exit, SL, and TP are full-width `createPriceLine`s (entry and exit solid, SL/TP dashed), plus entry/exit arrow markers, while SL/TP stay full-width `createPriceLine` references; chart-data loads all `trade_bars` for the trade in **one** query, returns `prefetched_bars` for M5+M15 when both are offered so toggling timeframe avoids a second DB round-trip (`routes/trades.py`, `static/js/trade_chart.js`); bar fetch uses a wide M5 window before/after the trade (~36h pre / ~12h post) (`templates/trade_entry.html`, `static/css/app_pages.css`, `celery_workers/mt5_sync.py`); trade chart intro uses a ~1.25s slow-in/slow-out (ease-in-out cubic) reveal (RAF) with fixed `autoscaleInfoProvider` range and whitespace slots so price/time framing stays stable while candles fill in
- MT5 batch flow work in `routes/trade_accounts.py`, `templates/trade_accounts.html`, `templates/index.html`, and `static/js/mt5_request_form.js`
- MT5 receipt confirmation now uses the shared HTML email pattern in `routes/trade_accounts.py` and `templates/emails/mt5-request-received.html`; messaging now reflects immediate setup start vs saved-but-not-queued fallback
- MT5 batch controls now live in admin: root admin can open, expand, and close MT5 sync batches while the public site shows remaining free slots from the active batch
- MT5 sync is now batch-gated but immediate for new users: once a batch slot is open and details are submitted, terminal setup is queued right away; dashboard/trade-account states now distinguish `Submit Details` -> `Setup Queued` -> `Setting Up` -> `Sync Active`
- Admin MT5 workflow labels now use Setup Queued, Setting Up, Active, and Inactive for current-user states, while legacy request-review routes remain only for old rows
- User-facing emails now share a common `templates/emails/_base.html` shell, MT5 ready emails use the same HTML template pattern via `templates/emails/mt5-ready.html`, and worker-triggered emails render through app-level Jinja (`render_app_template`) so weekly-review and MT5-ready emails do not depend on request context
- Admin MT5 tab now focuses on submitted MT5 accounts and setup actions instead of legacy request-review/manual-add panels; root admin can queue **Recalibrate all trade times**, **Clear all chart bars** (wipes `trade_bars` for every trade; trades unchanged), or per-account **Recalibrate times** (full-history sync with `refresh_closed_trade_timestamps`) to rewrite existing MT5 trades’ `opened_at`/`closed_at` from broker deals (`auth_account.py`, `templates/admin_signup_access.html`, `routes/mt5_internal.py`, `celery_workers/mt5_sync.py`, `tests/test_mt5_sync.py`, `tests/test_admin_route_gating.py`)
- Legacy MT5 `approved before details` path is being removed so dashboard/trade-account states now stay aligned with the one-step submit-then-review flow
- Admin MT5 table now uses explicit workflow statuses: Requested, Setting Up, Active, Inactive, while preserving Cleanup Pending for orphaned records
- Dashboard MT5 messaging, rolling performance/behaviour trend panel, and weekly AI presentation work in `routes/dashboard.py`, `templates/index.html`, and `helpers/trends.py`
- State-1 onboarding dashboard: “What’s next” journey banner spans full width; MT5 + weekly AI use the same two-column hero as the main dashboard (MT5 left); stacked breakpoint puts MT5 above weekly AI (`templates/index.html`)
- Weekly AI hero now drafts a split review layout: larger left narrative panel (summary + takeaways) with two right-side micro-panels for one actionable improvement and one strength to reinforce (`templates/index.html`)
- Light emoji prefixes on major section titles only (dashboard MT5/trades, analytics KPI bands, trade accounts, strategies, trades table); account settings headings stay plain with a calmer single-border overview list (`templates/account.html`)
- Worker/admin MT5 flow work in `auth_account.py`, `helpers/core.py`, and `celery_workers/mt5_setup.py`
- MT5 setup now retries the MT5 API verification cycle once inside the same Celery task after a first `mt5.initialize()` / account-detection failure, with explicit `mt5.shutdown()` between attempts, so transient authorization hiccups do not always spill into a whole-task retry (`celery_workers/mt5_setup.py`, `tests/test_mt5_setup_order.py`)
- MT5 sync accepts broker gold symbols as XAUUSD: `GOLD` is a CFD alias for `XAUUSD` (defaults + migration `20260411_0040`), and M5 bar fetch tries alias names when `copy_rates_range` needs the server’s symbol string (`trading.py`, `celery_workers/mt5_sync.py`)
- CFD `DEFAULT_CFD_SYMBOL_SPECS` now includes broader broker-root aliases for metals, index CFDs, and crypto (plus common suffix-stripped metal forms like `XAUUSDM`); migration `20260412_0042` merges those aliases into `CFD_Symbols` for deployed DBs (`trading.py`, `tests/test_trading_math.py`)
- Users can disconnect MT5 sync from **Trade Accounts** and the dashboard MT5 card (`POST /dashboard/trade-accounts/mt5/unlink`): deletes `MT5Account`, clears related `MT5AccessRequest` rows, decrements batch `total_slots_claimed` when applicable, queues terminal cleanup like admin delete, and invalidates dashboard cache (`helpers/core.py`, `routes/trade_accounts.py`, `templates/trade_accounts.html`, `templates/index.html`, `tests/test_mt5_access_requests.py`)
- MT5 sync diagnostics improved: internal ingest now logs explicit skip-reason counters and sync worker warns when a run is all-skipped (`routes/mt5_internal.py`, `celery_workers/mt5_sync.py`)
- MT5 rolling/full-history sync now shifts the `history_deals_get` request window into broker/server time before normalizing returned deal timestamps back to UTC, to avoid recent MT5 deals arriving 1-3 hours late on non-UTC brokers (`celery_workers/mt5_sync.py`, `tests/test_mt5_sync.py`)
- Celery worker logging now uses a shared ASCII-table formatter across MT5 sync, MT5 setup/cleanup, weekly AI generation, and weekly checkin cleanup so task logs read as consistent summaries instead of mixed plain lines (`celery_workers/logging_utils.py`, worker modules, `tests/test_celery_worker_logging.py`)
- Celery's built-in plain `celery.app.trace` task lifecycle lines are now filtered so worker output relies on the richer FX Journal table summaries instead of duplicate `Task ... succeeded/retry/failed` lines (`celery_app.py`, `tests/test_celery_app.py`)
- Windows MT5 worker consoles now keep a live window title with MT5 stats from DB + Redis queue depth, so the top bar can show labels like `MT5 Sync Window | 5 Accounts Active | 4 In Queue | 1 Running`; the PowerShell launchers also set clearer startup/restart titles before Celery finishes booting (`celery_app.py`, `celery_workers/cache.py`, `scripts/windows/run_mt5_sync_worker.ps1`, `scripts/windows/run_mt5_setup_worker.ps1`, `tests/test_celery_app.py`)
- Bundle review and weekly check-in outlier cards now render trade timestamps in the user display timezone instead of raw stored UTC (`routes/trades.py`, `routes/checkin.py`, `templates/bundle_review.html`, `templates/checkin.html`, route tests)
- After file import or MT5 sync, `queue_bundle_review_if_split_candidates` in `helpers/core.py` may set `bundle_review_requested_at` when `detect_outliers` finds split candidates (`routes/trades.py`, `routes/mt5_internal.py`, `tests/test_bundle_review_queue.py`)
- Optional per trade account: `default_trade_profile_id` on `TradeAccount` (null by default) tags **new** import/MT5-sync rows via `resolve_import_default_trade_profile_ids` + `build_normalized_trade_insert_batch` (`migrations/versions/20260405_0037_trade_account_default_strategy.py`, Trade Accounts dialog + `trade_accounts_page.js`, `tests/test_trading_import.py`)
- Strategies page quick action can set the active trade account default strategy directly from a strategy card (`routes/trade_profiles.py`, `templates/trade_profiles.html`, `tests/test_trade_profiles_routes.py`)
- App layout: centered narrow hero band (`--app-hero-max-width` on `body.app-layout`, `dash-head`, `app-page-hero`, `analytics-hero`) with full-width panels below
- Dashboard and analytics UX pass: grouped KPI sections (at-a-glance net PnL + edge + execution), collapsible session/pair/weekday breakdowns with chart render on open, trade and journal deep links from analytics, `?pair=` / `?session=` auto-filter on dashboard and trades tables (`trade_filters_shared.js`, `dashboard_page.js`, `trades_table.js`), dashboard primary-story line and small-sample win-rate note, calmer onboarding (pulse animation removed), analytics cache prefix bump to `analytics_v5` (`routes/dashboard.py`, `helpers/behavior_labels.py`, templates, tests)
- AI role/taste guidance refinement in `context/ROLES.md`, `AGENTS.md`, and `CLAUDE.md`
- Public acquisition messaging polish across `auth_account.py`, `templates/landing.html`, `templates/seo_page.html`, `templates/register.html`, `templates/login.html`, and `templates/base.html`
- Second-pass landing/auth copy tightening for clearer review-first positioning and lower perceived signup friction
- Public SEO: `robots.txt` allows `/login`, `/register`, and `/dashboard`; sitemap lists `/`, `/dashboard`, `/login`, `/register`, contact, legal, and SEO landing slugs; signed-out `/dashboard` serves an indexable gate page (`dashboard_public_gate.html`); login/register use dedicated titles, meta descriptions, and canonicals; `base.html` adds `og:locale` and `WebSite` JSON-LD; contact page title refined for SERPs

## Next Priorities

- [ ] Manual browser pass on MT5 batch badge + dashboard queued/setting-up states
- [ ] Decide whether to keep or retire legacy admin approve/reject endpoints for old MT5 request rows
- [ ] Verify ready-email trigger path against live worker/email config
- [ ] Review live conversion response to the landing/auth messaging refresh
