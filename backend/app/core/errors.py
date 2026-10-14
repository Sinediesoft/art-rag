"""統一錯誤格式 {"error": {"code", "message", "request_id"}}；錯誤碼見 shared/error_codes.md。"""

import json
import traceback
from collections.abc import AsyncIterator

import psycopg
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import log


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def error_body(code: str, message: str, request_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(error_body(exc.code, exc.message, _rid(request)), status_code=exc.status)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    msg = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return JSONResponse(error_body("VALIDATION_ERROR", msg, _rid(request)), status_code=422)


# 框架自己丟的 HTTP 錯誤（405、找不到路由）也用統一格式，不回 {"detail": ...}
HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
    message = {404: "沒有這個 API", 405: "這個 API 不接受這種方法"}.get(
        exc.status_code, str(exc.detail)
    )
    return JSONResponse(
        error_body(code, message, _rid(request)),
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # 例外內容可能含 SQL、檔案路徑、連線字串：只寫進伺服器 log，回應只給 request_id 讓管理員對照
    _log_error(_rid(request), exc)
    return JSONResponse(
        error_body("INTERNAL_ERROR", "伺服器內部錯誤，請把 request_id 提供給管理員", _rid(request)),
        status_code=500,
    )


# 資料庫連不上（Docker 停了、連線池拿不到連線）：回 503 與看得懂的訊息，不要變成籠統的 500
DB_DOWN = (psycopg.OperationalError, PoolTimeout)
DB_DOWN_MESSAGE = "資料庫暫時連不上，請稍後再試；一直這樣請通知管理員"


def _log_error(request_id: str, exc: BaseException) -> None:
    log.error(
        "unhandled_error",
        extra={
            "fields": {
                "request_id": request_id,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": "".join(traceback.format_exception(exc)),
            }
        },
    )


async def db_error_handler(request: Request, exc: Exception) -> JSONResponse:
    _log_error(_rid(request), exc)
    return JSONResponse(
        error_body("DATABASE_UNAVAILABLE", DB_DOWN_MESSAGE, _rid(request)), status_code=503
    )


async def safe_stream(stream: AsyncIterator[str], request_id: str) -> AsyncIterator[str]:
    """串流途中出錯：送一個 SSE error 事件再結束，不讓連線直接斷掉
    （前端只看到回答中斷、不知道為什麼）。例外內容只寫伺服器 log。"""
    try:
        async for chunk in stream:
            yield chunk
    except Exception as exc:  # noqa: BLE001 — 串流已經開始，只能用事件告知
        _log_error(request_id, exc)
        db = isinstance(exc, DB_DOWN) or type(exc).__name__ == "DatabaseUnavailable"
        code, message = (
            ("DATABASE_UNAVAILABLE", DB_DOWN_MESSAGE)
            if db
            else ("INTERNAL_ERROR", "伺服器內部錯誤，請把 request_id 提供給管理員")
        )
        data = {"code": code, "message": message, "request_id": request_id}
        yield f"event: error\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
