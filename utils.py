import os
from datetime import datetime, timezone
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

TRUE_VALUES = {"1", "true", "yes", "on"}


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def env_int(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify(
                    {
                        "ok": False,
                        "message": "Your session expired. Please sign in again.",
                        "redirect_url": url_for("login"),
                    }
                ), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated
