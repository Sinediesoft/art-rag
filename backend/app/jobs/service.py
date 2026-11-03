"""背景工作的對外操作（API 層呼叫）：建立、查詢、取消、提前停止、產物。

每個操作都先過 `services/security.py` 的政策：建立驗功能與資源授權、讀取／取消／下載驗目前身分
對這個工作的授權（看不到的工作回 404，和不存在同一個回應）。工作系統（REDIS_URL＋工作資料庫）
沒有啟用，或工作資料庫連不上，一律回 503 JOBS_UNAVAILABLE——不會改在 API 行程執行。
"""

import re
from pathlib import Path

from app.core.errors import AppError
from app.jobs import audit, broker, db, store
from app.jobs import enabled as jobs_enabled
from app.services import cad_access, security, uploads
from app.services.identity import Account

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SCHEDULE_SCOPE = "schedule:plant"
# 回給前端的 3D 重建來源（找回進行中的工作用）；source_owner_id 只供授權，不回傳
TARGET_KEYS = ("part_id", "image_id", "identified_part_id")
CONTENT_TYPES = {
    "stl": "model/stl",
    "step": "application/step",
    "png": "image/png",
    "py": "text/x-python",
}


def unavailable(message: str | None = None) -> AppError:
    return AppError(
        "JOBS_UNAVAILABLE",
        message
        or "背景工作系統目前無法使用（Redis 或工作資料庫）；請稍後再試，工作不會在 API 行程執行",
        503,
    )


def require_enabled() -> None:
    if not jobs_enabled():
        raise unavailable("這台伺服器沒有啟用背景工作系統（REDIS_URL、JOBS_DATABASE_URL）")


def check_key(key: str | None) -> str | None:
    if key is None:
        return None
    if not IDEMPOTENCY_KEY.match(key):
        raise AppError(
            "VALIDATION_ERROR", "Idempotency-Key 要 8～128 個英數字元或 . _ : -（建議用 UUID）", 422
        )
    return key


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except db.JobsUnavailable as e:
        raise unavailable(e.message) from e


def public(job: dict, *, with_artifacts: bool = True) -> dict:
    """對外的工作內容：不含 idempotency key、請求雜湊、租約與 worker 資訊。"""
    out = {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "owner_id": job["owner_id"],
        "attempt": job["attempt"],
        "max_attempts": job["max_attempts"],
        "cancel_requested": job["cancel_requested"],
        "stop_requested": job["stop_requested"],
        "progress": job["progress"],
        "result": job["result"],
        "error": job["error"],
        "input_version": job["input_version"],
        "params": job["params"],
        # 3D 重建的來源（圖紙或自己的照片）：找回進行中的工作、避免重複建立時比對用
        "target": ({k: v for k, v in job["resource"].items() if v and k in TARGET_KEYS} or None)
        if job["kind"] == "cad"
        else None,
        "created_at": store.iso(job["created_at"]),
        "started_at": store.iso(job["started_at"]),
        "finished_at": store.iso(job["finished_at"]),
        "last_event_id": job["event_seq"],
        "status_url": f"/api/v1/jobs/{job['id']}",
        "events_url": f"/api/v1/jobs/{job['id']}/events",
        "artifacts": [],
    }
    if with_artifacts and job["status"] in ("succeeded", "failed") and job["kind"] == "cad":
        out["artifacts"] = [
            {"name": a["name"], "url": f"/api/v1/jobs/{job['id']}/artifacts/{a['name']}",
             "content_type": a["content_type"], "size_bytes": a["size_bytes"]}
            for a in store.artifacts(job["id"])
        ]  # fmt: skip
    return out


def created(job: dict, new: bool, delayed: bool) -> dict:
    return {
        "job_id": job["id"],
        "status": job["status"],
        "kind": job["kind"],
        "status_url": f"/api/v1/jobs/{job['id']}",
        "events_url": f"/api/v1/jobs/{job['id']}/events",
        "idempotent_replay": not new,
        "delayed_dispatch": delayed,
    }


# ----------------------------------------------------------------------------- 建立
def create_cad(ctx: security.SecurityContext, body: dict, key: str | None, request_id: str):
    require_enabled()
    part_id, image_id = body.get("part_id"), body.get("image_id")
    if not part_id and not image_id:
        raise AppError("VALIDATION_ERROR", "請指定 part_id 或 image_id", 422)
    security.cad_check_create(ctx, part_id, image_id)  # 授權在建立工作之前：失敗不留任何工作
    params = {"strategy": body.get("strategy", "ortho2cad")}
    if body.get("regenerate"):  # 「結果不對，重新生成」：不查快取、重抽一個候選（docs/adr/042）
        params["regenerate"] = True
    # 照片來源記下照片擁有者：讀取權跟著照片的 ACL，這只在照片紀錄過期後用來認擁有者本人
    resource = {
        "part_id": part_id,
        "image_id": image_id,
        "source_owner_id": uploads.owner_of(image_id) if image_id else None,
    }
    scope = f"cad:{part_id or image_id}"
    job, new = _guard(
        store.create_job,
        kind="cad",
        owner_id=ctx.account.id,
        params=params,
        resource=resource,
        scope=scope,
        request_id=request_id,
        idempotency_key=check_key(key),
    )
    if new:
        audit.record(ctx.account, "建立", "cad_job", job["id"], "建立 3D 重建工作", request_id)
    return created(job, new, _dispatch(new))


