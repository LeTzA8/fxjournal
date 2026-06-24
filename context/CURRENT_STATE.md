# CURRENT_STATE

Last Updated: 2026-06-18

Rolling work log of recent, non-trivial changes and in-flight work. Durable architecture, capabilities, file ownership, env/config, and operational facts now live in [`PROJECT_MAP.md`](PROJECT_MAP.md) — keep this file short.

How to use:
- Add new entries at the top with a date heading.
- Once a fact has stabilized into project structure, fold it into `PROJECT_MAP.md` and remove it here.

> **2026-06-07 — Consolidation.** All prior entries (≈2026-04-17 → 2026-06-07) were folded into `PROJECT_MAP.md` and removed from this file. The full original chronological log remains in git history.

## Recent Changes

### 2026-06-24 - Dashboard bundle-aware metric disclosure

- Dashboard Account PnL and Win Rate copy now explicitly states the denominator is closed trade ideas after bundle detection.
- Trades This Month now shows both after-bundle and before-bundle opened-date counts so raw order volume and dashboard trade-idea volume are visible together.
- Tests: `tests/test_dashboard_weekly_ai.py` (`test_dashboard_metrics_show_raw_and_bundle_aware_month_counts`).

### 2026-06-22 — Manual trade form auto-calculates PnL

- Added `POST /api/trade-form-metrics` and `static/js/trade_form_metrics.js` so new/edit manual trade forms derive gross/net PnL, RR, and pips/ticks as entry, exit, and size are filled in.
- Save path still persists derived gross PnL when exit price is provided and the PnL field is left blank.
- Tests: `tests/test_trades_routes.py` (`test_trade_form_metrics_derives_pnl_from_entry_and_exit`, manual trade PnL assertion).

### 2026-06-18 — Expanded futures symbol catalog (31 new instruments)

- Added energy (NG, HO, RB, BRN), metals (SI, HG, PL, PA), FX futures (6E, M6E, 6B, 6J, 6A, 6C, 6S, 6N), bonds (ZN, ZB, ZF), grains (ZC, ZW, ZS, ZL, ZM), softs (KC, SB), volatility (VX), and crypto (BTC, MBT, ETH, MET).
- Migration: `20260618_0064_expand_futures_catalog.py`. Tick sizes/values are exchange-verified; `format_trade_price` and snap logic uses catalog specs automatically.

### 2026-06-18 — Futures price tick grid (0.25 default)

- Futures prices snap to each instrument's catalog tick size on display, manual entry, and CSV import; unknown roots default to `0.25` (2 dp).
- `format_trade_price`, trade forms, and import pipeline use `get_trade_price_step` / `snap_trade_price_to_tick`; catalog exceptions (YM `1.0`, CL `0.01`, etc.) unchanged.
- Decision: `D-009` in `DECISIONS.md`. Tests: `tests/test_trading_math.py`.

### 2026-06-17 — Weekly Review: bold lead pattern sentence

- `What mattered this week` (`summary.text`) now requires a leading `**...**` behavioral-pattern sentence in pass-1 prompt + JSON constraints; pass-2 rewrite preserves it.
- Summary segments parse `**...**` into a `strong` segment (`parse_bold=True` on summary only); template renders `<strong>` with scoped `.ai-review-summary strong` block styling.
- Fixed `_normalize_review_item_text` so leading `**` is not stripped as a bullet marker.
- Onboarding sample updated to match. Tests: `tests/test_ai_service.py`, `tests/test_dashboard_weekly_ai.py`.

### 2026-06-13 — Closed-trade CSV export

- Added `GET /dashboard/trades/export?format=csv` for the active trade account: closed trades only, UTC timestamps with explicit `UTC` suffix, optional `from`/`to` opened-at date filters.
- CSV includes a one-time strategy reference block (name + description) when strategies are attached, net PnL via `resolve_net_pnl`, and bundle columns (`bundle_group`, `bundle_trades`).
- Trades page shows an **Export CSV** button (hidden in manage mode).
- Tests: `tests/test_trades_routes.py` (`export` cases).

