"""API 行程與 worker 行程啟動時的接線。"""

import logging
import shutil

from app.core.config import get_settings
from app.jobs import broker, coordination, db, store
from app.services import cad_access

_log = logging.getLogger("artrag.jobs")


def _lookup(job_id: str) -> dict | None:
    """3D 重建工作的存取依據改查工作資料庫；連不上就回 None（退回工作資料夾裡的紀錄）。"""
    try:
        row = store.get(job_id)
    except db.JobsUnavailable:
        return None
    return row if row and row["kind"] == "cad" else None


def init() -> broker.Maintenance | None:
    """啟用工作系統時：掛上授權查詢、啟動 outbox 投遞／租約恢復的背景執行緒。"""
    if not get_settings().jobs_enabled:
        return None
    cad_access.set_job_lookup(_lookup)
    m = broker.Maintenance()
    m.start()
    return m


def init_worker(name: str = "", queues: list[str] | None = None) -> broker.Maintenance:
    """worker 行程啟動：載入索引（3D 重建要查知識庫圖紙）、庫存資料庫，並開維護執行緒——
    API 行程不在時，租約恢復與 outbox 投遞仍照常進行。索引和模型版本不符就拒絕啟動。"""
    from app.core.logging import setup_logging
    from app.repositories.index_store import get_store
    from app.repositories.inventory_repo import get_inventory_repo

    setup_logging()
    get_store().load()
    try:
        get_inventory_repo().ensure_built()
    except Exception as e:  # noqa: BLE001 — 庫存資料有誤只影響寫回後的重建，不擋 worker 啟動
        _log.warning("庫存資料庫建立失敗：%s", e)
    cad_access.set_job_lookup(_lookup)
    m = broker.Maintenance((name or default_name(), queues or []))
    m.start()
    return m


def default_name() -> str:
    import os
    import socket

    return f"worker@{socket.gethostname()}:{os.getpid()}"


def purge(ttl_days: float) -> int:
    """刪掉保存期限外已結束的工作與它們的產物資料夾。"""
    if not get_settings().jobs_enabled:
        return 0
    try:
        gone = store.purge(ttl_days)
    except db.JobsUnavailable:
        return 0
    root = get_settings().cad_jobs_dir
    for job_id, kind in gone:
        if kind == "cad":
            shutil.rmtree(root / job_id, ignore_errors=True)
    return len(gone)


def status() -> dict:
    """給 /status 的簡短狀態：是否啟用、資料庫、broker、待投遞數與各狀態的工作數。"""
    s = get_settings()
    if not s.jobs_enabled:
        return {
            "enabled": False,
            "database_ok": False,
            "broker_ok": False,
            "pending_dispatch": 0,
            "counts": {},
            "workers": [],
        }
    db_ok = db.ping()
    pending, counts = 0, {}
    if db_ok:
        try:
            pending, counts = store.pending_outbox(), store.counts()
        except db.JobsUnavailable:  # ping 之後又斷線：狀態頁照樣回得出來
            db_ok = False
    return {
        "enabled": True,
        "database_ok": db_ok,
        "broker_ok": broker.broker_ok(),
        "pending_dispatch": pending,
        "counts": counts,
        "workers": coordination.workers(),
    }
