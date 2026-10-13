"""背景工作：工單排程求解（schedule 佇列）。

求解本體重用 `schedule_service.solve_core`（和舊的 SSE 入口同一份），差別在外面這一層：
- 進度寫成持久化事件（最新一版排程快照也存在工作列上），頁面離開、API 重啟都查得到
- 輸入版本：建立時保存（工單／途程／機台／行事曆各一個雜湊）；開始求解與寫回時各比對，
  不一樣就是 SCHEDULE_INPUT_CHANGED，舊解不覆蓋新資料
- 停止與取消是兩件事：`stop_requested`＝提前結束、採用目前最佳的合法解並照常驗證寫回；
  `cancel_requested`＝放棄，不採用任何解、不寫回
- 寫回前驗證（硬限制、後端重算分數與 Timefold 一致）、重驗權限，取得寫回資格（reserve_commit）後
  才寫生產資料庫；run_id 由 job_id 決定，重送與舊 worker 延遲寫回都是同一列（先提交的那一次
  為準，之後的嘗試不覆蓋）。工作結果一律依資料庫裡實際存下的那一列排程填寫，所以就算舊嘗試在
  寫回鎖連線中斷後搶先提交，工作結果、排程詳情與工廠資料庫也一致
"""

import asyncio
import time

from app.core.errors import AppError
from app.jobs import audit, store
from app.jobs.control import JobCancelled, JobControl
from app.repositories.production_repo import get_production_repo
from app.services import memory_guard, schedule_service, security


def run(job: dict, control: JobControl) -> None:
    asyncio.run(_run(job, control))


def _authorize(job: dict) -> security.SecurityContext:
    ctx = security.SecurityContext.for_account(
        job["owner_id"], "schedule.job.run", request_id=job["request_id"] or ""
    )
    security.schedule_check_create(ctx)
    return ctx


async def _event(job: dict, control: JobControl, type_: str, data: dict, progress=None) -> None:
    await asyncio.to_thread(
        store.add_event, job["id"], job["attempt"], control.worker_id, type_, data, progress
    )


def _brief_meta(meta: dict) -> dict:
    return {
        "engine": meta["engine"],
        "engine_label": meta["engine_label"],
        "engine_version": meta["engine_version"],
        "fallback_reason": meta["fallback_reason"],
        "seconds": meta["seconds"],
        "unimproved_seconds": meta["unimproved_seconds"],
        "problem": meta["problem"],
        "work_orders": meta["work_orders"],
        "machines": meta["machines"],
        "axis": meta["axis"],
    }


async def _run(job: dict, control: JobControl) -> None:
    await asyncio.to_thread(_authorize, job)  # 領取後、求解前：撤權就不建問題、不呼叫 Timefold
    params = job["params"]
    outcome = None
    last_emit = 0.0
    try:
        async with memory_guard.flow("schedule", {"timefold"}):
            async for kind, data in schedule_service.solve_core(
                job["request_id"] or "",
                params.get("seconds"),
                params.get("engine", "timefold"),
                should_stop=control.should_stop,
                should_abort=control.should_abort,
                expected_version=job["input_version"],
            ):
                control.check("solve")
                if kind == "error":
                    status = 409 if data["code"] == "SCHEDULE_INPUT_CHANGED" else 422
                    raise AppError(data["code"], data["message"], status)
                if kind == "meta":
                    await _event(job, control, "meta", _brief_meta(data), {"stage": "solving"})
                elif kind in ("progress", "tick"):
                    now = time.monotonic()
                    if (kind == "tick" and now - last_emit < 1.0) or (
                        kind == "progress" and now - last_emit < 0.5
                    ):
                        continue
                    last_emit = now
                    await _event(job, control, kind, data, {"stage": "solving", **data})
                elif kind == "outcome":
                    outcome = data
    except schedule_service.Aborted:
        control.check("abort")  # 取消 → JobCancelled、租約遺失 → LeaseLost
        raise JobCancelled("abort") from None
    if outcome is None:
        raise AppError("SCHEDULE_FAILED", "排程沒有產出結果")
    control.check("commit")
    await asyncio.to_thread(_commit, job, control, outcome)


def _as_bool(v) -> bool | None:
    return None if v is None else bool(v)  # SQLite 存成 0／1


def _commit(job: dict, control: JobControl, o) -> None:
    jid, attempt, wid = job["id"], job["attempt"], control.worker_id
    schedule_service.validate_outcome(o)  # 後端重算的硬限制與分數；不通過 → 失敗、不寫回
    _authorize(job)  # 取得寫回資格前重驗權限
    if not store.reserve_commit(jid, attempt, wid):
        store.heartbeat(jid, attempt, wid)  # 租約或資源已被接手 → LeaseLost；否則就是被取消
        raise JobCancelled("commit")
    with store.commit_section(jid, attempt, wid) as section:  # 取得寫回鎖並 fence
        _authorize(job)  # 持有寫回鎖之後、寫入之前最後一次
        # fence 在寫入交易裡、真正寫入之前呼叫（見 production_repo.save_run）
        run_id = schedule_service.write_back(
            o, job["request_id"] or "", job_id=jid, fence=section.fence
        )
        total_ms = round((time.perf_counter() - o.t0) * 1000)
        # 以實際存下的那一列為準：同一個 run_id 已由先提交的嘗試寫入時，這次不覆蓋，
        # 結果也照存下的內容回報（不會出現「工作結果是 B 的分數、排程是 A 的解」）
        stored = get_production_repo().get_run(run_id) or {}
        result = {
            "run_id": run_id,
            "engine": stored.get("engine", o.used),
            "run_status": stored.get("status", o.status),  # done／stopped（提前結束）
            "score": stored.get("score", o.score),
            "hard": stored.get("hard", o.ev["hard"]),
            "medium": stored.get("medium", o.ev["medium"]),
            "soft": stored.get("soft", o.ev["soft"]),
            "initial_score": stored.get("initial_score", o.initial_score),
            "improvements": stored.get("improvements", o.improvements),
            "score_check": (o.score_check if not stored else _as_bool(stored.get("score_check"))),
            "input_version": o.versions["version"],
            "latency_ms": {"build": o.build_ms, "solve": o.solve_ms, "total": total_ms},
            "egress": {"images": 0, "chunks": 0, "bytes": 0},
        }
        store.finish(jid, attempt, wid, "succeeded", result=result)
        audit.record(
            None,
            "寫入",
            "schedule_run",
            run_id,
            f"排程結果寫回（{result['engine']}，{result['run_status']}，{result['score']}）",
            job["request_id"],
            detail={"job_id": jid, "attempt": attempt},
            actor_id=job["owner_id"],
        )
