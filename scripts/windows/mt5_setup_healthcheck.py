import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import celery_app as celery_app_module
from celery_workers.mt5_monitoring import build_mt5_setup_health_snapshot


def main():
    celery_app_module._load_runtime_env()
    snapshot = build_mt5_setup_health_snapshot()
    print(json.dumps(snapshot, default=str, ensure_ascii=True, sort_keys=True))
    if snapshot.get("error"):
        return 1
    if snapshot.get("stale"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
