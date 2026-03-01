from datetime import datetime, timedelta

from flask import Flask, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-in-production"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///FXJournal.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

def calc_pnl(trade):
    if trade.exit_price is None:
        return None  # still open

    diff = trade.exit_price - trade.entry_price
    if trade.side == "SELL":
        diff = -diff

    # simple placeholder formula (adjust later)
    return diff * 100000 * trade.lot_size


def resolve_pnl(trade):
    if trade.pnl is not None:
        return trade.pnl
    return calc_pnl(trade)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    trades = db.relationship("Trade", backref="user", lazy=True)


class Trade(db.Model):
    __tablename__ = "trades"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    symbol = db.Column(db.String(20), nullable=False, default="EURUSD")
    side = db.Column(db.String(10), nullable=False, default="BUY")
    entry_price = db.Column(db.Float, nullable=False, default=0.0)
    exit_price = db.Column(db.Float, nullable=True)
    lot_size = db.Column(db.Float, nullable=False, default=0.01)
    pnl = db.Column(db.Float, nullable=True)
    trade_note = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


with app.app_context():
    db.create_all()


@app.route("/")
def home():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    username = session.get("username", "User")
    user_id = session["user_id"]

    try:
        user_trades = (
            Trade.query.filter_by(user_id=user_id)
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        user_trades = []

    now = datetime.utcnow()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    closed = []
    for trade in user_trades:
        pnl_value = resolve_pnl(trade)
        if pnl_value is not None:
            closed.append((trade, pnl_value))

    total_closed = len(closed)
    wins = sum(1 for _, pnl in closed if pnl > 0)
    losses = sum(1 for _, pnl in closed if pnl < 0)
    win_rate = (wins / total_closed * 100) if total_closed else 0.0

    net_pnl_week = sum(
        pnl for trade, pnl in closed if trade.opened_at and trade.opened_at >= week_start
    )
    trades_this_month = sum(
        1
        for trade in user_trades
        if trade.opened_at
        and trade.opened_at.month == now.month
        and trade.opened_at.year == now.year
    )

    avg_win = (sum(p for _, p in closed if p > 0) / wins) if wins else 0
    avg_loss = (abs(sum(p for _, p in closed if p < 0)) / losses) if losses else 0
    avg_rr = (avg_win / avg_loss) if avg_loss else None

    recent_trades = []
    for trade in user_trades:
        pnl_value = resolve_pnl(trade)
        trade_date = trade.opened_at.strftime("%d %b %Y") if trade.opened_at else "-"
        trade_date_value = trade.opened_at.strftime("%Y-%m-%d") if trade.opened_at else ""
        recent_trades.append(
            {
                "symbol": trade.symbol,
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "lot_size": trade.lot_size,
                "pnl": pnl_value,
                "date": trade_date,
                "date_value": trade_date_value,
                "status": "Closed" if trade.exit_price is not None else "Running",
            }
        )

    chart_points = []
    closed_recent = list(reversed(closed[:30]))
    if closed_recent:
        for trade, pnl in closed_recent:
            chart_points.append(
                {
                    "label": trade.opened_at.strftime("%d %b"),
                    "pnl": round(float(pnl), 2),
                }
            )

    return render_template(
        "index.html",
        title="FX Journal",
        username=username,
        win_rate=win_rate,
        net_pnl_week=net_pnl_week,
        trades_this_month=trades_this_month,
        avg_rr=avg_rr,
        recent_trades=recent_trades,
        chart_points=chart_points,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("home"))

    created = request.args.get("created") == "1"

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            session["username"] = user.username
            return redirect(url_for("home"))

        return render_template(
            "login.html",
            title="Login | FX Journal",
            body_class="auth-layout",
            error="Invalid email or password.",
            success="Account created. Please log in." if created else None,
        )
    return render_template(
        "login.html",
        title="Login | FX Journal",
        body_class="auth-layout",
        success="Account created. Please log in." if created else None,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not username or not email or not password:
            return render_template(
                "register.html",
                title="Register | FX Journal",
                body_class="auth-layout",
                error="All fields are required.",
            )

        existing_user = User.query.filter(
            or_(User.username == username, User.email == email)
        ).first()
        if existing_user:
            return render_template(
                "register.html",
                title="Register | FX Journal",
                body_class="auth-layout",
                error="Username or email already exists.",
            )

        hashed_password = generate_password_hash(password)
        user = User(username=username, email=email, password=hashed_password)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for("login", created="1"))

    return render_template(
        "register.html",
        title="Register | FX Journal",
        body_class="auth-layout",
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/trades")
def trades():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    username = session.get("username", "User")
    user_id = session["user_id"]
    try:
        user_trades = (
            Trade.query.filter_by(user_id=user_id)
            .order_by(Trade.opened_at.desc())
            .all()
        )
    except OperationalError:
        db.session.rollback()
        user_trades = []


    return render_template(
        "trades.html",
        title="My Trades | FX Journal",
        username=username,
        trades=user_trades,
    )

@app.route("/trades/new", methods=["GET", "POST"])
def new_trade():
    if not session.get("user_id"):
        return redirect(url_for("login"))

    if request.method == "POST":
        symbol = request.form.get("symbol", "").strip().upper()
        side = request.form.get("side", "BUY").strip().upper()
        entry_price = float(request.form.get("entry_price", 0.0))
        exit_price = request.form.get("exit_price", "").strip()
        exit_price = float(exit_price) if exit_price else None
        lot_size = float(request.form.get("lot_size", 0.01))
        trade_note = request.form.get("trade_note", "").strip()
        pnl = request.form.get("pnl", "").strip()
        pnl = float(pnl) if pnl else None
        trade_date_raw = request.form.get("trade_date", "").strip()
        try:
            opened_at = (
                datetime.strptime(trade_date_raw, "%Y-%m-%d")
                if trade_date_raw
                else datetime.utcnow()
            )
        except ValueError:
            opened_at = datetime.utcnow()

        if pnl is not None and exit_price is None:
            exit_price = entry_price + (pnl / (100000 * lot_size)) * (1 if side == "BUY" else -1)
        if pnl is None and exit_price is not None:
            pnl = calc_pnl(Trade(
                user_id=session["user_id"],
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                lot_size=lot_size,
            ))
        trade = Trade(
            user_id=session["user_id"],
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            lot_size=lot_size,
            trade_note=trade_note,
            pnl=pnl,
            exit_price=exit_price,
            opened_at=opened_at,
        )
        db.session.add(trade)
        db.session.commit()
        return redirect(url_for("trades"))

    return render_template(
        "trade_entry.html",
        title="New Trade | FX Journal",
        username=session.get("username", "User"),
        trade=None,
        form_action=url_for("new_trade"),
        form_mode="new",
    )


@app.route("/trades/<int:trade_id>")
def trade_detail(trade_id):
    if not session.get("user_id"):
        return redirect(url_for("login"))

    trade = Trade.query.filter_by(id=trade_id, user_id=session["user_id"]).first_or_404()
    trade_pnl = resolve_pnl(trade)

    return render_template(
        "trade_detail.html",
        title="Trade Detail | FX Journal",
        username=session.get("username", "User"),
        trade=trade,
        trade_pnl=trade_pnl,
    )


@app.route("/trades/<int:trade_id>/edit", methods=["GET", "POST"])
def edit_trade(trade_id):
    if not session.get("user_id"):
        return redirect(url_for("login"))

    trade = Trade.query.filter_by(id=trade_id, user_id=session["user_id"]).first_or_404()

    if request.method == "POST":
        symbol = request.form.get("symbol", "").strip().upper()
        side = request.form.get("side", "BUY").strip().upper()
        entry_price = float(request.form.get("entry_price", 0.0))
        exit_price = request.form.get("exit_price", "").strip()
        exit_price = float(exit_price) if exit_price else None
        lot_size = float(request.form.get("lot_size", 0.01))
        trade_note = request.form.get("trade_note", "").strip()
        pnl = request.form.get("pnl", "").strip()
        pnl = float(pnl) if pnl else None
        trade_date_raw = request.form.get("trade_date", "").strip()
        try:
            opened_at = (
                datetime.strptime(trade_date_raw, "%Y-%m-%d")
                if trade_date_raw
                else trade.opened_at
            )
        except ValueError:
            opened_at = trade.opened_at

        if pnl is not None and exit_price is None:
            exit_price = entry_price + (pnl / (100000 * lot_size)) * (1 if side == "BUY" else -1)
        if pnl is None and exit_price is not None:
            pnl = calc_pnl(
                Trade(
                    user_id=session["user_id"],
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    lot_size=lot_size,
                )
            )

        trade.symbol = symbol
        trade.side = side
        trade.entry_price = entry_price
        trade.exit_price = exit_price
        trade.lot_size = lot_size
        trade.trade_note = trade_note
        trade.pnl = pnl
        trade.opened_at = opened_at

        db.session.commit()
        return redirect(url_for("trades"))

    return render_template(
        "trade_entry.html",
        title="Edit Trade | FX Journal",
        username=session.get("username", "User"),
        trade=trade,
        form_action=url_for("edit_trade", trade_id=trade.id),
        form_mode="edit",
    )


@app.route("/trades/<int:trade_id>/delete", methods=["POST"])
def delete_trade(trade_id):
    if not session.get("user_id"):
        return redirect(url_for("login"))

    trade = Trade.query.filter_by(id=trade_id, user_id=session["user_id"]).first_or_404()
    db.session.delete(trade)
    db.session.commit()
    return redirect(url_for("trades"))

if __name__ == "__main__":
    app.run(debug=True)
