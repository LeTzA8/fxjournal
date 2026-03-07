from datetime import datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    verification_sent_at = db.Column(db.DateTime, nullable=True)
    trade_accounts = db.relationship(
        "TradeAccount", backref="user", lazy=True, cascade="all, delete-orphan"
    )
    trades = db.relationship(
        "Trade", backref="user", lazy=True, cascade="all, delete-orphan"
    )


class TradeAccount(db.Model):
    __tablename__ = "trade_accounts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False, default="Main Account")
    external_account_id = db.Column(db.String(80), nullable=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    trades = db.relationship(
        "Trade", backref="trade_account", lazy=True, cascade="all, delete-orphan"
    )


class Trade(db.Model):
    __tablename__ = "trades"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    trade_account_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_accounts.id"),
        nullable=True,
        index=True,
    )
    symbol = db.Column(db.String(20), nullable=False, default="EURUSD")
    side = db.Column(db.String(10), nullable=False, default="BUY")
    entry_price = db.Column(db.Float, nullable=False, default=0.0)
    exit_price = db.Column(db.Float, nullable=True)
    lot_size = db.Column(db.Float, nullable=False, default=0.01)
    pnl = db.Column(db.Float, nullable=True)
    mt5_position = db.Column(db.String(64), nullable=True, index=True)
    import_signature = db.Column(db.String(80), nullable=True, index=True)
    trade_note = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)
