# FX Journal

#### Description:

FX Journal is a full-stack web application designed to help traders journal and visualize trading performance in a clean and structured way. The app supports account-based access, per-user trade history, and performance tracking backed by PostgreSQL.

---

## Core Features

### Authentication

The project includes a complete authentication system:

- User registration
- Email verification flow
- Secure login and logout
- Password hashing
- Session-based authentication
- Forgot/reset password flow

User credentials are securely stored, and sessions maintain login state across requests.

---

### Public Landing (`/`)

The root route is a public landing page with entry points to login/register and dashboard shortcuts for signed-in users.

---

### Dashboard (`/dashboard`)

The dashboard is the authenticated home page. It provides a summary of trading performance, including:

- Total profit/loss
- Win rate
- Number of trades
- Weekly and monthly metrics
- Average risk-reward ratio (RR)
- Equity chart points derived from closed trades

All statistics are calculated dynamically from user trade data.

---

### Trades (`/dashboard/trades`)

The trades area supports:

- Viewing historical trades
- Filtering and sorting
- Creating new trades (`/dashboard/trades/new`)
- Editing trades (`/dashboard/trades/<int:trade_id>/edit`)
- Deleting trades (`/dashboard/trades/<int:trade_id>/delete`)
- MT5 `.xlsx` import (`/dashboard/import`)
- Imported batch deletion (`/dashboard/imports/delete`)

This implements full CRUD using SQLAlchemy ORM connected to PostgreSQL.

---

### Individual Trade View (`/dashboard/trades/<int:trade_id>`)

Users can open an individual trade to review:

- Entry and exit price
- Position size
- Profit/loss
- Pips and risk context
- Trade notes

This supports both quantitative tracking and qualitative journaling.

---

### Account (`/account`)

Users can update profile data (username/email), review account stats, and trigger a password reset email from the account area.

---

## Route Map (`app.py`)

| Method(s) | Route | Purpose |
|---|---|---|
| `GET` | `/` | Public landing page |
| `GET` | `/dashboard` | Authenticated dashboard |
| `GET`, `POST` | `/login` | User login |
| `GET`, `POST` | `/register` | User registration |
| `GET`, `POST` | `/verify-email/pending` | Verification pending + resend flow |
| `GET` | `/verify-email/resend` | Alias redirect to verification pending |
| `GET` | `/verify-email/<token>` | Verify email token |
| `GET`, `POST` | `/password/forgot` | Request password reset email |
| `GET`, `POST` | `/password/reset/<token>` | Reset password with token |
| `POST` | `/logout` | Logout current session |
| `GET`, `POST` | `/account` | Account profile and updates |
| `POST` | `/account/password-reset-email` | Send reset email from account page |
| `GET` | `/dashboard/trades` | Trades list |
| `GET`, `POST` | `/dashboard/trades/new` | Create trade |
| `POST` | `/dashboard/import` | Import MT5 `.xlsx` trades |
| `GET` | `/dashboard/trades/<int:trade_id>` | Trade detail |
| `GET`, `POST` | `/dashboard/trades/<int:trade_id>/edit` | Edit trade |
| `POST` | `/dashboard/trades/<int:trade_id>/delete` | Delete single trade |
| `POST` | `/dashboard/imports/delete` | Delete imported trade batch |

---

## Technical Design

The application is built using:

- Python (Flask)
- SQLAlchemy ORM
- PostgreSQL
- HTML, CSS, and Bootstrap
- Jinja templating

### Backend

Flask routes in `app.py` handle HTTP requests and server-rendered pages.

SQLAlchemy models represent database tables, including:

- `users`
- `trades` (linked to users via foreign key)

Foreign key relationships enforce data integrity between users and their trades.

---

### Database Choice

PostgreSQL was chosen instead of SQLite to simulate a production-oriented environment with stronger concurrency and scalability characteristics.

---

### Deployment

The application is deployed on a cloud platform and connected to a managed PostgreSQL instance. Environment variables are used for sensitive configuration.

---

## Design Philosophy

Many trading journals are complex and expensive. FX Journal focuses on:

- Simplicity
- Clear statistics
- Minimal UI clutter
- Practical usability

The objective is a clean and understandable system rather than an overly feature-heavy one.

---

## Future Improvements

Possible future enhancements include:

- CSV import support in addition to MT5 Excel import
- Advanced analytics and charts
- Performance breakdown by instrument/session
- API endpoints for integrations

---

## Educational Value

This project demonstrates:

- Full-stack development
- Authentication and session management
- Relational database design
- ORM abstraction
- Server-side rendering
- Deployment with persistent storage

---

Note: Portions of this README were refined with assistance from AI tools (ChatGPT/OpenAI Codex).
