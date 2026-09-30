"""統一錯誤格式 {"error": {"code", "message", "request_id"}}；錯誤碼見 shared/error_codes.md。"""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


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


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        error_body("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", _rid(request)),
        status_code=500,
    )
