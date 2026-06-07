# MyFXJournal - Project Map

Last updated: 2026-06-07

This is the canonical orientation map: what the product is, how it runs, which files own which behavior, and what facts from `CURRENT_STATE.md` have become stable enough to treat as project structure. Keep `CURRENT_STATE.md` as the chronological work log.

## What This Product Is

MyFXJournal is a Flask web app for serious retail traders who want more than a trade log: multi-account journaling, multi-format imports (MT5 XLSX, Tradovate CSV, Topstep CSV) and live MT5 sync, analytics, bundle-aware interpretation, chart replay, cash-flow-aware running PnL, and persisted weekly AI coaching tied to the active trade account. Core journaling and the weekly review stay free; a 14-day premium workflow trial plus plan entitlements gate advanced replay timeframes and follow-up AI chat volume.

The product promise is review-first: help a trader understand the most important mistake or repeatable behavior in their trading without forcing spreadsheet maintenance or heavy platform setup. Current acquisition copy emphasizes "Stop guessing your trading", "Trade review without the homework", and "All the review. None of the overhead."

Core product surfaces:

- Public landing and SEO pages for review-first positioning, MT5 sync, trading-journal search intent, and "why am I not improving in trading" intent, plus a public MT5-server FAQ page (`/faq/mt5-server`).
- Auth, onboarding, email verification + email-change confirmation, Google OAuth, password reset, and signup/access controls (invite codes + allowed email domains).
- Multi-account dashboard with MT5 status, weekly AI review, trends, latest closed trade snapshot, and account-scoped filters.
- Trades/import hub with manual entry, MT5 XLSX, Tradovate CSV, Topstep CSV, MT5 sync ingest, bundle review, strategy attachment, and chart replay. Import surface adapts to active account type (CFD vs Futures).
- Analytics with KPI groups, collapsible pair/session/weekday breakdowns, running PnL, and deep links back to trades.
- Trade Accounts and Strategies pages for account settings, default strategy/playbook context, MT5 linking/unlinking, cash flows, and retries.
- Weekly check-in and outlier/bundle review flows.
- Public pricing page with plan tiers, a planned-features roadmap, and waitlist capture for gated/premium features.
- Admin-only conversational AI journal research surface (`/admin/journal`) plus an in-dashboard AI journal preview/context-selection flow.
- Root-admin operations panel for users, MT5 accounts/Worker-VM overview, CFD aliases, weekly reports, support view, waitlist, test-account QA fixtures, broadcast email, and operational dispatch.

## Architecture At A Glance

```
[Browser] -> HTTPS -> [Render: Gunicorn / Flask app:app]
                          |
                          |-> [Render: PostgreSQL]   (SQLAlchemy / Alembic)
                          |-> [Render: Redis]        (Celery broker/result, rate-limit/cache when configured)
                          |-> [Resend]               (transactional email)
                          |-> [Google OAuth]

[Render: Celery worker + Beat]
        |
        | consumes default `celery` queue
        |   - weekly AI generation/regeneration
        |   - weekly check-in cleanup
        |   - MT5 health checks and queue monitoring
        |
        | publishes scheduled work into Redis
            - `sync-all-mt5-accounts` every 30s (self-paced: enqueues the single
              stalest eligible account, per-VM when multi-VM is active, and skips
              when that VM's sync queue is already busy)
            - MT5 health checks about every 60s

[Hyonix Windows VM(s): Celery workers] <-> same Redis
        |
        | VM-scoped queues `mt5_priority.<slug>`, `mt5_sync.<slug>`
        |   - sync deals/open positions
        |   - fetch/backfill M5 chart bars
        |   - post normalized payloads to Flask internal MT5 APIs
        |
        | VM-scoped queue `mt5_setup.<slug>`
        |   - launch/verify per-account MT5 terminals
        |   - encrypted investor-password setup
        |   - terminal/AppData cleanup/reset/archive
        |   - admin broker-discovery refresh debug runs
        |
        -> MetaTrader 5 terminal + broker servers

All MT5 sync/priority/setup/cleanup tasks publish to VM-scoped queues
(`mt5_*.<slug>`); each VM worker listens on its own scoped queues, with optional
legacy-queue draining. See `context/MULTI_VM_MT5.md` (decision D-008).

OpenAI is called from weekly AI, review-chat, and AI-journal code paths. MT5 Python runs only on the Hyonix Windows VM(s).
```

Read this in 30 seconds: Users hit Render. App and DB live on Render. Redis ties Render web + Celery beat/worker to Hyonix. Render beat publishes a self-paced 30-second MT5 sweep, but Hyonix is the only place that consumes MT5 work and touches MetaTrader. With multi-VM affinity, work is routed to per-VM scoped queues so each VM only handles its own accounts. Hyonix posts normalized results back to Flask internal APIs using `MT5_SYNC_SECRET`.

## Infrastructure & Services

Fill in plan / region / cost with the real paid details. Placeholders are intentional so the map does not invent billing facts.

| Piece | Provider & plan | Region | Monthly | What it does | If it dies | Dashboard |
|-------|-----------------|--------|---------|--------------|------------|-----------|
| Web app (Gunicorn) | Render Web Service | | | Serves Flask, migrations on deploy, user-facing HTTP | Site down | Render Dashboard |
| PostgreSQL | Render Postgres | | | All durable app data | App errors / data unavailable | Render DB instance |
| Redis | Render Redis | | | Celery broker/result, cache/rate-limit backing when configured | Async tasks stop; rate limits may fall back to memory | Render Redis instance |
| Celery + Beat | Render Background Worker | | | Weekly AI, cleanup, health checks, and beat scheduling | Weekly AI stalls; MT5 sweep is not published | Render worker service |
| Windows VM | Hyonix | | | MT5 setup/sync/bar workers and MT5 terminals | MT5 setup/sync/bar backfill stops; web can still run | Hyonix provider panel |
| Email | Resend | | | Verification, MT5 receipts/ready notices, weekly review emails | Email paths fail or use placeholder mode | Resend dashboard |
| OpenAI | OpenAI Platform | | | Weekly AI and review follow-up chat | AI generation/chat fails | OpenAI dashboard |

