# FX Journal

FX Journal is a Flask trading journal for logging trades, reviewing performance, and organizing activity by trade account. It supports both CFD/FX workflows and futures workflows, including MT5 XLSX imports for CFD accounts and Tradovate CSV imports for futures accounts.

## What It Does

- Secure account system with registration, email verification, login, logout, and password reset
- Account management with email-change confirmation flow and account deletion
- Multi-account journaling with per-user trade accounts and active-account switching
- Manual trade entry and editing for CFD and futures trades
- Trade profile / strategy management with version history
- Dashboard and analytics views driven from stored trade history
- AI-generated weekly dashboard review support
- Import pipelines for:
  - MT5 Positions XLSX
  - Tradovate Performance CSV
- Deduplication safeguards for imported trades
- Contact form with persistence fallback when email delivery is unavailable
- Rate-limited destructive and sensitive actions

## Current App Structure

The app no longer keeps all route logic in one file.

- `app.py`
  Slim app bootstrap: Flask config, extension initialization, middleware, error handlers, blueprint registration
- `auth_account.py`
  Public/auth routes and admin access-control routes
- `routes/`
  Domain blueprints for dashboard, trades, trade accounts, trade profiles, account, and contact
- `helpers.py`
  Shared route helpers for timezone handling, trade account resolution, pubkey lookups, trade profile helpers, dedupe keys, and account cleanup
- `utils.py`
  Shared low-level utilities such as environment parsing, `utcnow_naive()`, and `@login_required`
- `trading.py`
  Trading math, symbol metadata, analytics, and import parsers
- `models.py`
  SQLAlchemy models
- `tests/`
  Pytest coverage for trading math, import parsing, and auth token flows

## Main Route Groups

### Public and Auth

- `/`
- `/login`
- `/register`
- `/verify-email/pending`
- `/verify-email/<token>`
- `/password/forgot`
- `/password/reset/<token>`
- `/logout`
- `/privacy`
- `/terms`

### Dashboard

- `/dashboard`
- `/dashboard/analytics`

### Trades

- `/dashboard/trades`
- `/dashboard/trades/manage`
- `/dashboard/trades/new`
- `/dashboard/trades/<trade_pubkey>`
- `/dashboard/trades/<trade_pubkey>/edit`
- `/dashboard/trades/<trade_pubkey>/delete`
- `/dashboard/trades/bulk-delete`
- `/dashboard/trades/batch-profile`
- `/dashboard/import`
- `/dashboard/imports/delete`

### Trade Accounts

- `/dashboard/trade-accounts`
- `/dashboard/trade-accounts/switch`
- `/dashboard/trade-accounts/<trade_account_pubkey>/default`
- `/dashboard/trade-accounts/<trade_account_pubkey>/update`
- `/dashboard/trade-accounts/<trade_account_pubkey>/delete`
- `/dashboard/trade-accounts/delete-all`

### Trade Profiles / Strategies

- `/dashboard/trade-profiles`
- `/dashboard/strategies`
- `/dashboard/trade-profiles/<profile_pubkey>/edit`
- `/dashboard/trade-profiles/<profile_pubkey>/archive`

### Account and Contact

- `/account`
- `/account/email-change/cancel`
- `/account/email-change/<token>`
- `/account/password-reset-email`
- `/account/delete`
- `/contact`

## Tech Stack

- Python
- Flask
- SQLAlchemy
- Flask-Migrate / Alembic
- Flask-WTF
- Flask-Limiter
- Jinja templates
- PostgreSQL in deployed environments
- SQLite fallback for local development

## Data Model Highlights

The app stores more than just users and trades. Core entities include:

- `User`
- `Trade`
- `TradeAccount`
- `TradeProfile`
- `TradeProfileVersion`
- `CFDSymbol`
- `FuturesSymbol`
- `ContactSubmission`
- AI review history models

Trades are tied to a user and a trade account. Futures trades can also carry a contract code. Imported trades store import signatures and dedupe keys to prevent accidental re-imports.

## Imports and Trading Logic

FX Journal includes custom parsing and math logic for trade journaling:

- PnL calculation for FX, metals, and futures
- Pip calculation for standard and JPY pairs
- Tick calculation for futures contracts
- Exit-price derivation from desired PnL
- Import profile detection before parsing
- Trade deduplication across repeated imports

The test suite includes fixed-value examples for these calculations so the expected math is visible and easy to verify.

## Local Setup

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Configure environment variables.

The app reads from `.env`. Important variables include:

- `SECRET_KEY`
- `TOKEN_SALT`
- `DATABASE_URL`
- `RATELIMIT_STORAGE_URI`
- `MAX_UPLOAD_MB`
- `APP_ENV`
- `FEEDBACK_TO_EMAIL`

4. Run migrations:

```bash
flask --app app db upgrade
```

5. Start the app:

```bash
flask --app app run
```

If `DATABASE_URL` is not set, the app falls back to local SQLite for development.

## Testing

Pytest is configured for the project.

Run the test suite with:

```bash
pytest -v
```

Current automated coverage focuses on:

- trading math
- import parsing
- auth token generation and verification

## Design Goals

FX Journal is built to stay practical rather than overloaded:

- clean journaling workflow
- account-aware organization
- understandable analytics
- import support for common retail platforms
- maintainable Flask structure with shared helpers and blueprints

## Notes

- Flash messages are used for one-time UI feedback after redirects.
- Sensitive POST endpoints are rate limited.
- Public and authenticated flows share the same app but are organized by domain.

Note: Portions of this README were refined with assistance from AI tools (ChatGPT/OpenAI Codex).