### 2026-06-12 — Futures sizing terminology: contracts not lots

- Futures accounts now use **contracts** / **contract count** in UI copy, validation, behavior badges, weekly AI constraints, and prompt injections; CFD accounts keep **lots**.
- Shared helpers in `trading.py`: `get_trade_size_unit`, `format_trade_size_with_unit`, `trade_size_must_be_positive_message`, `risk_size_claim_phrase`, `outlier_size_reason`.
- Templates updated: trades table size display, check-in, bundle review.

### 2026-06-07 — MT5 admin VM cards: hide unknown bucket

- MT5 Worker VMs panel now omits the `unknown` vm_id bucket; accounts without a stamped `vm_id` remain in the MT5 accounts table.
- Known VM cards sort by active account count first.

### 2026-06-07 — Futures dashboard: metrics in left column

- Futures dashboards move the six account metric cards into the left column (`metric-grid-side`) below week-on-week trends and latest closed trade; CFD layout unchanged.
- Removed the empty "bar replay not available" chart shell for futures latest-trade cards; trade summary + Review link remain.
- Extracted shared markup to `templates/_dashboard_metric_grid.html`.

### 2026-06-07 — MT5 admin cleanup records tab

- MT5 admin page now defaults to operational accounts only; credential-free / cleanup tombstone rows live under a **Cleanup Records** tab with count badge.
- VM overview and header stats exclude cleanup records from operational counts; cleanup count links to the tab.
- Tests: `tests/test_admin_mt5_views.py`; updated orphaned/admin delete render tests.

### 2026-06-07 - MT5 disconnect credential purge and cleanup tombstones

- MT5 disconnect/admin delete now removes saved MT5 credentials before VM cleanup is queued, preserves VM routing/runtime metadata until cleanup succeeds, and then clears terminal path/AppData/cleanup marks instead of deleting rows by default.
- Setup/sync/bar-fetch/admin actions now reject cleanup-pending or credential-free MT5 rows, and scheduled sync filters out credential-free rows.
- User/admin/legal copy now explains that credentials are removed immediately while limited VM cleanup metadata may remain until terminal cleanup completes; admin submission emails omit raw request notes for credential safety.
- Tests: `tests/test_mt5_access_requests.py`, `tests/test_mt5_cleanup.py`, `tests/test_mt5_dispatch.py`, `tests/test_mt5_orphaned_accounts.py`, `tests/test_mt5_sync.py`.

### 2026-06-07 — Exness MT5 failure copy

- Added conservative Exness detection for MT5 server/broker text and shared Exness setup note copy.
- MT5 setup failure emails, delayed MT5 details-saved emails, Dashboard failed-state copy, Trade Accounts failed-state copy, and the admin MT5 account row now tell Exness users not to resubmit saved details by default while we try to resolve broker-server discovery from our side first.
- Tests: `tests/test_mt5_copy.py`, `tests/test_email_templates.py`, `tests/test_mt5_access_requests.py`.

### 2026-06-07 — MT5 terminal folder naming (`mt5_uid*_taid*`)

- Added `helpers/mt5_terminal_paths.py` with labeled folder naming (`mt5_uid{user_id}_taid{trade_account_id}`), legacy `mt5_{uid}_{taid}` fallback, and `resolve_mt5_terminal_dir()` for setup compatibility.
- `setup_mt5_terminal` now resolves terminal dirs via the helper (persisted path → labeled on disk → legacy on disk → new labeled path). Raw MT5 login is excluded from folder names for privacy.
- Tests: `tests/test_mt5_terminal_paths.py`; updated `tests/test_mt5_setup_order.py`.

## Next Priorities

- [ ] Manual browser pass on MT5 batch badge + dashboard queued/setting-up states
- [ ] Decide whether to keep or retire legacy admin approve/reject endpoints for old MT5 request rows
- [ ] Verify ready-email trigger path against live worker/email config
- [ ] Review live conversion response to the landing/auth messaging refresh