VM repo path: `C:\Users\Administrator\fxjournal` is canonical on Hyonix. Task Scheduler XML files, PowerShell scripts, logs, and any VM-side paths must reference this path, not the dev path `C:\Coding Projects\FX Journal`.

## Start / Restart Commands

Run everything from the deployed repo root unless noted.

Render web service:

```bash
flask db upgrade && gunicorn app:app --timeout 90
```

Render Celery worker + Beat:

```bash
celery -A celery_app.celery worker --beat --loglevel=info --concurrency=4
```

Equivalent if `celery` is not on PATH:

```bash
python -m celery -A celery_app.celery worker --beat --loglevel=info --concurrency=4
```

Hyonix VM direct MT5 sync worker:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_mt5_sync_worker.ps1
```

Hyonix VM direct MT5 setup worker:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\run_mt5_setup_worker.ps1
```

Hyonix VM watchdog launchers exist for supervised restarts:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows\watch_mt5_sync_worker.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\windows\watch_mt5_setup_worker.ps1
```

VM after reboot: use Task Scheduler exports in `manual VM scripts/`. **Worker Direct** tasks are primary (enabled); **Watchdog (Legacy)** tasks are disabled by default — pick one per queue, never both. Setup and sync should run in the logged-on Administrator interactive desktop session so MetaTrader can open visible terminals.

Local dev:

```bash
.venv\Scripts\python.exe -m flask --app app run
```

Do not treat a local `.env` as production truth. Render env and the Hyonix VM env file are canonical.

## Environment Variables

Runtime env-file loading is shared by Flask and Celery: `FXJ_ENV_FILE` first, then `.env`, `FXJournal Main.env`, then `fxjournal.env`, without overriding already-set process env vars.

| Name | Where it matters | Notes |
|------|------------------|-------|
| `DATABASE_URL` | Render web, Render worker | Postgres connection string |
| `REDIS_URL` | Render web, Render worker, VM workers | Must be the same Redis across all Celery processes |
| `SECRET_KEY` | Render web | Session signing |
| `TOKEN_SALT` | Render web | Email/password-reset tokens |
| `ENCRYPTION_KEY` | Render web, VM workers | Fernet key for MT5 investor passwords |
| `MT5_SYNC_SECRET` | Render web, VM workers | Shared secret header for internal MT5 APIs |
| `FLASK_API_URL` | VM workers | Public base URL of the live site for internal POSTs |
| `PUBLIC_BASE_URL` | Render web | Canonical links and email links |
| `OPENAI_API_KEY` | Render worker, web chat paths | Weekly AI and follow-up chat |
| `AI_MODEL` | Render worker, web chat paths | Matching surrounding quotes are stripped; whitespace normalizes to hyphens, e.g. `gpt-5.4 mini` -> `gpt-5.4-mini` |
| `AI_REQUEST_TIMEOUT_SECONDS`, `AI_MAX_OUTPUT_TOKENS` | AI paths | Optional tuning |
| `RESEND_API_KEY` / `EMAIL_API_KEY` | Web + worker mail paths | Also `EMAIL_FROM`, `EMAIL_FROM_NAME`, `EMAIL_PROVIDER`, `EMAIL_SEND_ENABLED` |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Render web | Google OAuth |
| `ADMIN_USER_EMAILS` | Render web | Root admin access |
| `FEEDBACK_TO_EMAIL` | Render web | Contact routing |
| `APP_ENV` / `FLASK_ENV` | Render web | Environment classification |
| `MAX_UPLOAD_MB` | Render web | Upload cap |
| `RATELIMIT_STORAGE_URI` | Render web | Defaults from `REDIS_URL`, then `memory://` |
| `FXJ_ENV_FILE` | Render/VM optional | Explicit env-file path |
| `MT5_BASE_PATH`, `MT5_TERMINALS_DIR` | VM workers | MT5 install and per-account terminal roots |
| `CELERY_POOL` | optional | Worker pool override; MT5 workers should stay effectively solo |
| `FXJ_MT5_SYNC_ALERT_THRESHOLD_MINUTES` | Render health checks | Account sync stale threshold |
| `FXJ_MT5_STALE_THRESHOLD_MINUTES` | Render health checks | Sync queue stale threshold |
| `FXJ_MT5_SETUP_STALE_THRESHOLD_MINUTES` | Render health checks | Setup queue stale threshold |
| `FXJ_WORKER_DIAGNOSTIC` | Workers | Set `0` to disable copy-paste diagnostic blocks |
| `FXJ_WORKER_FILE_LOG`, `FXJ_WORKER_LOG_DIR` | VM workers | Rotating worker file logs |
| `FXJ_ASCII_LOG_MAX_WIDTH` | Workers | `0`/`full` disables value truncation |
| `FXJ_ASCII_LOG_LAYOUT`, `FXJ_ASCII_LOG_LINE_MAX` | VM workers | `narrow` for windowed consoles, `table` for wide output |
| `COMPUTERNAME` / `VM_ID` | VM workers | VM identity for scoped queues; `get_vm_id()` prefers `COMPUTERNAME` |
| `FXJ_MT5_SETUP_VM_IDS`, `FXJ_MT5_SETUP_FAILOVER_MAX_VMS` | Render web, beat | Public/default setup target list + failover cap across VMs |
| `FXJ_MT5_LISTEN_LEGACY_QUEUES` | VM workers | `1` to also drain pre-multi-VM legacy queues during migration |
| `FXJ_MT5_VM_PROFILES` | Render web | Optional region/provider/label metadata for the admin Worker VMs panel |
| `FXJ_MT5_HISTORY_STALE_THRESHOLD_MINUTES`, `FXJ_MT5_SOFT_RECONNECT_ON_STALE` | VM sync worker | Stale broker-history detection + soft-reconnect toggle |
| `WEEKLY_REVIEW_CHAT_MAX_OUTPUT_TOKENS` | Web chat path | Follow-up chat output cap (default 520; weekly review itself uses ~1500) |
| `ADMIN_EMAIL_FROM`, `ADMIN_EMAIL_SEND_DELAY_MS` | Render web | Admin broadcast compose from-header + per-recipient send delay |

App-setting toggles (DB `app_settings`, not env): automatic public chart-bar sync (default off) and `mt5_broker_discovery_refresh_enabled` (default off; blocks real broker-refresh runs, dry-run still allowed).

## Current Capability Inventory

### Trade Data And Interpretation

- Trade data can arrive from manual entry, MT5 XLSX import, Tradovate CSV import, Topstep CSV import, and automatic MT5 sync. `detect_trade_import_profile()` field-detects Topstep before Tradovate before MT5; Topstep PnL is gross with `Fees` + optional `Commissions` summed into the per-trade cost.
- The import/onboarding surface is account-type aware: Futures active accounts hide MT5 UI and point to Tradovate/Topstep CSV; CFD active accounts keep MT5 XLSX + MT5 sync and hide futures references.
- Imports and MT5 sync normalize symbols, timezones, PnL, dedupe keys, account scope, and default strategy attachment.
- Net PnL (after logged fees, commission, and swap) flows through one shared resolver used by dashboard Account PnL, Realized This Week, Trading Costs, analytics, running PnL, trade detail, and weekly AI inputs.
- Naive MT5 file timestamps are not guessed. Explicit timezone/offset input is converted to UTC-naive DB storage.
- `TradeAccount.default_trade_profile_id` can attach a default strategy/playbook to new import and MT5 rows.
- `trade_interpretation` stores bundle links and behavior flags (`is_revenge`, `is_reactive`, `is_corrective`) outside the `trades` row; history is append-only.
- Split-entry detection can queue bundle review after import or MT5 sync. Bundle candidate grouping uses split buckets, shared TP/SL, and a weaker `same_exit` similar-exit-price signal (`BUNDLE_EXIT_TOLERANCE_PCT`).
- Running/open state should use `helpers/trade_state.trade_is_closed`; live PnL alone must not imply closure.
- CFD canonicalization handles broad broker aliases, suffix stripping, metals, indices, crypto, commodities, and admin-editable aliases.
- Futures instruments resolve through the `FuturesSymbol` / `futures_symbols` contract-spec catalog (tick size/value, point math), parallel to CFD canonicalization, and back Tradovate/Topstep imports.

### MT5 Setup, Sync, Bars, And Lifecycle

- MT5 setup is batch-gated but immediate: when a public batch slot is open and details are submitted, setup is queued right away.
- Dashboard and Trade Accounts states distinguish Submit Details, Setup Queued, Setting Up, Sync Active, Sync Inactive, Archived, Cleanup Pending, and Connection Failed.
- Connection Failed accounts keep the row and allow in-place credential retry; admin and monitor views surface failures.
- Users can unlink MT5 from dashboard or Trade Accounts; unlink deletes the MT5 row, clears request rows, decrements batch slot usage when applicable, queues cleanup, and invalidates dashboard cache.
- Admins can archive/reactivate MT5 accounts without deleting read-only credentials; archive clears VM runtime metadata and queues cleanup.
- Root admins can reset terminal state without deleting credentials; setup stays blocked while cleanup is pending.
- `last_synced_at` is stamped on any successful internal sync, including zero-row syncs. `last_full_history_sync_at` is stamped only for full-history scope.
- Beat runs every 30s and self-paces: it enqueues the single stalest eligible account (per VM when multi-VM is active), skips when that VM's sync queue is busy or per-account locks are held, and tasks expire quickly so stale backlog does not crowd out fresh syncs.
- Multi-VM queue affinity: all MT5 sync/priority/setup/cleanup/pause tasks publish to VM-scoped queues (`mt5_*.<slug>`) via `helpers/mt5_dispatch.py`; missing `vm_id` skips account-bound dispatch. Public/default setup can fail over across `FXJ_MT5_SETUP_VM_IDS`; admin-targeted setup pins a single VM. Runbook: `context/MULTI_VM_MT5.md`, decision D-008.
- Manual/admin Trigger Sync, recalibration, and Backfill Bars publish to the scoped priority lane; the VM sync worker consumes its scoped `mt5_priority`/`mt5_sync` queues so operator work is picked first.
- Non-grandfathered Free MT5 sync pauses after the 14-day premium trial (`sync_paused_at` / `sync_pause_reason`); paused accounts are excluded from beat scheduling and shown as Sync Paused. Trial pause is entirely separate from archive (`archived_at` / `archive_reason`) and does not delete credentials.
- `vm_id` is stamped on setup success and every successful sync POST (using Windows `COMPUTERNAME`) so admin Worker-VM grouping and telemetry line up. A failure-driven `mt5_server_seed_shortlist` flags brokers that may need golden-master `servers.dat` seeding on the VM.
- Automatic chart-bar sync is root-admin gated in `app_settings`, defaults off, and queues capped batch bar work for closed MT5 trades with missing complete M5 coverage.
- Complete chart-bar coverage means from around entry through `min(closed_at + 144 M5 bars, now)` with a 15-minute tolerance. M5 is stored; M15 is derived in chart APIs.
- Bar fetch uses broker-visible symbol candidates and `symbol_select()` to handle server aliases like `GOLD`, suffixes, and broker-specific CFD names.
- Broker/server time offset is probed using 24/7 crypto aliases when possible, persisted by normalized server name, and reused during off-hours/downtime.

### Dashboard, Analytics, And Chart UX

