"""投遞與恢復：outbox → Redis（Celery）。API、worker 都會跑 `maintenance_once()`（SKIP LOCKED）。"""

import logging
import threading
from contextlib import suppress

import redis

from app.core.config import get_settings
from app.core.logging import log
from app.jobs import db, store
from app.jobs.celery_app import RUN_TASK, celery

_log = logging.getLogger("artrag.jobs")


_url = {"broker": None}


def _sync_broker() -> None:
    """broker 網址以目前設定為準（測試或設定切換後換掉舊連線池）；沒設定就拒絕投遞，
    不會退回 Celery 預設的記憶體 broker。"""
    url = get_settings().redis_url
    if not url:
        raise RuntimeError("沒有設定 REDIS_URL，不能投遞背景工作")
    if _url["broker"] != url:
        celery.conf.broker_url = url
        if getattr(celery, "_pool", None) is not None:
            celery._pool.force_close_all()
            celery._pool = None
        _url["broker"] = url


def publish(job_id: str, queue: str) -> None:
    _sync_broker()
    celery.send_task(RUN_TASK, args=[job_id], queue=queue)


def broker_ok() -> bool:
    url = get_settings().redis_url
    if not url:
        return False
    try:
        r = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        return bool(r.ping())
    except Exception:  # noqa: BLE001
        return False


def dispatch_now() -> tuple[int, int]:
    """建立工作後立刻試投一次（失敗留在 outbox，由維護迴圈重試）。"""
    try:
        return store.dispatch_pending(publish)
    except db.JobsUnavailable:
        return 0, 0


def maintenance_once(stall_s: float | None = None) -> dict:
    """一輪維護：恢復租約過期的工作 → 補投遞遺失的訊息 → 投遞 outbox。"""
    s = get_settings()
    recovered = store.recover_expired()
    stalled = store.redispatch_stalled(stall_s if stall_s is not None else s.jobs_lease_s * 2)
    sent, failed = store.dispatch_pending(publish)
    if recovered or stalled or failed:
        log.info(
            "jobs_maintenance",
            extra={
                "fields": {
                    "recovered": recovered,
                    "stalled": stalled,
                    "dispatched": sent,
                    "dispatch_failed": failed,
                }
            },
        )
    return {"recovered": recovered, "stalled": stalled, "dispatched": sent, "failed": failed}


class Maintenance(threading.Thread):
    """背景維護執行緒（API 行程與 worker 行程各自可以開）。資料庫或 broker 暫時不通就等下一輪。"""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="jobs-maintenance")
        self._stop_evt = threading.Event()

    def run(self) -> None:
        interval = get_settings().jobs_dispatch_interval_s
        while not self._stop_evt.wait(interval):
            with suppress(db.JobsUnavailable):
                try:
                    maintenance_once()
                except Exception as e:  # noqa: BLE001 — 維護失敗不能讓行程掛掉
                    _log.warning("jobs maintenance failed: %s", e)

    def stop(self) -> None:
        self._stop_evt.set()
