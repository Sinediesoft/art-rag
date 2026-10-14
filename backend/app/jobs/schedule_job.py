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
- 生產資料庫（SQLite）和工作資料庫（PostgreSQL）是兩個資料庫，沒有共同交易。寫回的稽核和排程
  在同一個 SQLite 交易；排程一旦提交，之後的結案（工作標成成功、填結果）失敗都不能把工作收成
  failed：先退讓重試，一直不行就留給恢復迴圈依生產資料庫補做（reconcile），只會有一筆排程與
  一筆稽核
"""

import asyncio
import logging
import time

from app.core.errors import AppError
from app.jobs import audit, store
from app.jobs.control import JobCancelled, JobControl
from app.repositories.production_repo import get_production_repo
from app.services import memory_guard, schedule_service, security

_log = logging.getLogger("artrag.jobs")
FINALIZE_TRIES = 4  # 排程寫入後結案的嘗試次數（間隔 0.5、1、2 秒）


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


def _result(job: dict, stored: dict, o=None) -> dict:
    """工作結果一律依生產資料庫實際存下的那一列：同一個 run_id 已由先提交的嘗試寫入時，這次
    不覆蓋，結果也照存下的內容回報（不會出現「工作結果是 B 的分數、排程是 A 的解」）。
    o 是這次的求解（恢復迴圈補做時沒有）：只用來補存下的那一列沒有的建問題與總耗時。"""
    total = round((time.perf_counter() - o.t0) * 1000) if o else None
    return {
        "run_id": stored["run_id"],
        "engine": stored["engine"],
        "run_status": stored["status"],  # done／stopped（提前結束）
        "score": stored["score"],
        "hard": stored["hard"],
        "medium": stored["medium"],
        "soft": stored["soft"],
        "initial_score": stored["initial_score"],
        "improvements": stored["improvements"],
        "score_check": _as_bool(stored.get("score_check")),
        "input_version": o.versions["version"] if o else job["input_version"],
        "latency_ms": {
            "build": o.build_ms if o else None,
            "solve": stored.get("solve_ms"),
            "total": total,
        },
        "egress": {"images": 0, "chunks": 0, "bytes": 0},
        **({} if o else {"reconciled": True}),  # 由恢復迴圈依生產資料庫補記
    }


def _audit(job: dict, attempt: int, run_id: str, o) -> dict:
    """排程寫回的稽核：交給寫入交易和排程一起提交（不是寫入之後另外記）。"""
    return audit.row(
        None,
        "寫入",
        "schedule_run",
        run_id,
        f"排程結果寫回（{o.used}，{o.status}，{o.score}）",
        job["request_id"],
        detail={"job_id": job["id"], "attempt": attempt},
        actor_id=job["owner_id"],
    )


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
            o, job["request_id"] or "", job_id=jid, fence=section.fence,
            audit=_audit(job, attempt, schedule_service.run_id_for_job(jid), o),
        )  # fmt: skip
        # 從這裡開始生產資料庫已經有這次排程（和它的稽核）：結案失敗不能收成 failed
        _finalize(job, control, run_id, o)


def _finalize(job: dict, control: JobControl, run_id: str, o) -> None:
    """排程提交之後的結案：依存下的那一列填結果、把工作標成成功。結案可以重做（結果只從
    存下的那一列算、稽核已在寫入交易裡），所以工作資料庫暫時不通就退讓重試；一直不行丟
    FinalizeDeferred——工作留在 running，租約過期後由恢復迴圈依生產資料庫補做（reconcile）。
    LeaseLost（工作已被恢復迴圈收回）照常往外丟：恢復迴圈會補做。"""
    jid, attempt, wid = job["id"], job["attempt"], control.worker_id
    last: Exception | None = None
    for i in range(FINALIZE_TRIES):
        try:
            stored = get_production_repo().get_run(run_id)
            if stored is None:
                raise LookupError(run_id)
            store.finish(jid, attempt, wid, "succeeded", result=_result(job, stored, o))
            return
        except store.LeaseLost:
            raise
        except Exception as e:  # noqa: BLE001 — 任何錯誤都只能延後結案，不能變成 failed
            last = e
            _log.warning("schedule job %s finalize attempt %d failed: %s", jid, i + 1, e)
            if i + 1 < FINALIZE_TRIES:
                time.sleep(0.5 * 2**i)
    raise store.FinalizeDeferred(jid) from last


def reconcile(job: dict) -> dict | None:
    """恢復迴圈用（store.recover_expired）：租約過期的排程工作，生產資料庫裡是否已經有它的排程
    （寫回後、結案前中斷）。有 → 依存下的那一列算出的工作結果，恢復迴圈據此把工作標成成功
    （稽核已在寫入交易裡，不再記）；沒有 → None，照一般規則重試或失敗。生產資料庫無法確認時
    丟例外：這一輪不動這個工作，下一輪再查。"""
    stored = get_production_repo().committed_run(schedule_service.run_id_for_job(job["id"]))
    return None if stored is None else _result(job, stored)