- Dashboard is active-account scoped and includes MT5 status, weekly AI, rolling performance/behavior trends, and a latest closed trade snapshot.
- Hero metric grid leads with Account PnL (all closed trades, net of logged fees/commission/swap) and includes Realized This Week (computed fresh from closed records + the current Monday boundary in the display timezone, not the analytics cache) and Trading Costs (gross-to-net cost drag, with partial-coverage messaging). Metric notes surface closed-trade and weekly trade counts plus the week start date.
- Rolling trends are week-on-week: this dashboard week's win rate, expectancy, and behavior pressure compared against the previous week, with a small-sample note below `SMALL_SAMPLE_MIN_TRADES`.
- Cache prefixes: dashboard analytics use `dashboard_v4`; trade analytics use `analytics_v5`. Both must be present in `invalidate()` (`celery_workers/cache.py`) whenever the prefix is bumped — a missed prefix serves stale data until TTL expires.
- Latest closed MT5 trades with bars render a compact Lightweight Charts candlestick chart, 5m/15m toggle, entry/exit/SL/TP markers, scale controls, price-axis drag, and wheel zoom.
- Manual trades or MT5 trades without bars show a status fallback instead of a broken chart.
- Trade detail chart uses stored M5 bars, derives M15, draws entry/exit/SL/TP price lines, and keeps framing stable during intro reveal.
- Analytics groups KPI sections, supports collapsible session/pair/weekday breakdowns, renders charts on open, and deep-links filters back to dashboard/trades with `?pair=` and `?session=`.
- Running PnL is cash-flow aware: deposits, withdrawals, adjustments, and realized trading PnL remain separate event streams with optional date filters.
- The recent-trades table supports inline add/edit of a per-trade note via `GET/POST /api/trades/<pubkey>/note` (modal editor, 8000-char cap, hidden in support view).
- Pure zero-data users (no trades, no MT5 submission on the active CFD row) see a "Start here" choose-your-path card (upload report / add manual trade / request MT5 sync / preview sample review) instead of the MT5-first journey banner, with a soft waitlist link after 1+ idle days. Admins get pre-activation user counts and `?activation=` filters (`helpers/admin_activation.py`).
- Dashboard fine print is mostly in accessible help bubbles so the primary state and action stay visible.

### Weekly AI And Follow-Up Chat

- Weekly dashboard AI is persisted per active trade account. Prompt history, payloads, final output, pass outputs, prompt versions, model, and metadata are stored for auditability.
- First weekly review on an account may run immediately after import/sync/dashboard visit. Returning accounts auto-queue/generate only after Friday 5:30 PM New York for that review week; admin force-regenerate bypasses the cutoff.
- Generation is two-pass: pass 1 chooses a coaching diagnosis from evidence, then pass 2 rewrites for plain-English clarity while preserving structure and citations. Pass-2 failure falls back to pass 1.
- The review is evidence-first: one main insight, concrete counts/examples, implication-first takeaways, one improvement, one strength, and one experiment.
- Payloads include tone context, current-week breakdowns, four-week patterns, recent experiments, strategy/playbook context, and bar-derived market context when available.
- One universal weekly JSON payload (`helpers/universal_weekly_payload.py`) is built once and shared: it is sent to pass-1, stored in `AIGeneratedResponse.payload_json`, and reused by the admin weekly audit and citation rewrite.
- A computed coaching layer feeds the prompt deterministic framing before the model writes: `execution_outcome` (an execution/outcome archetype + coaching stance, e.g. protect-confidence vs direct-correction, with `issue_evidence_level`) and ranked `coaching_hypotheses` (e.g. `outcome_disguised_habit`, `post_loss_decision_shift`) with evidence refs, confidence, and bounded false/better-lesson hints. The prompt may use at most one as framing and must not invent traps, motives, or lessons beyond the computed facts. (`helpers/weekly_signals.py`, `helpers/weekly_coaching_hypotheses.py`)
- Bar-derived AI evidence includes session overlap, large candles before entry, entry candle behavior, consecutive pre-entry bars, tick-volume ratio, in-trade/post-exit bar counts, MFE/MAE, post-exit continuation/reversal, TP/SL reach after exit, and cautious stop-management clues.
- Risk interpretation avoids lot-size inference and uses stronger available evidence when risk fields are unavailable or ambiguous. Serialized trades are enriched with a larger/smaller/same risk-change classification vs a baseline (`helpers/risk_comparison.py`) so the model can describe sizing shifts without inferring lots.
- After the NY Friday 5:30 PM cutoff for a review week, the dashboard no longer falls back to the previous week's review: the panel shows an empty state, then the generating spinner when Redis reports `queued`/`running`. `/api/ai-status` returns `{ ready, status }` and the dashboard polls to transition empty -> loading -> finished without a manual refresh.
- Follow-up chat is scoped to a single persisted weekly review and its trades. It refuses support-view writes, enforces ownership/account scope, validates length, and applies DB-backed non-admin caps. Trial/Free users get 5 user follow-up messages; replies are short (`WEEKLY_REVIEW_CHAT_MAX_OUTPUT_TOKENS`) and each reply offers 2-3 tap-to-send suggested prompts.
- Follow-up chat can cite the same weekly-review trade/bundle refs internally, returns clean text plus citation segments, and reuses dashboard trade/bundle highlight behavior.
- Visible "Ask about this review" prompt bubbles are generated from the displayed insight/evidence, with legacy static prompts as fallback.
- Dashboard AI journal context selection is user-directed during the admin preview: after a natural prompt, the user chooses full active-account context, the current review week, parsed trading-day context, or a recent specific trade before AI receives trade data. The current "full" context is still capped to recent closed trades; future paid entitlement should gate true full/all-history context and context outside the current week.
- Planned macro/regime context should enter AI as bounded evidence, not causal certainty. Candidate signals include high-impact economic calendar proximity, FOMC/central-bank/inflation/employment events, broad risk-on/risk-off tone, USD/yields pressure for FX/gold, and crypto/index correlation or beta. AI language should prefer "may have contributed," "macro volatility was elevated," or "the setup likely needed more confirmation/risk adjustment" rather than claiming macro caused wins or losses.

### Entitlements, Trial, And Monetization

- `helpers/entitlements.py` centralizes plan gating: plan state, MT5 trial state (grandfathered/not_started/active/expired/paused), MT5 sync access, and replay-timeframe access. Admins bypass; existing beta users are grandfathered (Trader-level) silently.
- A shared 14-day premium workflow trial uses `User.premium_trial_started_at` (durable, backfilled). Imports/account creation do not start the clock; first successful MT5 sync and first successful weekly-review follow-up reply stamp it. `MT5_TRIAL_DAYS=14`.
- Replay timeframe gating: M5/M15 are never gated; M1 returns `upgrade_required` (Free) or `timeframe_not_available` (entitled, since M1 is not yet implemented). Free-tier auto bar-fetch is capped by trade age (`FREE_REPLAY_MAX_TRADE_AGE_DAYS=90`).
- Schema-compat safety (`helpers/schema_compat.py`) lets entitlement/pricing pages fail open when Phase 2 columns are missing, so partially migrated environments do not 500.
- Waitlist capture: `UpgradeWaitlistEntry` stores `source`/`feature_interest`/`cta_context`; `/pricing/waitlist` validates + rate-limits + dedupes. Contextual waitlist CTAs (shared modal + lock cards) appear on pricing, dashboard chat lock, MT5 capacity/expired/paused, and replay 1m lock. Billing/checkout is not yet enforced; reactivation routes point at pricing/waitlist.

