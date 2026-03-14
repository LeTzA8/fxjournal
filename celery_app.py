import os

from celery import Celery

_flask_app = None


def _resolve_redis_url():
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        return redis_url, redis_url
    return "memory://", "cache+memory://"


def _resolve_flask_app():
    global _flask_app
    if _flask_app is None:
        from app import app as flask_app

        _flask_app = flask_app
    return _flask_app


def _create_celery():
    broker_url, backend_url = _resolve_redis_url()
    celery_app = Celery(
        "fxjournal",
        broker=broker_url,
        backend=backend_url,
        include=["celery_workers.tasks"],
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
    )

    class FlaskTask(celery_app.Task):
        abstract = True

        def __call__(self, *args, **kwargs):
            flask_app = _resolve_flask_app()
            with flask_app.app_context():
                return self.run(*args, **kwargs)

    celery_app.Task = FlaskTask
    return celery_app


celery = _create_celery()


def init_celery(app):
    global _flask_app
    _flask_app = app
    return celery
