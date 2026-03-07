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
    __table_args__ = (
        db.Index("ix_trade_accounts_user_name", "user_id", "name"),
        db.Index("ix_trade_accounts_user_external", "user_id", "external_account_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False, default="Main Account")
    external_account_id = db.Column(db.String(80), nullable=True)
    account_type = db.Column(db.String(16), nullable=False, default="CFD")
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    trades = db.relationship(
        "Trade", backref="trade_account", lazy=True, cascade="all, delete-orphan"
    )


class Trade(db.Model):
    __tablename__ = "trades"
    __table_args__ = (
        db.Index("ix_trades_user_trade_account", "user_id", "trade_account_id"),
        db.Index("ix_trades_user_mt5_position", "user_id", "mt5_position"),
        db.Index("ix_trades_user_import_signature", "user_id", "import_signature"),
        db.Index(
            "ix_trades_user_account_mt5_position",
            "user_id",
            "trade_account_id",
            "mt5_position",
        ),
        db.Index(
            "ix_trades_user_account_import_signature",
            "user_id",
            "trade_account_id",
            "import_signature",
        ),
    )

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
    contract_code = db.Column(db.String(24), nullable=True)
    trade_note = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)


class CFDSymbol(db.Model):
    __tablename__ = "CFD_Symbols"

    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(32), unique=True, nullable=False, index=True)
    aliases = db.Column(db.Text, nullable=True)
    contract_size = db.Column(db.Float, nullable=False, default=1.0)
    pip_size = db.Column(db.Float, nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class AllowedSignupEmailDomain(db.Model):
    __tablename__ = "allowed_signup_email_domains"

    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(255), unique=True, nullable=False, index=True)
    notes = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class FuturesSymbol(db.Model):
    __tablename__ = "futures_symbols"

    id = db.Column(db.Integer, primary_key=True)
    root_symbol = db.Column(db.String(16), unique=True, nullable=False, index=True)
    aliases = db.Column(db.Text, nullable=True)
    display_name = db.Column(db.String(120), nullable=True)
    exchange = db.Column(db.String(64), nullable=True)
    tick_size = db.Column(db.Float, nullable=False)
    tick_value = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(16), nullable=False, default="USD")
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
