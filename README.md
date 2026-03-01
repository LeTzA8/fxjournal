# FX Journal

#### Video Demo: <URL HERE>

#### Description:

FX Journal is a full-stack web application designed to help traders journal and visualize their trading performance in a clean and structured way. The goal of this project is to provide a simple and intuitive platform for logging trades across multiple asset classes (forex, indices, metals, futures) without overcomplicating statistical analysis.

The application allows users to create accounts, log in securely, and manage their own trade history. Each user's data is stored independently in a PostgreSQL database, ensuring persistence and proper separation of data between users.

---

## Core Features

### Authentication

The project includes a complete authentication system:

- User registration
- Secure login and logout
- Password hashing
- Session-based authentication

User credentials are securely stored, and sessions maintain login state across requests.

---

### Dashboard ("/")

The dashboard acts as the main landing page after login. It provides a summary of the trader’s performance, including:

- Total profit/loss
- Win rate
- Number of trades
- Monthly profit
- Average risk-reward ratio (RR)

All statistics are calculated dynamically from trade data stored in the database.

---

### Trades Page ("/trades")

The trades page allows users to:

- View all historical trades
- Filter and sort trades
- Edit trades
- Delete trades

This page implements full CRUD functionality (Create, Read, Update, Delete) using SQLAlchemy ORM connected to PostgreSQL.

---

### Individual Trade View

Users can open individual trades to view:

- Entry and exit price
- Position size
- Profit/loss
- Risk-reward ratio
- Notes attached to the trade

This allows both quantitative tracking and qualitative journaling.

---

## Technical Design

The application is built using:

- Python (Flask framework)
- SQLAlchemy ORM
- PostgreSQL database
- HTML, CSS, and Bootstrap
- Jinja templating engine

### Backend

Flask routes in `app.py` handle all HTTP requests.  
SQLAlchemy models represent database tables, including:

- `users`
- `trades` (linked to users via foreign key)

Foreign key relationships enforce data integrity between users and their trades.

---

### Database Choice

PostgreSQL was chosen instead of SQLite to simulate a production-ready environment. PostgreSQL provides:

- Better concurrency handling
- Improved scalability
- Stronger relational integrity

The database is hosted separately from the web server.

---

### Deployment

The application is deployed on a cloud platform and connected to a managed PostgreSQL instance. Environment variables are used to store database connection details securely.

---

## Design Philosophy

Many commercial trading journals are complex and expensive. FX Journal focuses on:

- Simplicity
- Clear statistics
- Minimal UI clutter
- Practical usability

The objective was to create a clean and understandable system rather than an overly feature-rich platform.

---

## Future Improvements

Possible future enhancements include:

- Trade import functionality (Excel/CSV)
- Advanced analytics and charts
- Performance breakdown by instrument
- Subscription-based monetization
- REST API endpoints

---

## Educational Value

This project demonstrates understanding of:

- Full-stack development
- Authentication systems
- Relational database design
- ORM abstraction
- Server-side rendering
- Cloud deployment
- Persistent data storage

It represents a progression from structured problem sets toward building a real-world web application with authentication, database relationships, and deployment.

---

Note: Portions of this README were refined with assistance from AI tools (ChatGPT/OpenAI Codex).