def create_schedule(ctx: security.SecurityContext, body: dict, key: str | None, request_id: str):
    require_enabled()
    security.schedule_check_create(ctx)
    from app.services import schedule_service

    versions = _guard(schedule_service.current_input_versions)
    expected = body.get("input_version")
    if expected and expected != versions["version"]:
        raise AppError(
            "SCHEDULE_INPUT_CHANGED",
            "工單、途程、機台或行事曆已經變更，您看到的資料已過時；請重新整理後再排程",
            409,
        )
    params = {"seconds": body.get("seconds"), "engine": body.get("engine", "timefold")}
    job, new = _guard(
        store.create_job,
        kind="schedule",
        owner_id=ctx.account.id,
        params=params,
        resource={},
        scope=SCHEDULE_SCOPE,
        request_id=request_id,
        input_version=versions["version"],
        input_versions=versions,
        idempotency_key=check_key(key),
    )
    if new:
        audit.record(ctx.account, "建立", "schedule_job", job["id"], "建立排程工作", request_id)
    return created(job, new, _dispatch(new))


def _dispatch(new: bool) -> bool:
    """建立後立刻試投一次；broker 不通留在 outbox（工作維持 queued，維護迴圈會重試）。"""
    if not new:
        return False
    sent, failed = broker.dispatch_now()
    return failed > 0 and sent == 0


# ----------------------------------------------------------------------------- 查詢
def get(account: Account, job_id: str) -> dict:
    require_enabled()
    job = _guard(store.get, job_id)
    return public(security.job_require_read(account, job))


def readable(account: Account, job_id: str) -> dict:
    require_enabled()
    return security.job_require_read(account, _guard(store.get, job_id))


def list_jobs(account: Account, kind: str | None, active: bool, limit: int) -> list[dict]:
    require_enabled()
    statuses = store.ACTIVE if active else None
    rows = _guard(store.list_jobs, kind=kind, statuses=statuses, limit=limit * 3)
    return [public(j) for j in rows if security.job_can_read(account, j)][:limit]


def event_batch(account_id: str, job_id: str, after: int) -> tuple[str | None, list[dict]]:
    """事件串流的一輪輪詢：回 (工作狀態, 新事件)。每一輪都依「目前的」權限重驗；
    工作不存在、沒有權限、帳號已被撤銷一律回 (None, [])——呼叫端送 PERMISSION_REVOKED 並結束。
    先讀工作狀態再讀事件：看到終態時，該工作的事件都已經寫入並在回傳的事件裡。"""
    try:
        account = security.resolve_account(account_id)
    except AppError:
        return None, []
    job = _guard(store.get, job_id)
    if job is None or not security.job_can_read(account, job):
        return None, []
    return job["status"], _guard(store.events_after, job_id, after)


# ----------------------------------------------------------------------------- 取消與提前停止
def cancel(account: Account, job_id: str) -> tuple[str, dict]:
    require_enabled()
    job = security.job_require_read(account, _guard(store.get, job_id))
    if not security.job_can_cancel(account, job):
        raise AppError("PERMISSION_DENIED", "目前身分沒有取消這個工作的權限", 403)
    result, row = _guard(store.cancel, job_id)
    if result in ("cancelled", "cancelling"):
        audit.record(account, "取消", f"{job['kind']}_job", job_id, "取消背景工作")
    if result == "too_late":
        raise AppError("JOB_COMMITTING", "工作已進入寫回階段，無法取消（會完成並寫入結果）", 409)
    if result == "finished":
        raise AppError("JOB_ALREADY_FINISHED", f"工作已經結束（{row['status']}），無法取消", 409)
    return result, public(row)


def stop_schedule(account: Account, job_id: str | None) -> tuple[str, dict | None]:
    """提前結束排程：採用目前最佳的合法解並照常驗證、寫回（和取消不同）。
    沒給 job_id 就是目前排程範圍裡執行中的工作。"""
    require_enabled()
    if not security.schedule_can_control(account):
        raise AppError("PERMISSION_DENIED", "目前身分沒有提前結束排程的權限", 403)
    if job_id:
        job = _guard(store.get, job_id)
        if job is None or job["kind"] != "schedule":
            raise AppError(*security.JOB_NOT_FOUND)
    else:
        job = _guard(store.active_job, SCHEDULE_SCOPE)
        if job is None:
            return "none", None
    result, row = _guard(store.request_stop, job["id"])
    if result == "stopping":
        audit.record(account, "提前結束", "schedule_job", job["id"], "提前結束排程，採用目前最佳解")
    return result, public(row)


# ----------------------------------------------------------------------------- 產物
def artifact(account: Account, job_id: str, name: str) -> tuple[Path, str]:
    """授權後回傳（檔案路徑, content-type）。名稱只接受白名單；工作還在執行（尚未發布）回 409。"""
    job = readable(account, job_id)
    if job["kind"] != "cad" or name not in cad_access.JOB_FILES:
        raise AppError("JOB_ARTIFACT_NOT_FOUND", "找不到這個產物", 404)
    if job["status"] not in ("succeeded", "failed"):
        raise AppError("JOB_NOT_FINISHED", "工作還沒完成，產物尚未發布", 409)
    # 和舊網址同一個判斷：只讀 fenced finish 記錄的那一次嘗試（恢復迴圈判定的失敗沒有產物）
    n = cad_access.published_attempt(job)
    row = next((a for a in _guard(store.artifacts, job_id) if a["name"] == name), None)
    path = cad_access.attempt_dir(job_id, n) / name if n is not None else None
    if row is None or path is None or not path.is_file():
        raise AppError("JOB_ARTIFACT_NOT_FOUND", "找不到這個產物", 404)
    return path, row["content_type"]
