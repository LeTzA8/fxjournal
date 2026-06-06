from models import AppSetting, db
from helpers.utils import utcnow_naive


MT5_AUTO_BAR_SYNC_PUBLIC_USERS_KEY = "mt5_auto_bar_sync_public_users"
MT5_BROKER_DISCOVERY_REFRESH_ENABLED_KEY = "mt5_broker_discovery_refresh_enabled"

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def get_app_setting_value(key, default=None):
    setting = db.session.get(AppSetting, key)
    if setting is None:
        return default
    return setting.value


def get_bool_app_setting(key, default=False):
    raw_value = get_app_setting_value(key, None)
    if raw_value is None:
        return bool(default)
    normalized = str(raw_value).strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return bool(default)


def set_bool_app_setting(key, enabled, *, updated_by_user_id=None):
    setting = db.session.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value="1" if enabled else "0")
        db.session.add(setting)
    else:
        setting.value = "1" if enabled else "0"
    setting.updated_at = utcnow_naive()
    setting.updated_by_user_id = updated_by_user_id
    return setting
