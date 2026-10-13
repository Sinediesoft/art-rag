"""工廠工單排程：工單管理、排程求解與結果（舊的 SSE 入口；背景工作在 jobs.py）。"""

from fastapi import APIRouter, Path, Request, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import jobs
from app.api import schemas as S
from app.api.deps import ID_PATTERN, IDEMPOTENCY
from app.core.errors import AppError
from app.jobs import legacy, service
from app.repositories.inventory_repo import get_inventory_repo
from app.repositories.production_repo import get_production_repo
from app.services import (
    change_service,
    idempotency,
    identity,
    schedule_service,
    security,
)

router = APIRouter()


# ---------------------------------------------------------------- 生產排程（Timefold）
@router.get("/production/overview", response_model=S.ProductionOverview, tags=["production"])
async def production_overview(request: Request):
    """排程頁：機台、行事曆、待排工單、排程服務狀態與目前排程。"""
    identity.require_domain(identity.current(request), "factory")
    return await schedule_service.overview()


@router.get("/production/parts/{part_id}", response_model=S.PartPlan, tags=["production"])
async def part_plan(request: Request, part_id: str = Path(pattern=ID_PATTERN)):
    """圖紙頁的「生產工單」：製程途程、依庫存建議的數量與交期、這張圖紙的工單與排程結果。"""
    identity.require_domain(identity.current(request), "factory")
    return await schedule_service.part_plan(part_id)


@router.post(
    "/production/work-orders",
    response_model=S.WorkOrderCreated,
    status_code=201,
    tags=["production"],
)
def create_work_order(
    body: S.WorkOrderCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY,
):
    """從圖紙頁開立工單：寫入生產資料庫，工廠資料庫（Text-to-SQL）自動同步，等待排程。

    權限與額度和智慧助理同一套（只有生管可以；急件要主管核准，回 409 APPROVAL_REQUIRED）。
    帶 Idempotency-Key 時，重送（連按、網路重試）拿到同一張工單，不會多開。
    """
    account = identity.current(request)
    params = body.model_dump()

    def audit_created(row: dict) -> None:
        get_production_repo().add_audit(
            {"actor_id": account.id, "actor_label": account.label, "action": "寫入",
             "op": "wo_create", "ref_no": row["wo_no"],
             "summary": f"圖紙頁開立 {row['wo_no']}〈{row['part_name']}〉{row['qty']} 件",
             "detail": None, "request_id": request.state.request_id},
            once=True,
        )  # fmt: skip

    def create() -> dict:
        change_service.direct("wo_create", params, account, request.state.request_id, "圖紙頁")
        row = schedule_service.create_work_order(
            body.part_id, body.qty, body.due_on, body.priority, body.release_on, body.note,
            created_by=account.id,
        )  # fmt: skip
        audit_created(row)
        return row

    def resume(marker: dict) -> dict:
        """開單已提交、回應還沒記錄就中斷：不再開一張，補做工廠資料庫重建與稽核，回覆同一張。"""
        row = schedule_service.work_order_view(marker["ref"])
        get_inventory_repo().ensure_built(force=True)
        audit_created(row)
        return row

    row, replay = idempotency.run(
        account.id, "wo_create", idempotency_key, params, create, resume=resume
    )
    if replay:
        response.headers["Idempotent-Replayed"] = "true"
    return row


@router.delete("/production/work-orders/{wo_no}", response_model=S.OkResponse, tags=["production"])
def cancel_work_order(
    wo_no: str,
    request: Request,
    response: Response,
    idempotency_key: str | None = IDEMPOTENCY,
):
    """取消圖紙頁開立的工單（kb/inventory 的既有工單不能取消；生管只能取消自己開的）。"""
    account = identity.current(request)

    def audit_cancelled() -> None:
        get_production_repo().add_audit(
            {"actor_id": account.id, "actor_label": account.label, "action": "寫入",
             "op": "wo_cancel", "ref_no": wo_no, "summary": f"取消工單 {wo_no}", "detail": None,
             "request_id": request.state.request_id},
            once=True,
        )  # fmt: skip

    def cancel() -> dict:
        w = get_production_repo().get_work_order(wo_no)
        if not w or w["status"] != "已開立":
            schedule_service.cancel_work_order(wo_no)  # 回 404 WORK_ORDER_NOT_FOUND
        change_service.direct(
            "wo_cancel", {"wo_no": wo_no}, account, request.state.request_id, "API"
        )
        schedule_service.cancel_work_order(wo_no)
        audit_cancelled()
        return {"ok": True}

    def resume(marker: dict) -> dict:
        get_inventory_repo().ensure_built(force=True)
        audit_cancelled()
        return {"ok": True}

    out, replay = idempotency.run(
        account.id, "wo_cancel", idempotency_key, {"wo_no": wo_no}, cancel, resume=resume
    )
    if replay:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.post(
    "/schedule/solve",
    tags=["production"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：meta／progress／solution／done／error（見 shared/sse_events.md）",
        }
    },
)
async def solve_schedule(body: S.ScheduleSolveRequest, request: Request):
    """把所有未完工工單的工序排到機台：Timefold Solver 求解，串流目前最佳解；結果寫回資料庫。

    只有生管可以執行（403 PERMISSION_DENIED，在串流開始前回 JSON 錯誤）。
    這是在 API 行程內求解的舊入口（連線中斷就停止）；背景工作請用 POST /schedule/jobs。
    """
    security.schedule_check_create(security.SecurityContext.for_request(request, "schedule.solve"))
    legacy.require_inprocess("schedule")
    stream = schedule_service.solve_stream(
        request_id=request.state.request_id, seconds=body.seconds, engine=body.engine
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/schedule/stop", response_model=S.OkResponse, tags=["production"])
async def stop_schedule(request: Request, body: S.ScheduleStopRequest | None = None):
    """提前結束目前的求解，採用目前最佳的合法解（照常驗證並寫回）——和取消不同：
    取消（POST /jobs/{id}/cancel）放棄、不寫回。要 schedule_run（生管）；訪客、業務、倉管、
    藝術家都是 403，也不會碰到任何求解。同時作用在背景工作與 API 行程內的舊入口。"""
    account = identity.current(request)
    if not security.schedule_can_control(account):
        identity.require_domain(account, "factory")
        identity.require(account, "schedule_run", "提前結束排程")
    stopped = False
    if jobs.enabled():
        try:
            result, _ = await run_in_threadpool(
                service.stop_schedule, account, body.job_id if body else None
            )
            stopped = result == "stopping"
        except AppError as e:
            if e.code != "JOBS_UNAVAILABLE":
                raise
    return S.OkResponse(ok=await schedule_service.stop() or stopped)


@router.get("/schedule/runs/{run_id}", response_model=S.ScheduleRunDetail, tags=["production"])
def schedule_run(run_id: str, request: Request):
    identity.require_domain(identity.current(request), "factory")
    return schedule_service.run_detail(run_id)


@router.post("/admin/production/reset", response_model=S.OkResponse, tags=["production"])
def reset_production(request: Request):
    """展示還原：清掉圖紙頁開立的工單、所有排程結果、智慧助理的異動、待核准單、稽核紀錄
    與攔截紀錄（只在展示模式、本機；生管或主管才可以，docs/adr/030）。"""
    identity.require_demo(request, "展示還原")
    identity.require(identity.current(request), "demo_reset", "展示還原")
    schedule_service.reset()
    return S.OkResponse()
