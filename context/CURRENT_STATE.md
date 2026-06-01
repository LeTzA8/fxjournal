# CURRENT_STATE

Last Updated: 2026-06-02

## Dashboard inline trade notes (2026-06-02)

- Dashboard recent-trades table adds an inline **Add note** / **Edit note** action (hidden in read-only support view).
- `GET/POST /api/trades/<pubkey>/note` updates only `trade_note` (8000 char cap), invalidates trade caches, and powers a modal editor on the dashboard (`static/js/dashboard_trade_note.js`).

## Weekly review follow-up hybrid prompt buttons (2026-06-02)

- Hybrid follow-up UX: up to 3 starter chips above the chat (hidden after the first user message); each assistant reply returns 2-3 `suggested_prompts` as tap buttons below the latest message only. Model outputs JSON `{reply, suggested_prompts}` (prompt v3); suggestions persist in assistant message content via `---FXJ_SUGGESTIONS---` trailer (no migration). API returns `suggested_prompts`; `weekly_review_chat.js` renders dynamic rows and reuses the same click handler as starters.

## Weekly review follow-up reply length (2026-06-02)

- Follow-up replies were long mainly because `weekly_review_followup.txt` required 4-5 "You could ask:" bullets every turn (on top of 2-5 answer sentences) while the dashboard already exposes quick prompt chips. Prompt v2 caps the main answer at 2-4 sentences, forbids bullet menus, and allows at most one optional closing line. Chat calls use `WEEKLY_REVIEW_CHAT_MAX_OUTPUT_TOKENS` (default 520) instead of the 1500-token weekly review ceiling.

## Weekly review follow-up chat UX fixes (2026-06-02)

- Follow-up chat log is now scroll-contained with auto-scroll on send/reply (matches journal chat behavior); failed sends keep the user bubble visible instead of removing it.
- Chat reply display expands bracketed refs like `[T1]` to inline trade labels (previously stripped entirely), skips duplicate labels when the symbol is already in the sentence, strips invented `[SYMBOL-SYMBOL]` / `NAS100-NAS100` refs, and removes redundant `DD Mon` text before a dated citation pill when the model cites after a partial date.
- Chat history bubbles use the same tight Jinja segment rendering as the weekly review panel (fixes citation pills breaking onto separate lines); prompt forbids invented symbol-pair refs.

## Weekly AI review gate CTAs in panel (2026-05-31)

- When bundle review or revenge/classification review blocks the weekly AI review, the AI review panel now shows the same primary CTA as the top workflow banner (e.g. Review Bundles, Review Revenge Signals) instead of a generic waiting message.
- Check-in gating remains in the AI panel with Open Check-In / Finish Check-In (after skip), Get Review Now, and optional Skip for now; workflow-gate stages take priority over the check-in prompt when both would apply (e.g. pending historical bundle review).
- Side panels (Improvement, Strength, Experiment) use a shared `weekly_ai_any_gate` state while any review gate is active.

## Futures CFD-proxy replay scaffold — Phase 1 (2026-05-30)

- Migration `20260530_0062_futures_proxy_replay_scaffold.py`: adds `trades.proxy_replay_symbol` (String 32), `trades.proxy_replay_status` (String 40), `trades.proxy_replay_window_minutes` (Text/JSON), and `futures_symbols.proxy_cfd_symbol` (String 32). Seeds default futures-root → CFD-proxy mappings: NQ/MNQ→NAS100, ES/MES→US500, YM/MYM→US30, RTY/M2K→US2000, GC/MGC→XAUUSD, CL/MCL→USOIL. Adds index on `(trade_account_id, proxy_replay_status)`.
- `helpers/futures_proxy.py` (new): five resolver functions — `resolve_proxy_cfd_symbol`, `resolve_proxy_status`, `compute_proxy_window_minutes`, `is_futures_trade`, `proxy_replay_api_block`. No Celery, no MT5, no bar fetching. Proxy window thresholds: ≤30 min→90/90, ≤4 h→180/180, ≤24 h→360/180, >24 h→720/360. Uses existing `parse_futures_contract_code` from trading.py and queries `FuturesSymbol.proxy_cfd_symbol` directly for admin-override support.
- Import-path scaffold: `routes/trades.py` `import_trade_file` sets `proxy_replay_symbol`, `proxy_replay_status`, `proxy_replay_window_minutes` on each FUTURES trade row after `add_all` and before `commit`. User is loaded from DB by `user_id` (not flask_login). Resolver errors are caught/logged without failing the import. No Celery tasks dispatched.
- Chart API scaffold: `trade_chart_data` in `routes/trades.py` now branches for futures trades before the `mt5_position` guard. Returns `{"status":"proxy_pending"|"proxy_unavailable", "proxy_replay":{...}, "markers":{entry_time, exit_time only}, "execution":{actual futures prices}}`. Horizontal entry/exit/SL/TP price levels are NOT in `markers` for proxy trades — they are in `execution` only. Existing CFD/MT5 paths unchanged.
- Frontend: `templates/trade_entry.html` shows the chart panel for closed futures trades (`account_type == "FUTURES" and trade.closed_at`). TF buttons hidden for proxy mode. `tradeProxyExecutionPanel` div added. `static/js/trade_chart.js` handles `proxy_pending`/`proxy_unavailable` statuses via `showProxyStatus()`, populates execution panel. `static/css/app_pages.css` adds proxy badge, execution card, disclaimer styles.
- Universal payload: `ai_service.py` serializes `proxy_replay_symbol`/`proxy_replay_status` into trade dicts. `helpers/universal_weekly_payload.py` `_build_universal_trade` adds `replay_accuracy="approximate"` and `chart_price_source="cfd_proxy"` for proxy trades and suppresses `tp_capture_pct`, `closed_before_tp`, `closed_before_sl`, `mfe_r`, `mae_r`, `post_exit_direction`, `post_exit_tp_reached` from both the entry and `market_context_summary`. CFD/MT5 trades unaffected.
- Prompt: `prompts/futures_proxy_replay_guardrails.txt` (new) — defines Phase 2 contract for approximate replay AI rules. NOT wired into `dashboard_advice.txt`, `dashboard_advice_rewrite.txt`, `weekly_review_followup.txt`, or `journal_chat.txt`. Existing prompts unchanged.
- Tests: 4 new test files (58 tests total) — `test_futures_proxy_resolver.py`, `test_futures_proxy_import.py`, `test_futures_proxy_chart_api.py`, `test_futures_proxy_payload.py`, `test_futures_proxy_prompts.py`. All 58 pass + 121 existing regression tests pass.
- Phase 2 (deferred): actual bar fetching via `copy_rates_range`, Celery proxy-fetch task, `TradeBars` `source_kind`/`source_symbol` columns, `proxy_replay_status="available"`, vertical time markers on live proxy chart.

## Weekly AI review checkin gate (2026-05-30)

- Auto-generation is now gated on check-in completion: `_get_weekly_ai_state` accepts `checkin_complete` (from `_get_review_workflow_banner_state`) and when False + no current-period review exists, suppresses the fallback/previous-week review and skips generation.
- Exception: if a review is already in-flight (`ai_status in {"queued","running"}`), the generating state shows regardless of check-in status (user previously clicked "Get Review Now").
- `?get_review=1` query param on `/dashboard` bypasses the check-in gate for one load, setting `force_review=True` and treating `checkin_complete=True`.
- `_get_review_workflow_banner_state` now returns `checkin_complete` in all branches (True when no trades this period or check-in done; False when any workflow stage is pending). Reordered in `home()` so banner state is computed before AI state.
- `weekly_ai_needs_checkin` context variable passed to template; when True the AI panel shows an empty state with "Open Check-In" and "Get Review Now" buttons instead of the previous-week's review or a waiting message.
- `weekly_checkin` stage of the workflow banner is suppressed in the template (`review_workflow_stage != "weekly_checkin"` guard) — the AI panel replaces it. `bundle_review` and `classification` banners are unaffected.
- Loading state: side panels (Improvement, Strength, Experiment) now show `ai-generating-shell--side` with `ai-loading-orb--sm` spinner when `weekly_ai_is_generating`, instead of just muted text.
- Tests: updated `test_dashboard_shows_finish_checkin_after_skip` to expect "Open Check-In"/"Get Review Now" (panel) not "Finish Check-In" (suppressed banner); fixed two pre-existing stale assertions (`b"Sample Weekly Review"` → actual sample badge text); fixed pre-existing FUTURES account test (`test_dashboard_home_prompts_switch_when_active_account_is_not_cfd`) to reflect `show_mt5_panel=False` for non-CFD accounts.

## Topstep CSV import support (2026-05-30)

- Added `TOPSTEP_REQUIRED_FIELDS`, `_TOPSTEP_DT_RE`, `_parse_topstep_timestamp()`, `sniff_topstep_csv_stream()`, and `parse_topstep_csv_stream()` to `trading.py`.
- Topstep export format: `Id,ContractName,EnteredAt,ExitedAt,EntryPrice,ExitPrice,Fees,PnL,Size,Type,...` with timestamps as `MM/DD/YYYY HH:MM:SS ±HH:MM`. PnL is gross; Fees is the total per-trade cost (commission + exchange).
- `_parse_topstep_timestamp` parses the Topstep timestamp format (not ISO) directly to UTC-naive; stores the explicit offset string (e.g. `+08:00`) as `source_timezone`.
- `detect_trade_import_profile()` now tries Topstep before Tradovate and MT5; field-based detection means no format ambiguity.
- `routes/trades.py`: imports `parse_topstep_csv_stream`; `import_trade_file` handles `topstep_csv` parser branch; import signature prefix `topstep`; system note `Imported from Topstep Export CSV`; flash and error messages now use detected platform name rather than hardcoded "Tradovate".
- `templates/trade_entry.html`: heading, help card, intro text, dropzone label, and `data-expected-upload-label` updated to cover both Tradovate and Topstep for FUTURES accounts.
- No model or migration changes required; the normalized row shape is identical to Tradovate.

## Account-type import surface split (2026-05-30)

- Futures active accounts hide all MT5 UI (dashboard MT5 panel, journey-banner MT5 nudges, choose-your-path MT5 sync card). Copy points to Tradovate CSV import instead.
- CFD active accounts hide Tradovate references in onboarding/import empty states; MT5 XLSX + MT5 sync remain.
- Dashboard passes `active_account_type`, `is_active_cfd`, `is_active_futures`, `show_mt5_panel`; analytics empty state uses the same split.
- Trade Accounts create/edit dialog strategy labels toggle MT5 wording via `static/js/trade_accounts_page.js` when account type changes.
- Import error for unrecognized files is account-type specific in `routes/trades.py`.
- Tests: futures zero-data + state-2 dashboard in `tests/test_zero_data_funnel.py`, `tests/test_dashboard_weekly_ai.py`.

## Waitlist CTA QA fixtures (2026-05-28)

- `qa_test_accounts` sidecar table (`models.QaTestAccount`, migration `20260528_0061`) — fixture metadata only; no `users` column flags.
- Flask CLI: `flask seed-waitlist-cta-test-data` / `flask reset-waitlist-cta-test-data` (`cli/qa_fixtures/`) with `--scenario`, `--reset`, `--allow-production`, `--yes`; production gating + TTY `I UNDERSTAND` confirm; `seed_all_fixture_scenarios()` for atomic full reseed.
- Six scenarios seeded via direct ORM (no register route, no `send_email_placeholder`, no Celery/MT5 dispatch): replay lock, AI follow-up cap, MT5 expired, MT5 paused, zero-data, normal active control. Password `TestPassword123!`; emails `dummy-cta-*@myfxjournal.test`. Replay `test_path` uses trade `pubkey`.
- Paused vs expired: `get_trial_state` uses `sync_paused_at` only (Scenario D sets `sync_pause_reason=trial_expired` for parity with prod rows).
- Admin: `GET /dashboard/admin/access/test-accounts` + POST notes/re-seed/delete (`cli/qa_fixtures/admin_ops.py`, `templates/admin_test_accounts.html`); re-seed/delete are root-admin-only; sidebar **QA Test Accounts**; header notes fixtures appear in user totals.
- Tests: `tests/test_qa_waitlist_cta_fixtures.py`, `tests/test_qa_waitlist_cta_fixture_scenarios.py` (§9.3 + §9.6), `tests/test_qa_test_accounts_admin.py` (§9.5); `tests/test_admin_route_gating.py` route list updated.

## Pre-activation zero-data funnel (2026-05-28)

- Pure zero-data users (`no trades` + no MT5 account/access request on active CFD row) see `templates/_choose_your_path_card.html` instead of the MT5-first journey banner: upload report, add manual trade, request MT5 sync (`#mt5-access`), preview sample review (`#weekly-ai-sample`).
- Dashboard flags in `routes/dashboard.py`: `is_pure_zero_data`, `has_mt5_submission` via `helpers/admin_activation.dashboard_row_has_mt5_submission` (excludes `requestable`-only rows).
- Soft waitlist: muted footer link after 1+ days zero-data (`source=zero_data_dashboard`, `feature=advanced_replay`, `cta_context=zero_data_soft_waitlist`); allowlist in `auth_account.py`.
- Pure zero-data layout: centered `Start here` card in `ai-hero-grid`, MT5 login panel hidden until `#mt5-access` / Submit MT5 details; Request MT5 sync card glow; sample weekly review uses real panel structure (`_weekly_ai_onboarding_sample.html`, `app_pages.css`, `dashboard_page.js`).
- Admin: `helpers/admin_activation.count_pre_activation_users` + Users stat tile; filters `?activation=zero_data_recent|zero_data_stuck` on `/dashboard/admin/access/users`.
- Styles: `static/css/app_pages.css` (cache `v=15` in `base.html`).
- Tests: `tests/test_zero_data_funnel.py`; updated `test_dashboard_weekly_ai.py` state-1 banner expectations.

## Contextual waitlist CTAs (2026-05-27)

- Shared waitlist modal extracted to `templates/_waitlist_modal.html` + `static/js/waitlist_modal.js` with updated copy ("Access opens gradually…", success: "We'll email you the moment access opens."). Logged-in users get read-only email prefill via `waitlist_user_email` context (`app.py`).
- Lock cards: `templates/_waitlist_lock_cards.html` macro + `.waitlist-lock-card` styles in `static/css/app_pages.css`.
- Placements: pricing (refactored), dashboard weekly chat lock, MT5 capacity notify, MT5 trial expired/paused cards (dashboard + trade accounts), trade replay 1m lock (`static/js/trade_chart.js`), landing roadmap lane CTAs.
- Backend: extended `WAITLIST_ALLOWED_SOURCES` / `WAITLIST_ALLOWED_FEATURES`; `_trial_cta` source → `ai_followup_lock`; `_replay_waitlist_cta` source → `replay_lock` + `cta_context`.
- Admin: `GET /dashboard/admin/access/waitlist` + CSV export; Users stat tile links to waitlist page; sidebar nav entry.
- Tests: `tests/test_waitlist_ctas.py`; updated trades/dashboard/admin gating tests. Stale futures-only continuity hero test aligned with no-MT5 dashboard layout.
- Post-audit fixes (2026-05-27): MT5 expired lock card copy uses "opening gradually" (no batch framing); `/pricing/waitlist` rejects invalid `source`/`feature_interest` with 400 JSON; modal JS surfaces server `error` on non-2xx even when JSON parse is partial; duplicate-enrichment test renamed; added invalid-input + MT5 lock-card render tests.

## Weekly AI universal payload (2026-05-27)

- One universal weekly JSON payload is built by `helpers/universal_weekly_payload.py` (`build_universal_weekly_payload`, `format_universal_weekly_payload`). `build_trade_payload` returns this shape; the same object is stored in `AIGeneratedResponse.payload_json` and sent to pass-1 via `build_dashboard_advice_messages` (pretty-printed JSON user message).
- Payload slim-down (2026-05-27): merged `review_scope` + `sample_context` into `week_summary` / `evidence_boundary`; canonical `post_loss_sequences[]` replaces `primary_sequences`, `post_loss_response`, and per-trade `post_loss_context`; removed prompt-like prose from `coaching_frame_triggers`; `primary_issue_evidence_level` → `week_summary.issue_scope`; trades add `trade_date_label`, `tp_capture_pct`, `closed_before_tp`, `closed_before_sl`, `risk_outlier`; slim `risk_authority` (basis/stable/risk_judgment_allowed only).
- `prompts/dashboard_advice.txt` + `REVIEW_JSON_OUTPUT_INSTRUCTIONS` aligned to final universal paths — no `notes_coverage`, `SURFACE_FACTS`, `strategy_coverage_pct`, or detailed bar microstructure; frame guidance lives in prompt, not payload.
- Citation rewrite (`helpers/weekly_review_ref_rewrite.py`) prefers `trade_date_label`, falls back to legacy `opened_at`.
- Admin weekly audit: `sample_context` derived from `evidence_boundary` when absent (backward compat for old stored payloads).
- Tests: `tests/test_universal_weekly_payload.py`, updated `tests/test_ai_service.py`, `tests/test_dashboard_weekly_ai.py`.

## Weekly AI pass-1 uses full audit payload (superseded 2026-05-27)

- Superseded by universal payload refactor above. Pass-1 and admin audit now share one slim interpreted payload, not the former full internal JSON.

## Weekly AI compressed prompt payload slimming (superseded 2026-05-27)

- Previously pass-1 used a compressed slice from `helpers/weekly_prompt_payload.py`. Removed — replaced by universal payload builder.

## Weekly AI compressed prompt payload (2026-05-26, superseded 2026-05-27)

- Superseded by universal payload refactor.

## Admin Send Email compose (2026-05-26)

