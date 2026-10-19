"""舊的 SSE 入口（POST /cad/reconstruct、/schedule/solve）能不能在 API 行程內執行。

LEGACY_SYNC_JOBS：auto＝沒有啟用背景工作系統才開（單機開發）；on＝一律開；off＝一律關。
關閉時回 409 USE_JOBS_API，指向對應的背景工作端點——不會靜默改成背景工作，也不會偷偷執行。
"""

from app.core.config import get_settings
from app.core.errors import AppError

JOBS_ENDPOINT = {"cad": "POST /api/v1/cad/jobs", "schedule": "POST /api/v1/schedule/jobs"}


def inprocess_allowed() -> bool:
    mode = get_settings().legacy_sync_jobs
    if mode == "on":
        return True
    if mode == "off":
        return False
    return not get_settings().jobs_enabled


def require_inprocess(kind: str) -> None:
    if not inprocess_allowed():
        raise AppError(
            "USE_JOBS_API",
            f"這台伺服器的 3D 重建與排程改由背景 worker 執行，請改用 {JOBS_ENDPOINT[kind]}"
            "（回 202 與 job_id，再用 GET /api/v1/jobs/{id}/events 或輪詢取得進度）",
            409,
        )
