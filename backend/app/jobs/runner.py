"""在 worker 行程執行一個工作：領取 → 重驗權限 → 執行 → 寫回／失敗／取消。

安全守則（TASK-20261013-001）：
- 佇列訊息只有 job_id；帳號從工作資料庫的 owner_id 對回「目前的」access.yaml——撤權、刪帳號
  在領取後、執行前、寫回前都會重驗，不能用提交當時的權限完成現在不允許的事
- 只有現行的執行嘗試（attempt＋lease_owner）可以寫事件與結果；租約過期被接手的舊 worker
  拿到 LeaseLost，什麼都不寫
- 取消與寫回的先後由資料庫決定（reserve_commit）：取消在前不寫回，寫回在前取消回 409
"""

import logging
import os
import socket
import uuid

from app.core.errors import AppError
from app.core.logging import log
from app.jobs import db, store
from app.jobs.control import JobCancelled, JobControl

_log = logging.getLogger("artrag.jobs")


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _error(e: Exception) -> dict:
    if isinstance(e, AppError):
        return {"code": e.code, "message": e.message}
    # 不把例外訊息（可能夾帶路徑、模型輸出、金鑰）放進對外的錯誤
    return {
        "code": "INTERNAL_ERROR",
        "message": f"工作執行失敗（{type(e).__name__}），已記錄在伺服器日誌",
    }


def execute(job_id: str, worker_id: str | None = None) -> str:
    """回傳 done／skipped（已被處理或不存在）／blocked（GPU 名額、排程範圍被占用，稍後再試）／
    retry（資料庫暫時不可用）／lost（租約遺失，已放棄）。"""
    worker_id = worker_id or default_worker_id()
    try:
        claim = store.claim(job_id, worker_id)
    except db.JobsUnavailable:
        return "retry"
    if claim.state == "blocked":
        return "blocked"
    if claim.state != "claimed":
        return "skipped"
    job = claim.job
    control = JobControl(job_id, job["attempt"], worker_id).start()
    log.info(
        "job_started",
        extra={"fields": {"job_id": job_id, "kind": job["kind"], "attempt": job["attempt"]}},
    )
    try:
        _run(job, control, worker_id)
        final = store.get(job_id) or {}
        log.info(
            "job_finished",
            extra={
                "fields": {
                    "job_id": job_id,
                    "attempt": job["attempt"],
                    "status": final.get("status"),
                }
            },
        )
        return "done"
    except store.LeaseLost:
        log.warning(
            "job_lease_lost", extra={"fields": {"job_id": job_id, "attempt": job["attempt"]}}
        )
        return "lost"
    finally:
        control.shutdown()


def _run(job: dict, control: JobControl, worker_id: str) -> None:
    from app.jobs import cad_job, schedule_job

    runner = {"cad": cad_job.run, "schedule": schedule_job.run}[job["kind"]]
    try:
        runner(job, control)
    except JobCancelled:
        store.finish(job["id"], job["attempt"], worker_id, "cancelled", note="已取消，沒有寫回")
    except store.LeaseLost:
        raise
    except Exception as e:  # noqa: BLE001 — 任何例外都要把工作收尾成 failed，不能留在 running
        if not isinstance(e, AppError):
            _log.exception("job %s failed", job["id"])
        store.finish(job["id"], job["attempt"], worker_id, "failed", error=_error(e))
