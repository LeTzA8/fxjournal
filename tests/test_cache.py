from celery_workers import cache as cache_module


def test_invalidate_deletes_current_and_legacy_dashboard_cache_keys(monkeypatch):
    deleted_keys = []

    class DummyRedis:
        def delete(self, *keys):
            deleted_keys.extend(keys)
            return len(keys)

    monkeypatch.setattr(cache_module, "_client", lambda: DummyRedis())
    monkeypatch.setattr(cache_module, "_run_redis", lambda callable_obj: callable_obj())

    cache_module.invalidate(user_id=7, trade_account_id=11)

    assert set(deleted_keys) == {
        "analytics:u7:a11",
        "analytics_v2:u7:a11",
        "analytics_v3:u7:a11",
        "analytics_v4:u7:a11",
        "analytics_v5:u7:a11",
        "rr_summary:u7:a11",
        "rr_summary_v2:u7:a11",
        "rr_summary_v3:u7:a11",
        "rr_summary_v4:u7:a11",
        "rr_summary_v5:u7:a11",
        "dashboard:u7:a11",
        "dashboard_v2:u7:a11",
        "dashboard_v3:u7:a11",
    }
