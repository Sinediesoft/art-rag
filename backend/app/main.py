"""FastAPI 進入點：uvicorn app.main:app --port 8000"""

import asyncio
import sys
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.agent import guard, local_router
from app.analysis import style
from app.api.routes import router
from app.core import jwt
from app.core.config import get_settings
from app.core.errors import (
    AppError,
    app_error_handler,
    error_body,
    unhandled_error_handler,
    validation_error_handler,
)
from app.core.logging import log, new_request_id, setup_logging
from app.rag.embedders import warmup
from app.repositories import db
from app.repositories.index_store import IndexMismatch, get_store
from app.repositories.inventory_repo import get_inventory_repo
from app.repositories.logs_repo import get_logs_repo
from app.services import identity, memory_guard, region_service
from app.services.cad_service import purge_jobs
from app.services.intake_service import purge_drafts


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    s = get_settings()
    # 一致性保護：索引與模型版本不符就拒絕啟動
    try:
        get_store().load()
    except IndexMismatch as e:
        log.error(f"拒絕啟動：{e}")
        print(f"\n[ArtRAG] 拒絕啟動：{e}\n", file=sys.stderr)
        raise SystemExit(1) from e
    for path in get_logs_repo().purge_uploads(s.upload_ttl_days):
        (s.uploads_dir / path).unlink(missing_ok=True)
    purge_jobs(s.upload_ttl_days)
    purge_drafts(s.upload_ttl_days)
    region_service.purge_drafts(s.upload_ttl_days)
    # 庫存資料庫（Text-to-SQL）：kb/inventory 有變動就重建；資料有誤只停用庫存查詢，不擋啟動
    try:
        get_inventory_repo().ensure_built()
    except Exception as e:  # noqa: BLE001
        log.error(f"庫存資料庫建立失敗：{e}")
    warmup()
    local_router.warmup()
    style.warmup()
    m = get_store().manifest
    where = f"PostgreSQL {db.describe(s.database_url)}" if s.database_url else "檔案索引＋SQLite"
    gpu = memory_guard.gpu_memory() if memory_guard.has_gpu() else None
    vram = (
        f"；顯示記憶體 {gpu.percent:.0f}%（{gpu.name}，"
        f"超過 {s.memory_gpu_high_pct:.0f}% 時釋放 VRAM 裡的閒置模型）"
        if gpu
        else ""
    )
    log.info(
        f"ArtRAG 就緒：{m['artwork_count']} 幅畫、{m.get('part_count', 0)} 張工廠圖紙，"
        f"kb_version={m['kb_version']}；資料存放：{where}；"
        f"工廠資料庫 {get_inventory_repo().manifest.get('tables', {})}；"
        f"記憶體 {memory_guard.memory_percent():.0f}%"
        f"（超過 {s.memory_high_pct:.0f}% 時釋放閒置模型）{vram}"
    )
    # 記憶體管理的背景監控：超過門檻就釋放最近一次流程用不到的模型
    watcher = asyncio.create_task(memory_guard.watch()) if s.memory_guard else None
    yield
    if watcher:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
    db.close_pool()


app = FastAPI(
    title="地端隱私多模態 RAG 專題 API",
    version="0.1.0",
    description=(
        "以多模態 RAG 打造的畫作導覽助理，以及工廠機械加工圖助理（Ortho2CAD 三視圖→3D、"
        "庫存 Text-to-SQL、Timefold 生產排程）。"
        "SSE 事件見 shared/sse_events.md。"
    ),
    lifespan=lifespan,
)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


def _reject(request: Request, code: str, message: str) -> JSONResponse:
    """七段權限控管第 1 段（docs/adr/015）：憑證不對就在閘道直接拒絕，請求碰不到任何模型與資料。
    簽章不符（竄改或偽造）寫進拒絕並記錄；沒帶、過期、舊金鑰簽發（後端重啟）是正常狀況
    （前端會重新取得），不記。"""
    rid = request.state.request_id
    if code == "TOKEN_INVALID":
        got = identity.token_of(request)
        claimed = jwt.peek(got[0]) if got else {}
        who = (
            f"憑證聲稱〈{claimed.get('name', '?')}〉clearance={claimed.get('clearance', '?')}："
            if claimed
            else ""
        )
        guard.log_block(
            1,
            "憑證無效",
            None,
            f"{who}{message} → {request.method} {request.url.path}",
            rid,
            "API 閘道（JWT）",
        )
    response = JSONResponse(error_body(code, message, rid), status_code=401)
    response.headers["WWW-Authenticate"] = 'Bearer realm="art-rag"'
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID") or new_request_id()
    if request.url.path.startswith("/api/"):
        # 第 1 段：API 閘道驗 JWT（簽章、效期）；不符就 401，後面的檢索、模型都不會執行
        if denied := identity.gateway(request):
            return _reject(request, *denied)
        # make index 後自動換上新索引。PostgreSQL 版要查資料庫，放到執行緒：
        # 資料庫停掉時最多等 5 秒，不能卡住其他正在串流的請求
        await run_in_threadpool(get_store().maybe_reload)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


app.include_router(router)

# 展示用：直接由後端提供前端建置檔（正式部署改由 Nginx 提供）
_dist = get_settings().frontend_dist
if _dist.is_dir():
    app.mount("/assets", StaticFiles(directory=_dist / "assets"), name="assets")

    _dist_root = _dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        # 先解析成絕對路徑再確認仍在 dist/ 底下：擋掉 /%2e%2e/%2e%2e/.env 這類路徑穿越
        file = (_dist_root / path).resolve()
        if path and file.is_relative_to(_dist_root) and file.is_file():
            return FileResponse(file)
        # index.html 不快取：重新建置後瀏覽器才會載到新的 assets（assets 檔名有 hash，可以快取）
        return FileResponse(_dist_root / "index.html", headers={"Cache-Control": "no-cache"})
