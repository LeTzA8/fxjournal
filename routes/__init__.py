from routes.dashboard import bp as dashboard_bp
from routes.trades import bp as trades_bp
from routes.trade_accounts import bp as trade_accounts_bp
from routes.trade_profiles import bp as trade_profiles_bp
from routes.account import bp as account_bp
from routes.contact import bp as contact_bp

all_blueprints = [
    dashboard_bp,
    trades_bp,
    trade_accounts_bp,
    trade_profiles_bp,
    account_bp,
    contact_bp,
]
