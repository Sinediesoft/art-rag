"""背景工作：3D 重建與排程求解的建立（202）、狀態、事件（SSE，可用 Last-Event-ID 續接）、
取消與受保護的產物。

- 建立：先驗功能與資源授權，通過才建立工作（失敗不留任何工作）；`Idempotency-Key` 讓重送拿同一個工作
- 讀取／事件／取消／產物：每次請求（SSE 是每一輪輪詢）都依「目前的」權限驗證；看不到的工作回 404，
  和不存在同一個回應
- 工作系統沒啟用或連不上：503 JOBS_UNAVAILABLE（可恢復），不會改在 API 行程執行
"""

import asyncio
import json
import re

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api import schemas as S
from app.core.config import get_settings
from app.core.errors import AppError
from app.jobs import service, store
from app.services import identity, security

router = APIRouter()

JOB_ID = re.compile(r"^(cad|sch)_[0-9a-f]{16}$")
IDEMPOTENCY = Header(
    default=None,
    alias="Idempotency-Key",
    description="重送保護：相同帳號、操作與 key 的相同請求拿到同一個工作；參數不同回 409",
)


def _job_id(job_id: str) -> str:
    if not JOB_ID.match(job_id):
        raise AppError(*security.JOB_NOT_FOUND)
    return job_id


@router.post("/cad/jobs", response_model=S.JobCreated, status_code=202, tags=["jobs"])
async def create_cad_job(
    body: S.CadJobCreate, request: Request, idempotency_key: str | None = IDEMPOTENCY
):
    """建立 3D 重建背景工作（gpu 佇列，由獨立 worker 執行）：授權通過後回 202、job_id 與事件網址。
    要能使用工廠圖紙；指定的圖紙、上傳的照片都要有權限。離開頁面不會取消工作。"""
    ctx = security.SecurityContext.for_request(request, "cad.job.create", domain="mfg")
    return await run_in_threadpool(
        service.create_cad, ctx, body.model_dump(), idempotency_key, request.state.request_id
    )


@router.post("/schedule/jobs", response_model=S.JobCreated, status_code=202, tags=["jobs"])
async def create_schedule_job(
    body: S.ScheduleJobCreate, request: Request, idempotency_key: str | None = IDEMPOTENCY
):
    """建立排程背景工作（schedule 佇列）：只有主管（schedule_run，docs/adr/036）；
    排程範圍已有排隊或執行中的工作回 409 SCHEDULE_BUSY。
    帶 input_version 而且和伺服器現在的版本不同，回 409 SCHEDULE_INPUT_CHANGED。"""
    ctx = security.SecurityContext.for_request(request, "schedule.job.create")
    return await run_in_threadpool(
        service.create_schedule, ctx, body.model_dump(), idempotency_key, request.state.request_id
    )


@router.get("/jobs", response_model=S.JobListResponse, tags=["jobs"])
async def list_jobs(
    request: Request,
    kind: str | None = Query(default=None, pattern="^(cad|schedule)$"),
    active: bool = False,
    limit: int = Query(default=10, ge=1, le=50),
):
    """目前身分看得到的工作（最新的在前）；重新整理後用 active=true 找回排隊或執行中的工作。"""
    account = identity.current(request)
    items = await run_in_threadpool(service.list_jobs, account, kind, active, limit)
    return {"items": items}


@router.get("/jobs/{job_id}", response_model=S.Job, tags=["jobs"])
async def get_job(job_id: str, request: Request):
    """工作的狀態、進度、結果與安全的錯誤訊息（不含模型輸出與程式碼）。"""
    account = identity.current(request)
    return await run_in_threadpool(service.get, account, _job_id(job_id))


@router.get(
    "/jobs/{job_id}/events",
    tags=["jobs"],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE：持久化的進度事件"}},
)
async def job_events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after: int = Query(default=0, ge=0, description="沒有 Last-Event-ID 標頭時的續接位置"),
):
    """進度與終止事件。每個事件有持久化遞增的 id；斷線後帶 Last-Event-ID（瀏覽器 EventSource
    自動帶，fetch 版自己帶）只收尚未收到的事件，不重複。終態（succeeded／failed／cancelled）
    送出後串流結束。每一輪都重驗目前權限：被撤權就送 error 事件並結束。"""
    account = identity.current(request)
    jid = _job_id(job_id)
    await run_in_threadpool(service.readable, account, jid)  # 不存在或沒權限 → 404（串流開始之前）
    try:
        last = int(last_event_id) if last_event_id else after
    except ValueError:
        last = after

    async def gen():
        nonlocal last
        poll = get_settings().jobs_poll_s
        idle = 0.0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            try:
                status, rows = await run_in_threadpool(service.event_batch, account.id, jid, last)
                if status is None:
                    err = {"code": "PERMISSION_REVOKED", "message": "您已沒有這個工作的讀取權限"}
                    yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"
                    return
            except Exception:  # noqa: BLE001 — 資料庫暫時不可用：通知前端稍後用 Last-Event-ID 重連
                err = {
                    "code": "JOBS_UNAVAILABLE",
                    "message": "背景工作系統暫時無法使用，請稍後重連",
                }
                yield f"event: error\ndata: {json.dumps(err, ensure_ascii=False)}\n\n"
                return
            for r in rows:
                data = {**r["data"], "at": store.iso(r["at"])}
                payload = json.dumps(data, ensure_ascii=False)
                yield f"id: {r['seq']}\nevent: {r['type']}\ndata: {payload}\n\n"
                last = r["seq"]
                idle = 0.0
            # 先讀工作狀態再讀事件：看到終態時，該工作的事件都已經寫入並在 rows 裡
            if status in store.TERMINAL and not rows:
                return
            if not rows:
                idle += poll
                if idle >= 15:
                    idle = 0.0
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/cancel", response_model=S.JobCancelResponse, tags=["jobs"])
async def cancel_job(job_id: str, request: Request):
    """取消工作：排隊中直接取消；執行中請 worker 停止，取消後不會再發布成功或寫回任何結果。
    已進入寫回階段 409 JOB_COMMITTING（會完成）、已結束 409 JOB_ALREADY_FINISHED。
    和排程的「提前結束」（POST /schedule/stop，採用目前最佳解並寫回）不同。"""
    account = identity.current(request)
    result, job = await run_in_threadpool(service.cancel, account, _job_id(job_id))
    return {"result": result, "job": job}


@router.get("/jobs/{job_id}/artifacts/{name}", response_class=FileResponse, tags=["jobs"])
async def job_artifact(
    job_id: str, name: str, request: Request, download: bool = Query(default=False)
):
    """受保護的產物預覽或下載（白名單檔名：input.png、code.py、model.stl、model.step、reproj.png）。
    工作完成後才發布；每次請求都重驗目前身分對這個工作的授權。"""
    account = identity.current(request)
    path, media = await run_in_threadpool(service.artifact, account, _job_id(job_id), name)
    return FileResponse(
        path,
        media_type=media,
        filename=f"{job_id}-{name}" if download else None,
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
