from __future__ import annotations

from functools import lru_cache

from flask import has_app_context
from sqlalchemy import inspect

from models import db


def _column_set(table_name: str) -> set[str]:
    if not has_app_context():
        return set()
    inspector = inspect(db.engine)
    return {column["name"] for column in inspector.get_columns(table_name)}


@lru_cache(maxsize=16)
def table_has_columns(table_name: str, columns: tuple[str, ...]) -> bool:
    if not has_app_context():
        return True
    try:
        return set(columns).issubset(_column_set(table_name))
    except Exception:
        # Fail open so we never block users on introspection errors.
        return True


def user_entitlement_columns_available() -> bool:
    return table_has_columns("users", ("plan_tier", "plan_grandfathered"))


def user_premium_trial_column_available() -> bool:
    return table_has_columns("users", ("premium_trial_started_at",))


def mt5_trial_columns_available() -> bool:
    return table_has_columns("mt5_account", ("mt5_trial_started_at", "sync_paused_at", "sync_pause_reason"))


def clear_schema_compat_cache() -> None:
    table_has_columns.cache_clear()
