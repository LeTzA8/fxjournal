import secrets

from flask_sqlalchemy import SQLAlchemy

from utils import utcnow_naive  # noqa: F401 – re-exported for existing callers


db = SQLAlchemy()


TRADE_PUBKEY_BYTES = 12
TRADE_ACCOUNT_PUBKEY_BYTES = 12


def generate_trade_pubkey():
    return secrets.token_hex(TRADE_PUBKEY_BYTES)


def generate_trade_account_pubkey():
    return secrets.token_hex(TRADE_ACCOUNT_PUBKEY_BYTES)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False, index=True)
    signup_status = db.Column(db.String(16), nullable=False, default="approved", index=True)
    pending_email = db.Column(db.String(120), nullable=True, index=True)
    pending_email_change_requested_at = db.Column(db.DateTime, nullable=True)
    pending_email_change_current_verified_at = db.Column(db.DateTime, nullable=True)
    pending_email_change_new_verified_at = db.Column(db.DateTime, nullable=True)
    timezone = db.Column(db.String(64), nullable=True)
    signup_code_used = db.Column(db.String(32), nullable=True, index=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    approved_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    verification_sent_at = db.Column(db.DateTime, nullable=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    trade_accounts = db.relationship(
        "TradeAccount", backref="user", lazy=True, cascade="all, delete-orphan"
    )
    trade_profiles = db.relationship(
        "TradeProfile", backref="user", lazy=True, cascade="all, delete-orphan"
    )
    trades = db.relationship(
        "Trade", backref="user", lazy=True, cascade="all, delete-orphan"
    )
    ai_generated_responses = db.relationship(
        "AIGeneratedResponse", backref="user", lazy=True, cascade="all, delete-orphan"
    )


class ContactSubmission(db.Model):
    __tablename__ = "contact_submissions"
    __table_args__ = (
        db.Index(
            "ix_contact_submissions_delivery_sent_created",
            "delivery_sent",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    contact_email = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(64), nullable=False)
    subject = db.Column(db.String(120), nullable=False)
    body = db.Column(db.Text, nullable=False)
    delivery_sent = db.Column(db.Boolean, nullable=False, default=False, index=True)
    delivery_mode = db.Column(db.String(32), nullable=False, default="unknown")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)


class TradeAccount(db.Model):
    __tablename__ = "trade_accounts"
    __table_args__ = (
        db.Index("ix_trade_accounts_user_name", "user_id", "name"),
        db.Index("ix_trade_accounts_user_external", "user_id", "external_account_id"),
        db.Index(
            "uq_trade_accounts_one_default_per_user",
            "user_id",
            unique=True,
            sqlite_where=db.text("is_default = 1"),
            postgresql_where=db.text("is_default"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    pubkey = db.Column(
        db.String(24),
        unique=True,
        nullable=False,
        index=True,
        default=generate_trade_account_pubkey,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False, default="Main Account")
    external_account_id = db.Column(db.String(80), nullable=True)
    account_size = db.Column(db.Float, nullable=True)
    account_type = db.Column(db.String(16), nullable=False, default="CFD")
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    trades = db.relationship(
        "Trade", backref="trade_account", lazy=True, cascade="all, delete-orphan"
    )
    ai_generated_responses = db.relationship(
        "AIGeneratedResponse",
        backref="trade_account",
        lazy=True,
        cascade="all, delete-orphan",
    )


class Trade(db.Model):
    __tablename__ = "trades"
    __table_args__ = (
        db.Index("ix_trades_user_trade_account", "user_id", "trade_account_id"),
        db.Index("ix_trades_user_mt5_position", "user_id", "mt5_position"),
        db.Index("ix_trades_user_import_signature", "user_id", "import_signature"),
        db.Index(
            "uq_trades_user_account_import_dedupe",
            "user_id",
            "trade_account_id",
            "import_dedupe_key",
            unique=True,
            sqlite_where=db.text("import_dedupe_key IS NOT NULL"),
            postgresql_where=db.text("import_dedupe_key IS NOT NULL"),
        ),
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
        db.Index("ix_trades_user_trade_profile", "user_id", "trade_profile_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    pubkey = db.Column(
        db.String(24),
        unique=True,
        nullable=False,
        index=True,
        default=generate_trade_pubkey,
    )
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
    stop_loss = db.Column(db.Float, nullable=True)
    take_profit = db.Column(db.Float, nullable=True)
    commission = db.Column(db.Float, nullable=True)
    swap = db.Column(db.Float, nullable=True)
    mt5_position = db.Column(db.String(64), nullable=True, index=True)
    import_signature = db.Column(db.String(80), nullable=True, index=True)
    import_dedupe_key = db.Column(db.String(64), nullable=True, index=True)
    source_timezone = db.Column(db.String(64), nullable=True)
    contract_code = db.Column(db.String(24), nullable=True)
    trade_note = db.Column(db.Text, nullable=True)
    trade_profile_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_profiles.id"),
        nullable=True,
        index=True,
    )
    trade_profile_version_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_profile_versions.id"),
        nullable=True,
        index=True,
    )
    opened_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    closed_at = db.Column(db.DateTime, nullable=True)
    trade_profile = db.relationship(
        "TradeProfile",
        foreign_keys=[trade_profile_id],
        backref=db.backref("trades", lazy=True),
    )
    trade_profile_version = db.relationship(
        "TradeProfileVersion",
        foreign_keys=[trade_profile_version_id],
        backref=db.backref("attached_trades", lazy=True),
    )


class TradeProfile(db.Model):
    __tablename__ = "trade_profiles"
    __table_args__ = (
        db.Index("ix_trade_profiles_user_name", "user_id", "name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    pubkey = db.Column(
        db.String(24),
        unique=True,
        nullable=False,
        index=True,
        default=generate_trade_pubkey,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    current_version_number = db.Column(db.Integer, nullable=False, default=1)
    is_archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
    )
    versions = db.relationship(
        "TradeProfileVersion",
        backref="trade_profile",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="TradeProfileVersion.version_number.asc()",
    )


class TradeProfileVersion(db.Model):
    __tablename__ = "trade_profile_versions"
    __table_args__ = (
        db.UniqueConstraint(
            "trade_profile_id",
            "version_number",
            name="uq_trade_profile_versions_profile_version",
        ),
        db.Index(
            "ix_trade_profile_versions_profile_created",
            "trade_profile_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    trade_profile_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_profiles.id"),
        nullable=False,
        index=True,
    )
    version_number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(80), nullable=False)
    short_description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)


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


class SignupCode(db.Model):
    __tablename__ = "signup_codes"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(32), unique=True, nullable=False, index=True)
    created_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    notes = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    max_uses = db.Column(db.Integer, nullable=True)
    used_count = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)


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


class AIPromptHistory(db.Model):
    __tablename__ = "ai_prompt_history"

    id = db.Column(db.Integer, primary_key=True)
    prompt_id = db.Column(db.String(64), nullable=False, index=True)
    prompt_sha256 = db.Column(db.String(64), unique=True, nullable=False, index=True)
    prompt_text = db.Column(db.Text, nullable=False)
    source_path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    generated_responses = db.relationship(
        "AIGeneratedResponse",
        backref="prompt_history",
        lazy=True,
        cascade="all, delete-orphan",
    )


class AIGeneratedResponse(db.Model):
    __tablename__ = "ai_generated_responses"
    __table_args__ = (
        db.Index(
            "ix_ai_generated_responses_user_kind_generated_at",
            "user_id",
            "kind",
            "generated_at",
        ),
        db.Index(
            "ix_ai_generated_responses_user_account_kind_generated_at",
            "user_id",
            "trade_account_id",
            "kind",
            "generated_at",
        ),
        db.Index(
            "ix_ai_generated_responses_prompt_generated_at",
            "prompt_history_id",
            "generated_at",
        ),
        db.Index(
            "ix_ai_generated_responses_user_account_kind_period_start",
            "user_id",
            "trade_account_id",
            "kind",
            "period_start_utc",
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
    prompt_history_id = db.Column(
        db.Integer,
        db.ForeignKey("ai_prompt_history.id"),
        nullable=False,
        index=True,
    )
    kind = db.Column(db.String(64), nullable=False, default="dashboard_advice")
    model = db.Column(db.String(64), nullable=False, default="gpt-5-mini")
    response_text = db.Column(db.Text, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=True, index=True)
    trade_count_used = db.Column(db.Integer, nullable=False, default=0)
    source_last_trade_id = db.Column(db.Integer, nullable=True)
    period_start_utc = db.Column(db.DateTime, nullable=True)
    period_end_utc = db.Column(db.DateTime, nullable=True)
    generated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