- `GET /dashboard/admin/access/send-email` (`admin_send_email`): admin-gated compose with searchable multi-select recipients, adjustable inactive filter (last login older than N days), native textarea body editor (no CDN editor dependency), clear `{{name}}` username token guidance, live sample/recipient preview, and sequential per-recipient send with confirmation, progress, and summary.
- `GET /dashboard/admin/access/send-email/recipients` + `POST /dashboard/admin/access/send-email/send-one`: JSON APIs; sends via existing Resend path with `from_header` from `ADMIN_EMAIL_FROM` (default `admin@myfxjournal.com`). Client sends plain text bodies and Resend fallback HTML preserves line breaks; client delays between sends (`ADMIN_EMAIL_SEND_DELAY_MS`, default 400ms).
- **Image insert (2026-05-27):** compose toolbar adds **Insert logo** (public `site-logo.png` URL) and **Insert image URL** (https prompt). When the body contains allowed HTML (`img`, links, basic formatting), the client sends `html_body` and preview renders images; server sanitizes HTML via `sanitize_admin_broadcast_html` before Resend.
- `auth_account.send_email_placeholder` accepts optional `from_header`; helpers `apply_admin_email_placeholders`, `html_to_plain_email_text`, `sanitize_admin_broadcast_html`, `admin_broadcast_message_contains_html`, `_resolve_admin_broadcast_from_header`.
- UI: `templates/admin_send_email.html`, `static/css/admin_send_email.css`, `static/js/admin_send_email.js`; nav link in `partials/admin_shell_start.html`.
- Tests: `tests/test_admin_send_email.py`, `tests/test_send_email.py` (custom from), `tests/test_admin_route_gating.py` route list.

## Admin users trial visibility + extension (2026-05-27)

- Registered Users panel (`/dashboard/admin/access/users`) now shows premium trial status in **Signals**: summary (`Active`, `Expired`, `Grandfathered`, `Not started`, paid/admin labels), trial start/end UTC timestamps when applicable.
- Root admins can extend an ended trial from the Tools column: inline days input (1–90, default 14) + **Extend** posts to `POST /dashboard/admin/access/users/<id>/extend-trial`. Extension grants that many full days remaining from now, clears trial-paused MT5 sync stamps, and queues MT5 setup when credentials exist.
- Helpers: `build_admin_user_trial_display`, `extend_premium_trial` in `helpers/entitlements.py`. Tests: `tests/test_admin_user_trial.py`.

## Admin users CSV export (2026-05-26)

- `GET /dashboard/admin/access/users/export` (`admin_signup_users_export`): admin-gated download of all users as CSV with columns name (username), email, signup date (`created_at`), last login date (`last_login_at`), last active date (`last_active_at`), using the same UTC timestamp format as the admin users table.
- `templates/admin_signup_access.html`: **Export all users (CSV)** link on the Registered Users panel toolbar.
- Tests: `tests/test_admin_route_gating.py` (route gating list, CSV payload, export link on users page).

## Trade edit/detail progressive disclosure (2026-05-26)

- `templates/trade_entry.html`: edit and read-only full-record forms now use native `<details>` collapsibles with section hints, chevrons, and default-open core sections (Instrument, Execution, Performance/Outcome, trade note). Timing, strategy description, and import note default collapsed; edit view adds a compact top summary line (symbol · side · net PnL · RR · status · date).
- Edit form moves status into the Timing section; strategy select + readonly description and import note sit in nested Journal sub-sections. New-trade manual entry form unchanged (flat sections).
- `static/js/trade_form_collapsible.js` opens collapsed sections when HTML5 validation fails. Edit route passes strategy description + source timezone context for template rendering only.
- Tests: `tests/test_trades_routes.py::test_trade_edit_uses_progressive_disclosure_sections`.

## SEO hero/panel copy refinement (2026-05-26)

- Second pass on all 18 `SEO_PAGE_DEFINITIONS` pages: shortened hero bodies and hero side panels, removed search-intent/meta copy (e.g. `/free-trading-journal` “Why traders search for free first”, `/trade-replay-chart` “If you searched for…”, template page search framing), and varied panel kickers (`panel_kicker`) plus “What it removes” headings (`cards_heading`) per page.
- Hero panels now use user-facing angles (what you get, best fit, what sync changes, what this replaces, first useful outcome) with 1 short paragraph + max 3 bullets; free-cluster and psychology pages got page-specific removal cards (spreadsheet upkeep, export cycles, blank-page journaling, rule drift, etc.).
- `templates/seo_page.html`: optional `panel_kicker` (removed hardcoded “Why this matters”) and optional `cards_heading` (replaces generic “Less admin. More clarity.” when set). No routing/canonical/sitemap changes; no new CSS/layout sizing changes — text trim only.
- Tests: `pytest tests/test_public_seo.py -v` (15 passed).

## SEO acquisition-to-activation pass (2026-05-26)

- Reviewed all configured public SEO pages in `SEO_PAGE_DEFINITIONS` through an acquisition -> activation -> retention lens. Copy now leads with search intent, import/sync expectations, first review value, and one clear next action instead of repeating broad product philosophy.
- Existing pages were tuned for safer claims: MT5 sync is framed as optional/capacity-managed/trial-gated where relevant, AI pages avoid signal/prediction/chatbot overpromising, comparison pages avoid stale competitor price claims, revenge/prop/losing-trade pages use evidence-bounded reflection language.
- The free SEO cluster remains the broad hub plus MT5, AI, forex, template-alternative, and prop-firm intents; repeated free/trial/no-card wording was consolidated, template copy stays clear that MyFXJournal is software rather than a downloadable spreadsheet.
- `templates/seo_page.html` now shows one dominant top CTA and one bottom CTA by removing repeated "See Product Proof" secondary buttons; the retained pricing text link keeps premium workflow/payment context without becoming a competing CTA.
- Public SEO tests now iterate all configured SEO page definitions and sitemap paths, checking unique page titles/descriptions/H1s plus 200/indexable/canonical output for every SEO slug.
- Tests: `.\.venv\Scripts\python.exe -m py_compile auth_account.py tests/test_public_seo.py`; `.\.venv\Scripts\pytest.exe tests/test_public_seo.py -v` (15 passed).

## Admin MT5 delete button fixes (2026-05-25)

- Fixed the per-account **Delete VM files** form submit handler: the confirm copy now uses JSON escaping, avoiding the decoded apostrophe syntax error that made the browser kill the submit before the POST.
- Regular MT5 **Delete** now remains clickable for cleanup-only/orphaned rows, matching the backend route that deletes those DB-only records. Admin delete no longer requires a target VM when the MT5 row has no stored terminal/AppData artifacts to clean up. Added focused route/render regressions. (`templates/admin_signup_access.html`, `auth_account.py`, `tests/test_mt5_access_requests.py`)
- Per-account **Delete VM files** is no longer rendered as a disabled button for blocked states. The POST now reaches backend validation and flashes the reason (active account, cleanup pending, no artifacts, cleanup-only) instead of appearing to do nothing.
- **Delete VM files** cleanup dispatch now always passes `mt5_account_id`, explicit `delete_account_row=False`, and `target_vm_id` in Celery kwargs so multi-VM workers can route/redispatch correctly (including Celery-prefixed `vm_id` values). (`helpers/core.py`, `helpers/mt5_dispatch.py`, `celery_workers/mt5_setup_tasks.py`, tests)
- Admin **Reactivate** now scopes setup dispatch to the row Target VM dropdown (`strict_target_vm`, `allow_failover=False`), including cross-VM reactivation when multi-VM is enabled. Form submit syncs the current dropdown value instead of resetting to stored `vm_id`; reactivation updates stored `vm_id` to the chosen target. (`helpers/core.py`, `auth_account.py`, `static/js/admin_mt5_accounts.js`, `templates/admin_signup_access.html`, tests)

## Contact page hero alignment (2026-05-25)

- `templates/contact.html` now uses shared `app_page_hero` inside `dash-wrap` / `dash-content`, matching Dashboard and settings pages. Redundant welcome/subtitle copy removed; support inbox and privacy guidance consolidated into hero + one form helper line. Public visitors load `app_pages.css` for hero parity via `head_extra`.

## Strategies + Trade accounts hero alignment (2026-05-25)

- `templates/trade_profiles.html` and `templates/trade_accounts.html` now use shared `app_page_hero` (`app-page-hero--settings-hub`) instead of `app-workbench-header`, with meta chips and primary CTAs in the hero actions row. Panel subtitles trimmed where hero carries the same context.
- Strategies + Trade accounts workbench grids now use full-width `dash-content` (removed 1120px cap). Long strategy descriptions collapse to a 3-line preview via native `<details>` with Show more/less; full text remains in Edit dialog `data-editor-description`. (`static/css/app_pages.css`, `templates/trade_profiles.html`)

## Dashboard MT5 setup de-duplication (2026-05-25)

- Continuity status (last sync, new trades, review) moved from standalone dashboard row into the MT5 sync panel as a compact strip; no separate "View review" or other next-action CTAs on that strip. When no CFD accounts exist, the same fields appear in hero meta chips instead.

- State-1 (no trades): removed dashboard header essay, MT5 panel title hint, Step 1 badge, onboarding trust paragraph, selected-account card, inline trial line, and wizard kicker; journey banner remains primary setup CTA; panel shows account name + wizard step 1 + Continue + encrypted footnote.
- State-2 (trades, no MT5): compact MT5 panel with setup wizard, no guided styling, header "Connect MT5" + one short line; progress stepper hidden until submitted; inline trial hidden during wizard requestable/pending only.
- Fixed `_mt5_use_wizard` alias so wizard steps render for state-1/2.

- Public/auth pages: consolidated repeated trial/MT5 copy on landing, SEO, pricing, register, login, and dashboard public gate.
- Dashboard: returning-user header drops welcome/essay copy; journey banner text shortened; onboarding banner hidden when setup/review blockers active; workflow banner suppresses duplicate journey banner.
- Audit follow-up: dashboard continuity strip inside the MT5 panel treats review workflow banners as the owner of action CTAs and status priority, so bundle/revenge/check-in prompts do not duplicate their primary button beside the banner.
- Audit follow-up: admin MT5 cleanup routing still asks for a target VM when multi-VM affinity is missing, but the rendered error now clearly says cleanup could not be queued.

## Admin MT5 panel load performance (2026-05-25)

- MT5 sync admin page no longer blocks on repeated Redis SCAN/LLEN round-trips per request. Worker monitor state is fetched with one SCAN (`list_mt5_worker_states`), queue depths use a Redis pipeline (`get_queue_depths`), and the combined monitor snapshot is cached for 20s (`admin_mt5_monitor_snapshot`).
- VM overview reuses cached worker buckets for selectable VM ids instead of scanning Redis again. POST action VM selectors load distinct `vm_id` values only (not full MT5 account rows).
- MT5 account list eager-loads `user` and `trade_account`; latest access-request status uses one grouped subquery instead of loading full request history.

## Admin MT5: Delete VM files replaces Reset Terminal (2026-05-25)

- Per-account **Delete VM files** (`POST …/mt5/<id>/delete-vm-files`) queues terminal/AppData cleanup only via `delete_mt5_account_vm_files`. It clears stored `terminal_path` / `appdata_hash` and sets `is_active=false`, but keeps credentials, `vm_id`, connection errors, and does **not** set `cleanup_marked_at`, so **Setup Terminal** is not blocked afterward. Rejects actively syncing accounts (still allows failed-connection rows with VM artifacts), rows with account cleanup pending, VM id mismatches, and missing target VM in multi-VM mode. Admin Target VM dropdown and cleanup dispatch canonicalize Celery-prefixed ids (`mt5-sync@HOST` → `HOST`) so delete/setup route to the correct scoped queue. Flash/confirm copy warns admins to wait for VM cleanup before Setup.

## Admin MT5: VM-targeted cleanup + setup selector (2026-05-25)

- Admin MT5 panel exposes a reusable **Target VM** selector when multi-VM is enabled or more than one VM is configured/observed. Applies to **Setup Terminal**, **Reactivate**, **Delete VM files**, and **Delete** so cleanup/setup routes to the chosen worker queue. Submissions are validated against configured/account/worker VM ids.
- **Archive** is state-only: sets `archived_at` / `archive_reason` and `is_active=false` without queueing VM cleanup or clearing `terminal_path` / `appdata_hash`. Use **Delete VM files** when terminal cleanup is needed.
- Each Worker VM card includes **Delete VM files** — queues terminal/AppData cleanup for **inactive or archived** MT5 accounts on that VM with stored runtime paths, then clears `terminal_path` / `appdata_hash` / `is_active` in the DB. Active accounts are skipped.
- Delete/reset/archive now return **errors** when cleanup cannot be queued but VM artifacts still exist (multi-VM missing `vm_id` or invalid target VM). Delete no longer marks cleanup-only state when dispatch fails. Archive no longer queues cleanup.
- Backend: `queue_mt5_account_cleanup(..., target_vm_id=)`, `queue_mt5_accounts_cleanup_for_vm`, `collect_admin_selectable_vm_ids`, `resolve_admin_target_vm_id`, route `POST /dashboard/admin/access/mt5/vm-delete-files`. (`helpers/core.py`, `helpers/admin_mt5_ops.py`, `auth_account.py`, `templates/admin_signup_access.html`, `static/css/admin_panel.css`, tests)

## VM Task Scheduler XML: direct workers primary, watchdogs legacy (2026-05-25)

- **Primary:** `FX Journal MT5 Setup Worker Direct.xml` + `FX Journal MT5 Sync Worker Direct.xml` — portable any-user `LogonTrigger` + `InteractiveToken` as `.\Administrator`; enabled by default.
- **Legacy (disabled by default):** `FX Journal MT5 Setup Watchdog (Legacy).xml` + `FX Journal MT5 Sync Watchdog (Legacy).xml` — superseded by Worker Direct; import only if you still want health-check supervision.
- Re-import on each VM after pulling; run `python "manual VM scripts/reencode_task_xml_utf16.py"` after editing XML locally. Paths stay `C:\Users\Administrator\fxjournal`.

## VM Task Scheduler XML: portable logon trigger (2026-05-25)

- All four `manual VM scripts/FX Journal MT5 *.xml` exports now use an **any-user** `LogonTrigger` (no `UserId`) and run as `.\Administrator` with `InteractiveToken`, so imports work on any Hyonix VM without editing `COMPUTERNAME`. Repo paths remain `C:\Users\Administrator\fxjournal`. Re-import on each VM after pulling; run `reencode_task_xml_utf16.py` if you edit XML locally.

## Audit fixes: MT5 shortlist XSS, dispatch skips, waitlist CSRF (2026-05-25)

- Fixed stored admin XSS in MT5 server seed shortlist confirm dialogs by JSON-escaping server names with `tojson` instead of inline single-quoted strings. (`templates/admin_signup_access.html`, tests)
- Multi-VM dispatch skips (`missing vm_id`) now surface as errors/warnings at callers: `queue_mt5_account_cleanup`, `delete_mt5_account_vm_files`, and admin MT5 sync/recalibrate/backfill routes check `mt5_dispatch_was_skipped()` before reporting success. (`helpers/mt5_dispatch.py`, `helpers/core.py`, `auth_account.py`, tests)
- Dashboard MT5 capacity waitlist fetch now sends `X-CSRFToken`; `/pricing/waitlist` rejects support-view sessions; notify button hidden in support view. (`templates/index.html`, `auth_account.py`, tests)
- Restored decision D-007 (weekly AI persistence) alongside D-008 in `context/DECISIONS.md`.

## State + Action UX: Trade Accounts and Strategies workbench (2026-05-24)

- Reframed Trade Accounts and Strategies as returning-user workbenches: compact headers (title, counts, active/default context, primary create action), state-first cards, and education only in empty states plus create/edit/archive dialogs.
- Trade Accounts cards now show trade count, AI review count, default strategy, account size/external ID when present, MT5 chips, last sync when linked, and MT5 one-liners + dashboard CTAs only when action is needed. Replaced the tips/action side panel with Needs attention / Recent activity derived from MT5 state and timestamps.
- Strategies cards now show playbook description (preserved line breaks), last edited, total usage, and last-7-days usage scoped to the active trade account when one exists (otherwise all user trades). Removed side-panel tips; no duplicate action in v1.
- Added `helpers/settings_workbench.py` view-model builders; route render contexts pass `account_cards`, `accounts_sidebar`, and `strategy_cards`. Updated `static/css/app_pages.css`; JS unchanged aside from moved dialog triggers sharing existing IDs.
- Tests: updated `test_trade_accounts_page_shows_mt5_status_only`; added strategies workbench render tests in `tests/test_trade_profiles_routes.py`. (`routes/trade_accounts.py`, `routes/trade_profiles.py`, `templates/trade_accounts.html`, `templates/trade_profiles.html`, `helpers/settings_workbench.py`, `static/css/app_pages.css`, tests)

## First-run UX: dashboard-first onboarding (2026-05-24)

- Removed the pre-dashboard questionnaire gate: login/signup success now lands on `/dashboard` instead of `/onboarding`. `/onboarding` remains optional via the dashboard banner.
- Unified MT5-first copy across register, trade accounts, landing, dashboard journey banner, and optional onboarding page. Canonical bridge message: connect MT5 first; import a report while setup runs.
- State-2 dashboard (trades present, MT5 not connected) now nudges Connect MT5 next in the journey banner and import-success banner on trade entry.
- MT5 setup capacity closed state now offers Import a report now plus Notify me when MT5 setup opens (reuses `/pricing/waitlist` with `source=dashboard_mt5_capacity`, `feature_interest=mt5_sync`).
- MT5 AJAX submit success card now shows setup queued, email-when-ready copy, and an import-while-setup-runs action; progress UI still updates from JSON payload.
- Tests: auth redirect, register copy, state-2 MT5 nudge, capacity-closed actions. (`auth_account.py`, `routes/dashboard.py`, `routes/trades.py`, templates, `static/js/mt5_request_form.js`, tests)

## Multi-VM MT5 queue affinity (2026-05-24)

- Added `helpers/mt5_dispatch.py`: VM slug normalization, scoped queue names, centralized `dispatch_mt5_*` helpers, wrong-VM guard with bounded re-dispatch, setup target/failover helpers.
- **2026-05-26:** Removed `FXJ_MT5_MULTI_VM` gate — all MT5 sync/priority/setup/cleanup/pause tasks always publish to VM-scoped queues (`mt5_*.<slug>`). Beat uses per-VM queue busy checks; missing `vm_id` skips account-bound dispatch. VM workers listen on scoped queues only; optional `FXJ_MT5_LISTEN_LEGACY_QUEUES=1` for migration drain.
- Producers refactored: beat (per-VM busy check when flag on), sync/bar/pause/cleanup/setup paths in `celery_workers/mt5_sync_tasks.py`, `celery_workers/mt5_setup_tasks.py`, `routes/mt5_internal.py`, `routes/trade_accounts.py`, `auth_account.py`, `helpers/core.py`.
- Setup failover for public/default setup: cleans partial VM artifacts, clears runtime fields, re-queues on next VM from `FXJ_MT5_SETUP_VM_IDS` up to `FXJ_MT5_SETUP_FAILOVER_MAX_VMS`. Admin targeted setup uses `allow_failover=False` + optional `target_vm_id` selector.
- `get_vm_id()` now prefers `COMPUTERNAME` over `VM_ID`. PowerShell workers dual-listen scoped + legacy queues. Admin Worker VMs panel shows per-VM scoped queue depths and no-worker warnings. Runbook: `context/MULTI_VM_MT5.md`, decision D-008.

## MT5-first dashboard onboarding (2026-05-24)

- Redesigned state-1 dashboard onboarding to lead with MT5 as the primary path instead of competing import/MT5 signals. Journey banner, header copy, and MT5 panel now frame connect-once automatic sync as step 1; import is a secondary "while setup runs" bridge.
- Added a 4-step MT5 setup wizard for state-1 (`data-mt5-setup-wizard` in `static/js/mt5_request_form.js`): account number → investor password → server (help collapsed) → confirm/start.
- Replaced empty Weekly AI Review with a sample preview on state-1 so the right panel sells the moat outcome before data exists.
- Aligned MT5-first copy on register, trade accounts, landing comparison bullet, and requestable MT5 status note. (`templates/index.html`, `routes/dashboard.py`, `templates/register.html`, `templates/trade_accounts.html`, `templates/landing.html`, tests)

## MT5 server seed shortlist (2026-05-24)

- Added failure-driven `mt5_server_seed_shortlist` table and admin MT5 panel for broker servers that may need golden-master `servers.dat` seeding on the VM. Setup auto-adds rows only after bootstrap when final setup failure looks server/IPC related; successful setup auto-resolves the row. Admin can mark seeded (after RDP/script) or dismiss. Migration `20260524_0060_mt5_server_seed_shortlist.py`. (`models.py`, `helpers/mt5_server_seed_shortlist.py`, `celery_workers/mt5_setup_tasks.py`, `auth_account.py`, `templates/admin_signup_access.html`, tests)

## MT5 beat self-paced dispatch (2026-05-24)

- Beat no longer fans out every active MT5 account every 30s. `sync_all_active_mt5_accounts` now self-paces: skip when `mt5_sync` already has work, pick the stalest eligible account (`last_synced_at` nulls-first), skip per-account sync locks, and enqueue **one** task with `expires=600`. (`celery_workers/mt5_sync_tasks.py`, tests)
- Worker sync POSTs now include `trigger_source`; beat-triggered noop ingests skip automatic bar-fetch sweeps (`skipped_reason=beat_noop`) so priority queue work does not starve scheduled sync on the solo VM worker. Manual/admin ingest and beat ingests with saved/updated trades still queue auto bar sync. (`routes/mt5_internal.py`, `celery_workers/mt5_sync_tasks.py`, tests)

## Admin panel: approved default + MT5 VM overview (2026-05-24)

- Users & approvals now defaults to the **Approved** tab instead of Pending. Pending spotlight remains at the top for quick review. (`auth_account.py`, `templates/admin_signup_access.html`, tests)
- MT5 sync admin removed the batch management panel (create/open/add slots/close). Batch backend routes remain for legacy compatibility but are no longer surfaced in admin UI. (`auth_account.py`, `templates/admin_signup_access.html`, `templates/partials/admin_shell_start.html`)
- MT5 sync admin now shows a **Worker VMs** panel: accounts grouped by `vm_id`, optional region/provider/label from worker env or `FXJ_MT5_VM_PROFILES`, live sync/setup worker heartbeat status, queue depths, expandable per-account lists, and a VM column on the accounts table. (`helpers/admin_mt5_ops.py`, `celery_workers/worker_monitor.py`, `static/css/admin_panel.css`, tests)
- MT5 `vm_id` now stamps on **setup success** and every **successful sync POST**, using Windows `COMPUTERNAME` (before Celery hostname) so Hyonix host names like `MYFXJOURNAL-SG` align worker telemetry and account grouping in admin. (`celery_workers/worker_monitor.py`, `celery_workers/mt5_setup_tasks.py`, `celery_workers/mt5_sync_tasks.py`, tests)

## User last-active tracking (2026-05-24)

- Added `User.last_active_at` with Alembic migration `20260524_0059_user_last_active_at.py`. Authenticated requests now stamp this field at most once every five minutes, while keeping `last_login_at` as the login-only signal. Static, internal MT5, and read-only support-view traffic are skipped so admin support browsing does not pollute activity data. The admin users table can display and sort by Last active. Audit follow-up: stamp failures log-and-continue instead of failing the request; weekly AI `require_recent_login` now prefers `last_active_at` with `last_login_at` fallback; regression tests cover interval rewrite, session cache, MT5 skip, and login-only separation. (`models.py`, `app.py`, `auth_account.py`, `ai_service.py`, `templates/admin_signup_access.html`, `tests/test_auth.py`, `tests/test_admin_route_gating.py`, `tests/test_ai_service.py`)

## Dashboard AI journal chat UX cleanup (2026-05-17)

- Added an admin access-panel waitlist stat tile that counts distinct waitlist email addresses, so duplicate intent rows for the same person do not inflate the "people on waitlist" number. (`auth_account.py`, `templates/partials/admin_shell_start.html`, `tests/test_admin_route_gating.py`)
- Changed the dashboard AI journal starter from "AI chooses the most likely context first" to "user chooses context before AI sees trade data." The context candidate response now includes full active-account context (currently implemented as the latest closed trades), current review week, parsed trading-day options, and recent specific trades; the UI shows all options immediately instead of hiding them behind "Choose another." Future product intent: gate true full/all-history context and context outside the current week behind paid entitlement once billing is live. (`routes/dashboard.py`, `templates/index.html`, `static/js/dashboard_journal.js`, `tests/test_dashboard_weekly_ai.py`)
- Product note for future AI evidence: add macro/regime context to weekly AI and AI journal when reliable data is available. Start evidence-bounded: tag scheduled high-impact news windows, broad risk-on/risk-off conditions, USD/yields pressure for FX/gold, crypto/index beta, and relevant central-bank/inflation/employment event proximity. The AI should frame this as "macro may have contributed / required different confirmation or risk" rather than confidently claiming macro caused a trade outcome, so it helps users separate setup quality from market regime without becoming a hindsight excuse machine.
- Reworked the dashboard AI journal slide so the inline session reads like an actual chat. The message log and composer are now wrapped in one bordered `.dashboard-journal-chat` container: the log no longer has its own border/scroll box, the "Message" label is gone, and the textarea + Send button sit inline as a composer that auto-grows and supports Enter-to-send / Shift+Enter newline. (`static/js/dashboard_journal.js`, `templates/index.html`)
- Removed the per-message research feedback row (`Useful` / `Generic` / `Missing context` / `Missing feature` buttons plus the Optional note field) from the user-facing dashboard journal. `dashboard_journal.js` marks its session root with `data-hide-feedback` and `admin_journal.js` `createBubble` skips the feedback controls when that flag is set; the standalone `/admin/journal` research page still keeps feedback. (`static/js/dashboard_journal.js`, `static/js/admin_journal.js`)
- Replaced the "Research Signals" side panel (sessions / feedback clicks / missing-data note counts) with a simpler "Recent reflections" list — a short hint plus the recent session rows (now up to 6). Dropped the now-unused `session_count` / `feedback_count` / `missing_signal_count` from `_build_weekly_journal_preview` and the unused `JournalMessage` import. (`routes/dashboard.py`, `templates/index.html`)
- Fixed AI journal session overflow without returning to double-scroll: on desktop the active journal card is height-bounded with hidden outer overflow, while the message log is the single scroll area and auto-scrolls to the newest message. (`templates/index.html`, `static/js/admin_journal.js`)

## Dashboard AI journal + trial copy alignment (2026-05-17)

- AI journal trade payloads now carry the actual strategy, not just its name. `helpers/journal_context._trade_dict` adds `strategy_description` (from `TradeProfileVersion.short_description`) and `strategy_version` alongside the existing `strategy` name, and the prompt-line serializer emits them, so the journal model sees the real strategy text. The trade query already eager-loads `trade_profile_version`, so no extra queries. (`helpers/journal_context.py`)
- Replaced the dashboard AI journal's separate Trade / Day / Open starters with a unified chat-first start flow. Admin users type a natural reflection prompt, `/dashboard/journal/context-candidates` deterministically suggests a trade/day/week/freeform context, and the app only creates the scoped `JournalSession` plus sends the original message after the user confirms. Added `week` journal scope using the New York market-week start stored in `scope_date`; week payloads load only closed trades for the active account inside that market week. Standalone `/admin/journal` scoped forms remain intact. (`models.py`, `helpers/journal_context.py`, `routes/admin_journal.py`, `routes/dashboard.py`, `templates/index.html`, `static/js/admin_journal.js`, `static/js/dashboard_journal.js`, `tests/test_admin_journal.py`, `tests/test_dashboard_weekly_ai.py`)
- Added an admin journal Past Sessions purge action. Admins can permanently delete their own AI journal session rows from `/admin/journal`; related messages are deleted with the session, while cross-admin and non-admin guessed purge attempts still 404. (`routes/admin_journal.py`, `templates/admin_journal.html`, `static/css/admin_journal.css`, `tests/test_admin_journal.py`)
- Fixed trade-scoped AI journal payloads so reflecting on one specific trade only includes that selected trade. The previous same-symbol context behavior could make a BTCUSD reflection show older BTCUSD refs in the context summary, which felt like the wrong trade was opened. Day and freeform scopes still intentionally include multiple trades. (`helpers/journal_context.py`, `tests/test_admin_journal.py`)
- Changed dashboard AI journal reflect sessions to open chat-first: the inline dashboard renderer no longer shows Title/Tags/Notes metadata fields, keeps the scoped context and chat composer, and focuses the message textarea after opening a session. The standalone admin journal research page still keeps metadata fields. Replaced undefined `--line`/`--panel` journal CSS variables with active theme variables and strengthened chat composer/focus styling. (`static/js/dashboard_journal.js`, `static/css/admin_journal.css`, `tests/test_dashboard_weekly_ai.py`)
- Hardened the dashboard AI journal session flow so admin-only inline journal starters return JSON for trade/day/freeform sessions, support-view and non-admin blocks stay JSON/XHR-friendly, and CSRF/HTTP failures no longer collapse into generic frontend "Request failed" copy. Dashboard journal create rejections now log user/scope/error context without message or trade payloads. (`app.py`, `routes/dashboard.py`, `static/js/dashboard_journal.js`, `tests/test_dashboard_weekly_ai.py`, `tests/test_admin_journal.py`)
- Removed customer-facing MT5 slot/scarcity framing from dashboard/public copy. Internal MT5 batch/slot mechanics remain for admin operations and capacity gating, but user-facing states now describe a 14-day premium workflow trial, free core journal, and MT5 setup capacity availability. (`helpers/core.py`, `routes/trade_accounts.py`, `templates/index.html`, `templates/landing.html`, `templates/partials/public_header.html`, `auth_account.py`, public SEO/pricing tests)
- Changed the shared premium workflow trial clock so imports/account creation do not start it. `User.premium_trial_started_at` is now the durable shared start timestamp, backfilled from existing MT5 trial starts; first successful MT5 sync and first successful weekly-review follow-up reply stamp it when needed. MT5 account trial stamps remain for sync pause compatibility. (`models.py`, `helpers/entitlements.py`, `celery_workers/mt5_sync_tasks.py`, `routes/dashboard.py`, `migrations/versions/20260517_0058_premium_trial_started_at.py`, entitlement/dashboard/replay tests)

## Phase 2.5 admin conversational journal MVP (2026-05-11)

- Added the admin-only conversational journal research surface at `/admin/journal`, gated invisibly with `user_has_admin_access` and 404s for non-admin/anonymous access. It supports trade, day, and freeform sessions, a single chat thread per session, session title/tags/notes, optional end-session stamping, and per-assistant-message feedback (`useful`, `generic`, `needed_more_context`, `missing_feature`). (`routes/admin_journal.py`, `templates/admin_journal.html`, `templates/admin_journal_session.html`, `static/js/admin_journal.js`, `static/css/admin_journal.css`)
- Added an admin-only product-context preview for the journal inside the dashboard Weekly AI Review panel: arrow/tab carousel controls switch between the existing weekly review surface and an "AI journal" slide with trade/day/freeform session starters, recent sessions, and research signal counts. Non-admin users do not see the carousel tab. (`routes/dashboard.py`, `templates/index.html`, `static/js/dashboard_page.js`, `tests/test_dashboard_weekly_ai.py`)
- Added lean journal persistence with `JournalSession` and `JournalMessage`, plus Alembic migration `20260512_0057_journal_mvp.py`. The migration chains after the current repo head `20260510_0057_waitlist_intent_fields.py` to avoid an Alembic branch while keeping the journal revision id distinct. (`models.py`, `migrations/versions/20260512_0057_journal_mvp.py`, `tests/test_migration_ordering.py`)
- Added structured journal payload building in `helpers/journal_context.py`: trade scope includes only the selected focal trade with deep market context, day scope summarizes closed trades on the UTC date with minimal market context (hard-capped at 50 trades per UTC day, chronological, with `day_scope_truncation` when truncated), and freeform scope uses the last 20 closed trades on the active trade account with behavior flag counts.
- Added `prompts/journal_chat.txt` and `ai_service` helpers (`load_journal_chat_prompt_text`, `build_journal_chat_messages`, `generate_journal_chat_reply`) that mirror the weekly review follow-up chat pattern while keeping scope/refusal/missing-context/interview behavior in the prompt.
- Regression coverage added for admin gating, session creation by scope, prompt-file loading, chat persistence/citations, feedback writes, tags/notes saves, session ending, admin route inventory, day-scope payload cap, cross-admin / non-admin isolation on journal routes, and dashboard carousel visibility. Full local suite is clean: `650 passed, 5 skipped`.

## Premium workflow trial gating (2026-05-11)

- Broadened the 14-day trial semantics from MT5-only to a shared premium workflow trial in `helpers/entitlements.py`. As of 2026-05-17, `get_trial_state(user, account=None)` reads the shared `User.premium_trial_started_at` timestamp first, falls back to existing MT5 trial storage for compatibility, and does not use user creation/import time as a trial start; `get_mt5_trial_state()` remains as a compatibility wrapper.
- Weekly review generation/display remains core/free. Weekly review follow-up chat now uses entitlement helpers, allows 5 user-sent trial follow-up messages, blocks new sends after the cap or after trial expiry with waitlist CTA metadata, and keeps existing chat history visible in the dashboard. Assistant replies are not counted toward the cap. (`helpers/entitlements.py`, `routes/dashboard.py`, `templates/index.html`, `static/js/weekly_review_chat.js`, `tests/test_dashboard_weekly_ai.py`)
- Advanced replay gating now follows the shared trial/premium gate: M5/M15 standard replay stays available, active-trial/Trader/Pro/grandfathered users can request advanced timeframes, and currently unimplemented M1 still returns `timeframe_not_available` instead of success. (`helpers/entitlements.py`, `routes/trades.py`, `tests/test_trades_routes.py`)
- MT5 sync still uses the same trial window and pause behavior; on first successful sync, the MT5 account stores the broader trial start when available instead of extending the clock. Grandfathered/admin/paid-plan paths remain safe. (`celery_workers/mt5_sync_tasks.py`, `tests/test_entitlements.py`, `tests/test_mt5_access_requests.py`)
- Landing, SEO, pricing, register, dashboard, and Trade Accounts copy now frame this as a 14-day premium workflow trial with no credit card required, while keeping the core journal/free weekly review language clear and avoiding checkout/billing enforcement claims. (`templates/base.html`, `templates/landing.html`, `templates/pricing.html`, `templates/register.html`, `templates/seo_page.html`, `templates/trade_accounts.html`)

## Public SEO pricing endpoint fix (2026-05-11)

- Fixed the shared SEO page pricing/waitlist CTA to use the registered `pricing_page` Flask endpoint instead of stale `pricing`, preventing BuildError 500s on public SEO routes such as `/revenge-trading-journal`. Added a regression check for the revenge-trading journal landing page and its `/pricing` link. (`templates/seo_page.html`, `tests/test_public_seo.py`)

## Free-trial expiry email (2026-05-11)

