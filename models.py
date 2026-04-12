import secrets

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, or_
from sqlalchemy.orm import Session

from helpers.utils import utcnow_naive  # noqa: F401 – re-exported for existing callers


db = SQLAlchemy()


TRADE_PUBKEY_BYTES = 12
TRADE_ACCOUNT_PUBKEY_BYTES = 12


def generate_trade_pubkey():
    return secrets.token_hex(TRADE_PUBKEY_BYTES)


def generate_trade_account_pubkey():
    return secrets.token_hex(TRADE_ACCOUNT_PUBKEY_BYTES)


def mask_mt5_account_number_for_cleanup(account_number):
    text_value = str(account_number or "").strip()
    if text_value.startswith("cleanup-"):
        return text_value[:50]
    digits_only = "".join(character for character in text_value if character.isdigit())
    suffix = digits_only[-4:] if digits_only else (text_value[-4:] if text_value else "unknown")
    return f"cleanup-{suffix}"[:50]


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    google_sub = db.Column(db.String(255), unique=True, nullable=True)
    password = db.Column(db.String(255), nullable=False)
    email_verified = db.Column(db.Boolean, nullable=False, default=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False, index=True)
    signup_status = db.Column(db.String(16), nullable=False, default="approved", index=True)
    pending_email = db.Column(db.String(120), nullable=True, index=True)
    password_reset_nonce = db.Column(db.String(64), nullable=True)
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
        "TradeAccount",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    mt5_accounts = db.relationship(
        "MT5Account",
        backref="user",
        lazy=True,
        passive_deletes=True,
    )
    mt5_access_requests = db.relationship(
        "MT5AccessRequest",
        foreign_keys="MT5AccessRequest.user_id",
        back_populates="user",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reviewed_mt5_access_requests = db.relationship(
        "MT5AccessRequest",
        foreign_keys="MT5AccessRequest.reviewed_by_user_id",
        back_populates="reviewed_by_user",
        lazy=True,
        passive_deletes=True,
    )
    user_profile = db.relationship(
        "UserProfile",
        backref="user",
        lazy=True,
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    trade_profiles = db.relationship(
        "TradeProfile",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    trades = db.relationship(
        "Trade",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    ai_generated_responses = db.relationship(
        "AIGeneratedResponse",
        backref="user",
        lazy=True,
        passive_deletes=True,
    )
    weekly_checkins = db.relationship(
        "WeeklyCheckin",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class UserProfile(db.Model):
    __tablename__ = "user_profile"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    trading_style = db.Column(db.String(50), nullable=True)
    instruments = db.Column(db.String(200), nullable=True)
    experience_level = db.Column(db.String(50), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    skipped = db.Column(db.Boolean, default=False, nullable=False)


class WeeklyCheckin(db.Model):
    __tablename__ = "weekly_checkin"
    __table_args__ = (
        db.Index(
            "uq_weekly_checkin_user_account_week_start",
            "user_id",
            "trade_account_id",
            "week_start_utc",
            unique=True,
        ),
        db.Index("ix_weekly_checkin_week_start_utc", "week_start_utc"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trade_account_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    week_start_utc = db.Column(db.DateTime, nullable=False)
    week_end_utc = db.Column(db.DateTime, nullable=False)
    emotional_state = db.Column(db.String(32), nullable=True)
    plan_adherence = db.Column(db.String(32), nullable=True)
    execution_quality = db.Column(db.String(32), nullable=True)
    additional_context = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)


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
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(80), nullable=False, default="Main Account")
    external_account_id = db.Column(db.String(80), nullable=True)
    account_size = db.Column(db.Float, nullable=True)
    account_type = db.Column(db.String(16), nullable=False, default="CFD")
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    bundle_review_requested_at = db.Column(db.DateTime, nullable=True)
    bundle_review_completed_at = db.Column(db.DateTime, nullable=True)
    default_trade_profile_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    default_trade_profile = db.relationship(
        "TradeProfile",
        foreign_keys=[default_trade_profile_id],
    )
    trades = db.relationship(
        "Trade",
        backref="trade_account",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    mt5_accounts = db.relationship(
        "MT5Account",
        backref="trade_account",
        lazy=True,
        passive_deletes=True,
    )
    mt5_access_requests = db.relationship(
        "MT5AccessRequest",
        foreign_keys="MT5AccessRequest.trade_account_id",
        back_populates="trade_account",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    ai_generated_responses = db.relationship(
        "AIGeneratedResponse",
        backref="trade_account",
        lazy=True,
        passive_deletes=True,
    )
    weekly_checkins = db.relationship(
        "WeeklyCheckin",
        backref="trade_account",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
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
        db.Index(
            "ix_trades_user_account_closed_at",
            "user_id",
            "trade_account_id",
            "closed_at",
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
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trade_account_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_accounts.id", ondelete="CASCADE"),
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
    system_trade_note = db.Column(db.Text, nullable=True)
    trade_profile_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trade_profile_version_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_profile_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    opened_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    closed_at = db.Column(db.DateTime, nullable=True)
    trade_profile = db.relationship(
        "TradeProfile",
        foreign_keys=[trade_profile_id],
        backref=db.backref(
            "trades",
            lazy=True,
            cascade="save-update, merge",
            passive_deletes=True,
        ),
    )
    trade_profile_version = db.relationship(
        "TradeProfileVersion",
        foreign_keys=[trade_profile_version_id],
        backref=db.backref(
            "attached_trades",
            lazy=True,
            cascade="save-update, merge",
            passive_deletes=True,
        ),
    )
    trade_bars = db.relationship(
        "TradeBars",
        backref="trade",
        lazy=True,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    interpretation = db.relationship(
        "TradeInterpretation",
        back_populates="trade",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def is_revenge(self):
        interp = self.interpretation
        return bool(interp.is_revenge) if interp is not None else False

    @property
    def is_reactive(self):
        interp = self.interpretation
        return bool(interp.is_reactive) if interp is not None else False

    @property
    def is_corrective(self):
        interp = self.interpretation
        return bool(interp.is_corrective) if interp is not None else False

    @property
    def bundle_pubkey(self):
        interp = self.interpretation
        if interp is None or not interp.bundle_pubkey:
            return None
        return str(interp.bundle_pubkey).strip() or None


class TradeInterpretation(db.Model):
    """Bundling and user-confirmed behavior flags (derived / interpretation layer)."""

    __tablename__ = "trade_interpretation"
    __table_args__ = (db.Index("ix_trade_interpretation_bundle_pubkey", "bundle_pubkey"),)

    trade_id = db.Column(
        db.Integer,
        db.ForeignKey("trades.id", ondelete="CASCADE"),
        primary_key=True,
    )
    bundle_pubkey = db.Column(db.String(24), nullable=True)
    is_revenge = db.Column(db.Boolean, nullable=False, default=False)
    is_reactive = db.Column(db.Boolean, nullable=False, default=False)
    is_corrective = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)
    trade = db.relationship("Trade", back_populates="interpretation")


class TradeInterpretationHistory(db.Model):
    """Append-only log of interpretation changes (detection vs user confirmation, audits, future tuning)."""

    __tablename__ = "trade_interpretation_history"
    __table_args__ = (
        db.Index("ix_trade_interpretation_history_trade_id_created", "trade_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    trade_id = db.Column(
        db.Integer,
        db.ForeignKey("trades.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bundle_pubkey = db.Column(db.String(24), nullable=True)
    is_revenge = db.Column(db.Boolean, nullable=False, default=False)
    is_reactive = db.Column(db.Boolean, nullable=False, default=False)
    is_corrective = db.Column(db.Boolean, nullable=False, default=False)
    source = db.Column(db.String(32), nullable=False)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)


class TradeBars(db.Model):
    __tablename__ = "trade_bars"
    __table_args__ = (
        db.Index("ix_trade_bars_trade_id", "trade_id"),
        db.Index(
            "uq_trade_bars_trade_timeframe_bartime",
            "trade_id",
            "timeframe",
            "bar_time",
            unique=True,
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    trade_id = db.Column(
        db.Integer,
        db.ForeignKey("trades.id", ondelete="CASCADE"),
        nullable=False,
    )
    timeframe = db.Column(db.String(8), nullable=False)
    bar_time = db.Column(db.Integer, nullable=False)  # Unix epoch UTC
    open = db.Column(db.Float, nullable=False)
    high = db.Column(db.Float, nullable=False)
    low = db.Column(db.Float, nullable=False)
    close = db.Column(db.Float, nullable=False)
    tick_volume = db.Column(db.Integer, nullable=True)
    fetched_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)


class MT5SyncBatch(db.Model):
    __tablename__ = "mt5_sync_batch"
    __table_args__ = (
        db.Index(
            "uq_mt5_sync_batch_one_open",
            "is_open",
            unique=True,
            sqlite_where=db.text("is_open = 1"),
            postgresql_where=db.text("is_open"),
        ),
        db.Index("ix_mt5_sync_batch_created_at", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    capacity_total = db.Column(db.Integer, nullable=False, default=0)
    total_slots_claimed = db.Column(db.Integer, nullable=False, default=0)
    is_open = db.Column(db.Boolean, nullable=False, default=True, index=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    closed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utcnow_naive,
        onupdate=utcnow_naive,
    )
    created_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    updated_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    mt5_access_requests = db.relationship(
        "MT5AccessRequest",
        back_populates="batch",
        lazy=True,
        passive_deletes=True,
    )


class MT5Account(db.Model):
    __tablename__ = "mt5_account"
    __table_args__ = (
        db.UniqueConstraint("trade_account_id", name="uq_mt5_account_trade_account_id"),
    )

    ARCHIVE_REASON_INACTIVITY = "inactivity"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trade_account_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    account_number = db.Column(db.String(50), nullable=False)
    investor_password_encrypted = db.Column(db.Text, nullable=True)
    server = db.Column(db.String(100), nullable=False)
    terminal_path = db.Column(db.String(500), nullable=True)
    appdata_hash = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    last_synced_at = db.Column(db.DateTime, nullable=True)
    cleanup_marked_at = db.Column(db.DateTime, nullable=True, index=True)
    archived_at = db.Column(db.DateTime, nullable=True, index=True)
    archive_reason = db.Column(db.String(32), nullable=True)
    mt5_consent_accepted_at = db.Column(db.DateTime, nullable=True)
    mt5_consent_version = db.Column(db.String(32), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    @property
    def is_orphaned(self):
        return self.user_id is None or self.trade_account_id is None

    @property
    def is_cleanup_only(self):
        return self.is_orphaned and not self.investor_password_encrypted

    @property
    def is_archived(self):
        return self.archived_at is not None

    def mark_for_cleanup(self, *, marked_at=None):
        self.user_id = None
        self.trade_account_id = None
        self.account_number = mask_mt5_account_number_for_cleanup(self.account_number)
        self.investor_password_encrypted = None
        self.is_active = False
        self.cleanup_marked_at = marked_at or self.cleanup_marked_at or utcnow_naive()
        self.archived_at = None
        self.archive_reason = None
        self.mt5_consent_accepted_at = None
        self.mt5_consent_version = None


class MT5AccessRequest(db.Model):
    __tablename__ = "mt5_access_request"
    __table_args__ = (
        db.Index(
            "uq_mt5_access_request_pending_trade_account",
            "trade_account_id",
            unique=True,
            sqlite_where=db.text("status = 'pending'"),
            postgresql_where=db.text("status = 'pending'"),
        ),
        db.Index("ix_mt5_access_request_status_created", "status", "created_at"),
    )

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trade_account_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    batch_id = db.Column(
        db.Integer,
        db.ForeignKey("mt5_sync_batch.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = db.Column(db.String(16), nullable=False, default=STATUS_PENDING, index=True)
    request_note = db.Column(db.Text, nullable=True)
    reviewed_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="mt5_access_requests",
    )
    reviewed_by_user = db.relationship(
        "User",
        foreign_keys=[reviewed_by_user_id],
        back_populates="reviewed_mt5_access_requests",
    )
    trade_account = db.relationship(
        "TradeAccount",
        foreign_keys=[trade_account_id],
        back_populates="mt5_access_requests",
    )
    batch = db.relationship(
        "MT5SyncBatch",
        foreign_keys=[batch_id],
        back_populates="mt5_access_requests",
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
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
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
        passive_deletes=True,
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
        db.ForeignKey("trade_profiles.id", ondelete="CASCADE"),
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
        passive_deletes=True,
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
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trade_account_id = db.Column(
        db.Integer,
        db.ForeignKey("trade_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    prompt_history_id = db.Column(
        db.Integer,
        db.ForeignKey("ai_prompt_history.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    kind = db.Column(db.String(64), nullable=False, default="dashboard_advice")
    model = db.Column(db.String(64), nullable=False, default="gpt-5-mini")
    response_text = db.Column(db.Text, nullable=False)
    response_meta_json = db.Column(db.Text, nullable=True)
    payload_json = db.Column(db.Text, nullable=True)
    payload_hash = db.Column(db.String(64), nullable=True, index=True)
    trade_count_used = db.Column(db.Integer, nullable=False, default=0)
    source_last_trade_id = db.Column(db.Integer, nullable=True)
    period_start_utc = db.Column(db.DateTime, nullable=True)
    period_end_utc = db.Column(db.DateTime, nullable=True)
    generated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)


@event.listens_for(Session, "before_flush")
def mark_deleted_mt5_accounts_for_cleanup(session, _flush_context, _instances):
    deleted_user_ids = {
        user.id
        for user in session.deleted
        if isinstance(user, User) and user.id is not None
    }
    deleted_trade_account_ids = {
        trade_account.id
        for trade_account in session.deleted
        if isinstance(trade_account, TradeAccount) and trade_account.id is not None
    }
    if not deleted_user_ids and not deleted_trade_account_ids:
        return

    filters = []
    if deleted_user_ids:
        filters.append(MT5Account.user_id.in_(deleted_user_ids))
    if deleted_trade_account_ids:
        filters.append(MT5Account.trade_account_id.in_(deleted_trade_account_ids))

    marked_at = utcnow_naive()
    with session.no_autoflush:
        mt5_accounts = session.query(MT5Account).filter(or_(*filters)).all()

    for account in mt5_accounts:
        if account in session.deleted:
            continue
        account.mark_for_cleanup(marked_at=marked_at)