### AI Journal (Admin Research Surface)

- Admin-only conversational journal at `/admin/journal` (invisible gating, 404 for non-admin) with trade/day/week/freeform scopes, a single chat thread per session, title/tags/notes, and per-message research feedback. Persisted via `JournalSession` / `JournalMessage`.
- The dashboard embeds an AI journal preview beside the weekly review (admin-only). Start flow is chat-first: the user types a reflection, `/dashboard/journal/context-candidates` deterministically proposes a trade/day/week/freeform context, and the scoped session is created only after confirmation so the user chooses context before AI sees trade data.
- Journal payloads (`helpers/journal_context.py`) carry the real strategy description; trade scope includes only the focal trade, day scope is capped, freeform uses recent closed trades with behavior-flag counts. Prompt + helpers live in `prompts/journal_chat.txt` / `ai_service.py`.
- Product intent: gate true full/all-history and out-of-week context behind paid entitlement once billing is live; planned macro/regime context should enter as bounded evidence ("may have contributed"), never causal certainty.

### Admin, Support, Legal, And Growth

- Root admins have a read-only support view that swaps request context to a target user for approved GET routes, blocks user-facing writes centrally, and shows an exit banner/account switcher.
- Weekly review chat POSTs are blocked in support view and return JSON errors for API/chat paths.
- Root admins can delete non-root users with related data cleanup.
- Admin panel uses sidebar navigation, contextual stat tiles, section-scoped quick filters, server-side search/sort for Users and MT5, admin weekly report links, a root-admin CFD aliases tab, and a Users-section waitlist tile counting distinct waitlist email addresses.
- Access control surfaces: invite/signup codes (`SignupCode`, create + activate/deactivate) and allowed signup email domains (`AllowedSignupEmailDomain`) gate registration. A noindex background/theme preview tool exists at `/dashboard/admin/background-preview` (appears legacy/unused).
- Per-user maintenance ops from the Users table: regenerate weekly AI advice, backfill bundles, unbundle trades, plus approve/reject/suspend/delete and admin-toggle.
- Admin MT5 panel owns submitted MT5 accounts, setup actions, reset/Delete VM files/archive/delete, VM-targeted setup/cleanup selector, recalibration, clear-all-bars, manual sync, backfill bars, and queue diagnostics. A Worker VMs panel groups accounts by `vm_id` with per-VM heartbeat/queue depth (snapshot cached ~20s). Batch open/expand/close UI was removed (backend routes remain for legacy rows). A root-admin MT5 Debug Tools panel can dispatch a broker-discovery refresh (feature-flagged, dry-run by default).
- Growth/admin tooling: waitlist page + CSV export; broadcast Send Email compose (searchable recipients, `{{name}}` token, optional sanitized HTML/images, saved reusable signatures); Users CSV export; per-user trial visibility + root-admin trial extension; QA waitlist-CTA fixture seeding via Flask CLI + a QA Test Accounts admin page (`qa_test_accounts` sidecar table); user `last_active_at` tracking (stamped at most every 5 min, support/internal traffic skipped).
- Public landing, auth, and SEO pages emphasize review-first positioning, the 14-day premium workflow trial, MT5 setup capacity when available, import-first fallback when not, and lower signup friction.
- Legal docs cover weekly-review follow-up chat, sync/import fallibility, optional Google OAuth, processors, live/real-funded MT5 account risk, read-only support access, and service availability expectations.

## Core User Flows

### Signup, Onboarding, And Access

1. User registers (optionally gated by invite code / allowed email domain), verifies email if required, and may use Google OAuth.
2. Flask creates `User`, session state, onboarding state, and account context.
3. Admin/root access is controlled by verified/approved admin checks plus root-admin emails.
4. Account page supports email change with a confirmation-token flow (`/account/email-change/...`) and password reset.
5. Wrong: email config missing -> verification/notices fail. Wrong: `SECRET_KEY`/`TOKEN_SALT` missing -> unstable sessions/tokens.
6. Looks broken if login fails, verification mail never arrives, or onboarding never advances.

### Trade Import And Review

1. User imports MT5 XLSX / Tradovate CSV / Topstep CSV or enters trades manually.
2. `trading.py` parses and normalizes rows, applies account strategy defaults, dedupes, computes PnL, and writes `Trade` rows.
3. Post-ingest hooks may queue bundle review and weekly AI review.
4. Wrong: parser regression -> bad PnL/analytics. Wrong: `MAX_UPLOAD_MB` too low -> upload rejected.
5. Looks broken if import flashes errors, imports zero rows, duplicates appear, or dashboard math is obviously wrong.

### MT5 Batch-Gated Setup

1. Admin opens/expands an MT5 sync batch.
2. Public UI describes MT5 setup capacity; user submits investor/read-only details when capacity is available.
3. Setup is queued on `mt5_setup`; Hyonix launches/verifies a terminal and stores encrypted credentials/runtime metadata.
4. Failed setup writes user-friendly connection failure state and allows in-place retry.
5. Wrong: setup worker stopped -> stuck at Setup Queued/Setting Up. Wrong: `ENCRYPTION_KEY` mismatch -> decrypt failures.
6. Looks broken if Trade Accounts never reaches Sync Active or monitor/admin shows failed setup/cleanup pending.

### MT5 Sync, Open Trades, And Bars

