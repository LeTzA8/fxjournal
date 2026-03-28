from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

try:
    from authlib.integrations.flask_client import OAuth
except ImportError:  # pragma: no cover - dependency is optional until installed
    OAuth = None

limiter = Limiter(key_func=get_remote_address, default_limits=[])
oauth = OAuth() if OAuth is not None else None
