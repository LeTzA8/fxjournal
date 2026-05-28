# Waitlist CTA QA Fixtures

Last updated: 2026-05-28

## Purpose

This document describes the implemented QA fixture system for manually testing contextual waitlist CTAs. The fixtures create six deterministic dummy users, each representing one product state:

- Replay lock
- AI follow-up limit reached
- MT5 trial expired
- MT5 sync paused
- Zero-data baseline
- Normal active control

All fixture users use `dummy-cta-*@myfxjournal.test` emails and password `TestPassword123!`.

## Commands

Run from the Flask app root:

```bash
flask seed-waitlist-cta-test-data
flask reset-waitlist-cta-test-data
```

Supported flags:

- `--scenario <key>` seeds or resets one scenario.
- `--reset` deletes selected fixture users before seeding.
- `--allow-production` is required when the environment looks production-like.
- `--yes` is required for production-classified non-TTY runs and skips the interactive prompt.

Production detection checks `APP_ENV`, `FLASK_ENV`, and `PUBLIC_BASE_URL`. In an interactive production-classified shell, the command also requires typing `I UNDERSTAND`.

## Safety Invariants

- Fixture operations only target emails matching `^dummy-cta-[a-z0-9_-]+@myfxjournal\.test$`.
- Reset refuses to delete a matching dummy user if the user has no `qa_test_accounts` sidecar row.
- Seed and reset create users directly through ORM code; they do not call registration routes.
- Seed does not call `send_email_placeholder`, Celery, OpenAI, MT5 dispatch helpers, or MT5 terminals.
- Full fixture reseed deletes and recreates the selected fixture set in one transaction batch.
- Admin re-seed and delete actions are root-admin-only.

## Data Model

The `qa_test_accounts` sidecar table stores fixture metadata only:

- `user_id`
- `scenario_key`
- `label`
- `fixture_version`
- `notes`
- `test_path`
- `expected_cta_json`
- seed and verification timestamps

It intentionally does not add fixture flags to the `users` table.

## Admin Page

Admins can view fixture state at:

```text
/dashboard/admin/access/test-accounts
```

The page lists the six registry scenarios, expected CTA metadata, fixture email, test path, notes, and seed status. Notes are admin-editable. Re-seed and delete actions are root-admin-only and still validate dummy email plus sidecar ownership before changing data.

## Scenario Keys

| Key | Email | Test path | Expected CTA |
| --- | --- | --- | --- |
| `replay_lock` | `dummy-cta-replay@myfxjournal.test` | Trade detail | `source=replay_lock`, `feature_interest=advanced_replay`, `cta_context=trade_replay_1m` |
| `ai_followup` | `dummy-cta-ai-followup@myfxjournal.test` | Dashboard | `source=ai_followup_lock`, `feature_interest=weekly_followup_chat`, `cta_context=weekly_followup_trial_limit` |
| `mt5_expired` | `dummy-cta-mt5-expired@myfxjournal.test` | Dashboard | extension + MT5 sync waitlist CTAs |
| `mt5_paused` | `dummy-cta-mt5-paused@myfxjournal.test` | Dashboard | `source=mt5_trial_expired`, `feature_interest=mt5_sync`, `cta_context=mt5_sync_paused_waitlist` |
| `zero_data` | `dummy-cta-zero-data@myfxjournal.test` | Dashboard | No lock CTA expected |
| `normal_active` | `dummy-cta-normal-active@myfxjournal.test` | Dashboard | No lock CTA expected |

## Verification

Focused tests:

```bash
.\.venv\Scripts\pytest.exe tests/test_qa_waitlist_cta_fixtures.py tests/test_qa_waitlist_cta_fixture_scenarios.py tests/test_qa_test_accounts_admin.py -q
```

Manual QA should log in as each fixture user, open the listed test path, submit the waitlist CTA where expected, and confirm the resulting row appears on the admin waitlist page with matching `source`, `feature_interest`, and `cta_context`.
