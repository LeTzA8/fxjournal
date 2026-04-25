import json
import logging

from kombu.utils.url import maybe_sanitize_url


def describe_celery_broker(task):
    """Return a sanitized broker URL for the task's Celery app."""
    app = getattr(task, "app", None)
    conf = getattr(app, "conf", None)
    broker_url = getattr(conf, "broker_url", None) if conf is not None else None
    broker_text = str(broker_url or "").strip()
    if not broker_text:
        return "unknown"
    return maybe_sanitize_url(broker_text)


def dispatch_celery_task(task, *, args=None, kwargs=None, queue=None, log=None, label=None, extra=None):
    """Publish a Celery task with producer-side diagnostics."""
    logger = log or logging.getLogger(__name__)
    task_name = getattr(task, "name", None) or repr(task)
    broker_url = describe_celery_broker(task)
    label_text = str(label or task_name).strip() or task_name
    extra_json = json.dumps(extra or {}, default=str, ensure_ascii=True, sort_keys=True)

    publish_kwargs = {}
    if args is not None:
        publish_kwargs["args"] = args
    if kwargs is not None:
        publish_kwargs["kwargs"] = kwargs
    if queue is not None:
        publish_kwargs["queue"] = queue

    logger.warning(
        "Celery publish attempt label=%s task=%s queue=%s broker=%s extra=%s",
        label_text,
        task_name,
        queue or "default",
        broker_url,
        extra_json,
    )
    try:
        async_result = task.apply_async(**publish_kwargs)
    except Exception as exc:
        logger.warning(
            "Celery publish failed label=%s task=%s queue=%s broker=%s extra=%s error=%s",
            label_text,
            task_name,
            queue or "default",
            broker_url,
            extra_json,
            exc,
        )
        raise
    logger.warning(
        "Celery publish success label=%s task=%s task_id=%s queue=%s broker=%s extra=%s",
        label_text,
        task_name,
        getattr(async_result, "id", None),
        queue or "default",
        broker_url,
        extra_json,
    )
    return async_result