- Added a general free-trial expiry email template that frames the message as an account-status notice first, explains that existing journal data remains available, and links to `/pricing` for paid access / waitlist options with restrained upsell copy. (`templates/emails/free-trial-expired.html`)
- MT5 trial expiry now sends this email once when the sync worker first stamps `sync_paused_at` because entitlement reason is `expired`; existing paused accounts are not re-emailed by beat because paused accounts are excluded from scheduling. (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`, `tests/test_email_templates.py`)
- Removed the `BILLING_LAUNCH_DATE` enforcement switch. MT5 sync trial expiry is enforced as soon as a non-grandfathered Free user's 14-day MT5 sync trial is expired; the reactivation path is the pricing/waitlist page even before paid billing is live. (`helpers/entitlements.py`, `templates/pricing.html`, `tests/test_entitlements.py`)
- Current limitation: MT5 sync is still the only feature with a concrete trial-expiry transition. Other gated features use entitlement checks but do not yet have an account-level trial clock or expiry event to trigger this email.

## Pricing page polish (2026-05-11)

- Raised the pricing hero by reducing top padding and made the main headline larger/wider for stronger first-viewport emphasis.
- Fixed the featured Trader "Most popular" badge being clipped by card overflow; the badge now sits above the card with its own stacking/shadow.
- Sharpened pricing tier contrast with per-tier card/panel accents for Free, Trader, and Pro, and improved roadmap/planned item contrast with warmer planned badges. (`templates/pricing.html`)
- Moved Trader and Pro planned features out of the tier cards into one shared roadmap panel below the pricing grid, with separate Trader and Pro lanes so the cards stay focused on current tier value. (`templates/pricing.html`)
- Rebalanced desktop pricing card heights after the shared roadmap extraction: Free limitations are shorter, side cards share a baseline height, and Trader remains slightly larger as the featured tier. (`templates/pricing.html`)
- Removed forced pricing-card min-heights that created blank empty areas, and added compact "Best fit" panels to Trader and Pro so card balance comes from useful content instead of empty vertical space. (`templates/pricing.html`)

## Landing hero + pricing layout fixes (2026-05-11)

- **Landing hero**: Removed the `hero-availability` scarcity card (slot-count, batch-open/closed states, animated conic border) and all its CSS. Replaced with a clean `hero-mt5-trial` section that conveys: "MT5 sync trial — 14 days free, no card required. Import trades first and sync when ready. Trial pauses after 14 days unless you upgrade later." No scarcity framing. Card is only shown to logged-out visitors. (`templates/landing.html`)
- **Pricing page layout**: Fixed desktop layout that was rendering at mobile/tablet widths due to `base.html`'s global `main { max-width: 760px; margin: 4rem auto; }` overriding the full-width layout. Added `body.landing-layout main` override (removes the 760px cap), `.landing-glass` background gradient/blur layer, and proper `.pricing-wrap` padding (`clamp(0.8rem, 3vw, 2rem)`) to match landing page spacing. (`templates/pricing.html`)
- **Pricing page theming**: Replaced all `--text-primary` / `--text-muted` / `--text-secondary` custom properties (undefined in the dark theme, falling back to light-mode hex colors → near-invisible text) with the correct design system variables: `var(--ink)`, `var(--muted)`. Updated hardcoded accent/success/error hex values (`#3658e8`, `#16a34a`, `#f0fdf4` etc.) to use `var(--accent)`, `var(--good)`, `var(--bad)` and `color-mix` equivalents. (`templates/pricing.html`)

## Monetization rollout final hardening (2026-05-11)

- Replay chart API no longer normalizes unsupported timeframe requests before gating. Free users requesting `M1` now receive a 403 `upgrade_required` JSON response with `/pricing` waitlist CTA metadata; Trader/Pro users requesting `M1` receive a clean 501 `timeframe_not_available` response until 1m replay is actually implemented. Returned `available_timeframes` only lists implemented + entitled UI options (`M5`, `M15` for now). (`routes/trades.py`, `tests/test_trades_routes.py`)
- Explicit MT5 pause state is now respected outside the sync worker: Beat excludes accounts with `sync_paused_at`, shared MT5 access state does not treat paused accounts as active, and dashboard/trade-account templates render `Sync Paused` instead of `Connected`. Grandfathered beta MT5 access now has a lightweight visible indicator. (`helpers/entitlements.py`, `helpers/core.py`, `celery_workers/mt5_sync_tasks.py`, `routes/dashboard.py`, `templates/index.html`, `templates/trade_accounts.html`, `tests/test_mt5_access_requests.py`)
- MT5 sync regression pass restored live open-position `profit` -> outbound `pnl` propagation, stale-history diagnostics, soft reconnect on stale broker history, compact benign noop logging, and chunked history helper coverage. The obsolete Redis global-lock skip expectation was aligned to the current per-account lock design. (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`)
- Pricing copy now frames 1m replay, review archives, multi-timeframe replay, multiple MT5 accounts, annual billing, and refund terms as planned/future rollout instead of live/legal commitments before billing exists. (`templates/pricing.html`, `auth_account.py`)
- Waitlist duplicate handling now enriches an existing row with `user_id` and missing `cta_context` when a later duplicate submission is authenticated. `/pricing/waitlist` rate-limit responses are JSON with `ok: false`. The waitlist intent migration now documents its PostgreSQL-specific duplicate-cleanup/constraint caveat for local SQLite. (`auth_account.py`, `app.py`, `migrations/versions/20260510_0057_waitlist_intent_fields.py`, `tests/test_pricing_waitlist.py`)

## Legal pages — Terms & Privacy pass-through (2026-05-11)

- Updated `templates/terms_and_conditions.html` and `templates/privacy_policy.html` so disclosures match current product behavior: MT5 sync vs file import, behavioral/coaching-hypothesis analytics, persisted weekly AI, review-scoped conversational chat threads, profile/check-in context, waitlist and phased rollout language, and subscription/payment expectations (processor-handled cards, no enterprise compliance claims).
- `helpers/legal.py` `LEGAL_LAST_UPDATED` set to **May 11, 2026** (also used for MT5 consent version stamping).

## SEO / indexing consistency (2026-05-11)

- Canonical hygiene: production-style bases use `https://` when the app is not in local dev and the host is not loopback; paths normalize to no trailing slash except `/`. Default template canonicals run through `build_external_url(normalize_public_path(...))` in `app.py`.
- Duplicate legal URLs: `GET /privacy-policy` → 301 `/privacy`; `GET /terms-and-conditions` → 301 `/terms`.
- `robots.txt`: disallow `/auth/`, `/session/`, and `/verify-email` prefix; `/register` stays crawlable.
- Sitemap: single source `SEO_SITEMAP_PATHS` (includes `/pricing` and all SEO landings); matches page-level canonical URLs.
- `base.html`: `noindex` for any `/auth/*` HTML response path.

## Phase 1 + Phase 2 hardening pass (2026-05-11)

- Added schema-compat safety for partially migrated environments:
  - `User.plan_tier` / `User.plan_grandfathered` and `MT5Account.mt5_trial_started_at` / `sync_paused_at` / `sync_pause_reason` are ORM-deferred so authenticated non-entitlement pages (for example `/pricing`) do not hard-fail when Phase 2 columns are missing.
  - New `helpers/schema_compat.py` introspects DB columns with cache; entitlement helpers fail open (`schema_compat`) when those columns are absent so legacy environments avoid 500/blocking behavior until migrations are applied.
- Waitlist intent tracking extended:
  - `UpgradeWaitlistEntry` now stores `source`, `feature_interest`, and optional `cta_context`.
  - New migration `20260510_0057_waitlist_intent_fields.py` adds fields, backfills existing rows, deduplicates existing duplicates, and enforces unique key `uq_upgrade_waitlist_email_tier_source_feature`.
  - `/pricing/waitlist` now accepts + validates intent fields, applies defaults, rate-limits submissions (`5/min;40/hour`), and handles duplicate races gracefully via unique constraint + `IntegrityError` rollback.
- Public header/nav consistency:
  - Added reusable shared public header partials: `templates/partials/public_header.html`, `public_header_styles.html`, `public_header_script.html`.
  - Landing, SEO pages, and pricing now render the same shared header/navigation component.
  - Mobile navigation now has a lightweight toggle menu instead of removing nav entirely on small screens.
- Added regression coverage:
  - `tests/test_public_seo.py`: authenticated `/pricing` render test asserts user queries do not select Phase 2 entitlement columns (pre-0056 compatibility guardrail).
  - `tests/test_pricing_waitlist.py`: intent storage + duplicate-submission graceful handling.
  - `tests/test_migration_ordering.py`: explicit migration chain checks for `0055 -> 0056 -> 0057`.
- Deploy sequencing clarified: when shipping pricing/waitlist + entitlement code, run migrations in order up to `20260510_0057` before or with app deploy (`0055` waitlist table, `0056` entitlement columns, `0057` waitlist intent + unique key) to keep all environments aligned.

## Phase 2 — Entitlement / Gating Infrastructure (2026-05-10)

Central entitlement system, MT5 14-day trial enforcement, replay timeframe gating, and VM pause-on-expiry.

**New files:**
- `helpers/entitlements.py` — all plan-gating decisions. Constants: `MT5_TRIAL_DAYS=14`, `FREE_REPLAY_TIMEFRAMES={M5,M15}`, `TRADER_REPLAY_TIMEFRAMES={M1,M5,M15}`, `FREE_REPLAY_MAX_TRADE_AGE_DAYS=90`. Helpers: `get_user_plan_state`, `is_mt5_sync_paused`, `get_mt5_trial_state` (states: grandfathered/not_started/active/expired/paused), `can_use_mt5_sync`, `can_access_replay_timeframe`, `can_generate_replay_bars`, `get_replay_entitlement`. Admin bypass via `_is_admin → user_has_admin_access`. Grandfathered users get Trader entitlement throughout.
- `migrations/versions/20260510_0056_entitlement_fields.py` — adds `plan_tier`/`plan_grandfathered` to `users`; adds `mt5_trial_started_at`/`sync_paused_at`/`sync_pause_reason` to `mt5_account`. Data upgrade sets `plan_grandfathered=TRUE` for all users with `last_synced_at IS NOT NULL` (existing beta users grandfathered silently).
- `tests/test_entitlements.py` — 37 tests, all passing. Uses `SimpleNamespace` mocks; no DB fixtures.

**Modified files:**
- `models.py` — `User.plan_tier` (String 32, default "free"), `User.plan_grandfathered` (Boolean, default False); `MT5Account.mt5_trial_started_at`, `MT5Account.sync_paused_at`, `MT5Account.sync_pause_reason` (String 64).
- `celery_workers/mt5_setup_tasks.py` — new `pause_mt5_terminal_process` Celery task. Uses `_terminate_mt5_processes(terminal_path)` only (no file deletion, no `cleanup_mt5_terminal`). Sets `sync_paused_at` if not already set.
- `celery_workers/mt5_sync_tasks.py` — trial guard after `is_orphaned` check; stamps `mt5_trial_started_at` on first successful sync for non-grandfathered users.
- `routes/trades.py` — `trade_chart_data()` returns 403 `upgrade_required` for Free user requesting M1, returns 501 `timeframe_not_available` for Trader/Pro requesting M1 before implementation, and filters `available_timeframes` to implemented + plan-allowed UI timeframes.
- `routes/mt5_internal.py` — `_queue_auto_trade_bar_sync()` skips trades outside Free-tier age limit via `can_generate_replay_bars`.
- `routes/trade_accounts.py` — computes `mt5_trial_states_by_trade_account` dict passed to template.
- `templates/trade_accounts.html` — trial state badge: days-remaining (green/amber), expired soft CTA, not-started notice.

**Critical invariants:**
- MT5 trial expiry is enforced now: a non-grandfathered Free user's MT5 sync pauses after `MT5_TRIAL_DAYS=14`; explicit `sync_paused_at` blocks scheduling/sync until pause state is cleared
- Grandfathered path bypasses all trial logic; existing beta users are unaffected
- Trial pause ≠ archive: `sync_paused_at`/`sync_pause_reason` are entirely separate from `archived_at`/`archive_reason`/`cleanup_mt5_terminal`
- M5/M15 replay is never gated for any user; M1 gating is forward-compatible (M1 not yet stored)

## Dashboard trends changed to week-on-week comparisons (2026-05-10)

- The dashboard trend panel is now labeled "Week-on-week" and compares this dashboard week's win rate and expectancy directly against the previous week. Expectancy uses realized PnL per closed trade, and the panel shows a limited-sample note when either side has fewer than `SMALL_SAMPLE_MIN_TRADES` closed trades. Behaviour now compares objective trade-behaviour pressure from this week's closed trades against the previous week's closed trades, using the same scoring engine as weekly AI but without waiting for saved weekly review payloads. Focused tests cover the worse-current-week expectancy case and the current-week behaviour deterioration case. (`routes/dashboard.py`, `templates/index.html`, `tests/test_dashboard_weekly_ai.py`)

## MT5 sync completion heartbeat stamp (2026-05-04)

- MT5 sync workers now directly stamp `MT5Account.last_synced_at` after a full successful worker run: MT5 connected, history/open positions were read, and the internal sync API accepted the payload. This makes "Last synced" a completed-task heartbeat as well as an ingest success marker, so stale timestamps distinguish tasks that are not finishing from successful zero-change/skip-only sync runs. Full-history worker completions also stamp `last_full_history_sync_at`. (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`)

## Cookie banner dismissal fix (2026-05-04)

- Fixed the analytics cookie banner so `hidden` reliably removes it after Accept or Decline. The banner CSS now explicitly hides `#cookie-banner[hidden]`, and consent storage access is guarded so blocked browser storage does not keep the banner stuck on the current page. (`templates/base.html`)

## Weekly AI bounded coaching hypotheses (2026-05-03)

- Added a computed coaching-hypothesis layer between weekly signals and AI review copy. `helpers/weekly_coaching_hypotheses.py` now emits ranked `current_week_breakdowns.coaching_hypotheses` objects for `outcome_disguised_habit`, `post_loss_decision_shift`, `single_trade_masked_week`, and `session_edge_disguised_as_skill`, with evidence refs, confidence, bounded facts, false/better lesson hints, and prompt instructions.
- Weekly review payloads now include those hypotheses, and the dashboard advice prompt/JSON contract tells the model to use at most one as framing while not inventing traps, motives, danger windows, or false lessons beyond the computed facts/hints.
- Focused tests cover hypothesis detection, weekly payload composition, prompt formatting, and prompt contract language. (`helpers/weekly_coaching_hypotheses.py`, `helpers/weekly_signals.py`, `ai_service.py`, `prompts/dashboard_advice.txt`, tests)
- Follow-up audit fixes: `outcome_disguised_habit` now requires a supporting ranked issue or `revenge_evidence.pattern_class` of `isolated`/`repeated`, dominant-trade fallback keeps the replacement ref and symbol aligned, and timing facts are exposed as `retry_timing_range_minutes` rather than pre-labeling them as a danger window. Tests cover the guard and fallback cases.
- Follow-up mechanism pass: `outcome_disguised_habit` now carries an explicit contrast pair (`habit_rewarded_by_*`, `habit_exposed_by_*`), `mechanism_hint`, `what_the_trader_may_have_mislearned`, and `contrast_instruction`. Prompt rules now require the model to explain which trade rewarded the habit, which trade exposed it, and to avoid generic-only "winning retry is unsafe" phrasing.
- Follow-up mechanism audit: the contrast pair now selects the biggest winning retry as the habit reward and the worst losing retry as the habit exposure, instead of first/last chronological retries. `_hypothesis` rejects `extra_fields` that would overwrite core keys, and prompt rules require both sides of the contrast plus the sequence mechanism rather than allowing mechanism-only generic wording.
- Follow-up copy-shape fix: `outcome_disguised_habit` now exposes `writing_shape=reward -> cost -> mislesson -> better_lesson`, and prompt rules tell the model to use that shape instead of flattening the point into "the problem is the decision after the loss." Weekly review normalization also dedupes repeated `You're already strong at:` wording in structured/display text, and experiment guidance now blocks same-symbol-cap repeats when the improvement already covers same-symbol re-entry.
- Follow-up structure/generalization pass: Pass-1 prompt rules for `outcome_disguised_habit` spell out a five-step copy flow (name rewarded trade, explain reinforcement, name exposed trade, explain contradiction, state corrected rule). Prompt rules forbid restating the same mechanism twice and require refs to appear inside sentences rather than as trailing fragments. Dashboard citation rendering now drops unmatched refs instead of appending orphan pills, and tests cover clean/noisy/single-big-winner generalization fixtures.

## Weekly AI execution/outcome archetype rail (2026-05-03)

- Weekly AI payloads now include a deterministic `execution_outcome` signal under `current_week_breakdowns`, classifying the week by realised outcome and observable execution quality before the model writes: good execution/good outcome, good execution/bad outcome, leaky or bad execution/good outcome, bad execution/bad outcome, or random/unclear execution.
- The archetype gives pass 1 a coaching stance such as `full_praise`, `protect_confidence`, `good_week_but_habits_are_leaking`, `direct_correction`, or `measure_first`, while the prompt explicitly forbids showing those internal labels to the trader. Concentrated outlier results are treated as unclear rather than bad execution so valid "let winners run" weeks are not penalized automatically, and 2-3 trade weeks are not downgraded solely for sample size.
- The pass-1 weekly review prompt now uses `execution_outcome` as a tone/structure rail before choosing the final diagnosis, so profitable weeks with leaks can warn without overcorrecting and losing weeks with clean process can protect confidence. (`helpers/weekly_signals.py`, `ai_service.py`, `prompts/dashboard_advice.txt`, tests)
- Follow-up tightening: single confirmed revenge evidence now remains `isolated`; repeated revenge requires at least two confirmed or strong sequences, preventing one tagged mistake from forcing direct-correction tone. Concentrated outlier weeks only become `measure_first` when no process leak is present, and the prompt now documents `leaky_execution_bad_outcome` as light correction. Tests pin same-symbol/same-idea re-entry counts when those serialized fields are present. (`helpers/weekly_signals.py`, `prompts/dashboard_advice.txt`, tests)
- Follow-up ranker: `execution_outcome` now includes `primary_issue`, `primary_issue_hint`, ranked issue metadata, and `do_not_lead_with` so the weekly review has a deterministic lead signal instead of letting the model pick from equal-looking surface stats. The prompt treats outlier concentration as context when a stronger process leak exists, while still leaving causal interpretation, citation choice, and final coaching copy to the model. (`helpers/weekly_signals.py`, `ai_service.py`, `prompts/dashboard_advice.txt`, tests)
- Follow-up flat/isolated calibration: flat clean weeks now use a neutral hold-steady archetype instead of the losing-week confidence-protection lane; flat weeks with leaks get flat-specific correction labels. `execution_outcome.issue_evidence_level` now exposes `none` / `isolated` / `moderate` / `strong` so the prompt frames one revenge signal as a watch item while repeated evidence can stay direct. (`helpers/weekly_signals.py`, `ai_service.py`, `prompts/dashboard_advice.txt`, tests)

## Project map synced with current state (2026-05-02)

- `context/PROJECT_MAP.md` was refreshed from the accumulated `CURRENT_STATE.md` entries into the durable project map: product surface, Render/Hyonix architecture, 30s MT5 queue model, MT5 setup/sync/bar lifecycle, weekly AI + follow-up chat, running PnL/cash flows, admin/support/legal/growth surfaces, key file ownership, operational guardrails, known weak points, and current next priorities.

## Weekly review follow-up scope and dynamic questions (2026-05-02)

- `prompts/weekly_review_followup.txt` now scopes every chat turn to the active weekly review and its trades, anchors answers to the review's main insight, requires concise evidence-backed explanations, redirects off-topic questions back to the review, and requires fresh 4-5 follow-up questions generated from the actual insight/evidence on every response.
- Follow-up chat can now cite the same weekly-review trade refs internally: the prompt receives a trade-link ref map, the model may emit refs like `[T1]` after natural trade phrases, the route returns clean reply text plus citation segments, and dynamically added chat citation pills reuse the dashboard trade/bundle highlight behavior. (`ai_service.py`, `routes/dashboard.py`, `static/js/weekly_review_chat.js`, `static/js/dashboard_page.js`, tests)
- Follow-up prompt tone was softened so answers stay chat-like while keeping the same scope and evidence guardrails: plain truth first, natural "you" language, and a compact "You could ask:" set for dynamic next questions. (`prompts/weekly_review_followup.txt`)
- The dashboard's visible "Ask about this review" preset bubbles are now generated from the displayed weekly review insight/evidence (issue type plus cited trade/bundle label when available). Legacy/static prompts remain as fallback when an older review lacks usable dynamic display context. (`routes/dashboard.py`, `templates/index.html`, `tests/test_dashboard_weekly_ai.py`)

## Weekly AI prompt synthesis-flow tightening (2026-05-02)

- `prompts/dashboard_advice.txt` now pushes the model to choose the likely week-level diagnosis before selecting metrics, with context signals (timing, range location, session/volatility, post-exit behavior, sequence, exit handling, risk authority) weighted above candle microstructure.
- Risk interpretation now avoids lot-size inference and treats unavailable/ambiguous risk as a reason to use stronger available evidence rather than output filler caveats. The former end-of-prompt hard-prohibition/self-check blocks were folded into the core evidence, thinking, writing, and output flow, and the AI service prompt contract test was updated to pin the new structure. (`prompts/dashboard_advice.txt`, `tests/test_ai_service.py`)
- Follow-up prompt pass: pass 1 now makes "What this suggests" bullets implication-first instead of raw event recaps, and citation rules caution against attaching multiple same-looking refs to one short same-symbol sequence sentence. Pass 2 now explicitly cleans vague wording such as "pressed the same idea" without using scenario-specific examples that could bias the model. (`prompts/dashboard_advice.txt`, `prompts/dashboard_advice_rewrite.txt`, `ai_service.py`, `tests/test_ai_service.py`)
- Weekly review pass 1 now asks for at least one representative cited trade or bundle when a safe example exists, with a second citation reserved for useful contrast. The JSON output instructions mirror this so reviews feel grounded without turning every pattern bullet into citation clutter. (`prompts/dashboard_advice.txt`, `ai_service.py`, `tests/test_ai_service.py`)

## Dashboard fine-print tooltip cleanup (2026-05-02)

- Dashboard left-rail fine print is now tucked into accessible question-mark help bubbles instead of always-visible paragraphs. The MT5 sync card keeps the progress rail, account, and main action visible while moving status detail, setup capacity, linked-account explanation, and disconnect consequences into tooltips. Rolling trends and latest closed trade also use the shared tooltip pattern for their explanatory subtitles. (`templates/index.html`, `static/js/mt5_request_form.js`)

## Weekly AI evidence-first insight prompt rewrite (2026-05-02)

- Weekly dashboard AI prompt now forces one main coaching insight instead of scattered recap points: the opening "What mattered this week" read starts with a human conclusion, anchors it to a count or concrete trade example, and combines at least two signal families when available.
- Entry candle and other candle-level fields are now framed as supporting evidence only; timing, range location, session/volatility context, post-loss/re-entry sequence, exit handling, and risk authority drive the diagnosis. The JSON output instruction in `ai_service.py` mirrors the same evidence and structure requirements so pass-1 generation cannot drift back to metric summaries.
- The prompt's output mapping now explicitly aligns `summary`, `takeaways`, `improvement`, `strength`, and `experiment` with the dashboard sections: "🧠 What mattered this week", "What this suggests", "🎯 Actionable Improvement", "✅ Strength to Reinforce", and "🧪 This Week's Experiment". (`prompts/dashboard_advice.txt`, `ai_service.py`)

## Extended bar-derived trade context (2026-05-02)

- Weekly AI payloads now include 9 additional bar-derived context fields per trade (no external dependencies): `entry_active_sessions` (list of sessions active at entry — DST-aware via pytz; London/NY overlap shifts ~1 hour between winter/summer), `entry_in_session_overlap` (true when 2+ sessions active), `large_candle_before_entry` (true if any of the 3 M5 bars before entry had range >2x prior median — flags possible chase entries), `entry_bar_body_ratio` (body/range ratio of entry candle, 0–1), `entry_bar_closes_in_trade_direction` (entry candle closed with or against trade direction), `pre_entry_bars_in_trade_direction` (consecutive same-direction M5 bars before entry — momentum signal), `entry_tick_volume_vs_median` (entry bar tick volume relative to prior median), `post_exit_price_move` (raw price change from exit in first 12 post-exit bars), `post_exit_direction` ("continued"/"reversed"/"flat" — whether price moved favorably or against trade direction after exit). `_bar_dict` now includes `tick_volume` from stored rows. Aggregate breakdown in weekly payload adds `large_candle_entry_count`, `entry_bar_against_direction_count`, `post_exit_continued_count`, `post_exit_reversed_count`. Prompt updated with interpretation guardrails for each new field. (`helpers/ai_market_context.py`, `ai_service.py`, `prompts/dashboard_advice.txt`, tests)

## Weekly AI bar-derived market context and stop-management clues (2026-05-02)

- Weekly AI payloads now enrich each reviewed trade with stored M5 bar context when available: in-trade/post-exit bar counts, MFE/MAE price movement, optional MFE/MAE in R when the recorded SL is still on the initial-risk side, entry candle range vs recent prior bars, entry location within the prior range, and whether stored post-exit bars reached TP/SL after the recorded exit.
- Weekly AI payloads also include cautious stop-management clues from the stored SL and system/import note text. A final SL beyond breakeven/profit is marked as a protective stored stop, and system notes such as breakeven/trailing/moved-stop language raise confidence; the prompt forbids claiming the user moved/trailing-stopped unless evidence supports it. Current-week breakdowns summarize bar coverage, post-exit TP reaches, protective stops, and trailing/breakeven-stop clues. (`helpers/ai_market_context.py`, `ai_service.py`, `prompts/dashboard_advice.txt`, tests)

## MT5 chart-bar post-close completion check (2026-05-01)

- MT5 trade-bar coverage checks now treat stored M5 bars as incomplete until they cover the intended chart tail: from around entry through `min(closed_at + 144 M5 bars, now)`, with the existing 15-minute tolerance. This means automatic MT5 sync, worker-side skip logic, and admin backfill continue refetching recently closed/partially backfilled trades until the 12-hour post-close context is present instead of stopping as soon as bars reach the close time. Shared constants/helper live in `helpers/trade_bars.py`; bar fetch still stores M5 only and derives M15 in the chart API. (`helpers/trade_bars.py`, `routes/mt5_internal.py`, `celery_workers/mt5_sync_tasks.py`, `auth_account.py`, tests)

## Weekly AI diagnosis pass and strategy context (2026-05-01)

- Weekly dashboard AI pass 1 is now framed as a coaching diagnosis rather than a report: the prompt tells the model to connect evidence to a decision habit, compare possible diagnoses internally, identify likely user misunderstanding, and produce a next behavior rather than category advice. Strategy/playbook context has explicit guardrails: use it as intent context, not proof of plan-following or setup quality.
- Weekly AI payloads now include attached strategy/playbook context per trade (`strategy_name`, `strategy_version`, `strategy_description`) and a current-week strategy breakdown/coverage summary. Trade payload queries eager-load strategy relationships to avoid N+1 access during review generation. (`ai_service.py`, `prompts/dashboard_advice.txt`, tests)

## Weekly AI plain-English rewrite guardrails (2026-05-01)

- Weekly dashboard AI pass 2 is now framed as a plain-English readability pass rather than a shortening/compression pass. The rewrite prompt explicitly says simpler does not mean shorter, asks for complete natural sentences, bans awkward compressed labels such as "London/New York idea", and forbids removing/renaming/reordering trade refs or existing trade labels.
- The pass-1 dashboard advice prompt now also nudges toward globally understandable English and avoids compressed trade/session/date labels before the rewrite pass runs.
- Dashboard rendering now carries original structured summary/takeaway citations onto pass-2 text, preserving trade hyperlinks even when the rewrite omits visible `T1`/`B1` refs or uses more generic wording. If pass 2 changes the takeaway count, the display falls back to the original structured review to avoid misattached evidence. (`prompts/dashboard_advice.txt`, `prompts/dashboard_advice_rewrite.txt`, `routes/dashboard.py`, tests)

## Landing comparison simplification (2026-05-01)

- Public landing comparison/positioning section now uses a shorter "lesson, not another platform" message, a tighter trial card, and simplified competitor cards with one positioning sentence plus two tags instead of repeated multi-row feature labels. Follow-up copy sharpens the moat around "All the review. None of the overhead." while positioning competitor options as heavier, higher-friction workflows without changing pricing claims. Existing pricing/date caveat remains, but the explanatory footnote was shortened. No backend behavior or CTA routes changed. (`templates/landing.html`)

## Runtime env loading and AI model normalization (2026-05-01)

- Flask web startup now uses the same runtime env-file loading path as Celery: `FXJ_ENV_FILE` first, then `.env`, `FXJournal Main.env`, and `fxjournal.env`, without overriding already-set environment variables. This keeps AI routes in the web process aligned with worker-side config.
- `AI_MODEL` values are normalized before OpenAI requests: surrounding matching quotes are stripped and internal whitespace is converted to hyphens, so accidental values like `gpt-5.4 mini` resolve to `gpt-5.4-mini`. Explicit model IDs are otherwise left unvalidated to avoid stale allowlists. OpenAI request start/failure logs now include the resolved model string so deployment-service logs can confirm what the app actually sent. (`helpers/runtime_env.py`, `app.py`, `celery_app.py`, `ai_service.py`, tests)

## Dashboard latest closed trade snapshot (2026-04-26)

- Follow-up: compact latest-trade chart default y-scale now centers around entry/exit/SL/TP plus realistic in-trade candles instead of all stored bar extremes, avoiding flattened BTC-style charts when the stored window has outlier highs/lows. The widget is slightly taller and includes tighten/widen/reset scale controls plus price-axis drag support. (`templates/index.html`, `static/js/dashboard_latest_trade_chart.js`)
- Follow-up: the latest-trade chart now anchors its default y-axis directly to the entry and exit prices, using the entry-to-exit move as the readable gauge before any candle extremes are considered. This keeps the first render focused on the actual trade result while the widen control remains available for broader context. (`static/js/dashboard_latest_trade_chart.js`)
- Follow-up: latest-trade snapshot chart now includes 5m/15m timeframe switching using the existing chart-data endpoint, resets to the trade-focused y-scale on each switch, and disables unavailable timeframes based on API metadata. (`templates/index.html`, `static/js/dashboard_latest_trade_chart.js`)
- Follow-up: mouse wheel over the latest-trade chart now zooms the chart instead of scrolling the dashboard behind it. (`static/js/dashboard_latest_trade_chart.js`)
- Dashboard left rail now fills the post-MT5/trends space with a "Latest closed trade" snapshot for the active account. It shows symbol/side, realized PnL, closed time, session, duration, and a Review link, with an empty state until the account has a closed trade.
- Closed MT5 trades with synced bars render a compact Lightweight Charts candlestick view using the existing `/api/trades/<pubkey>/chart-data` endpoint and entry/exit/SL/TP markers; manual trades or MT5 trades without bars fall back to a status message. (`routes/dashboard.py`, `templates/index.html`, `static/js/dashboard_latest_trade_chart.js`)

## Admin-gated public automatic MT5 chart bars (2026-04-26)

- Added a root-admin MT5 panel switch for automatic chart-bar sync for public users. The setting is persisted in `app_settings` (`20260426_0054`) and defaults off.
- When enabled, every successful internal MT5 trade ingest scans that MT5 account's closed MT5 trades for missing complete M5 coverage and queues bar fetch work, including normal non-admin/public users and older closed trades not present in the current rolling broker payload. Automatic dispatch is routed to `mt5_priority`, capped per sync, and visible in the VM sync result table as `Auto Bar Tasks`; manual Backfill Bars remains available. (`auth_account.py`, `routes/mt5_internal.py`, `celery_workers/mt5_sync_tasks.py`, `templates/admin_signup_access.html`, `helpers/app_settings.py`, tests)
- Follow-up diagnosis/fix: `fetch_trade_bars` now tries broker-visible CFD symbol candidates from `cfd_mt5_symbol_name_candidates()` and best-effort `symbol_select()` before `copy_rates_range`, then logs the broker symbol that worked. This covers suffix/alias servers where the journal stores canonical symbols but MT5 exposes names such as broker-suffixed FX/metals or `GOLD`. (`celery_workers/mt5_sync_tasks.py`, tests)
- Follow-up dispatch visibility: automatic bar fetches now route to `mt5_priority` so the solo VM sync worker should consume them before normal beat syncs, and beat sync responses/logs include an `auto_bar_sync` diagnostic object (`enabled`, `closed_trades`, `missing_m5`, `queued`, `capped`, `max_tasks_per_sync`, `queue`) even when no bars are queued. (`routes/mt5_internal.py`, `celery_workers/mt5_sync_tasks.py`, tests)
- Follow-up dispatch control: removed the per-trade Redis debounce lock for automatic bar fetches. Each sync now queues at most 20 missing-bar tasks per MT5 account, making progress visible without silently suppressing dispatch. (`routes/mt5_internal.py`)
- Follow-up batching: automatic bar sync now queues one `fetch_trade_bars_batch` task per account sync with up to 20 trade IDs. The VM worker skips trades whose complete M5 bars are already stored before logging into MT5, opens one MT5 session for the remaining trades, and POSTs all fetched bars once to `/api/internal/mt5/trade-bars/batch`. The single-trade endpoint/task remain for manual compatibility. (`routes/mt5_internal.py`, `celery_workers/mt5_sync_tasks.py`, `celery_app.py`, tests)

## Landing section order pass (2026-04-26)

- Follow-up fix: collapsed the old hero viewport-height reserve after moving the proof out of the hero, removing the large gap between the hero CTA row and the heavy-lifting panel. The Weekly AI Review proof section now uses the same liquid-glass outer section treatment as the other landing panels. (`templates/landing.html`)
- Public landing order now follows the newer narrative: hero/relief/CTA first, then "The journal does the heavy lifting", then the Weekly AI Review proof, then the simplified Capture/Understand/Act features section. The AI proof block was moved out of the hero and placed after the heavy-lifting section; its content and static landing-only chat/experiment preview were kept. No backend logic, nav, CTA copy, or feature copy changed. (`templates/landing.html`)

## Landing features simplification pass (2026-04-26)

- Public landing "What's Live Right Now" / features section now leads with "Built so you actually stick with it." and a commented alternative, then replaces the previous dashboard/analytics/import/MT5-sync card spread with three outcome groups: Capture, Understand, and Act. Copy is shortened to 2-3 line blocks, AI chat is positioned as part of the Act step, and the trade replay panel is simplified to "See your trades on the chart" with the existing replay screenshot kept. Hero, above-the-fold proof, navigation, CTAs, SEO structure, and backend logic were not changed. (`templates/landing.html`)

## Landing first proof review focus pass (2026-04-26)

- Reworked the translucent weekly-review back panel from a pseudo-element into a real content wrapper so the panel starts below the proof heading instead of overlapping the title/subtitle. Restored dashboard-style emoji cues on the review, warning, improvement, strength, experiment, and ask labels. (`templates/landing.html`)
- Added static landing-only versions of the dashboard "This week's experiment" and "Ask about this review" panels beneath the moved weekly-review proof, including example quick prompts and an inert ask input. No chat/backend routes were wired. (`templates/landing.html`)
- Follow-up visual refinement: the moved weekly-review proof now uses a dashboard-like structure with "AI Coach" / "Weekly Only" pills, a "Weekly AI Review" title, a compact left review panel, and two right-side proof cards for actionable improvement and strength. The oversized sparse card treatment was replaced with denser, left-aligned review content while keeping the above-the-fold scope only. (`templates/landing.html`, `static/js/landing_page.js`)
- Public landing above-the-fold now removes the smaller decorative hero preview/timeline panel and moves the real weekly review proof card into the hero flow immediately after the relief panel. The review card is static/readable immediately and reduced to a short outcome, pattern, and next-week fix. CTA copy, navigation, backend logic, and lower feature sections were left unchanged. (`templates/landing.html`, `static/js/landing_page.js`)

## Landing above-the-fold relief pass (2026-04-26)

- Public landing hero now uses the shorter "Stop guessing your trading." / "Understand your trading without turning journaling into a second job." copy with "guessing" color-accented only, plus commented headline/subheadline alternatives for future manual A/B edits.
- The old "How Traders Start Here" 1-2-3 instructional block was removed from the above-the-fold landing hero and replaced with a compact relief panel: no spreadsheets, no manual reviews, one clear fix. The hero eyebrow now reads "Trade review without the homework." The hero content is top-aligned with tighter top padding so the review preview appears earlier. CTA buttons, review preview card, JS behavior, backend logic, and lower feature/AI demo sections were left unchanged. (`templates/landing.html`)

## Legal + security alignment pass (2026-04-26)

- Terms and Privacy updated for weekly-review follow-up chat, import/sync data fallibility, optional Google OAuth, processors (hosting/Postgres/Redis/email/OAuth/AI), and service availability expectations. `LEGAL_LAST_UPDATED` bumped to match.
- App: JSON `429` responses for `/api/*` and weekly-review chat paths; weekly review chat `fetch` sends `X-Requested-With` so support-view POST blocks return JSON; `POST /dashboard/weekly-review/<id>/chat` refuses support session and enforces DB-backed caps for non-admins only (`user_has_admin_access`: verified + approved + `is_admin` or root-admin email); caps: 5 user turns per review, 15 user messages per UTC day per user, 3 user messages per rolling 60s; empty/`>800` chars rejected for everyone; per-minute check fails open on DB error with warning. Index `ix_weekly_review_chat_user_role_created` on `(user_id, role, created_at)`. `GET /api/ai-status` still Flask-Limiter–backed. Tests: autouse limiter reset in `test_dashboard_weekly_ai.py`, chat limit coverage in same file.

## Weekly AI review follow-up chat (2026-04-26)

- Follow-up: the weekly-review follow-up chat system prompt now lives in `prompts/weekly_review_followup.txt` and is loaded through the shared prompt-file loader instead of being embedded inline in `ai_service.py`. A focused dashboard weekly-AI test asserts the chat builder uses that prompt file. (`ai_service.py`, `prompts/weekly_review_followup.txt`, `tests/test_dashboard_weekly_ai.py`)
- Follow-up prompt tone pass: `prompts/weekly_review_followup.txt` now steers replies toward short conversational coaching, explicitly avoids repeating the weekly review, and gives special handling for "explain simply" requests: lead with the plain point, explain the behavior behind it, then give one concrete next step.
- Dashboard Weekly AI Review now includes a compact "💬 Ask about this review" follow-up chat rendered inside the existing review panel only when a persisted weekly review is shown. The UI uses Jinja + vanilla `fetch()` via `static/js/weekly_review_chat.js`, appends user/assistant bubbles without refreshing, includes quick prompts, loading/error states, and posts with the existing CSRF header.
- New `POST /dashboard/weekly-review/<review_id>/chat` route requires login, resolves the active trade account, verifies the review belongs to the current user and active account, validates message length (max 800) and usage limits on stored `WeeklyReviewChatMessage` rows (`role=user` only), calls the existing OpenAI Responses infrastructure with only the stored weekly review/pass-1/payload/meta context plus optional recent chat history, and does not mutate the review row itself.
- New `WeeklyReviewChatMessage` model/table stores successful user and assistant chat turns with user/account/review scope, model used, prompt version, and timestamps. Migration `20260426_0052` creates `weekly_review_chat_messages`.
- Chat AI context now mirrors dashboard ref handling: internal T1/B1 codes are rewritten or stripped, `refs` and per-trade `review_ref` are omitted from the prompt, ISO timestamps in JSON use a space instead of `T` so substring matches do not look like refs, and assistant replies are post-filtered for stray ref tokens. Loading UI uses a typing-indicator animation instead of static “Thinking…”.

## Weekly AI dashboard panel readability pass (2026-04-26)

- Dashboard-only presentation change: the Weekly AI Review main card now leads with "🧠 What mattered this week" using the existing summary as the focal paragraph, relabels takeaways to "What this suggests", renders only the first two takeaways, lightly separates takeaway items, and line-clamps the actionable improvement card to keep the action scannable. No AI generation, payload, schema, backend sync, or analytics logic changed. (`templates/index.html`)

## Weekly AI dashboard advice now runs a clarity rewrite pass (2026-04-26)

- Weekly dashboard AI generation keeps the existing pass-1 insight prompt and trade payload path unchanged, then calls the same AI client a second time with only the pass-1 review text and the clarity/tone rewrite prompt from `prompts/dashboard_advice_rewrite.txt`. The rewrite prompt now explicitly preserves the story / Key Takeaways / action-line structure and asks for natural trading-coach sentences instead of clipped report fragments.
- `ai_generated_responses` now stores `pass_1_output`, `pass_2_output`, `prompt_version_pass_1`, `prompt_version_pass_2`, `model_used`, and `created_at` while keeping `response_text` as the final user-facing text for existing frontend/admin paths. Pass-2 failures, including rewrites that drop the expected story / Key Takeaways / improvement structure, log a warning and fall back to pass 1 so weekly review generation still completes with the dashboard format intact. The dashboard still preserves structured side-card metadata such as the weekly experiment when the main review text comes from pass 2. (`ai_service.py`, `models.py`, `routes/dashboard.py`, migration `20260426_0051`, weekly AI tests)

## Weekly AI dashboard advice tuned away from report voice (2026-04-25)

- `prompts/dashboard_advice.txt` now keeps the fixed dashboard AI structure but explicitly treats the format as a container for coaching judgment, not recap. The prompt tells the model that every sentence inside the structure should explain why evidence matters, challenge the trader's likely read, or turn evidence into a next decision rule.
- The good/bad examples were tightened so Key Takeaways are interpretations anchored in evidence, while report filler like "this week showed a mix of strengths and weaknesses," session recaps, and category-label advice are explicitly rejected.

## MT5 broker-time probe seeds 24/7 aliases on demand (2026-04-25)

- Shared MT5 Market Watch probe candidates now live in `celery_workers/mt5_market_watch.py`, covering expanded BTC/XBT/ETH aliases with common broker suffixes (`.m`, `.r`, `.raw`, `.pro`, `.ecn`, `micro`, etc.) plus slash forms like `BTC/USD` and `ETH/USD`.
- Setup still clears inherited Market Watch state and seeds crypto-first symbols before fallback symbols, but now uses the expanded shared alias set.
- Sync and bar fetch now retry broker-time offset probing after best-effort selecting the first available 24/7 crypto alias into Market Watch when the initial probe finds no fresh tick.
- Last known broker-server UTC offsets are persisted by normalized MT5 server name in `mt5_broker_server_offset` (`20260425_0050`). A fresh live probe wins and updates the DB; if the broker/feed is down, sync/bar fetch falls back to the DB server offset before the older per-account Redis cache and only then `0`. This protects weekend/off-hours and downtime timestamp correction for history windows and bar backfills. (`models.py`, migration, `celery_workers/mt5_market_watch.py`, `celery_workers/mt5_setup_tasks.py`, `celery_workers/mt5_sync_tasks.py`, MT5 tests)
- Live broker-time probes now reject tick deltas that are not close to a whole-hour offset and also require the tick's minute/second position within the hour to match current UTC within tolerance. Fresh ticks are snapped to the nearest hour to absorb small tick latency, but minute-level deltas or stale minute mismatches fall back to the stored DB/Redis server offset path. (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`)
- Bar backfill now follows the same timing pipeline as trade sync: `fetch_trade_bars` shifts the `copy_rates_range` request window into broker/server time, treats returned bar epochs as broker/server time, and always subtracts the resolved/stored server offset before POSTing bars for DB storage. The old UTC fallback/inference path was removed so persisted `trade_bars.bar_time` is normalized exactly like MT5 deal timestamps. (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`)

## MT5 priority queue + beat starvation guard restored (2026-04-25)

- Admin/manual MT5 sync actions now publish to `mt5_priority` instead of the shared `mt5_sync` beat lane, specifically manual **Trigger Sync**, trade-time recalibration, and **Backfill Bars** dispatches. The VM sync worker now consumes `mt5_priority,mt5_sync` so operator-triggered work is picked first.
- Beat-driven `sync_all_active_mt5_accounts` once again protects the queue from permanent buildup: each beat-published sync task gets `expires=28`, and the beat skips publishing when combined `mt5_priority` + `mt5_sync` depth exceeds `150`.
- MT5 sync worker health/monitoring now treats `mt5_priority` + `mt5_sync` as one combined sync lane for queue depth and activity timestamps, while still storing the stale-alert flag on the existing `mt5_sync` monitor key.
- Goal: stop 30s beat traffic from starving manual sync/backfill tasks indefinitely on the solo VM worker. (`auth_account.py`, `celery_workers/mt5_sync_tasks.py`, `celery_app.py`, `scripts/windows/run_mt5_sync_worker.ps1`, `scripts/windows/watch_mt5_sync_worker.ps1`, tests)

## Celery publish diagnostics for admin/web-triggered tasks (2026-04-25)

- Added shared producer-side dispatch helper `helpers/celery_dispatch.py` so site-triggered Celery publishes log a sanitized broker URL plus the returned task id.
- Admin routes that queue MT5 setup/sync/recalibration/bar backfill and weekly AI regeneration now log `Celery publish attempt` / `success` / `failed` on the Render web side, with route context (account/trade ids, admin user id) before redirecting.
- Goal: separate "website accepted the POST" from "task was actually published to Redis" when VM logs stay quiet. (`helpers/celery_dispatch.py`, `auth_account.py`, `tests/test_celery_dispatch.py`)


## MT5 sync timestamps — last sync vs full history (2026-04-19)

- `MT5Account.last_synced_at` is stamped on every **successful** internal `/api/internal/mt5/sync` completion (including zero trade rows).
- New nullable column `last_full_history_sync_at` is stamped only when the worker reports `history_scope: full` (initial backfill until first full run completes, every manual `full_history` sync, or any beat run while the marker is still null).
- Celery `sync_mt5_account` chooses the wide history window when `last_full_history_sync_at` is null **or** `full_history=True`, and sends the matching `history_scope` for the API to record.
- Admin MT5 table shows both columns.

## MT5 pipeline rollback — setup + sync restored to Apr 11 baseline (2026-04-18)

**Context:** Between Apr 12–18, the MT5 setup + sync workers accumulated a large set of changes (shared `ensure_mt5_terminal_ready` pipeline, Redis global MT5 lock across setup/sync/cleanup/bar-fetch, repeated launch-strategy churn between `mt5.initialize` / `os.startfile` / Popen, IPC -10001 stale recovery bootstrap, connection-status writes, reset-terminal flow, interactive-session watchdog changes). In practice this produced IPC timeouts, fragile init, and setup/sync interference — the pipeline no longer worked reliably.

**Rollback:** `celery_workers/mt5_setup_tasks.py` and `celery_workers/mt5_sync_tasks.py` were fully replaced with the contents of `celery_workers/mt5_setup.py` and `celery_workers/mt5_sync.py` at commit **`df00681`** (2026-04-11 14:47 UTC), the last commit before the user's confirmed-working window at 15:21 UTC. Filenames kept as `*_tasks.py` to preserve every external import (`auth_account.py`, `helpers/core.py`, `routes/trade_accounts.py`, `celery_app.py`, tests). Line counts: 1377 → 622 (setup), 1869 → 816 (sync).

**What's gone (deliberately):** shared `ensure_mt5_terminal_ready`, Redis `mt5_global_lock` acquire/release calls (helpers still in `cache.py`, but now unreferenced dead code), soft-reconnect on stale broker history, terminal64 bootstrap on IPC -10001, per-account VM id stamping, connection_status / connection_error_message writes from the setup task, running-P&L positions_get overlay, beat queue-depth guard + sync task `expires=28`, and all launch-strategy churn from Apr 18.

**What's intact:** DB models + migrations (including `connection_status`, `archived_at`, `vm_id` columns — they just aren't written by the worker anymore and will stay at defaults for new rows), UI, routes, admin flows, cache.py global lock helpers (unused), tests (many will fail against the old surface — stabilization first, tests + improvements to follow).

**Likely regression causes (now reverted):**
1. `ensure_mt5_terminal_ready` switched terminal launch to `mt5.initialize(path=...)` only, dropping the earlier `os.startfile`/Popen launch that reliably surfaced the terminal window — `mt5.initialize` auto-launch was brittle and produced the IPC timeouts.
2. Redis global lock serialized setup, sync, cleanup, and bar fetch through one key; any stuck release or long TTL blocked the entire MT5 pipeline across both queues.
3. Six launch-strategy flips within ~40 minutes on Apr 18 left the setup path in a contradictory state (init-first vs launch-first, hard-stop bootstrap, no-startfile).
4. Sync's `ensure_mt5_terminal_ready` dependency meant any setup-path regression was instantly mirrored into sync.

**Follow-ups (later, controlled reintroduction):** global lock, lifecycle helpers, connection-status writes, and running-P&L overlay should come back one at a time on a stable baseline rather than as a single bundled change.

## MT5 global runtime lock — setup + sync no longer overlap (2026-04-18)

**Problem:** MT5's Python API keeps process-global state on the VM, but the `mt5_setup` and `mt5_sync` workers are separate processes. With sync now every 30s, a sync cycle could fire while setup was bootstrapping/logging in, causing the IPC/auth hiccups that made "delete + re-setup" the workaround.

**Change:** a Redis-backed global lock (`mt5_global_lock`) serializes every MT5 runtime operation on the VM — setup, sync, cleanup, bar fetch — across both queues. Queues remain split (`mt5_setup` / `mt5_sync`, no Celery routing change); the lock only gates concurrent MT5 access.

**Lock design (`celery_workers/cache.py`):**
- Built on existing token-based `claim_lock` / `release_lock` (Lua-script release, SET NX EX).
- Key: `mt5_global_lock`. Per-operation TTLs: setup 600s, cleanup 180s, sync 120s, bar fetch 120s.
- New helpers: `acquire_mt5_global_lock(token, ttl, wait_seconds=0, poll_interval=0.5)`, `release_mt5_global_lock(token)`, `peek_mt5_global_lock_holder()`.
- Crash safety: Redis TTL auto-recovers the lock if an owner dies; release is token-gated so a late release can never unlock another owner.

**Behavior:**
- `setup_mt5_terminal` (`celery_workers/mt5_setup_tasks.py`): blocking acquire (wait up to 60s). If still busy → Celery retry in 30s. Released in `finally:` (covers success, retry, `PermanentSetupError`, `TradingPasswordDetectedError`).
- `cleanup_mt5_terminal`: blocking acquire (wait 45s) then retry. Released in `finally:`. Prevents sync from running against a terminal we're about to kill.
- `sync_mt5_account` (`celery_workers/mt5_sync_tasks.py`): **non-blocking** acquire — if setup holds the lock, log `"global MT5 lock busy (setup in progress)"` and return `{"skipped": "global MT5 lock busy"}`. Combined with existing `expires=28` on sync tasks and the beat-level queue-depth guard, no backlog builds. Acquired before the existing per-account `mt5_sync_lock:<id>`; released in the same `finally:`.
- `fetch_trade_bars`: blocking acquire (wait 45s) around the `_MT5_API_SESSION_LOCK` block; if still busy → Celery retry.

**Logging:** every acquire/release logs with the owner token (e.g. `setup:<task_id>:<mt5_account_id>`); busy/skip paths log the current holder via `peek_mt5_global_lock_holder()` so ops can see which task is blocking.

**Why not unify queues:** `mt5_setup` and `mt5_sync` still route to different Windows VM workers (separate processes, separate Task Scheduler entries, separate watchdogs) so a stuck setup can't starve sync of its consumer. The global lock gives mutual exclusion on the MT5 runtime without giving up that isolation, and leaves the existing beat/expires/per-account-lock safeguards untouched.

**Tests:** `tests/test_mt5_sync.py` updated — existing "already locked" test now pins `acquire_mt5_global_lock` to True so it still exercises the per-account lock branch; new `test_sync_mt5_account_skips_when_global_mt5_lock_busy` covers the new skip path.

## MT5 lifecycle refactor — `ensure_mt5_terminal_ready` (2026-04-18)

**Problem:** MT5 IPC (`mt5.initialize()`) fails unless the terminal is opened properly. Headless-style launches and MT5's auto-launch are unreliable. Manual double-clicking works because the terminal fully initializes.

**New rule:** `ensure_mt5_terminal_ready` uses **only** `mt5.initialize(path=...)` to connect (and let MetaTrader start the terminal if needed per API docs) — **no** `os.startfile` / `subprocess` in that helper. Then `login` + `account_info` verification.

**New helper (`celery_workers/mt5_setup_tasks.py: ensure_mt5_terminal_ready`):**
Single pipeline for setup, sync, and bar fetch: **INITIALIZE → LOGIN → VERIFY**.
1. Require `terminal64.exe` on disk; optional log if a process is already running.
2. Bounded retry loop (8 attempts, 3s delay): `mt5.initialize(path=...)` until IPC connects.
3. Explicit `mt5.login(login, password, server)`.
4. Verify with `mt5.account_info()` (login match check).
5. Does NOT call `mt5.shutdown()` on success (caller manages session); DOES shutdown on failure after successful init.
6. Returns dict with `success`, `account_info`, `error`, `attempts`, `elapsed_seconds`, `terminal_launched`, `pid`.

**Setup bootstrap:** After copy, `mt5.initialize(path=…)` (same package, retries) until IPC connects, then wait for AppData / copy `servers.dat`; **`mt5.shutdown()`**; then **hard-stop** any remaining `terminal64.exe` (`_terminate_mt5_processes` + wait) before `ensure_mt5_terminal_ready` runs again.

**Setup flow changes (`setup_mt5_terminal`):**
- On Celery retry (`retries > 0`): fully cleans per-user terminal state (kill process, delete terminal dir, delete AppData) before re-running — setup retries always start from clean state.
- Replaced `_verify_mt5_terminal_login` with `ensure_mt5_terminal_ready`.
- Trading password check (`trade_allowed`) remains setup-only, applied after ensure returns.

**Sync flow changes (`sync_mt5_account`):**
- Replaced the 100+ line IPC recovery block (`-10001` bootstrap, kill→Popen→servers.dat→credential init) with a single `ensure_mt5_terminal_ready(...)` call.
- Soft reconnect (stale broker history) now also uses `ensure_mt5_terminal_ready` instead of raw `mt5.initialize`/`mt5.login`.
- `terminal_path` is now required (raises early if not set).

**Bar fetch changes (`fetch_trade_bars`):**
- Same `ensure_mt5_terminal_ready` pattern replaces raw `mt5.initialize` + `mt5.login`.

**Removed:** `_verify_mt5_terminal_login` (replaced by ensure), `_kill_terminal_if_running`.

## MT5 launch visibility hint (2026-04-18)

Setup uses **only** `MetaTrader5.initialize` to start/connect to the terminal (bootstrap + `ensure_mt5_terminal_ready`). Visibility still depends on the Windows session (interactive Task Scheduler vs Session 0).

## MT5 setup watchdog interactive session pass (2026-04-18)

Follow-up to the April 11 comparison: the strongest regression signal was the **worker session model**, not the MT5 Python calls. The setup watchdog path is now adjusted back toward the earlier working shape:

- `manual VM scripts/FX Journal MT5 Setup Watchdog.xml` now targets the interactive Administrator desktop session with `LogonTrigger` + `InteractiveToken` instead of password/background scheduling.
- `scripts/windows/watch_mt5_setup_worker.ps1` no longer hardcodes `Start-Process ... -WindowStyle Hidden`; it now forces a visible worker launch (`Normal` by default). Existing hidden env overrides are ignored for the setup worker path so the watchdog does not accidentally relaunch setup invisibly.
- Scope is intentionally narrow: this change is for the **setup** worker path so MT5 setup/bootstrap can surface the real terminal window again. Sync watchdog behavior is unchanged.

**Operational note:** importing/replacing the Task Scheduler entry on the VM is still required; editing the XML export in the repo does not change the live scheduler by itself.

## MT5 reset-terminal cleanup fix (2026-04-18)

`reset_mt5_terminal_state` now queues VM cleanup when **either** `terminal_path` or `appdata_hash` exists, instead of incorrectly requiring both runtime fields. This matters for failed/partial setups where only one artifact was saved, which previously made "Reset Terminal" appear to succeed while leaving VM state behind.

`cleanup_mt5_terminal` is also hardened for partial metadata:
- blank `terminal_path` no longer tries to resolve/kill a fake executable path
- hash-only cleanup can delete the AppData folder directly from the stored 32-char hash without needing a matching terminal dir
- terminal-only cleanup still removes the terminal dir and can fall back to origin lookup for AppData when available

**Reset/setup race follow-up:** reset no longer pretends setup can be rerun immediately while VM cleanup is still in flight. Reset now marks the account as cleanup-pending, queues VM cleanup with a clear-marker callback, and admin `Setup Terminal` is blocked until that marker clears. This avoids the old race where reset cleanup could arrive late and wipe a newly rebuilt terminal at the same deterministic path. `cleanup_mt5_terminal` also stops using silent `ignore_errors=True` deletes for the reset path, so the cleanup marker is only cleared after the terminal/AppData folders are actually gone.

Short-term operational memory.

## Admin — MT5 Reset Terminal State (2026-04-17)

New admin action "Reset Terminal" on the MT5 accounts table. Lets root admins clear broken per-user terminal state and retry setup without deleting the MT5 account row.

**What it does:**
- Queues `cleanup_mt5_terminal` on `mt5_setup` for any existing terminal/AppData paths (same VM cleanup used by Archive/Delete)
- Clears terminal-runtime DB fields: `terminal_path`, `appdata_hash`, `vm_id`, `is_active → False`, `connection_status → pending`, `connection_error_message`, `archived_at`, `archive_reason`, `cleanup_marked_at`
- Keeps untouched: `user_id`, `trade_account_id`, `account_number`, `investor_password_encrypted`, `server`, consent fields, `last_synced_at`
- After reset: Setup Terminal stays blocked until VM cleanup finishes, then can be used to bootstrap a fresh terminal with the same credentials

**Files changed:** `helpers/core.py` (`reset_mt5_terminal_state`), `auth_account.py` (import + `POST …/reset-terminal` route), `templates/admin_signup_access.html` (Reset Terminal button in MT5 actions cell)

## Current Focus

- **Manual VM helper:** `manual VM scripts/run_mt5_broker_seeding.bat` → `manual VM scripts/mt5_broker_seeding/mt5_seed_brokers.py` — CLI countdown, then **keystrokes only** into the focused MT5 company field (each line from `brokers_to_seed.txt` + submit; no UIA/window attach). Dependencies: `manual VM scripts/mt5_broker_seeding/requirements.txt` (pywinauto for `send_keys` only).
- MT5 **connection state system**: `MT5Account` gains `connection_status` (`pending` / `connected` / `failed`) and `connection_error_message` (Text nullable); migration `20260416_0048`. Setup task writes `connected` on success; writes `failed` + user-friendly message after final retry exhausted (`_classify_setup_error` maps MT5 error strings to human copy). New `POST /dashboard/trade-accounts/mt5/retry` route updates credentials in-place on a failed account, resets status to `pending`, and re-queues setup — no new DB row, no delete required. Dashboard status logic adds `"failed"` state (checked before `setting_up`/`queued`). Dashboard MT5 card shows error message + pre-filled retry form when failed; trade accounts page shows `"MT5 Connection Failed"` chip + error text + link to dashboard to fix. JS `syncSubmitState` updated to not require consent checkbox (retry form omits it). (`models.py`, `celery_workers/mt5_setup_tasks.py`, `routes/trade_accounts.py`, `routes/dashboard.py`, `templates/index.html`, `templates/trade_accounts.html`, `static/css/app_pages.css`, `static/js/mt5_request_form.js`) — **Admin + monitor visibility also added**: `_build_admin_mt5_status` now returns "Connection Failed" chip before "Setting Up" fallback; `admin_mt5_accounts` route computes `failed_mt5_count` passed to template; admin MT5 stat bar shows a red "Connection Failed" tile when count > 0; MT5 accounts table shows `connection_error_message` inline under failed accounts; MT5 monitor `_fetch_accounts` now selects `connection_status` + `connection_error_message`; `_stats_block` shows `failed` count in red; `_accounts_block` shows `FAIL` in red instead of `INACTV` for failed accounts; new `_failed_setups_block` renders a dedicated FAILED SETUPS section with error message per account when any failures exist. (`auth_account.py`, `templates/partials/admin_shell_start.html`, `templates/admin_signup_access.html`, `scripts/windows/mt5_monitor.py`)
- MT5 **running / live P&L** on sync: deal-aggregated open rows used `pnl=None` and `positions_get()` rows were skipped when the same `mt5_position` was already in the deal list, so the internal API never wrote `Trade.pnl` for open MT5 trades. The worker now overlays `positions_get().profit` onto open deal-derived rows before POST; worker logs `MT5 sync positions … running_pnl_overlay_rows=…`, internal sync logs each open `written_pnl` / warns when pnl missing (`celery_workers/mt5_sync_tasks.py`, `routes/mt5_internal.py`, `tests/test_mt5_sync.py`). Also: `import time` restored for stale-reconnect `time.sleep`.
- MT5 sync **soft reconnect** on stale broker view: when the broker reports no open positions and deal history is missing or older than `FXJ_MT5_HISTORY_STALE_THRESHOLD_MINUTES` while the DB still has open MT5-linked trades, the worker performs one extra `mt5.shutdown()` → `initialize` → `login` and refetches before POST; no process kill, disable with `FXJ_MT5_SOFT_RECONNECT_ON_STALE=0`; logs and `mt5_sync_diag` Redis state include `soft_reconnect` (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`)
- MT5 monitoring thresholds recalibrated for 30s sync beat: account alert 10→5 min, sync queue staleness 10→5 min, setup staleness 20→15 min; 5 min = 10 missed syncs at 30s beat so only genuine outages alert; health check beats reduced 300s→60s for fast recovery detection (`celery_workers/mt5_monitoring.py`, `celery_app.py`); all thresholds env-var-overridable (`FXJ_MT5_SYNC_ALERT_THRESHOLD_MINUTES`, `FXJ_MT5_STALE_THRESHOLD_MINUTES`, `FXJ_MT5_SETUP_STALE_THRESHOLD_MINUTES`)
- MT5 beat queue-buildup protection: `sync_all_active_mt5_accounts` skips the beat if `mt5_sync` queue depth exceeds 150; individual `sync_mt5_account` tasks now have `expires=28` so unprocessed tasks discard themselves before the next beat fires — a recovering worker always picks up fresh work only (`celery_workers/mt5_sync_tasks.py`)
- MT5 sync beat interval reduced from 300s → 30s; `sync_mt5_account` task now uses `ignore_result=True` to skip Redis result backend writes (task is fire-and-forget, nothing reads its result). Drops wall-clock per task from ~7s to ~300ms, allowing the tighter interval without queue buildup (`celery_app.py`, `celery_workers/mt5_sync_tasks.py`)
- Root-admin **read-only support view**: root admins can enter a user's account context from Admin and browse dashboard, analytics, trades, strategies, trade accounts, weekly check-in, and account pages in a blocked-write support mode; request context swaps to the target user for approved GET routes, mutating user-facing posts are centrally rejected, and the global app shell now shows a support-view banner with exit action + account switcher (`app.py`, `auth_account.py`, `helpers/core.py`, user-facing routes, `templates/base.html`, admin users table, tests)
- Legal copy refresh: Terms and Privacy now explicitly cover live/real-funded MT5 account risk, read-only troubleshooting access to user pages/data, and the no-edit support boundary (`templates/terms_and_conditions.html`, `templates/privacy_policy.html`, `helpers/legal.py`)
- **Admin panel UX** (`static/css/admin_panel.css`, `templates/partials/admin_shell_start.html`, `templates/partials/admin_panel_head.html`, `templates/admin_signup_access.html`, `templates/admin_weekly_report.html`, `templates/base.html`): sidebar navigation replaces top tabs; contextual stat tiles per section; env flags in a collapsible block; user **Delete** beside username/ID, MT5 **Delete** beside login; invite-code activate/deactivate on the code row; **Suspended** user filter exposed; main nav highlights all admin routes including weekly report and CFD symbols; panel-scoped quick filters (`static/js/admin_panel_filter.js`, `templates/admin_signup_access.html`, `admin_shell_end.html`) now narrow only the rows/cards inside each list panel instead of filtering the whole admin view (`/` focuses the first available panel filter); users tab keeps server search for paging vs local row filtering; **Users** and **MT5** panels add server-side **Sort** (`?sort=`) for created/approved/login/username/email/ID and MT5 linked/sync/login/server/ID, preserved across status tabs, search, and pagination (`auth_account.py`, `tests/test_admin_route_gating.py`)
- Root admin **CFD aliases** tab at `/dashboard/admin/access/cfd-symbols`: edit comma-separated broker/MT5 names per `CFD_Symbols` row, validate unique normalized keys across **active** symbols, persist normalized storage, and call `clear_cfd_symbol_cache()` (`auth_account.py`, `trading.py`, `templates/admin_signup_access.html`, `templates/admin_weekly_report.html`, `tests/test_admin_cfd_symbols.py`); `render_admin_page` / `render_admin_weekly_report_page` merge template context with `dict.update` for Python 3.13+ (duplicate `**kwargs` to the same function call is rejected)
- Admin Users tab: root admins can permanently delete non–root-email accounts via `POST .../users/<id>/delete` (same data cleanup as self-service delete: `delete_users_with_related_data`), with confirm dialog in `admin_signup_access.html`
- `context/PROJECT_MAP.md` reorganized into a MyFXJournal architecture + operations cheat sheet (Render vs Hyonix, exact start commands, env ownership); fill in plan/region/cost table when convenient
- Trades hub UX: shared app-page hero, `static/css/app_pages.css`, corrected nav `request.endpoint` names, scoped trades CSS under `.trades-page`
- MT5 batch-gated immediate setup flow
- Dashboard MT5 state and weekly AI display
- Test coverage around MT5 batch/admin paths and ready-email behavior
- Activation copy pass: import-first onboarding, clearer first weekly-review unlock rule, MT5 sync framed as optional after import, and customer-facing brand copy standardized to `MyFXJournal`
- Weekly AI personalization v2: payload now includes `tone_context`, `current_week_breakdowns`, `four_week_patterns`, `experiment_context`, and `recent_experiments`; prompt allows exact user numbers with contrast and explicit tone-mode routing; structured meta adds optional `experiment` stored in `response_meta_json` while `response_text` stays legacy summary/takeaways/improvement/strength; dashboard weekly queueing now uses merged closed trade ideas and the weekly AI panel renders a separate "This Week's Experiment" card only when experiment text exists (`ai_service.py`, `prompts/dashboard_advice.txt`, `routes/dashboard.py`, `templates/index.html`, weekly AI tests)
- Weekly AI sizing copy + metrics: `current_week_breakdowns.sizing` adds `median_planned_risk_dollars` and `median_risk_pct_of_account` (from `resolve_planned_risk_dollars` + synced `account_size`); `format_payload_for_prompt` lists them before `median_lot_size` and orders sections for narrative reading (SUMMARY and TONE before breakdowns, TRADES last); `dashboard_advice.txt` + `REVIEW_JSON_OUTPUT_INSTRUCTIONS` steer plain English across the whole review (not only sizing: behaviour, exits, concentration examples), document risk-based `outlier_size`, and require experiments that are bolder and not a repeat of the improvement line
- Weekly AI onboarding vs returning users: `get_latest_trade_week_period` still falls back to the latest closed trade when every close is after the dashboard week boundary so a review window exists for new data. **Returning** trade accounts (any prior `AIGeneratedResponse` with weekly dashboard kind on that account) only auto-queue / auto-generate after **Friday 5:30 PM New York** for that review week (`eligible_at_utc`); the **first** weekly review on an account may still run immediately after import/sync or dashboard visit. Ingest hook: `queue_weekly_ai_review_after_ingest` (`helpers/weekly_ai_queue.py`, `routes/trades.py`, `routes/mt5_internal.py`). Admin `force_regenerate` bypasses the cutoff (`maybe_generate_weekly_dashboard_advice`).

## Active Areas

- **Running/open-trade close-state fix:** shared helper `helpers/trade_state.py::trade_is_closed` now drives running/open-trade decisions so `pnl` alone never implies closure, open trades with live P&L stay open, and running-P&L event inclusion follows real close signals with `closed_at` preferred over the weaker `exit_price` fallback. Regression coverage added in `test_running_pnl.py`, `test_running_pnl_routes.py`, and `test_trading_math.py`.

- **Running P&L feature:** `AccountCashFlow` model (`account_cash_flows` table) tracks deposits, withdrawals, and adjustments per trade account (migration `20260413_0047`). `helpers/running_pnl.py` builds a chronological event stream from closed trades + cash flows with cumulative running realized P&L, running cash flow, and running net result — trading performance and cash flows are never mixed. API endpoint `GET /api/running-pnl` serves the event data with optional `?from=`/`?to=` date range. Cash flow CRUD at `/dashboard/trade-accounts/cash-flows` (POST/GET/DELETE). Analytics page now includes a Running P&L chart panel with line toggles (Trading P&L / Cash Flow / Both / Net) and summary stats, driven by `static/js/running_pnl.js` fetching the API. 31 tests (`test_running_pnl.py` + `test_running_pnl_routes.py`) cover cumulative P&L, deposit/withdrawal isolation, mixed ordering, identical timestamps, open-trade exclusion, date filtering, and CRUD.

- **Trade interpretation layer:** bundle link and behavior flags (`is_revenge` / `is_reactive` / `is_corrective`) moved off `trades` into `trade_interpretation` (one row per trade when non-empty) plus append-only `trade_interpretation_history` (`source`, `user_id`, `created_at`). Writes go through `helpers/trade_interpretation.apply_interpretation`; `Trade` exposes read-only `.is_*` / `.bundle_pubkey` for templates and existing `getattr` callers. Migration `20260412_0044`. Bulk trade loads use `selectinload` for `Trade.interpretation`, and where analytics/display call `resolve_pips` / `get_trade_account_type`, also `Trade.trade_account` (dashboard + trades list + AI `_query_trades_for_payload`). Bundle review / bundle-confirm closed-trade queries eager-load interpretation (+ profiles for review page). `get_user_trade_by_pubkey_or_404` loads interpretation, profiles, and `trade_account` in one round-trip. Admin unbundle uses `join(TradeInterpretation)` + `contains_eager(Trade.interpretation)` so joined rows populate the relationship without per-trade lazy loads.

- **SQL / query hygiene (broader pass):** admin overview user stats collapsed to one aggregated `User` query plus two `COUNT`s (`signup_codes`, `mt5_account`); delete-all trade accounts loads `TradeAccount` rows once and reuses IDs for MT5 counts; MT5 unlink batches `MT5SyncBatch` loads by distinct `batch_id` instead of `get` per request row; user delete preloads `Trade.interpretation` with trades; duplicate-trade detection uses `load_only` + `selectinload(Trade.trade_account)` for `resolve_pnl` fallback; EI trend + AI payload builder use `load_only` on `payload_json`/`period_start_utc` for small recent-review queries; admin weekly report uses `load_only` on latest-record probe and `joinedload` for `prompt_history` on the ordered list; batch strategy updates resolve profile/version once (`resolve_user_trade_profile_attachment` + `attach_trade_profile_objects`). **Production:** deploy app code in the same release as running `alembic upgrade` through `20260412_0044` so the ORM never selects dropped `trades.is_*` columns.
- **Heuristic tuning:** possible reactive is stricter (`REACTIVE_REENTRY_WINDOW_MINUTES` 75, `REACTIVE_POSSIBLE_THRESHOLD` 1.55, post-loss/same-size/loss-streak bonuses gated on `quick_context`). Revenge uses `REVENGE_POSSIBLE_THRESHOLD` 1.5 and avoids double-counting strong post-loss re-entry vs minute-tier ladder; corrective requires a structural prerequisite (e.g. closed before SL, quick hold, or outlier size) before stacking context. Heuristic corrective drops the old “free” base points on losers and uses `CORRECTIVE_HEURISTIC_MIN_RAW` 1.25 (`helpers/trade_analysis.py`, `helpers/scoring.py`). Pytest: `conftest.py` autouse clears `CFD_Symbols` rows + `trading.clear_cfd_symbol_cache()` after each test so admin CFD tests do not shrink the cached catalog for `test_trading_math.py`.
- MT5 sync shifts `history_deals_get` windows into current broker/server time before querying and normalizes returned trade timestamps back to UTC for storage; bar backfill/fetch follows the same broker-offset model for `copy_rates_range` windowing and normalizes returned bar epochs to UTC before insert, including the UTC-boundary fallback path (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`)
- MT5 setup now clears any inherited MarketWatch selection and reseeds broker-time probe symbols after login: crypto-first (`BTCUSD`, `BTCUSDT`, `XBTUSD`) so weekend/off-hours offset checks have a live 24/7 symbol when available, with fallback to `XAUUSD` / `EURUSD` if no crypto symbol can be selected (`celery_workers/mt5_setup_tasks.py`, `tests/test_mt5_setup_order.py`)
- MT5 terminal reset now keeps `cleanup_marked_at` as the guardrail until the cleanup worker confirms terminal/AppData removal, the cleanup worker accepts reset metadata (`delete_account_row`, `clear_cleanup_mark`, `cleanup_marked_at`) so reset can clear only the pending-cleanup mark without deleting the row, and admin reset stays blocked/disabled while cleanup is pending (`helpers/core.py`, `celery_workers/mt5_setup_tasks.py`, `templates/admin_signup_access.html`, `tests/test_mt5_cleanup.py`, `tests/test_mt5_access_requests.py`)
- Admin MT5 Backfill Bars no longer treats “any existing M5 row” as complete; it now requeues trades whose stored M5 coverage does not span the trade from around `opened_at` through `closed_at`, which helps recent trades recover from partial/short bar inserts without clearing every account’s bars first (`auth_account.py`, `tests/test_mt5_sync.py`)
- MT5 file imports still avoid guessing timezone for naive timestamps; explicit timezone/offset input is converted to UTC-naive for DB storage (`trading.py`, `routes/mt5_internal.py`, `templates/trade_entry.html`, `tests/test_trading_import.py`)
- Closed MT5 trade detail includes Lightweight Charts with admin-gated chart API, 5m/15m toggle (M15 aggregated from stored M5), price-scale labels for entry/exit/SL/TP; entry, exit, SL, and TP are full-width `createPriceLine`s (entry and exit solid, SL/TP dashed), plus entry/exit arrow markers, while SL/TP stay full-width `createPriceLine` references; chart-data loads all `trade_bars` for the trade in **one** query, returns `prefetched_bars` for M5+M15 when both are offered so toggling timeframe avoids a second DB round-trip (`routes/trades.py`, `static/js/trade_chart.js`); bar fetch uses a wide M5 window before/after the trade (~36h pre / ~12h post) (`templates/trade_entry.html`, `static/css/app_pages.css`, `celery_workers/mt5_sync_tasks.py`); trade chart intro uses a ~1.25s slow-in/slow-out (ease-in-out cubic) reveal (RAF) with fixed `autoscaleInfoProvider` range and whitespace slots so price/time framing stays stable while candles fill in
- MT5 batch flow work in `routes/trade_accounts.py`, `templates/trade_accounts.html`, `templates/index.html`, and `static/js/mt5_request_form.js`
- MT5 archive/reactivate flow: `MT5Account` now supports `archived_at` / `archive_reason` (`20260412_0045`), so stale syncs can be archived without deleting the DB row or saved read-only credentials. Archiving clears VM terminal metadata, queues terminal/AppData cleanup, stops sync, and surfaces **Sync Inactive** / **Archived** states in dashboard, Trade Accounts, and admin; users can reactivate archived MT5 sync from the dashboard/Trade Accounts without re-entering credentials (`helpers/core.py`, `routes/trade_accounts.py`, `routes/dashboard.py`, `auth_account.py`, `templates/index.html`, `templates/trade_accounts.html`, `templates/admin_signup_access.html`, `tests/test_mt5_access_requests.py`)
- MT5 receipt confirmation now uses the shared HTML email pattern in `routes/trade_accounts.py` and `templates/emails/mt5-request-received.html`; messaging now reflects immediate setup start vs saved-but-not-queued fallback
- MT5 batch controls now live in admin: root admin can open, expand, and close MT5 sync batches while the public site describes MT5 setup capacity without exposing exact slot counts.
- MT5 sync is now batch-gated but immediate for new users: once setup capacity is available and details are submitted, terminal setup is queued right away; dashboard/trade-account states now distinguish `Submit Details` -> `Setup Queued` -> `Setting Up` -> `Sync Active`
- Admin MT5 workflow labels now use Setup Queued, Setting Up, Active, and Inactive for current-user states, while legacy request-review routes remain only for old rows
- User-facing emails now share a common `templates/emails/_base.html` shell, MT5 ready emails use the same HTML template pattern via `templates/emails/mt5-ready.html`, and worker-triggered emails render through app-level Jinja (`render_app_template`) so weekly-review and MT5-ready emails do not depend on request context
- Admin MT5 tab now focuses on submitted MT5 accounts and setup actions instead of legacy request-review/manual-add panels; root admin can queue **Recalibrate all trade times**, **Clear all chart bars** (wipes `trade_bars` for every trade; trades unchanged), or per-account **Recalibrate times** (full-history sync with `refresh_closed_trade_timestamps`) to rewrite existing MT5 trades’ `opened_at`/`closed_at` from broker deals (`auth_account.py`, `templates/admin_signup_access.html`, `routes/mt5_internal.py`, `celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`, `tests/test_admin_route_gating.py`)
- Legacy MT5 `approved before details` path is being removed so dashboard/trade-account states now stay aligned with the one-step submit-then-review flow
- Admin MT5 table now uses explicit workflow statuses: Requested, Setting Up, Active, Inactive, while preserving Cleanup Pending for orphaned records
- Dashboard MT5 messaging, rolling performance/behaviour trend panel, and weekly AI presentation work in `routes/dashboard.py`, `templates/index.html`, and `helpers/trends.py`
- State-1 onboarding dashboard: “What’s next” journey banner spans full width; MT5 + weekly AI use the same two-column hero as the main dashboard (MT5 left); stacked breakpoint puts MT5 above weekly AI (`templates/index.html`); copy/layout trimmed to reduce duplicate unlock messaging, drop extra MT5/AI callout pills, and hide empty AI side cards until a review exists; journey banner default styling is more compact (no guided glow on the banner); open MT5 batches show a prominent slots-left callout with fill meter on the dashboard MT5 card; dashboard MT5 request card copy de-duplicated (single workflow line, no extra hint above the form, investor/master warning consolidated in the consent checkbox)
- Weekly AI hero now drafts a split review layout: larger left narrative panel (summary + takeaways) with two right-side micro-panels for one actionable improvement and one strength to reinforce (`templates/index.html`)
- Light emoji prefixes on major section titles only (dashboard MT5/trades, analytics KPI bands, trade accounts, strategies, trades table); account settings headings stay plain with a calmer single-border overview list (`templates/account.html`)
- Worker/admin MT5 flow work in `auth_account.py`, `helpers/core.py`, and `celery_workers/mt5_setup_tasks.py`
- MT5 setup now retries the MT5 API verification cycle once inside the same Celery task after a first `mt5.initialize()` / account-detection failure, with explicit `mt5.shutdown()` between attempts, so transient authorization hiccups do not always spill into a whole-task retry (`celery_workers/mt5_setup_tasks.py`, `tests/test_mt5_setup_order.py`)
- MT5 sync accepts broker gold symbols as XAUUSD: `GOLD` is a CFD alias for `XAUUSD` (defaults + migration `20260411_0040`), and M5 bar fetch tries alias names when `copy_rates_range` needs the server’s symbol string (`trading.py`, `celery_workers/mt5_sync_tasks.py`)
- CFD `DEFAULT_CFD_SYMBOL_SPECS` now includes broader broker-root aliases for metals, index CFDs, and crypto (plus common suffix-stripped metal forms like `XAUUSDM`); migration `20260412_0042` merges those aliases into `CFD_Symbols` for deployed DBs (`trading.py`, `tests/test_trading_math.py`)
- CFD canonicalization strips common broker suffix tokens after `normalize_symbol` (e.g. `EURUSDR` / `XAUUSDMICRO` → majors and metals), merges `USSPX500` / `NIKKEI225`, expands MT5 bar-fetch symbol tries with dotted and no-dot suffix spellings (`trading.py`, `tests/test_trading_math.py`); `DEFAULT_CFD_SYMBOL_SPECS` includes oil, exotic FX, platinum/palladium and cross metals, nat gas, `USDX`/`VIX`/`SMI20`, soft/industrial commodities, `DOTUSD`/`LINKUSD`, common US equity CFD roots, and extra index/metal/oil aliases — migration `20260412_0043` inserts any missing `CFD_Symbols` from defaults and merges broker aliases (`trading.py`)
- Users can disconnect MT5 sync from **Trade Accounts** and the dashboard MT5 card (`POST /dashboard/trade-accounts/mt5/unlink`): deletes `MT5Account`, clears related `MT5AccessRequest` rows, decrements batch `total_slots_claimed` when applicable, queues terminal cleanup like admin delete, and invalidates dashboard cache (`helpers/core.py`, `routes/trade_accounts.py`, `templates/trade_accounts.html`, `templates/index.html`, `tests/test_mt5_access_requests.py`)
- Celery worker ASCII summaries: default `narrow` stacked layout on Windows + `FXJ_ASCII_LOG_LINE_MAX` in MT5 PowerShell launchers for windowed consoles (`celery_workers/logging_utils.py`, `scripts/windows/run_mt5_*.ps1`)
- MT5 rolling/beat sync: worker uses quiet one-line logs for benign idle noops; internal API supports `skip_debug_mode` (`worrisome` skips per-row debug for `existing_already_closed` only); full detail still on first/full-history sync, errors, or worrisome skips (`celery_workers/mt5_sync_tasks.py`, `routes/mt5_internal.py`)
- MT5 sync quiet logs now include broker freshness hints without widening the file log too much: one-line `noop` / compact sync logs add `latest_deal_utc`, `latest_deal_position`, `open_positions`, and short `open_position_ids` previews so stale-close investigations can tell whether the broker history advanced or a position still looked open (`celery_workers/mt5_sync_tasks.py`, `tests/test_mt5_sync.py`)
- MT5 sync now flags stale broker-history cases more explicitly: when broker positions are flat, the newest MT5 deal is older than the configurable threshold (`FXJ_MT5_HISTORY_STALE_THRESHOLD_MINUTES`, default 30), and the DB still has MT5 trades open, the quiet sync line is promoted to a warning and adds `latest_deal_lag_min`, `db_open_mt5`, and `history_stale=1`; per-account diagnostics are cached so the Windows MT5 monitor dashboard (`manual VM scripts/monitor.bat` -> `scripts/windows/mt5_monitor.py`) can show `History stale` summary plus a small per-account diagnostics table, while the worker-title path also has the same signal available when used (`celery_workers/mt5_sync_tasks.py`, `scripts/windows/mt5_monitor.py`, `celery_app.py`, `tests/test_mt5_sync.py`, `tests/test_celery_app.py`, `tests/test_mt5_monitor.py`)
- MT5 sync diagnostics improved: internal ingest now logs explicit skip-reason counters and sync worker warns when a run is all-skipped (`routes/mt5_internal.py`, `celery_workers/mt5_sync_tasks.py`)
- MT5 logging: sync worker emits a full JSON line for API `skip_reasons` / `insert_validation_reasons` (avoids ASCII-table truncation); setup worker logs milestones (base AppData, bootstrap launch, new hash, `servers.dat`, pre-verify); bar fetch logs when `copy_rates_range` returns no rates; internal API logs invalid normalized rows, successful bar ingests; admin actions log when Celery tasks are queued (`celery_workers/mt5_sync_tasks.py`, `celery_workers/mt5_setup_tasks.py`, `routes/mt5_internal.py`, `auth_account.py`); successful sync path logs one combined `MT5 Sync` ASCII table (context + API outcome + optional alert / skip_debug JSON) instead of separate Context/Result/Warning tables
- MT5 worker ops monitoring: `MT5Account` now stores the last handling `vm_id`, while `MT5SyncVMState` stores one `vm_alert_sent` flag per VM; successful internal sync stamps the VM on the account, and Render-side Celery beat runs `check_mt5_sync_health` (group stale accounts by VM, send one alert per stale VM, reset only when all accounts on that VM recover) plus queue-health checks for **both** `mt5_sync` and `mt5_setup`; sync staleness is based on queued backlog + no processed tasks, setup staleness uses queued backlog + no started-or-finished activity inside a longer threshold, and both emit `WORKER STALE DETECTED` / `WORKER STALE RECOVERED` logs with broker/heartbeat evidence (`celery_app.py`, `celery_workers/mt5_monitoring.py`, `celery_workers/worker_monitor.py`)
- VM scheduler assets (four exports; pick **watchdog** *or* **direct** per queue, never both): `manual VM scripts/FX Journal MT5 Setup Watchdog.xml` + `manual VM scripts/FX Journal MT5 Sync Watchdog.xml` (recommended: supervision + restarts), and `manual VM scripts/FX Journal MT5 Setup Worker Direct.xml` + `manual VM scripts/FX Journal MT5 Sync Worker Direct.xml` (direct `run_mt5_*.ps1` only; default **disabled** in XML so they do not race the watchdogs). **Setup and sync** both use interactive desktop scheduling (`LogonTrigger` + `InteractiveToken`) whether via watchdog or direct so Celery and MetaTrader can run in the logged-on Administrator session (visible consoles / terminal UI). Older headless sync exports used `Password` logon; re-import the repo XML on the VM when upgrading. The PowerShell launchers still guard console-title writes so the scripts do not crash in edge non-interactive hosts. (`manual VM scripts/`, `scripts/windows/run_mt5_*.ps1`, `scripts/windows/watch_mt5_*_worker.ps1`). **All Task Scheduler XML exports use the VM repo path `C:\Users\Administrator\fxjournal` — never replace with the dev machine path `C:\Coding Projects\FX Journal`.** Save/edit these exports as **UTF-16 LE with BOM** (`encoding="UTF-16"`); UTF-8 XML fails `schtasks` import on the VM. After editing any Task XML in `manual VM scripts/`, run `python "manual VM scripts/reencode_task_xml_utf16.py"` so the file is **`FF FE` + UTF-16 LE** (do not use Python `write_text` with the `utf-16` codec for these exports — it can emit malformed multi-line bytes on Windows). The script normalizes all four `FX Journal MT5 *.xml` exports.
- MT5 worker ops baseline: keep Windows Time (`W32Time`) running with reliable time sync and enable hypervisor/cloud guest time sync so rolling windows, staleness checks, and logs stay stable (`celery_workers/mt5_sync_tasks.py`, VM runbook steps)
- Celery worker logging now uses a shared ASCII-table formatter across MT5 sync, MT5 setup/cleanup, weekly AI generation, and weekly checkin cleanup so task logs read as consistent summaries instead of mixed plain lines (`celery_workers/logging_utils.py`, worker modules, `tests/test_celery_worker_logging.py`)
- VM support: failed Celery tasks and each retry emit a copy-paste block `FXJ_WORKER_DIAGNOSTIC_BEGIN`…`END` (task id/name, args/kwargs, traceback, env snapshot without secret values); MT5 sync and bar POST `HTTPError`s log an ASCII table plus `FXJ_HTTP_ERROR_SUMMARY_*` with status/URL/body preview (`celery_app.py`, `celery_workers/worker_diagnostics.py`, `celery_workers/mt5_sync_tasks.py`, `tests/test_worker_diagnostics.py`). Disable noise: `FXJ_WORKER_DIAGNOSTIC=0`
- Celery's built-in plain `celery.app.trace` task lifecycle lines are now filtered so worker output relies on the richer FX Journal table summaries instead of duplicate `Task ... succeeded/retry/failed` lines (`celery_app.py`, `tests/test_celery_app.py`)
- Windows MT5 worker consoles now keep a live window title with MT5 stats from DB + Redis queue depth, so the top bar can show labels like `MT5 Sync Window | 5 Accounts Active | 4 In Queue | 1 Running`; the PowerShell launchers also set clearer startup/restart titles before Celery finishes booting (`celery_app.py`, `celery_workers/cache.py`, `scripts/windows/run_mt5_sync_worker.ps1`, `scripts/windows/run_mt5_setup_worker.ps1`, `tests/test_celery_app.py`)
- Celery workers whose `--hostname` matches `mt5-sync@…` or `mt5-setup@…` automatically attach a rotating file handler under `logs/workers/<hostname>/celery.log` (override root with `FXJ_WORKER_LOG_DIR`, disable with `FXJ_WORKER_FILE_LOG=0`); default Render-style hostnames stay stdout-only unless `FXJ_WORKER_FILE_LOG` / `FXJ_WORKER_LOG_DIR` is set (`celery_app.py`); rotation size is fixed in code (~32 MiB × 10 files ≈ 320 MiB per log name, aimed at ~1 week for ~10–20 MT5 accounts on a 5m beat)
- ASCII worker tables: long **value** cells default to 72-char truncation; `FXJ_ASCII_LOG_MAX_WIDTH` widens or `0`/`full` disables truncation; Windows MT5 PowerShell launchers set `FXJ_ASCII_LOG_MAX_WIDTH=0` so skip-reason columns are not cut off (`celery_workers/logging_utils.py`, `scripts/windows/run_mt5_sync_worker.ps1`, `scripts/windows/run_mt5_setup_worker.ps1`)
- Bundle review and weekly check-in outlier cards now render trade timestamps in the user display timezone instead of raw stored UTC (`routes/trades.py`, `routes/checkin.py`, `templates/bundle_review.html`, `templates/checkin.html`, route tests)
- After file import or MT5 sync, `queue_bundle_review_if_split_candidates` in `helpers/core.py` may set `bundle_review_requested_at` when `detect_outliers` finds split candidates (`routes/trades.py`, `routes/mt5_internal.py`, `tests/test_bundle_review_queue.py`)
- Optional per trade account: `default_trade_profile_id` on `TradeAccount` (null by default) tags **new** import/MT5-sync rows via `resolve_import_default_trade_profile_ids` + `build_normalized_trade_insert_batch` (`migrations/versions/20260405_0037_trade_account_default_strategy.py`, Trade Accounts dialog + `trade_accounts_page.js`, `tests/test_trading_import.py`)
- Strategies page quick action can set the active trade account default strategy directly from a strategy card (`routes/trade_profiles.py`, `templates/trade_profiles.html`, `tests/test_trade_profiles_routes.py`)
- App layout: centered narrow hero band (`--app-hero-max-width` on `body.app-layout`, `dash-head`, `app-page-hero`, `analytics-hero`) with full-width panels below
- Dashboard and analytics UX pass: grouped KPI sections (at-a-glance net PnL + edge + execution), collapsible session/pair/weekday breakdowns with chart render on open, trade and journal deep links from analytics, `?pair=` / `?session=` auto-filter on dashboard and trades tables (`trade_filters_shared.js`, `dashboard_page.js`, `trades_table.js`), dashboard primary-story line and small-sample win-rate note, calmer onboarding (pulse animation removed), analytics cache prefix bump to `analytics_v5` (`routes/dashboard.py`, `helpers/behavior_labels.py`, templates, tests)
- AI role/taste guidance refinement in `context/ROLES.md`, `AGENTS.md`, and `CLAUDE.md`
- Public acquisition messaging polish across `auth_account.py`, `templates/landing.html`, `templates/seo_page.html`, `templates/register.html`, `templates/login.html`, and `templates/base.html`
- Second-pass landing/auth copy tightening for clearer review-first positioning and lower perceived signup friction
- Landing and SEO hero sections originally surfaced live MT5 beta availability from `get_mt5_sync_batch_state()`; as of 2026-05-17, public copy no longer exposes exact slot counts and instead frames MT5 as setup-capacity-managed inside the 14-day premium workflow trial, while keeping the import-first fallback honest.
- Public SEO: `robots.txt` allows `/login`, `/register`, and `/dashboard`; sitemap lists `/`, `/dashboard`, `/login`, `/register`, contact, legal, and SEO landing slugs; signed-out `/dashboard` serves an indexable gate page (`dashboard_public_gate.html`); login/register use dedicated titles, meta descriptions, and canonicals; `base.html` adds `og:locale` and `WebSite` JSON-LD; contact page title refined for SERPs
- SEO content refresh: new `/trading-journal` and `/why-am-i-not-improving-in-trading` pages added; `/mt5-trading-journal` rewritten with mistake-first positioning ("find your biggest trading mistake — without turning trading into a second job"); all three emphasize automated MT5 sync, zero manual logging, and weekly review over feature lists; Explore dropdown and landing use-case grid updated to include new pages (`auth_account.py`, `templates/seo_page.html`, `templates/landing.html`)

## Next Priorities

- [ ] Manual browser pass on MT5 batch badge + dashboard queued/setting-up states
- [ ] Decide whether to keep or retire legacy admin approve/reject endpoints for old MT5 request rows
- [ ] Verify ready-email trigger path against live worker/email config
- [ ] Review live conversion response to the landing/auth messaging refresh