1. Render beat publishes `sync-all-mt5-accounts` every 30s.
2. Work is routed to `mt5_sync`; manual/admin work goes to `mt5_priority`.
3. Hyonix sync worker queries MT5 deals/open positions, overlays running PnL for open trades, normalizes broker/server timestamps to UTC, and POSTs to internal Flask APIs with `MT5_SYNC_SECRET`.
4. Internal ingest writes/updates trades, stamps sync times, logs skip/validation counters, and may queue automatic chart-bar batch fetches.
5. Bar fetch stores M5 bars and posts batches to `/api/internal/mt5/trade-bars/batch`; chart APIs derive M15.
6. Wrong: Redis mismatch -> beat fires but VM never consumes. Wrong: VM worker down -> queue grows and MT5 stale. Wrong: broker-history stale -> DB open trades may not close until reconnect/history recovers.
7. Looks broken if VM logs are quiet while Render beat publishes, monitor shows queue/backlog/stale warnings, or charts lack expected bars.

### Weekly AI Review

1. Ingest/dashboard visit/admin action calls weekly AI queue/generation helpers.
2. The active trade account's latest eligible week is selected; first account review may run immediately, returning accounts wait until Friday 5:30 PM New York.
3. AI payload builder gathers closed trades, strategies, interpretation flags, market/bar context, trends, recent experiments, and tone context.
4. Pass 1 produces the structured diagnosis; pass 2 rewrites for natural plain English; metadata and outputs persist.
5. Dashboard renders the review, experiment, citations, and generated follow-up prompt bubbles.
6. Wrong: Render worker down -> no new reviews. Wrong: OpenAI key/model config bad -> generation/chat errors.
7. Looks broken if dashboard AI stays stale, worker logs OpenAI errors, or citations/side cards do not line up with the review.

### Weekly Review Follow-Up Chat

1. User asks a question inside a persisted weekly review panel.
2. Route verifies login, active account ownership, review ownership, support-view write block, message length, and caps.
3. AI sees only the stored review/pass-1/payload/meta context plus limited recent chat history.
4. Assistant returns concise review-scoped coaching, optional trade citations, and fresh dynamic next questions.
5. Wrong: rate-limit/cap issues -> JSON error. Wrong: prompt/ref handling regression -> off-topic answers or stray ref tokens.
6. Looks broken if chat posts fail, citation pills do not highlight trades, or responses ignore the weekly review.

### Running PnL And Cash Flows

1. User manages deposits/withdrawals/adjustments under Trade Accounts.
2. `helpers/running_pnl.py` merges closed trades and cash-flow events chronologically without mixing trading performance and cash movement.
3. Analytics fetches `/api/running-pnl` with optional date filters and renders line toggles.
4. Wrong: open trades included as closed -> running PnL lies. Wrong: cash flows merged into trading PnL -> wrong performance story.

### Weekly Check-In And Bundle Review

1. User completes check-in and reviews outlier/bundle suggestions in active account context.
2. Bundle review uses interpretation rows and timezone-aware display.
3. Beat cleanup prunes old check-ins on schedule.
4. Wrong: worker down -> cleanup lag. Wrong: bundle normalization skipped -> behavior scoring overcounts split entries.

### Admin Operations

1. Root admin uses `/dashboard/admin/access` and related admin pages.
2. Admin can manage users, MT5 batches/accounts, CFD aliases, weekly reports, support view, queue dispatches, and destructive MT5 chart-bar clears.
3. Producer-side Celery dispatch logs task publish attempts/success/failure with sanitized broker URL and context.
4. Wrong: admin accepts POST but task publish fails -> Render logs show dispatch failure; VM remains quiet.
5. Looks broken if admin state changes without task IDs, MT5 table state mismatches dashboard, or support view allows a write.

## Key Files And What They Do

