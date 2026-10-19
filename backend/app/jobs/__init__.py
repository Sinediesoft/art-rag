"""背景工作系統（TASK-20261013-001，docs/adr/041）：

PostgreSQL 保存工作、進度事件、擁有者、資源與資料版本（jobs／job_events／job_outbox／
job_artifacts／resource_leases）；建立工作與 outbox 同一交易，dispatcher 投遞到 Redis（Celery），
獨立 worker 行程執行 3D 重建（gpu 佇列）與排程求解（schedule 佇列）。
API 行程只建立、查詢、取消，不持有耗時工作的生命週期。
"""

from app.core.config import get_settings


def enabled() -> bool:
    """REDIS_URL 與工作資料庫都設定了才啟用。"""
    return get_settings().jobs_enabled
