"""路由與請求驗證：只處理輸入輸出，商業邏輯在 services/。所有路徑加 /api/v1 前綴。"""

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api import schemas as S
from app.core.config import REPO_ROOT, get_models_config, get_settings, kb_version
from app.core.errors import AppError
from app.rag import providers
from app.rag.kb import current_kb_hash
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services import cad_service, chat_service, search_service

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


@router.get("/parts", response_model=S.PartListResponse, tags=["parts"])
def list_parts():
    store = get_store()
    return {
        "kb_version": store.manifest.get("kb_version", ""),
        "items": [search_service.part_summary(p) for p in store.parts],
    }


@router.get("/parts/{part_id}", response_model=S.PartDetail, tags=["parts"])
def get_part(part_id: str):
    p = _part_or_404(part_id)
    summary = search_service.part_summary(p)
    return {
        **p,
        "drawing_url": summary["drawing_url"],
        "thumb_url": summary["thumb_url"],
        "model_url": summary["model_url"],
        "step_url": f"/api/v1/parts/{part_id}/model.step",
    }


@router.get("/parts/{part_id}/drawing", response_class=FileResponse, tags=["parts"])
def get_part_drawing(part_id: str, size: str = "full"):
    p = _part_or_404(part_id)
    store = get_store()
    path = (
        store.parts_dir / "thumbs" / f"{part_id}.jpg"
        if size == "thumb"
        else REPO_ROOT / p["drawing"]
    )
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/parts/{part_id}/model.{ext}", response_class=FileResponse, tags=["parts"])
def get_part_model(part_id: str, ext: str):
    """知識庫零件的標準 3D 模型（由 kb/cad/<id>.py 產生）：stl 給前端 3D 檢視、step 給 CAD 軟體。"""
    _part_or_404(part_id)
    if ext not in ("stl", "step"):
        raise AppError("VALIDATION_ERROR", "只提供 stl 或 step", 422)
    path = get_store().parts_dir / "gt" / f"{part_id}.{ext}"
    media = "model/stl" if ext == "stl" else "application/step"
    return FileResponse(path, media_type=media, filename=f"{part_id}.{ext}")


@router.post("/search/drawing", response_model=S.DrawingSearchResponse, tags=["search"])
def search_drawing(body: S.DrawingSearchRequest):
    return search_service.identify_drawing(body.image_id, body.top_k)


@router.post("/search/any", response_model=S.AnySearchResponse, tags=["search"])
def search_any(body: S.ImageSearchRequest):
    """不指定領域的以圖搜圖：先判斷是畫作還是工廠圖紙（領域路由），再做該領域的辨識。"""
    return search_service.identify_any(body.image_id, body.top_k)


@router.get("/search/parts", response_model=S.PartTextSearchResponse, tags=["search"])
def search_parts(q: str = Query(min_length=1, max_length=200), top_k: int | None = None):
    return search_service.search_parts_text(q, top_k)


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
    """工廠圖紙 → Ortho2CAD 產生 CadQuery 程式碼 → 沙箱執行 → 3D 模型。"""
    stream = cad_service.reconstruct_stream(
        request_id=request.state.request_id,
        part_id=body.part_id,
        image_id=body.image_id,
        strategy=body.strategy,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/cad/jobs/{job_id}", response_model=S.CadJobSummary, tags=["cad"])
def get_cad_job(job_id: str):
    """已完成的 3D 重建（meta／result／done 三個事件的內容），給前端重看結果、不必重跑。"""
    path = get_settings().cad_jobs_dir / job_id / "summary.json"
    if not cad_service.JOB_ID.match(job_id) or not path.is_file():
        raise AppError("CAD_JOB_NOT_FOUND", "找不到這個 3D 重建結果，可能已超過保存期限", 404)
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/parts/{part_id}/reconstructions", response_model=S.CadJobListResponse, tags=["cad"])
def list_part_reconstructions(part_id: str):
    _part_or_404(part_id)
    root = get_settings().cad_jobs_dir
    rows = [
        r
        for r in get_logs_repo().recent_cad_for_part(part_id, 10)
        if (root / r["job_id"] / "summary.json").is_file()
    ]
    return {"items": rows}


@router.get("/cad/jobs/{job_id}/{name}", response_class=FileResponse, tags=["cad"])
def get_cad_file(job_id: str, name: str):
    if not cad_service.JOB_ID.match(job_id) or name not in cad_service.JOB_FILES:
        raise AppError("CAD_JOB_NOT_FOUND", "找不到這個 3D 重建結果", 404)
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


@router.post("/feedback", response_model=S.OkResponse, tags=["chat"])
def feedback(body: S.FeedbackRequest):
    get_logs_repo().add_feedback(body.request_id, body.rating, body.note)
    return S.OkResponse()


@router.get("/eval/runs", response_model=S.EvalRunsResponse, tags=["eval"])
def eval_runs():
    runs = []
    for p in sorted((REPO_ROOT / "eval" / "runs").glob("*.json"), reverse=True):
        if not p.name.endswith(("-cad.json", "-router.json")):  # 圖紙與領域路由評估另有格式
            runs.append(json.loads(p.read_text(encoding="utf-8")))
    return {"runs": runs}


@router.get("/eval/cad-runs", response_model=S.CadEvalRunsResponse, tags=["eval"])
def cad_eval_runs():
    """工廠圖紙評估（make eval-cad）：圖紙辨識與 Ortho2CAD 3D 重建。"""
    paths = sorted((REPO_ROOT / "eval" / "runs").glob("*-cad.json"), reverse=True)
    return {"runs": [json.loads(p.read_text(encoding="utf-8")) for p in paths]}


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
    )


@router.post("/admin/outage", response_model=S.OkResponse, tags=["system"])
def simulate_outage(body: S.OutageRequest):
    """展示用：模擬主推論伺服器斷線，本地備援模型不受影響（DEMO_CONTROLS=false 時停用）。"""
    if not get_settings().demo_controls:
        raise AppError("FORBIDDEN", "展示控制已停用", 403)
    providers.OUTAGE["enabled"] = body.enabled
    return S.OkResponse()