| File / area | Purpose | Risk if changed |
|-------------|---------|-----------------|
| `app.py` | Flask creation, config, env loading, request context, blueprints, support-view blocking | Whole site, auth/session, support boundaries |
| `models.py` | SQLAlchemy schema for users, trades, MT5, AI, chat, cash flows, interpretation, settings | Migrations/data loss required if changed carelessly |
| `auth_account.py` | Auth/admin routes, signup gates, admin MT5 actions, user deletion, CFD aliases, emails | Auth bypass, admin leakage, destructive ops |
| `helpers/core.py` | Shared account/trade helpers, MT5 unlink/archive/reset/delete cleanup, bundle queue hook | Cross-flow state drift |
| `helpers/mt5_dispatch.py` | VM slug normalization, scoped queue names, centralized MT5 dispatch + failover/wrong-VM guard | Wrong-VM routing, dropped MT5 tasks |
| `helpers/entitlements.py` | Plan/trial state, MT5 sync + replay gating, trial extension | Wrong gating, trial bypass/lockout |
| `helpers/schema_compat.py` | Deferred-column introspection so entitlement/pricing pages fail open | 500s on partially migrated DBs |
| `helpers/runtime_env.py` | Shared env-file loading for Flask/Celery | Deployment/config drift |
| `helpers/celery_dispatch.py` | Web/admin Celery publish logging wrapper | Harder async diagnosis |
| `helpers/admin_mt5_ops.py` | Admin MT5 ops, Worker-VM overview, VM-targeted cleanup | Admin MT5 control/visibility |
| `helpers/admin_activation.py` | Pre-activation zero-data detection + admin activation stats/filters | Misleading onboarding funnel/state |
| `helpers/settings_workbench.py` | Trade Accounts / Strategies workbench view-models | Account/strategy page render drift |
| `celery_app.py` | Celery app, queues, routes, beat schedule, logging filters, worker titles, health tasks | Wrong queue = MT5/AI stuck |
| `celery_workers/mt5_sync_tasks.py` | MT5 deal/open-position sync, chart-bar fetch, broker offsets, HTTP ingest | Data corruption, stale trades, broken bars |
| `celery_workers/mt5_setup_tasks.py` | Terminal setup/verification/cleanup, connection failure classification | Broker credential handling, stuck setup |
| `celery_workers/mt5_market_watch.py` | Broker-time probe symbols and alias seeding | Timestamp drift/off-hours failures |
| `celery_workers/mt5_monitoring.py`, `celery_workers/worker_monitor.py` | Account/queue staleness checks and alert reset logic | Missed worker outages or noisy alerts |
| `celery_workers/logging_utils.py`, `celery_workers/worker_diagnostics.py` | Worker ASCII summaries, diagnostics, HTTP error summaries | Loss of operator visibility |
| `celery_workers/cache.py` | Redis locks/cache helpers, worker title stats | Queue/lock behavior; note some old MT5 global-lock helpers may be historical |
| `routes/mt5_internal.py` | Secret-gated internal MT5 ingest and bar APIs | Trust boundary, data corruption |
| `routes/trade_accounts.py` | Trade account settings, MT5 submit/retry/unlink, cash flows | Account state and MT5 user flow |
| `routes/dashboard.py` | Active-account dashboard, weekly AI panel/chat route, latest trade snapshot, MT5 state | Primary UX, AI ownership, cache |
| `routes/trades.py` | Trade list/detail, imports, bundle review, chart-data API | Imports, chart replay, ownership |
| `routes/checkin.py` | Weekly check-in and outlier presentation | Account-scoped review data |
| `routes/trade_profiles.py` | Strategies/playbooks and default strategy quick action | Strategy context for imports/AI |
| `trading.py` | Import parsing, PnL math, CFD normalization, symbol alias cache | Silent wrong analytics |
| `helpers/trade_state.py` | Closed/open trade truth | Running PnL/open-trade correctness |
| `helpers/running_pnl.py` | Closed-trade + cash-flow event stream | Misstated performance/cash movement |
| `helpers/trade_analysis.py`, `helpers/scoring.py`, `helpers/trade_interpretation.py` | Bundle detection, behavior scoring, interpretation rows/history | False coaching signals |
| `helpers/ai_market_context.py`, `helpers/trade_bars.py` | M5 bar-derived AI context and chart-bar completeness checks | Bad AI evidence or incomplete charts |
| `helpers/weekly_ai_queue.py` | Ingest-triggered weekly AI queue eligibility | Premature/missing reviews |
| `helpers/universal_weekly_payload.py` | Single shared weekly JSON payload (pass-1, storage, audit, citations) | Prompt-contract/citation drift |
| `helpers/weekly_signals.py`, `helpers/weekly_coaching_hypotheses.py` | Deterministic execution_outcome archetype + ranked coaching hypotheses | Mis-framed/over-confident AI coaching |
| `ai_service.py` | Weekly AI payloads, prompt formatting, Responses calls, persistence, follow-up + journal chat builders | Leaked/wrong context, stale prompt contract |
| `helpers/journal_context.py`, `routes/admin_journal.py` | AI journal scopes/payloads and admin journal routes | Journal scope leakage |
| `prompts/dashboard_advice.txt` | Weekly AI pass-1 diagnosis prompt | Review quality and evidence rules |
| `prompts/dashboard_advice_rewrite.txt` | Weekly AI pass-2 clarity prompt | Citation/structure drift |
| `prompts/weekly_review_followup.txt`, `prompts/journal_chat.txt` | Review-scoped follow-up + AI journal chat prompts | Off-topic or unsafe chat behavior |
| `templates/pricing.html` | Plan tiers, roadmap, waitlist CTAs | Conversion/claim drift |
| `templates/index.html` | Dashboard, weekly AI, MT5 card, latest trade snapshot, chat UI | Primary user-facing dashboard |
| `static/js/weekly_review_chat.js` | Follow-up chat fetch/UI/citations | Chat interaction regressions |
| `static/js/dashboard_page.js` | Dashboard interactions, filters, citation highlighting | Dashboard behavior drift |
| `static/js/dashboard_latest_trade_chart.js` | Latest closed trade chart widget | Broken chart snapshot |
| `static/js/trade_chart.js` | Trade-detail Lightweight Charts replay | Broken chart replay |
| `static/js/running_pnl.js` | Analytics running PnL chart | Incorrect analytics UI |
| `static/js/mt5_request_form.js` | MT5 submit/retry/disconnect UI states | Bad onboarding/retry UX |
| `templates/trade_accounts.html`, `static/js/trade_accounts_page.js` | Trade account settings, defaults, MT5 state | Account config regressions |
| `templates/landing.html`, `templates/seo_page.html`, auth templates | Public positioning, SEO pages, signup/login copy | Conversion/claim drift |
| `templates/admin_signup_access.html`, admin partials, `static/js/admin_panel_filter.js`, `static/css/admin_panel.css` | Admin shell, filters, stat tiles, MT5/user/CFD controls | Admin usability and safety |
| `templates/emails/_base.html` + per-event templates (verify, email-change confirm, password-reset, welcome, MT5 ready/received/failed, weekly-review, waitlist-confirmation, free-trial-expired) | Transactional email shell and user-facing notices | Broken email rendering |
| `helpers/legal.py`, legal templates | Legal last-updated and policy text | Compliance/trust drift |
| `manual VM scripts/` | Operator helpers, MT5 broker seeding, monitor, Task Scheduler XML exports | VM operations break if path/encoding wrong |
| `scripts/windows/run_mt5_*.ps1`, `scripts/windows/watch_mt5_*_worker.ps1`, `scripts/windows/mt5_monitor.py` | VM worker launchers/watchdogs/monitor | MT5 workers do not survive reboot or are invisible |
| `migrations/` | Alembic history | Deploy mismatch against DB |
| `tests/` | Regression coverage for auth, MT5, AI, imports, PnL, admin, charts | Missing safety net |

Sensitive areas: auth/admin/support view, `models.py`, migrations, MT5 credentials/setup/sync/internal APIs, AI prompts/payloads, trading math/imports, account ownership checks, cash-flow/running PnL.

## Third Party Dependencies

| Service | Purpose | If it goes down | Dashboard |
|---------|---------|-----------------|-----------|
| Render | Web, worker, Postgres, Redis hosting | Site or async features down | https://dashboard.render.com |
| Hyonix VM | MT5 workers and terminals | MT5 setup/sync/bar fetch stop | Provider panel |
| MetaTrader 5 / brokers | Broker history, open positions, chart bars | Empty/stale sync or bar fetch failures | Broker/MT5 terminal |
| Resend | Transactional mail | No verification/MT5/weekly emails | Resend dashboard |
| Google OAuth | Login with Google | OAuth route fails | Google Cloud Console |
| OpenAI | Weekly AI and follow-up chat | No new AI/chat output | OpenAI platform |

