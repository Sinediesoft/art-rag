"""Celery 任務：訊息只有 job_id。領取不到資源（GPU 名額滿、排程範圍被占用）就稍後重試，
其他情況（已被處理、已取消、重複投遞）直接結束。"""

from celery.signals import worker_ready

from app.jobs import boot, runner
from app.jobs.celery_app import RUN_TASK, celery

_maintenance = None


@worker_ready.connect
def _on_worker_ready(sender=None, **_kw) -> None:
    global _maintenance
    name = getattr(sender, "hostname", "") or ""
    try:
        queues = sorted(sender.app.amqp.queues.consume_from)
    except Exception:  # noqa: BLE001 — 取不到佇列名稱只影響健康狀態頁的顯示
        queues = []
    _maintenance = boot.init_worker(name, queues)


@celery.task(bind=True, name=RUN_TASK, acks_late=True, max_retries=None)
def run_job(self, job_id: str) -> str:
    result = runner.execute(job_id)
    if result == "blocked":
        raise self.retry(countdown=2)
    if result == "retry":
        raise self.retry(countdown=5)
    return result
