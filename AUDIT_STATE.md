# MyFXJournal — Audit State
Last updated: 2026-04-11

---

## How To Use
- Say "run weekly audit" for the 15 min check
- Say "run monthly audit" for the 30 min check
- Only log issues that could break the site
  or compromise user data
- Everything else goes in Trello
- Mark issues resolved by deleting the row
  and adding to Recently Resolved

---

## Severity Definitions
High   → happening now, affects users or exposes data
         fix within 24 hours
Medium → will cause real damage if left too long
         fix within the week
Monitor → not dangerous now, becomes dangerous later
          noted so it isn't forgotten

---

## What Changed Since Last Audit
Last audit: 2026-04-11 (first run — weekly + monthly)

- Found: analytics cache not invalidating on trade changes (analytics_v4/v5 missing from invalidate list)
- Found: cryptography library 3 CVEs — used for MT5 investor password encryption
- Found: requests and pygments also have CVEs, lower priority
- Passed: admin gating, IDOR protection, session cookies, MT5 password storage,
  no secrets in templates or JS, .gitignore coverage, rate limits on all sensitive routes

---

## Active Issues
Issues that are breaking something or exposing risk right now.

| Issue | Area | Severity | First Seen | Next Action |
|-------|------|----------|------------|-------------|

---

## Watching
Issues that are not dangerous now but will be if ignored.
Includes architecture observations deferred until user growth justifies action.

| Issue | Area | Why Not Now | Act When |
|-------|------|-------------|----------|
| `PENDING_REGISTRATIONS = {}` in `auth_account.py:44` is a process-local dict — lost on worker restart and invisible across web processes. Signup confirmation codes vanish silently if the process restarts mid-flow. | `auth_account.py` | Single-process deploy, not currently triggered | Move to multi-process or horizontal scaling |
| `User.query.filter_by(id=user_id).first()` runs on every authenticated request inside `inject_trade_account_context()` in `app.py`. One extra DB hit per page load for all logged-in users. | `app.py` | User count too low for this to be measurable | ~500 active users when dashboard load becomes noticeably slow |
| `invalidate()` in `cache.py` maintains a hand-curated list of every versioned cache prefix. A prefix bump requires remembering to update this list — the current analytics_v4/v5 bug is a direct consequence. Will recur on the next prefix bump. | `celery_workers/cache.py` | Low risk while cache versions stay stable | After the current analytics bug is fixed — consider a prefix registry or glob-style scan instead of a manual list |
| MT5Account uses `ondelete=SET NULL` + session event `mark_for_cleanup()` to handle user deletion. Not verified to fire correctly in production — only covered in tests. Orphaned encrypted passwords would persist in DB if this is broken. | `models.py` / `auth_account.py` | Not confirmed broken — tests pass | Verify once in prod by deleting a test account and confirming the MT5Account row gets the cleanup flag set |

---

## Recently Resolved
Keep last 5 only. Delete oldest when adding new.

| Issue | Resolved | How |
|-------|----------|-----|
| Analytics cache not invalidating after trade changes (analytics_v4/v5 missing from invalidate list) | 2026-04-11 | Added analytics_v4 and analytics_v5 to invalidate() in cache.py |
| cryptography 3 CVEs + requests + pygments CVEs | 2026-04-11 | Upgraded cryptography 45.0.7→46.0.7, requests 2.32.5→2.33.1, pygments 2.19.2→2.20.0 |
| MT5 sync fetching recent deals late on non-UTC brokers | 2026-03-31 | Shift history_deals_get window into broker time before normalising back to UTC |
| Celery built-in task lifecycle logs polluting worker output | 2026-04-03 | SuppressCeleryTraceTaskLogs filter in celery_app.py |
| MT5 chart bars misaligned with entry/exit markers | 2026-04-11 | Apply same _adjust_mt5_unix_epoch delta to bar times as to deal times |

---

## Weekly Audit
*Run time target: 15 minutes*

When I say "run weekly audit" check only these:

### Site Health
☐  Site is up and loading correctly
☐  No 500 errors in Render web service logs
   this week
☐  Error count same or lower than last week
   if higher investigate immediately
☐  No unhandled exceptions in worker logs

### Core Features Working
☐  MT5 sync running on schedule
   beat firing every 5 minutes
   tasks succeeding not retrying
☐  AI weekly reviews generating successfully
   no stuck or failed tasks
☐  Email delivery working
   check Resend dashboard for failures
☐  User login and signup working
   including Google OAuth

### Data Integrity
☐  No duplicate trades being imported
☐  Trade times showing correctly
   not sync time or wrong timezone
☐  Correct account syncing to correct
   trade account in DB

### Security Pulse
☐  No suspicious activity in logs
   unusual IPs, repeated failed logins
   unexpected spikes in requests
☐  Internal MT5 sync endpoint still
   requiring correct secret header
☐  No sensitive data appearing in
   any log output

### After Weekly Audit
→  update What Changed Since Last Audit
→  add any new Active Issues found
→  remove resolved issues
→  update Recently Resolved
→  update date at top of file

---

## Monthly Audit
*Run time target: 30 minutes*
*Run the weekly audit first then continue:*

### Security
☐  All admin routes still protected
   spot check 3 admin pages directly
☐  No way to access another user's data
   by changing an ID in the URL
   test this manually with 2 test accounts
☐  Session cookies have secure, httponly,
   samesite flags configured correctly
☐  MT5 investor passwords encrypted in DB
   never appearing in logs or error messages
☐  No API keys or secrets in any template,
   JS file, or public facing code
☐  Private repo has no sensitive files
   accidentally committed
☐  .gitignore still covering all
   sensitive files
☐  Rate limiting active on login, register,
   password reset, and contact form

### Backups
☐  Render PostgreSQL last backup ran
   within the last 24 hours
☐  You know how to restore from backup
   right now if needed
   if not, figure this out before continuing

### User Data
☐  Account deletion removes all user data
   trades, profiles, checkins, MT5 accounts
☐  AI responses anonymised correctly
   on user deletion not deleted entirely
☐  No orphaned data from deleted accounts

### Dependencies
☐  Run pip-audit on requirements
   note any high severity vulnerabilities
   do not fix during audit
   schedule as dedicated task if found

### Architecture Watch
☐  Review Watching table
   has anything crossed the threshold
   where action is now needed?
☐  Any new technical debt this month
   worth adding to Watching?

### After Monthly Audit
→  complete weekly audit updates
→  add any new findings to correct table
→  schedule any critical fixes found
   as dedicated tasks not fixed now
→  update date at top of file

---

## Fix Rules
These apply to any fix attempted during an audit.
If any condition fails, log it and move on.

☐  Change is purely cosmetic or copy only
   zero logic, routes, or queries touched
☐  Does not touch base template or any
   shared partial used across multiple pages
☐  Zero backend involvement
   no Python, models, routes, or tasks
☐  Reversible in under 1 minute
☐  Visually verified in browser
   before marking as done

100% confident on all five = fix now
Any doubt at all = log in Active Issues
and fix in a dedicated session

A logged issue is always safer than
a new problem introduced during audit.