## Deployment Workflow

1. Push to the connected Git branch. Public/private repo status does not change Render mechanics; keep secrets in Render/VM only.
2. Render web service builds and runs `flask db upgrade && gunicorn app:app --timeout 90`.
3. Render worker redeploys when `celery_app.py`, worker modules, prompts, AI dependencies, or scheduled task behavior changes.
4. Hyonix VM pulls new code (`manual VM scripts\gitpull.bat` or `git pull` in `C:\Users\Administrator\fxjournal`) and restarts MT5 workers when MT5 worker/scripts dependencies change.
5. Alembic migrations must run before new code relies on new columns. Ship schema + app code together for model changes.
   For May 2026 pricing/entitlement rollout, apply in order: `20260510_0055` -> `20260510_0056` -> `20260510_0057` before enabling the new waitlist + entitlement code paths.
   Later migrations (through ~`20260602_0063`) add journal tables, shared trial start, MT5 connection/offset/server-seed/VM columns, `last_active_at`, QA fixtures, and drop the futures-proxy scaffold. Always run `alembic upgrade head` in the same release as the matching app code.
6. If Task Scheduler XML changes, re-import the XML on the VM. Editing repo exports does not update live scheduler entries.

## Operational Guardrails

- MT5 workers must run on Windows/Hyonix. Do not try to run MetaTrader work on Render.
- MT5 Python API has process-global behavior. Keep VM MT5 workers effectively `solo`/concurrency 1.
- Render beat owns the 30s MT5 sweep. If Render worker/beat is down, Hyonix can be healthy but no routine sync tasks are published.
- Hyonix sync worker consumes `mt5_priority,mt5_sync`; operator work belongs in priority so beat traffic does not starve it.
- Beat skip/backlog guard and task `expires=28` keep a recovered worker focused on fresh sync tasks.
- `REDIS_URL` must match across Render and VM or queues silently split.
- With multi-VM affinity, each VM's `COMPUTERNAME`/`VM_ID` slug must match the scoped queues it consumes and the slug producers route to (stamped `vm_id`), or that VM's accounts silently stop syncing.
- `ENCRYPTION_KEY` must match web and VM or MT5 credentials cannot decrypt.
- `MT5_SYNC_SECRET` and `FLASK_API_URL` must match the live site or internal posts fail.
- VM Task Scheduler XML exports must use `C:\Users\Administrator\fxjournal`, not the local dev path.
- Task Scheduler XML exports must be UTF-16 LE with BOM. After editing any `FX Journal MT5 *.xml`, run `python "manual VM scripts/reencode_task_xml_utf16.py"` and verify `FF FE`.
- Keep Windows Time (`W32Time`) and hypervisor/cloud guest time sync healthy on the VM; stale clock breaks windows, staleness checks, and logs.
- Use producer-side Celery dispatch logs to distinguish "web accepted the admin POST" from "task was published to Redis".
- Support view is read-only. User-facing POSTs should be centrally blocked, and chat POSTs should return JSON errors.
- Legal/product claims must stay tied to actual product capability: import/sync fallibility, read-only MT5, optional OAuth, AI processor use, and support-view access.

## Known Weak Points

- Some signup/onboarding paths still have single-process assumptions; horizontal web scaling can expose memory/session drift.
- Render beat and Hyonix MT5 workers are separate failure domains. Queue depth and VM logs tell different halves of the story.
- MT5 broker behavior is messy: symbol aliases, server offsets, weekend/off-hours ticks, stale broker history, and partial bars all need defensive handling.
- Automatic chart bars are admin-gated and capped; charts/AI market context can be missing for older/public trades until backfill catches up.
- Weekly AI quality depends on evidence payload shape. Prompt edits must preserve account scope, citations, structure, and evidence bounds.
- Bundle normalization should precede behavior interpretation or weekly AI can overcount split trade ideas.
- Cache prefixes need manual attention when bumped: `invalidate()` (`celery_workers/cache.py`) must list every live prefix (currently `dashboard_v4` and `analytics_v5`). A missed prefix silently serves stale data until TTL expires — this exact bug left `dashboard_v4` uninvalidated for up to an hour after trade changes.
- Multi-VM MT5 is a separate failure surface: queues are split by VM slug, missing `vm_id` skips account-bound dispatch, and setup failover must stay bounded. Wrong slug/legacy-queue config silently splits work.
- Admin operations include destructive actions such as clear-all-bars and user deletion; keep confirmation, root-admin gating, and logging tight.
- Broad MT5 lifecycle/global-lock refactors caused instability and were rolled back in April. Reintroduce setup/sync lifecycle changes one at a time and verify against the live VM.

## Planned Changes

- Manual browser pass on MT5 batch badge and dashboard queued/setting-up states.
- Decide whether to keep or retire legacy admin approve/reject endpoints for old MT5 request rows.
- Verify MT5-ready email trigger path against live worker/email config.
- Review live conversion response to the landing/auth messaging refresh.
- Add paid entitlement gates for dashboard AI journal full/all-history context and any context outside the current review week when billing access is live.
- Design macro/regime evidence for weekly AI and AI journal: source reliable calendar/market context, store deterministic tags around trade entry/exit and review weeks, and keep AI interpretation evidence-bounded.
- Fill in infrastructure plan/region/cost table with real billing facts.

## Tech Stack

Python, Flask, SQLAlchemy, Alembic, Jinja, vanilla JavaScript, Lightweight Charts, Celery, PostgreSQL, Redis, Render, Windows/Hyonix, MetaTrader5 Python package, Resend, Google OAuth, OpenAI.

## Essential Local Commands

```bash
.venv\Scripts\python.exe -m flask --app app db migrate -m "describe change"
.venv\Scripts\python.exe -m flask --app app db upgrade
.venv\Scripts\pytest.exe -q
```

For focused checks, prefer the test module that owns the touched surface, e.g. weekly AI tests for prompt changes, MT5 sync/setup tests for worker changes, import/math tests for `trading.py`, and admin route tests for admin/support-view behavior.

---

If this file disagrees with Render/Hyonix reality, update this file and `CURRENT_STATE.md` immediately. This map is the working "how it runs and where things live" cheat sheet.
