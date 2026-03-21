from routes.dashboard import bp as dashboard_bp
from routes.checkin import bp as checkin_bp
from routes.trades import bp as trades_bp
from routes.trade_accounts import bp as trade_accounts_bp
from routes.trade_profiles import bp as trade_profiles_bp
from routes.account import bp as account_bp
from routes.contact import bp as contact_bp
from routes.mt5_internal import bp as mt5_internal_bp

all_blueprints = [
    dashboard_bp,
    checkin_bp,
    trades_bp,
    trade_accounts_bp,
    trade_profiles_bp,
    account_bp,
    contact_bp,
    mt5_internal_bp,
]
