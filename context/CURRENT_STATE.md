# CURRENT_STATE

Last Updated: 2026-04-05

Short-term operational memory.

## Current Focus

- MT5 request/setup flow polish
- Dashboard MT5 state and weekly AI display
- Test coverage around MT5 request/admin paths and ready-email behavior

## Active Areas

- MT5 request flow work in `routes/trade_accounts.py`, `templates/trade_accounts.html`, and `static/js/mt5_request_form.js`
- Dashboard MT5 messaging and weekly AI presentation work in `routes/dashboard.py` and `templates/index.html`
- Worker/admin MT5 flow work in `auth_account.py`, `helpers/core.py`, and `celery_workers/mt5_setup.py`
- AI role/taste guidance refinement in `context/ROLES.md`, `AGENTS.md`, and `CLAUDE.md`
- Public acquisition messaging polish across `auth_account.py`, `templates/landing.html`, `templates/seo_page.html`, `templates/register.html`, `templates/login.html`, and `templates/base.html`
- Second-pass landing/auth copy tightening for clearer review-first positioning and lower perceived signup friction

## Next Priorities

- [ ] Finish MT5 request/setup work
- [ ] Verify dashboard MT5 messaging against actual account state
- [ ] Verify ready-email trigger path
- [ ] Review live conversion response to the landing/auth messaging refresh
