"""FastAPI 進入點：uvicorn app.main:app --port 8000"""

import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.core.errors import (
    AppError,
    app_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.core.logging import log, new_request_id, setup_logging
from app.rag.embedders import warmup
from app.repositories.index_store import IndexMismatch, get_store
from app.repositories.logs_repo import get_logs_repo
from app.services.cad_service import purge_jobs


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
    warmup()
    m = get_store().manifest
    log.info(
        f"ArtRAG 就緒：{m['artwork_count']} 幅畫、{m.get('part_count', 0)} 張工廠圖紙，"
        f"kb_version={m['kb_version']}"
    )
    yield


app = FastAPI(
    title="畫語 ArtRAG API",
    version="0.1.0",
    description=(
        "以多模態 RAG 打造的畫作導覽助理，以及工廠機械加工圖助理（Ortho2CAD 三視圖→3D）。"
        "SSE 事件見 shared/sse_events.md。"
    ),
    lifespan=lifespan,
)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID") or new_request_id()
    if request.url.path.startswith("/api/"):
        get_store().maybe_reload()  # make index 後自動換上新索引
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
