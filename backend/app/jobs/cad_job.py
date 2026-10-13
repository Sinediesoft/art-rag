"""背景工作：工廠圖紙 → 3D 重建（gpu 佇列）。

流程重用 `cad_service._reconstruct_stream`（和舊的 SSE 入口同一條管線：辨識 → 讀尺寸 →
Ortho2CAD → AST 檢查 → 平台沙箱 → 孔位校正的整體回退），差別只在外面這一層：
- 事件：階段與進度（生成中只給字元數，不給模型輸出）；程式碼只放在受保護的 code.py 產物
- 產物：先寫到暫存資料夾，成功寫回時整個資料夾一次改名發布（原子），讀取端不會看到半成品。
  每次嘗試發布到自己的資料夾（<job>/a<attempt>，不刪、不覆蓋任何既有資料夾）；讀取端只認工作
  資料庫記錄的那一次嘗試——租約已失效的舊嘗試晚到發布，也不會蓋掉新嘗試的產物
- 權限：執行前、寫回前（取得寫回資格後、發布前）各依「目前的」access.yaml 重驗一次
"""

import asyncio
import hashlib
import os
import shutil
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import AppError
from app.jobs import store
from app.jobs.control import JobCancelled, JobControl
from app.jobs.sse_util import parse
from app.services import cad_access, cad_service, memory_guard, security
from app.services.cad_access import JOB_FILES

CONTENT_TYPES = {
    "stl": "model/stl",
    "step": "application/step",
    "png": "image/png",
    "py": "text/x-python",
}


def artifact_url(job_id: str, name: str) -> str:
    return f"/api/v1/jobs/{job_id}/artifacts/{name}"


def run(job: dict, control: JobControl) -> None:
    asyncio.run(_run(job, control))


def _authorize(job: dict) -> security.SecurityContext:
    """提交的帳號現在還能不能做這件事：帳號、工廠圖紙領域、圖紙範圍、上傳照片的擁有者。"""
    ctx = security.SecurityContext.for_account(
        job["owner_id"], "cad.job.run", domain="mfg", request_id=job["request_id"] or ""
    )
    res = job["resource"]
    security.cad_check_create(ctx, res.get("part_id"), res.get("image_id"))
    return ctx


async def _run(job: dict, control: JobControl) -> None:
    ctx = await asyncio.to_thread(_authorize, job)  # 領取後、執行前：撤權就不讀圖、不呼叫模型
    staging = get_settings().cad_jobs_dir / ".staging" / f"{job['id']}-a{job['attempt']}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        meta, result, done = await _drive(job, control, ctx, staging)
        control.check("commit")
        await asyncio.to_thread(_commit, job, control, staging, meta, result, done)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def _event(job: dict, control: JobControl, type_: str, data: dict, progress=None) -> None:
    await asyncio.to_thread(
        store.add_event, job["id"], job["attempt"], control.worker_id, type_, data, progress
    )


async def _drive(job: dict, control: JobControl, ctx, staging: Path):
    params, res = job["params"], job["resource"]
    strategy = params.get("strategy", "ortho2cad")
    models = {"ortho2cad" if strategy == "ortho2cad" else "qwen"}
    if res.get("image_id"):
        models |= {"clip", "qwen"}
    events = cad_service._reconstruct_stream(
        request_id=job["request_id"] or "",
        part_id=res.get("part_id"),
        image_id=res.get("image_id"),
        strategy=strategy,
        account=ctx.account,
        job_id=job["id"],
        out_dir=staging,
        file_url=artifact_url,
        include_code=False,
        checkpoint=control.check,
    )

    async def consume():
        meta = result = done = None
        chars, last_emit = 0, 0.0
        async for raw in memory_guard.stream("reconstruct", models, events):
            name, data = parse(raw)
            if name == "meta":
                meta = data
                part = data.get("identified") or {}
                if part.get("matched") and part.get("best_part_id"):
                    await asyncio.to_thread(
                        store.update_resource,
                        job["id"],
                        job["attempt"],
                        control.worker_id,
                        {"identified_part_id": part["best_part_id"]},
                    )
                await _event(
                    job, control, "meta", data, {"stage": "generating", "label": "模型生成中"}
                )
            elif name == "token":
                chars += len(data.get("text", ""))
                now = time.monotonic()
                if now - last_emit >= 1.0:  # 只給字元數，不給模型輸出（程式碼走受保護的 code.py）
                    last_emit = now
                    p = {"stage": "generating", "label": "模型生成中", "chars": chars}
                    await _event(job, control, "progress", p, p)
            elif name == "executing":
                p = {"stage": "executing", "label": "沙箱執行中", **data}
                await _event(job, control, "stage", p, p)
            elif name == "result":
                result = data
            elif name == "done":
                done = data
            elif name == "error":
                raise AppError(data["code"], cad_service.public_error(data["message"]) or "失敗")
        return meta, result, done

    task = asyncio.create_task(consume())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.3)
            if control.lost or control.cancelled:
                task.cancel()  # 生成中斷；沙箱子行程由 run_cad 在 CancelledError 時殺掉
        try:
            meta, result, done = await task
        except asyncio.CancelledError:
            control.check("cancelled")
            raise
    finally:
        if not task.done():
            task.cancel()
    if result is None or done is None:
        raise AppError("CAD_NO_RESULT", "3D 重建沒有產出結果")
    return meta, result, done


def _artifact_rows(directory: Path) -> list[dict]:
    rows = []
    for name in sorted(JOB_FILES):
        p = directory / name
        if p.is_file():
            data = p.read_bytes()
            rows.append(
                {
                    "name": name,
                    "content_type": CONTENT_TYPES[name.rsplit(".", 1)[1]],
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return rows


def _commit(job: dict, control: JobControl, staging: Path, meta, result, done) -> None:
    jid, attempt, wid = job["id"], job["attempt"], control.worker_id
    _authorize(job)  # 取得寫回資格之前再驗一次；撤權就失敗，不發布
    if not store.reserve_commit(jid, attempt, wid):
        store.heartbeat(jid, attempt, wid)  # 租約或資源已被接手 → LeaseLost；否則就是被取消
        raise JobCancelled("commit")
    with store.commit_section(jid, attempt, wid) as section:  # 取得寫回鎖並 fence
        _authorize(job)  # 寫回前（持有寫回鎖之後）最後一次驗證
        final = cad_access.attempt_dir(jid, attempt)  # 這一次嘗試自己的資料夾
        section.fence()  # 不可回滾的發布之前，在持有寫回鎖的連線上再確認一次
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():  # 同一次嘗試不會發布兩次；有就是異常，不刪不蓋
            raise AppError("CAD_PUBLISH_CONFLICT", "這次嘗試的產物資料夾已經存在，沒有覆蓋")
        # 原子發布：整個資料夾一次出現。就算這之前寫回鎖的連線斷了、工作被收回由新嘗試完成，
        # 這裡也只會多出一個沒人讀的舊嘗試資料夾，新嘗試的產物不受影響
        os.replace(staging, final)
        ok = bool(result.get("ok"))
        error = None
        if not ok:
            error = {
                "code": "CAD_EXEC_FAILED",
                "message": cad_service.public_error(result.get("error")) or "3D 重建執行失敗",
            }
        store.finish(
            jid,
            attempt,
            wid,
            "succeeded" if ok else "failed",
            result={"meta": meta, "result": result, "done": done},
            error=error,
            artifact_rows=_artifact_rows(final),
        )
