"""舊的 SSE 排程入口在 API 行程內求解時，也要拿和背景工作同一個排程範圍的租約（跨行程互斥）。

沒有啟用工作系統（單機開發）時只有行程內的鎖，這裡什麼都不做。"""

import contextlib
import logging

from app.core.config import get_settings
from app.jobs import db, store

SCOPE = "schedule:plant"
_log = logging.getLogger("artrag.jobs")


class _Local:
    resource = None


LOCAL = _Local()


class Lease:
    def __init__(self, holder: str):
        self.holder = holder
        self.resource = SCOPE


def acquire(request_id: str) -> Lease | _Local | None:
    """拿到回租約；排程範圍已被背景工作（或別的行程）占用回 None；沒有工作系統回 LOCAL。"""
    s = get_settings()
    if not s.jobs_enabled:
        return LOCAL
    holder = f"legacy:{request_id}"
    try:
        if store.active_job(SCOPE) is not None:
            return None
        return Lease(holder) if store.acquire_lease(SCOPE, holder, s.jobs_lease_s) else None
    except db.JobsUnavailable:
        _log.warning("工作資料庫無法使用，舊排程入口只用行程內的鎖")
        return LOCAL


def renew(lease) -> None:
    if isinstance(lease, Lease):
        with contextlib.suppress(db.JobsUnavailable):
            store.renew_lease(lease.resource, lease.holder, get_settings().jobs_lease_s)


def release(lease) -> None:
    if isinstance(lease, Lease):
        with contextlib.suppress(db.JobsUnavailable):
            store.release_lease(lease.resource, lease.holder)
