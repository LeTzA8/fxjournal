# CURRENT_STATE

Short-term operational memory.

## Current Focus

- MT5 request/setup flow polish
- Dashboard MT5 state and weekly AI display
- Test coverage around MT5 request/admin paths and ready-email behavior

## Recent Meaningful Changes

- MT5 request flow is being reshaped in `routes/trade_accounts.py`, `templates/trade_accounts.html`, and `static/js/mt5_request_form.js`
- Dashboard MT5 messaging and weekly AI presentation are being adjusted in `routes/dashboard.py` and `templates/index.html`
- Worker/admin MT5 flow is moving through `auth_account.py`, `helpers/core.py`, and `celery_workers/mt5_setup.py`

## Known Issues

- Active app changes already exist in MT5/dashboard files
- `templates/analytics.html` is dirty in the worktree but unrelated to this context cleanup

## Next Priorities

- Finish MT5 request/setup behavior work
- Verify dashboard MT5 messaging against actual account state
- Verify ready-email trigger path
