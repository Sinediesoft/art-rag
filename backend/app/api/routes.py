"""路由與請求驗證：只處理輸入輸出，商業邏輯在 services/。所有路徑加 /api/v1 前綴。"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.agent import jev
from app.api import schemas as S
from app.core.config import (
    REPO_ROOT,
    get_agent_config,
    get_models_config,
    get_settings,
    kb_version,
)
from app.core.errors import AppError
from app.rag import providers
from app.rag.kb import current_kb_hash
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.text2sql import load_examples, prompt_versions
from app.repositories.index_store import get_store
from app.repositories.inventory_repo import get_inventory_repo
from app.repositories.logs_repo import get_logs_repo
from app.repositories.production_repo import get_production_repo
from app.services import (
    agent_service,
    cad_service,
    change_service,
    chat_service,
    color_service,
    identity,
    memory_guard,
    schedule_service,
    search_service,
    sql_service,
)

router = APIRouter(prefix="/api/v1", responses={"4XX": {"model": S.ErrorResponse}})

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


@router.post("/images", response_model=S.ImageUploadResponse, tags=["images"])
async def upload_image(file: UploadFile = File(...)):
    s = get_settings()
    if file.content_type not in ALLOWED_TYPES:
        raise AppError("IMAGE_TYPE_NOT_ALLOWED", "只收 JPEG、PNG、WebP", 415)
    raw = await file.read()
    if len(raw) > s.upload_max_mb * 1024 * 1024:
        raise AppError("IMAGE_TOO_LARGE", f"圖片超過 {s.upload_max_mb} MB", 413)
    img = load_image(raw)  # EXIF 轉正、縮圖；重新編碼後不留 EXIF／GPS
    image_id = "img_" + uuid.uuid4().hex[:16]
    s.uploads_dir.mkdir(parents=True, exist_ok=True)
    (s.uploads_dir / f"{image_id}.jpg").write_bytes(to_jpeg_bytes(img))
    get_logs_repo().add_upload(image_id, f"{image_id}.jpg")
    return S.ImageUploadResponse(image_id=image_id, width=img.width, height=img.height)


@router.get("/images/{image_id}", response_class=FileResponse, tags=["images"])
def get_image(image_id: str):
    path = get_logs_repo().get_upload(image_id)
    if not path:
        raise AppError("IMAGE_NOT_FOUND", "找不到這張照片", 404)
    return FileResponse(get_settings().uploads_dir / path, media_type="image/jpeg")


@router.get("/images/{image_id}/colors", response_model=S.ColorAnalysis, tags=["images"])
def get_photo_colors(image_id: str):
    return color_service.photo_colors(image_id)


@router.get("/images/{image_id}/colormap.png", response_class=Response, tags=["images"])
def get_photo_colormap(image_id: str):
    return Response(color_service.photo_colormap(image_id), media_type="image/png")


@router.post("/search/image", response_model=S.ImageSearchResponse, tags=["search"])
def search_image(body: S.ImageSearchRequest):
    return search_service.identify(body.image_id, body.top_k)


@router.get("/search/text", response_model=S.TextSearchResponse, tags=["search"])
def search_text(q: str = Query(min_length=1, max_length=200), top_k: int | None = None):
    return search_service.search_text(q, top_k)


@router.get("/artworks", response_model=S.ArtworkListResponse, tags=["artworks"])
def list_artworks():
    store = get_store()
    return {
        "kb_version": store.manifest.get("kb_version", ""),
        "items": [search_service.artwork_summary(a) for a in store.artworks],
    }


@router.get("/artworks/{artwork_id}", response_model=S.ArtworkDetail, tags=["artworks"])
def get_artwork(artwork_id: str):
    a = get_store().get_artwork(artwork_id)
    if not a:
        raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {artwork_id}", 404)
    summary = search_service.artwork_summary(a)
    return {**a, "image_url": summary["image_url"], "thumb_url": summary["thumb_url"]}


@router.get("/artworks/{artwork_id}/image", response_class=FileResponse, tags=["artworks"])
def get_artwork_image(artwork_id: str, size: str = "full"):
    a = get_store().get_artwork(artwork_id)
    if not a:
        raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {artwork_id}", 404)
    path: Path = (
        get_settings().index_dir / "thumbs" / f"{artwork_id}.jpg"
        if size == "thumb"
        else REPO_ROOT / a["image"]["path"]
    )
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/artworks/{artwork_id}/colors", response_model=S.ColorAnalysis, tags=["artworks"])
def get_artwork_colors(artwork_id: str):
    return color_service.artwork_colors(artwork_id)


@router.get("/artworks/{artwork_id}/colormap.png", response_class=FileResponse, tags=["artworks"])
def get_artwork_colormap(artwork_id: str):
    return FileResponse(
        color_service.artwork_colormap_path(artwork_id),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post(
    "/chat",
    tags=["chat"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：sources／token／done／error",
        }
    },
)
async def chat(body: S.ChatRequest, request: Request):
    """圖文問答。工廠圖紙要看目前身分的資料範圍（看不到的回 403 DATA_SCOPE_DENIED）；
    檢索只取看得到的段落（Metadata Filter），放進 prompt 前先過濾夾帶指令的段落（docs/adr/012）。"""
    account = identity.current(request)
    if body.part_id:
        identity.require_part(account, _part_or_404(body.part_id))
    stream = chat_service.chat_stream(
        question=body.question,
        request_id=request.state.request_id,
        strategy=body.strategy,
        artwork_id=body.artwork_id,
        image_id=body.image_id,
        use_retrieval=body.use_retrieval,
        allow_fallback=body.allow_fallback,
        part_id=body.part_id,
        rearrange=body.rearrange,
        account=account,
        post_filter=body.post_filter,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- 工廠機械加工圖
def _part_or_404(part_id: str) -> dict:
    p = get_store().get_part(part_id)
    if not p:
        raise AppError("PART_NOT_FOUND", f"找不到圖紙 {part_id}", 404)
    return p


def _visible_part(part_id: str, request: Request) -> dict:
    """圖紙存在、而且目前身分看得到（資料範圍：領域＋機密等級）。"""
    p = _part_or_404(part_id)
    identity.require_part(identity.current(request), p)
    return p


@router.get("/parts", response_model=S.PartListResponse, tags=["parts"])
def list_parts(request: Request):
    """只列目前身分看得到的圖紙（業務看不到機密圖紙；訪客不能使用工廠圖紙 → 403）。"""
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    store = get_store()
    items = [p for p in store.parts if account.can_see(p["confidentiality"])]
    return {
        "kb_version": store.manifest.get("kb_version", ""),
        "items": [search_service.part_summary(p) for p in items],
        "hidden": len(store.parts) - len(items),
    }


@router.get("/parts/{part_id}", response_model=S.PartDetail, tags=["parts"])
def get_part(part_id: str, request: Request):
    p = _visible_part(part_id, request)
    summary = search_service.part_summary(p)
    return {
        **p,
        "drawing_url": summary["drawing_url"],
        "thumb_url": summary["thumb_url"],
        "model_url": summary["model_url"],
        "step_url": f"/api/v1/parts/{part_id}/model.step",
    }


@router.get("/parts/{part_id}/drawing", response_class=FileResponse, tags=["parts"])
def get_part_drawing(part_id: str, request: Request, size: str = "full"):
    p = _visible_part(part_id, request)
    store = get_store()
    path = (
        store.parts_dir / "thumbs" / f"{part_id}.jpg"
        if size == "thumb"
        else REPO_ROOT / p["drawing"]
    )
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/parts/{part_id}/model.{ext}", response_class=FileResponse, tags=["parts"])
def get_part_model(part_id: str, ext: str, request: Request):
    """知識庫零件的標準 3D 模型（由 kb/cad/<id>.py 產生）：stl 給前端 3D 檢視、step 給 CAD 軟體。"""
    _visible_part(part_id, request)
    if ext not in ("stl", "step"):
        raise AppError("VALIDATION_ERROR", "只提供 stl 或 step", 422)
    path = get_store().parts_dir / "gt" / f"{part_id}.{ext}"
    media = "model/stl" if ext == "stl" else "application/step"
    return FileResponse(path, media_type=media, filename=f"{part_id}.{ext}")


def _scope_drawing_result(result: dict, account: identity.Account) -> dict:
    """以圖搜圖紙的結果只留看得到的圖紙；照片辨識出的正是看不到的圖紙 → 403。"""
    if result["matched"]:
        identity.require_part(account, get_store().get_part(result["best_part_id"]))
    return {
        **result,
        "results": [r for r in result["results"] if account.can_see(r["part"]["confidentiality"])],
    }


@router.post("/search/drawing", response_model=S.DrawingSearchResponse, tags=["search"])
def search_drawing(body: S.DrawingSearchRequest, request: Request):
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    return _scope_drawing_result(
        search_service.identify_drawing(body.image_id, body.top_k), account
    )


@router.post("/search/any", response_model=S.AnySearchResponse, tags=["search"])
def search_any(body: S.ImageSearchRequest, request: Request):
    """不指定領域的以圖搜圖：先判斷是畫作還是工廠圖紙（領域路由），再做該領域的辨識。
    判成工廠圖紙時要看目前身分的資料範圍（訪客不能使用工廠圖紙 → 403）。"""
    account = identity.current(request)
    result = search_service.identify_any(body.image_id, body.top_k)
    if result["drawing_result"] is not None:
        identity.require_domain(account, "mfg")
        result["drawing_result"] = _scope_drawing_result(result["drawing_result"], account)
    return result


@router.get("/search/parts", response_model=S.PartTextSearchResponse, tags=["search"])
def search_parts(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    top_k: int | None = None,
):
    """以文字找圖紙：只檢索目前身分看得到的圖紙段落（Metadata Filter）。"""
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    return search_service.search_parts_text(q, top_k, account.levels)


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
    """工廠圖紙 → Ortho2CAD 產生 CadQuery 程式碼 → 沙箱執行 → 3D 模型。
    要能使用工廠圖紙；指定或照片辨識出的圖紙也要看得到（資料範圍）。"""
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    if body.part_id:
        identity.require_part(account, _part_or_404(body.part_id))
    stream = cad_service.reconstruct_stream(
        request_id=request.state.request_id,
        part_id=body.part_id,
        image_id=body.image_id,
        strategy=body.strategy,
        account=account,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _cad_job(job_id: str, request: Request) -> dict:
    """3D 重建結果的摘要；是知識庫圖紙的重建時，也要看得到那張圖紙（資料範圍）。"""
    path = get_settings().cad_jobs_dir / job_id / "summary.json"
    if not cad_service.JOB_ID.match(job_id) or not path.is_file():
        raise AppError("CAD_JOB_NOT_FOUND", "找不到這個 3D 重建結果，可能已超過保存期限", 404)
    summary = json.loads(path.read_text(encoding="utf-8"))
    account = identity.current(request)
    identity.require_domain(account, "mfg")
    part = (summary.get("meta") or {}).get("part")
    if part and (p := get_store().get_part(part["id"])):
        identity.require_part(account, p)
    return summary


@router.get("/cad/jobs/{job_id}", response_model=S.CadJobSummary, tags=["cad"])
def get_cad_job(job_id: str, request: Request):
    """已完成的 3D 重建（meta／result／done 三個事件的內容），給前端重看結果、不必重跑。"""
    return _cad_job(job_id, request)


@router.get("/parts/{part_id}/reconstructions", response_model=S.CadJobListResponse, tags=["cad"])
def list_part_reconstructions(part_id: str, request: Request):
    _visible_part(part_id, request)
    root = get_settings().cad_jobs_dir
    rows = [
        r
        for r in get_logs_repo().recent_cad_for_part(part_id, 10)
        if (root / r["job_id"] / "summary.json").is_file()
    ]
    return {"items": rows}


@router.get("/cad/jobs/{job_id}/{name}", response_class=FileResponse, tags=["cad"])
def get_cad_file(job_id: str, name: str, request: Request):
    if not cad_service.JOB_ID.match(job_id) or name not in cad_service.JOB_FILES:
        raise AppError("CAD_JOB_NOT_FOUND", "找不到這個 3D 重建結果", 404)
    if (get_settings().cad_jobs_dir / job_id / "summary.json").is_file():
        _cad_job(job_id, request)  # 還在重建中（沒有摘要）的檔案只有發起的人知道網址
    path = get_settings().cad_jobs_dir / job_id / name
    if not path.is_file():
        raise AppError("CAD_JOB_NOT_FOUND", "找不到這個 3D 重建結果，可能已超過保存期限", 404)
    media = {
        "stl": "model/stl",
        "step": "application/step",
        "png": "image/png",
        "py": "text/x-python",
    }
    return FileResponse(path, media_type=media[name.rsplit(".", 1)[1]], filename=f"{job_id}-{name}")


# ---------------------------------------------------------------- 工廠庫存（Text-to-SQL）
@router.get("/inventory/schema", response_model=S.InventorySchemaResponse, tags=["inventory"])
def inventory_schema(request: Request):
    """庫存資料庫的資料表與欄位說明（與給模型看的 schema 同一份來源）。"""
    identity.require_domain(identity.current(request), "factory")
    repo = get_inventory_repo()
    repo.ensure_built()
    version = prompt_versions()[0]
    return {
        **repo.schema_info(),
        "prompt_version": version,
        "examples": [e["question"] for e in load_examples(version)],
    }


@router.get("/inventory/overview", response_model=S.InventoryOverviewResponse, tags=["inventory"])
def inventory_overview(request: Request):
    identity.require_domain(identity.current(request), "factory")
    repo = get_inventory_repo()
    items = repo.overview()
    return {"as_of": repo.as_of, "company": repo.manifest.get("company", ""), "items": items}


@router.get("/inventory/parts/{part_id}", response_model=S.PartInventory, tags=["inventory"])
def part_inventory(part_id: str, request: Request):
    """單一圖紙的庫存明細（固定查詢，不經模型）：各倉儲位、未完工工單、未出貨訂單、最近異動。"""
    identity.require_domain(identity.current(request), "factory")
    data = get_inventory_repo().part_inventory(part_id)
    if not data:
        raise AppError("PART_NOT_FOUND", f"庫存資料庫中沒有圖紙 {part_id}", 404)
    return data


@router.post(
    "/inventory/ask",
    tags=["inventory"],
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE：meta／attempt／sql_token／sql／result／token／done／error"
            "（見 shared/sse_events.md）",
        }
    },
)
async def inventory_ask(body: S.InventoryAskRequest, request: Request):
    """Text-to-SQL：中文問題 → 本地模型產生 SQL → 唯讀執行 → 依結果回答。"""
    identity.require_domain(identity.current(request), "factory")
    stream = sql_service.ask_stream(
        question=body.question,
        request_id=request.state.request_id,
        strategy=body.strategy,
        allow_fallback=body.allow_fallback,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------- 生產排程（Timefold）
@router.get("/production/overview", response_model=S.ProductionOverview, tags=["production"])
async def production_overview(request: Request):
    """排程頁：機台、行事曆、待排工單、排程服務狀態與目前排程。"""
    identity.require_domain(identity.current(request), "factory")
    return await schedule_service.overview()


@router.get("/production/parts/{part_id}", response_model=S.PartPlan, tags=["production"])
async def part_plan(part_id: str, request: Request):
    """圖紙頁的「生產工單」：製程途程、依庫存建議的數量與交期、這張圖紙的工單與排程結果。"""
    identity.require_domain(identity.current(request), "factory")
    return await schedule_service.part_plan(part_id)


@router.post(
    "/production/work-orders",
    response_model=S.WorkOrderCreated,
    status_code=201,
    tags=["production"],
)
def create_work_order(body: S.WorkOrderCreate, request: Request):
    """從圖紙頁開立工單：寫入生產資料庫，工廠資料庫（Text-to-SQL）自動同步，等待排程。

    權限與額度和智慧助理同一套（只有生管可以；急件要主管核准，回 409 APPROVAL_REQUIRED）。
    """
    account = identity.current(request)
    params = body.model_dump()
    change_service.direct("wo_create", params, account, request.state.request_id, "圖紙頁")
    row = schedule_service.create_work_order(
        body.part_id, body.qty, body.due_on, body.priority, body.release_on, body.note,
        created_by=account.id,
    )  # fmt: skip
    get_production_repo().add_audit(
        {"actor_id": account.id, "actor_label": account.label, "action": "寫入", "op": "wo_create",
         "ref_no": row["wo_no"], "summary": f"圖紙頁開立 {row['wo_no']}〈{row['part_name']}〉"
         f"{row['qty']} 件", "detail": None, "request_id": request.state.request_id}
    )  # fmt: skip
    return row


@router.delete("/production/work-orders/{wo_no}", response_model=S.OkResponse, tags=["production"])
def cancel_work_order(wo_no: str, request: Request):
    """取消圖紙頁開立的工單（kb/inventory 的既有工單不能取消；生管只能取消自己開的）。"""
    w = get_production_repo().get_work_order(wo_no)
    if not w or w["status"] != "已開立":
        schedule_service.cancel_work_order(wo_no)  # 回 404 WORK_ORDER_NOT_FOUND
    account = identity.current(request)
    change_service.direct("wo_cancel", {"wo_no": wo_no}, account, request.state.request_id, "API")
    schedule_service.cancel_work_order(wo_no)
    get_production_repo().add_audit(
        {"actor_id": account.id, "actor_label": account.label, "action": "寫入", "op": "wo_cancel",
         "ref_no": wo_no, "summary": f"取消工單 {wo_no}", "detail": None,
         "request_id": request.state.request_id}
    )  # fmt: skip
    return S.OkResponse()


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
    """
    identity.require(identity.current(request), "schedule_run", "執行排程")
    stream = schedule_service.solve_stream(
        request_id=request.state.request_id, seconds=body.seconds, engine=body.engine
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/schedule/stop", response_model=S.OkResponse, tags=["production"])
async def stop_schedule():
    """提前結束目前的求解，採用目前最佳解。"""
    return S.OkResponse(ok=await schedule_service.stop())


@router.get("/schedule/runs/{run_id}", response_model=S.ScheduleRunDetail, tags=["production"])
def schedule_run(run_id: str, request: Request):
    identity.require_domain(identity.current(request), "factory")
    return schedule_service.run_detail(run_id)


@router.post("/admin/production/reset", response_model=S.OkResponse, tags=["production"])
def reset_production(request: Request):
    """展示還原：清掉圖紙頁開立的工單、所有排程結果、智慧助理的異動、待核准單、稽核紀錄
    與五段防護的攔截紀錄（DEMO_CONTROLS=false 時停用；生管或主管才可以）。"""
    if not get_settings().demo_controls:
        raise AppError("FORBIDDEN", "展示控制已停用", 403)
    identity.require(identity.current(request), "demo_reset", "展示還原")
    schedule_service.reset()
    return S.OkResponse()


# ---------------------------------------------------------------- 記憶體管理
@router.get("/memory", response_model=S.MemoryStatus, tags=["system"])
def memory_status():
    """系統記憶體使用率、各模型是否載入／使用中、最近的釋放紀錄。"""
    return memory_guard.guard.status()


@router.post("/admin/memory/release", response_model=S.MemoryReleaseResponse, tags=["system"])
def release_memory():
    """展示用：不管使用率，立刻釋放目前流程與其他請求用不到的模型。"""
    if not get_settings().demo_controls:
        raise AppError("FORBIDDEN", "展示控制已停用", 403)
    g = memory_guard.guard
    event = g.check("手動釋放", set(g.current_models), force=True)
    return {"event": event, "status": g.status()}


@router.post("/feedback", response_model=S.OkResponse, tags=["chat"])
def feedback(body: S.FeedbackRequest):
    get_logs_repo().add_feedback(body.request_id, body.rating, body.note)
    return S.OkResponse()


@router.get("/eval/runs", response_model=S.EvalRunsResponse, tags=["eval"])
def eval_runs():
    runs = []
    for p in sorted((REPO_ROOT / "eval" / "runs").glob("*.json"), reverse=True):
        # 圖紙、領域路由、Text-to-SQL、色彩分析、展示測試、智慧助理路由（-route）與
        # 五段防護第 2 段（-guard）的評估另有格式
        if not p.name.endswith(
            (
                "-cad.json",
                "-router.json",
                "-sql.json",
                "-color.json",
                "-demo.json",
                "-route.json",
                "-guard.json",
            )
        ):
            runs.append(json.loads(p.read_text(encoding="utf-8")))
    return {"runs": runs}


@router.get("/eval/cad-runs", response_model=S.CadEvalRunsResponse, tags=["eval"])
def cad_eval_runs():
    """工廠圖紙評估（make eval-cad）：圖紙辨識與 Ortho2CAD 3D 重建。"""
    paths = sorted((REPO_ROOT / "eval" / "runs").glob("*-cad.json"), reverse=True)
    return {"runs": [json.loads(p.read_text(encoding="utf-8")) for p in paths]}


@router.get("/eval/sql-runs", response_model=S.SqlEvalRunsResponse, tags=["eval"])
def sql_eval_runs():
    """工廠庫存 Text-to-SQL 評估（make eval-sql）：執行正確率、可執行率、修正次數。"""
    paths = sorted((REPO_ROOT / "eval" / "runs").glob("*-sql.json"), reverse=True)
    return {"runs": [json.loads(p.read_text(encoding="utf-8")) for p in paths]}


@router.get("/eval/route-runs", response_model=S.RouteEvalRunsResponse, tags=["eval"])
def route_eval_runs():
    """智慧助理路由評估（make eval-route）：Jev 與本地路由的正確率、修改誤判、延遲、外送量。"""
    paths = sorted((REPO_ROOT / "eval" / "runs").glob("*-route.json"), reverse=True)[:10]
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    for r in runs:  # 清單只回摘要，逐題結果留在檔案
        for e in r.get("engines", []):
            e.pop("rows", None)
    return {"runs": runs}


@router.get("/health", response_model=S.HealthResponse, tags=["system"])
async def health():
    s = get_settings()
    cfg = get_models_config().strategies
    store = get_store()
    problems = store.check_manifest(store.manifest) if store.manifest else ["索引未載入"]
    if store.manifest and not problems and current_kb_hash() != store.manifest.get("kb_hash"):
        problems.append("kb/ 內容已變更但尚未重建索引（請執行 make index）")

    hybrid_model = s.hybrid_model or cfg["hybrid"].default_model
    fallback_model = s.hybrid_fallback_model or cfg["hybrid_fallback"].default_model
    fallback_url = s.hybrid_fallback_base_url or s.hybrid_base_url
    if s.llm_mode == "mock":
        hybrid_ok, hybrid_detail = True, "LLM_MODE=mock"
        fallback_ok, fallback_detail = True, "LLM_MODE=mock"
        ortho_ok, ortho_detail = True, "LLM_MODE=mock（回傳標準模型程式碼）"
    else:
        hybrid_ok, hybrid_detail = await providers.ping_ollama(s.hybrid_base_url)
        fallback_ok, fallback_detail = await providers.ping_ollama(
            fallback_url, simulate_outage=False
        )
        ortho_ok, ortho_detail = await providers.ping_ollama(
            s.ortho2cad_base_url, simulate_outage=False
        )
        if not ortho_ok:
            ortho_detail += "；請執行 make ortho2cad"
    cloud_ok, cloud_detail = providers.cloud_status()
    strategies = {
        "hybrid": S.StrategyStatus(
            label=cfg["hybrid"].label,
            model=hybrid_model,
            available=hybrid_ok,
            detail=f"{s.hybrid_base_url}（{hybrid_detail}）",
        ),
        "hybrid_fallback": S.StrategyStatus(
            label=cfg["hybrid_fallback"].label,
            model=fallback_model,
            available=fallback_ok,
            detail=f"{fallback_url}（{fallback_detail}）",
        ),
        **{
            k: S.StrategyStatus(
                label=cfg[k].label,
                model=s.api_model or cfg[k].default_model,
                available=cloud_ok,
                detail=cloud_detail,
            )
            for k in ("api_nokb", "api_kb")
        },
        "ortho2cad": S.StrategyStatus(
            label=cfg["ortho2cad"].label,
            model=s.ortho2cad_model or cfg["ortho2cad"].default_model,
            available=ortho_ok,
            detail=f"{s.ortho2cad_base_url}（{ortho_detail}）",
        ),
        "lora": S.StrategyStatus(
            label=cfg["lora"].label,
            model=s.lora_model or cfg["lora"].default_model,
            available=s.lora_enabled,
            detail="已啟用" if s.lora_enabled else "選做：插槽已保留，未啟用",
        ),
    }
    logs = get_logs_repo()
    # PostgreSQL 容器停了也要回得出狀態頁：ping 放到執行緒（最多等 5 秒，不卡住其他請求），
    # 失敗就不查最近紀錄
    db_ok = await run_in_threadpool(logs.ping)
    inv = get_inventory_repo()
    inv_ok = inv.ping()
    scheduler = await schedule_service.engine_status()
    memory = await asyncio.to_thread(memory_guard.guard.status)
    return S.HealthResponse(
        status="ok" if db_ok and not problems and hybrid_ok else "degraded",
        db=db_ok,
        index_consistent=not problems,
        index_problems=problems,
        kb_version=kb_version(),
        manifest=store.manifest,
        strategies=strategies,
        llm_mode=s.llm_mode,
        embed_mode=s.embed_mode,
        allow_cloud=s.allow_cloud,
        outage_simulated=providers.OUTAGE["enabled"],
        demo_controls=s.demo_controls,
        recent_chats=logs.recent_chats(10) if db_ok else [],
        recent_cad=logs.recent_cad(10) if db_ok else [],
        inventory={
            "ok": inv_ok,
            "as_of": inv.as_of,
            "tables": inv.manifest.get("tables", {}),
            "problems": inv.problems,
        },
        recent_sql=logs.recent_sql(10) if db_ok else [],
        scheduler=scheduler,
        memory=memory,
        system1=_system1_status(),
        recent_routes=logs.recent_routes(10) if db_ok else [],
    )


def _system1_status() -> S.System1Status:
    s = get_settings()
    cfg = get_agent_config()
    ok, detail = jev.status()
    return S.System1Status(
        jev_configured=ok,
        detail=detail,
        model=s.jev_model,
        timeout_s=s.jev_timeout_s,
        thresholds=cfg["thresholds"],
        clarify_margin=cfg["clarify_margin"],
    )


@router.post("/admin/outage", response_model=S.OkResponse, tags=["system"])
def simulate_outage(body: S.OutageRequest):
    """展示用：模擬主推論伺服器斷線，本地備援模型不受影響（DEMO_CONTROLS=false 時停用）。"""
    if not get_settings().demo_controls:
        raise AppError("FORBIDDEN", "展示控制已停用", 403)
    providers.OUTAGE["enabled"] = body.enabled
    return S.OkResponse()


# ---------------------------------------------------------------- 智慧助理（docs/adr/011）
def _accounts(account: identity.Account) -> dict:
    return {
        "current": account.public(),
        "accounts": [a.public() for a in identity.accounts().values()],
        "demo_controls": get_settings().demo_controls,
        "pending_approvals": len(get_production_repo().approvals(status="待核准")),
    }


@router.get("/auth/accounts", response_model=S.AccountsResponse, tags=["agent"])
def list_accounts(request: Request):
    """展示帳號與目前身分（身分存在伺服器端的工作階段，預設訪客）。"""
    return _accounts(identity.current(request))


@router.post("/auth/switch", response_model=S.AccountsResponse, tags=["agent"])
def switch_account(body: S.SwitchAccountRequest, request: Request, response: Response):
    """展示版切換身分（不用密碼；DEMO_CONTROLS=false 時停用）。"""
    return _accounts(identity.switch(request, response, body.account_id))


@router.post("/agent/route", response_model=S.RouteResponse, tags=["agent"])
async def agent_route(body: S.RouteRequest, request: Request):
    """五段防護的第 1、2 段（docs/adr/012）：個資遮蔽 → 本地分流判斷意圖＋信心閘門 →
    RBAC（身分、資料範圍、動作權限）→ Jev 第一層護欄 → 分派到哪個本地模組；擋下就拒絕並記錄。"""
    if not body.question.strip() and not body.image_id:
        raise AppError("VALIDATION_ERROR", "請輸入一句話或附一張照片", 422)
    return await agent_service.route(
        body.question,
        identity.current(request),
        request.state.request_id,
        image_id=body.image_id,
        forced_intent=body.forced_intent,
        engine=body.engine,
    )


@router.post("/changes/preview", response_model=S.ChangePreview, tags=["agent"])
async def change_preview(body: S.ChangePreviewRequest, request: Request):
    """修改資料流程 1～4：參數抽取 → 權限判定 → 試算（交易內套用後回滾）→ 額度判斷 → 確認卡。"""
    return await change_service.preview(
        identity.current(request),
        request.state.request_id,
        question=body.question,
        op=body.op,
        params=body.params,
    )


@router.post("/changes/{pending_id}/commit", response_model=S.ChangeCommitted, tags=["agent"])
def change_commit(pending_id: str, request: Request):
    """按確認：寫入前再驗權限與資料指紋 → 寫異動單與稽核紀錄 → 依資料庫讀回結果回覆。"""
    return change_service.commit(pending_id, identity.current(request), request.state.request_id)


@router.post("/changes/{pending_id}/request-approval", response_model=S.Approval, tags=["agent"])
def change_request_approval(pending_id: str, body: S.ApprovalRequest, request: Request):
    """超過額度：建立待核准單（AP-）送主管。"""
    return change_service.request_approval(
        pending_id, identity.current(request), body.note, request.state.request_id
    )


@router.get("/approvals", response_model=S.ApprovalsResponse, tags=["agent"])
def list_approvals(request: Request):
    """待核准清單（主管處理）、我的申請、最近的核准紀錄；超過 24 小時的自動失效。"""
    return change_service.list_approvals(identity.current(request))


@router.post("/approvals/{ap_no}/approve", response_model=S.ApprovalDecision, tags=["agent"])
def approve(ap_no: str, body: S.ApprovalRequest, request: Request):
    """主管核准：重新試算比對申請時的資料 → 寫入前再驗權限與資料版本 → 寫入。不接受用對話核准。"""
    return change_service.approve(
        ap_no, identity.current(request), body.note, request.state.request_id
    )


@router.post("/approvals/{ap_no}/return", response_model=S.ApprovalDecision, tags=["agent"])
def return_approval(ap_no: str, body: S.ReturnRequest, request: Request):
    """主管退回（要附理由，申請人在「我的申請」看得到）。"""
    return change_service.return_(
        ap_no, identity.current(request), body.reason, request.state.request_id
    )


@router.get("/security/logs", response_model=S.SecurityLogsResponse, tags=["agent"])
def security_logs(limit: int = Query(default=20, ge=1, le=200)):
    """五段防護的拒絕並記錄（docs/adr/012）：RBAC 與 Jev 護欄擋下的請求、Jev 過濾移除的段落。
    只存遮蔽個資後的文字。"""
    repo = get_logs_repo()
    tz = timezone(timedelta(hours=8))
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = repo.count_security(midnight.astimezone(UTC).isoformat())
    return {
        "items": repo.recent_security(limit),
        "today": {"rbac": counts.get(1, 0), "guard": counts.get(2, 0), "post": counts.get(4, 0)},
    }


@router.get("/audit", response_model=S.AuditResponse, tags=["agent"])
def audit_log(limit: int = Query(default=30, ge=1, le=200)):
    """稽核紀錄（寫入、拒絕、送核准、核准、退回、失效）與最近的異動單。"""
    prod = get_production_repo()
    return {"items": prod.audit(limit), "changes": prod.changes(limit=10)}
