"""工廠 3D 重建：圖紙 → 零件模型。

背景工作的建立、進度、取消與產物在 jobs.py（`POST /cad/jobs` 回 202）；這裡是既有的 SSE 入口
（`POST /cad/reconstruct`，在 API 行程內執行）與既有結果的相容讀取。兩條路徑共用同一套授權：
工作從建立起就有擁有者與來源資源，未完成、失敗、已完成的摘要、程式碼、輸入圖、預覽與模型都驗
`security.cad_can_read`，不看 `summary.json` 在不在。
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.api import schemas as S
from app.api.deps import visible_part
from app.core.errors import AppError, safe_stream
from app.repositories.logs_repo import get_logs_repo
from app.services import cad_access, cad_service, identity, security

router = APIRouter()


@router.post(
    "/cad/reconstruct",
    tags=["cad"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：meta／token／result／done／error（見 shared/sse_events.md）",
        }
    },
)
async def reconstruct(body: S.ReconstructRequest, request: Request):
    """工廠圖紙 → Ortho2CAD 產生 CadQuery 程式碼 → 沙箱執行 → 3D 模型（在 API 行程內執行的舊入口；
    建議改用 `POST /cad/jobs` 背景工作）。要能使用工廠圖紙；指定的圖紙、上傳的照片、
    照片辨識出的圖紙也都要有權限（資料範圍）。"""
    ctx = security.SecurityContext.for_request(request, "cad.reconstruct", domain="mfg")
    security.cad_check_create(ctx, body.part_id, body.image_id)
    from app.jobs import legacy

    legacy.require_inprocess("cad")
    stream = cad_service.reconstruct_stream(
        request_id=request.state.request_id,
        part_id=body.part_id,
        image_id=body.image_id,
        strategy=body.strategy,
        account=ctx.account,
    )
    return StreamingResponse(
        safe_stream(stream, request.state.request_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _authorized_job(job_id: str, request: Request) -> security.CadAccess:
    """工作存在而且目前身分有權限：不存在、不是你的、看不到的圖紙都是同一個 404。
    領域不符（例如一般員工）在查工作之前就 403，和工作存不存在無關。"""
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    return security.cad_require_read(account, cad_access.read_access(job_id))


@router.get("/cad/jobs/{job_id}", response_model=S.CadJobSummary, tags=["cad"])
def get_cad_job(job_id: str, request: Request):
    """已完成的 3D 重建（meta／result／done 三個事件的內容），給前端重看結果、不必重跑。
    還在執行或沒有摘要的工作用 `GET /jobs/{id}` 查狀態。"""
    _authorized_job(job_id, request)
    d = cad_access.published_dir(job_id)
    if d is None or not (d / "summary.json").is_file():
        raise AppError(*security.CAD_JOB_NOT_FOUND)
    return json.loads((d / "summary.json").read_text(encoding="utf-8"))


@router.get("/parts/{part_id}/reconstructions", response_model=S.CadJobListResponse, tags=["cad"])
def list_part_reconstructions(part_id: str, request: Request):
    visible_part(part_id, request)
    account = identity.current(request)
    rows = [
        r
        for r in get_logs_repo().recent_cad_for_part(part_id, 10)
        if (a := cad_access.read_access(r["job_id"])) is not None
        and security.cad_can_read(account, a)  # 列表和單筆讀取同一套政策
        and (d := cad_access.published_dir(r["job_id"])) is not None
        and (d / "summary.json").is_file()
    ]
    return {"items": rows}


@router.get("/cad/jobs/{job_id}/{name}", response_class=FileResponse, tags=["cad"])
def get_cad_file(job_id: str, name: str, request: Request):
    """3D 重建的檔案（舊網址，和 `GET /jobs/{id}/artifacts/{name}` 同一套授權）。"""
    if name not in cad_access.JOB_FILES:
        raise AppError(*security.CAD_JOB_NOT_FOUND)
    _authorized_job(job_id, request)
    d = cad_access.published_dir(job_id)  # 背景工作：只讀資料庫記錄的那一次嘗試
    path = d / name if d is not None else None
    if path is None or not path.is_file():
        raise AppError(*security.CAD_JOB_NOT_FOUND)
    media = {
        "stl": "model/stl",
        "step": "application/step",
        "png": "image/png",
        "py": "text/x-python",
    }
    return FileResponse(
        path,
        media_type=media[name.rsplit(".", 1)[1]],
        filename=f"{job_id}-{name}",
        headers={"Cache-Control": "private, no-store"},
    )
