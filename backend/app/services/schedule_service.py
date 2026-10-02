"""生產排程流程：圖紙頁開立工單 → 排程頁送 Timefold 求解（SSE 串流進度）→ 結果寫回資料庫。

整合三個部分（docs/adr/005）：
- 圖紙：每張圖紙一份製程途程（kb/production/routings，由「加工製程」段落整理）
- 資料庫：工單（既有＋圖紙頁開立）與排程結果；Text-to-SQL 查得到（schedule_ops、v_wo_plan）
- 排程：Timefold Solver 排程服務（scheduler/，make scheduler）；
  連不上時改用簡易排程（交期優先派工），
  並在畫面標示「未最佳化」。排程不用任何 AI 模型，進入流程時記憶體超過門檻會先釋放其他模型。

SSE 事件見 shared/sse_events.md：meta → progress（多次）→ solution → done／error。
"""

import asyncio
import contextlib
import math
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.core.errors import AppError
from app.core.logging import log
from app.repositories.inventory_repo import get_inventory_repo
from app.repositories.logs_repo import get_logs_repo
from app.repositories.production_repo import get_production_repo
from app.scheduling import timefold_client
from app.scheduling.problem import Problem, build_problem, job_dict, load_setup
from app.scheduling.solution import decorate, evaluate, greedy_schedule
from app.services import memory_guard
from app.services.chat_service import NO_EGRESS, sse

ENGINE_LABEL = {"timefold": "Timefold Solver", "greedy": "簡易排程（交期優先派工）"}
PHASE_LABEL = {"建構解": "建構初始解", "局部搜尋": "局部搜尋最佳化"}

_active: dict = {"job_id": None, "request_id": None}
_busy = asyncio.Lock()


def _cfg(key: str, default: float) -> float:
    return float(get_models_config().scheduling.get(key, default))


# ---------------------------------------------------------------- 工單
def _taken_wo_numbers() -> set[str]:
    """知識庫（含 kb_staging，make demo-add 會加入）已用掉的工單號。"""
    import json

    taken = set()
    for root in (REPO_ROOT / "kb", REPO_ROOT / "kb_staging"):
        for path in (root / "inventory" / "items").glob("*.json"):
            taken |= {
                w["wo_no"] for w in json.loads(path.read_text(encoding="utf-8"))["work_orders"]
            }
    return taken


def create_work_order(
    part_id: str,
    qty: int,
    due_on: str,
    priority: str = "一般",
    release_on: str | None = None,
    note: str | None = None,
    created_by: str | None = None,
) -> dict:
    site, routings, parts, _, cal = load_setup()
    if part_id not in parts:
        raise AppError("PART_NOT_FOUND", f"找不到圖紙 {part_id}", 404)
    if part_id not in routings:
        raise AppError(
            "ROUTING_NOT_FOUND",
            f"〈{parts[part_id]['name']['zh']}〉還沒有製程途程（kb/production/routings/{part_id}.json），無法排程",
            422,
        )
    start = str(cal.start)
    release_on = max(release_on or start, start)
    if due_on < start:
        raise AppError("VALIDATION_ERROR", f"交期不能早於排程起始日 {start}", 422)
    if release_on > due_on:
        raise AppError("VALIDATION_ERROR", "可開工日不能晚於交期", 422)
    repo = get_production_repo()
    yymm = cal.start.strftime("%y%m")
    row = repo.create_work_order(
        {
            "wo_no": repo.next_wo_no(yymm, _taken_wo_numbers()),
            "part_id": part_id,
            "qty": qty,
            "priority": priority,
            "release_on": release_on,
            "due_on": due_on,
            "note": note,
            "created_by": created_by,
        }
    )
    get_inventory_repo().ensure_built(force=True)
    log.info(
        "work_order", extra={"fields": {"wo_no": row["wo_no"], "part_id": part_id, "qty": qty}}
    )
    return {**row, "part_name": parts[part_id]["name"]["zh"], "source": "系統開立"}


def next_wo_no() -> tuple[str, str]:
    """下一個工單號（智慧助理的確認卡先顯示預定單號，實際寫入時再配一次）與排程起始日。"""
    *_, cal = load_setup()
    return get_production_repo().next_wo_no(cal.start.strftime("%y%m"), _taken_wo_numbers()), str(
        cal.start
    )


def cancel_work_order(wo_no: str) -> None:
    if not get_production_repo().cancel_work_order(wo_no):
        raise AppError(
            "WORK_ORDER_NOT_FOUND",
            f"找不到可取消的工單 {wo_no}（只能取消圖紙頁開立、尚未取消的工單）",
            404,
        )
    get_inventory_repo().ensure_built(force=True)


def _suggest(inv: dict | None, lead_days: int | None, plan_start: date) -> dict:
    """依庫存建議工單數量與交期：可用＋生產中扣掉未出貨需求與安全庫存，不足就補到批量。"""
    default_due = str(plan_start + timedelta(days=lead_days or 14))
    if not inv:
        return {
            "qty": 50,
            "due_on": default_due,
            "priority": "一般",
            "reason": "這張圖紙沒有庫存資料，以下為預設值",
        }
    supply = inv["available"] + inv["in_production"]
    need = inv["open_demand"] + (inv["safety_stock"] or 0) - supply
    reorder = inv["reorder_qty"] or 50
    cum, trigger = 0, None
    for o in sorted(inv["sales_orders"], key=lambda o: o["due_on"]):
        cum += o["qty"] - o["qty_shipped"]
        if cum > inv["available"] and trigger is None:
            trigger = o
    base = (
        f"可用 {inv['available']}、生產中 {inv['in_production']}、未出貨需求 {inv['open_demand']}、"
        f"安全庫存 {inv['safety_stock']}"
    )
    if need <= 0:
        return {
            "qty": reorder,
            "due_on": default_due,
            "priority": "一般",
            "reason": f"{base}：數量足夠，以下為一般補貨批量（前置 {lead_days} 天）",
        }
    qty = max(reorder, math.ceil(need / reorder) * reorder)
    due = trigger["due_on"] if trigger else default_due
    rush = trigger is not None and (date.fromisoformat(due) - plan_start).days < (lead_days or 14)
    who = (
        f"；{trigger['so_no']}（{trigger['customer']}）交期 {trigger['due_on']} 會缺貨"
        if trigger
        else ""
    )
    return {
        "qty": qty,
        "due_on": max(due, str(plan_start)),
        "priority": "急件" if rush else "一般",
        "reason": f"{base}，還差 {need} 件{who}",
    }


async def part_plan(part_id: str) -> dict:
    """圖紙頁的「生產工單」卡：途程、建議數量與交期、這張圖紙的工單與目前排程結果。"""
    site, routings, parts, _, cal = await asyncio.to_thread(load_setup)
    if part_id not in parts:
        raise AppError("PART_NOT_FOUND", f"找不到圖紙 {part_id}", 404)
    inv = await asyncio.to_thread(get_inventory_repo().part_inventory, part_id)
    problem = await asyncio.to_thread(build_problem)
    run = get_production_repo().latest_run()
    planned = {w["wo_no"]: w for w in (run or {}).get("work_orders", [])}
    machines_by_type: dict[str, list[str]] = {}
    for m in site["machines"]:
        machines_by_type.setdefault(m["machine_type"], []).append(m["machine_id"])
    routing = routings.get(part_id)
    return {
        "part_id": part_id,
        "part_name": parts[part_id]["name"]["zh"],
        "plan_start": str(cal.start),
        "has_routing": routing is not None,
        "routing": [
            {**op, "machines": machines_by_type.get(op.get("machine_type", ""), [])}
            for op in (routing or {}).get("operations", [])
        ],
        "suggestion": _suggest(inv, (inv or {}).get("lead_time_days"), cal.start),
        "work_orders": [
            {**job_dict(j), "plan": planned.get(j.wo_no)}
            for j in problem.jobs
            if j.part_id == part_id
        ],
        "skipped": [s for s in problem.skipped if s["part_id"] == part_id],
        "schedule_run_id": run["run_id"] if run else None,
    }


# ---------------------------------------------------------------- 總覽
def _run_detail(run: dict, problem: Problem) -> dict:
    ops = get_production_repo().run_ops(run["run_id"])
    planned = {w["wo_no"] for w in run["work_orders"]}
    current = {j.wo_no for j in problem.jobs}
    end = max([o["end_min"] for o in ops] + [w["due_min"] for w in run["work_orders"]], default=0)
    return {
        **{k: run[k] for k in run if k not in ("work_orders", "kpis", "analysis")},
        "engine_label": ENGINE_LABEL.get(run["engine"], run["engine"]),
        "kpis": run["kpis"],
        "analysis": run["analysis"],
        "work_orders": run["work_orders"],
        "operations": ops,
        "axis": problem.calendar.axis(end + problem.calendar.day_minutes),
        "missing": sorted(current - planned),
        "removed": sorted(planned - current),
    }


async def engine_status() -> dict:
    s = get_settings()
    if s.scheduler_mode == "mock":
        return {
            "available": False,
            "engine": "greedy",
            "detail": "SCHEDULER_MODE=mock：使用簡易排程",
        }
    info = await timefold_client.health()
    if info is None:
        return {
            "available": False,
            "engine": "greedy",
            "detail": f"{s.scheduler_base_url} 連不上（請執行 make scheduler）；改用簡易排程",
        }
    return {
        "available": True,
        "engine": "timefold",
        "version": info.get("version"),
        "java": info.get("java"),
        "memory": info.get("memory"),
        "active_jobs": info.get("activeJobs"),
        "detail": f"{s.scheduler_base_url}（Timefold Solver {info.get('version')}，"
        f"Java {info.get('java')}）",
    }


async def overview() -> dict:
    site, _, _, _, cal = await asyncio.to_thread(load_setup)
    problem = await asyncio.to_thread(build_problem)
    run = get_production_repo().latest_run()
    return {
        "plan_start": str(cal.start),
        "calendar": {
            "shifts": site["calendar"]["shifts"],
            "workdays": site["calendar"]["workdays"],
            "holidays": site["calendar"].get("holidays", []),
            "day_minutes": cal.day_minutes,
        },
        "priority_weights": site["priority_weights"],
        "machine_types": site["machine_types"],
        "machines": site["machines"],
        "work_orders": [job_dict(j) for j in problem.jobs],
        "skipped": problem.skipped,
        "problem": problem.summary(),
        "engine": await engine_status(),
        "current": _run_detail(run, problem) if run else None,
        "runs": get_production_repo().recent_runs(8),
        "solving": _active["job_id"] is not None or _busy.locked(),
        "settings": {
            "solve_seconds": int(_cfg("solve_seconds", 20)),
            "max_seconds": int(_cfg("max_seconds", 60)),
        },
    }


# ---------------------------------------------------------------- 求解
def _timed_from(assignments: list[dict]) -> dict[str, dict]:
    return {
        a["operationId"]: {
            "op_id": a["operationId"],
            "machine_id": a["machineId"],
            "start_min": a["startMin"],
            "end_min": a["endMin"],
            "setup_min": a["setupMin"],
        }
        for a in assignments
        if a.get("startMin") is not None and a.get("endMin") is not None
    }


def _score_fields(st: dict) -> dict:
    return {k: st.get(k) for k in ("score", "hard", "medium", "soft", "structural", "feasible")}


async def solve_stream(
    request_id: str, seconds: int | None = None, engine: str = "timefold"
) -> AsyncIterator[str]:
    if _busy.locked():
        yield sse(
            "error",
            {
                "code": "SCHEDULE_BUSY",
                "request_id": request_id,
                "message": "已有排程正在計算，請等它完成或按「提前結束」",
            },
        )
        return
    async with _busy, memory_guard.flow("schedule", {"timefold"}):
        async for event in _solve(request_id, seconds, engine):
            yield event


async def _solve(request_id: str, seconds: int | None, engine: str) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    s = get_settings()
    seconds = int(min(max(seconds or _cfg("solve_seconds", 20), 3), _cfg("max_seconds", 60)))

    def err(code: str, message: str) -> str:
        return sse("error", {"code": code, "request_id": request_id, "message": message})

    try:
        problem = await asyncio.to_thread(build_problem)
    except AppError as e:
        yield err(e.code, e.message)
        return
    if not problem.ops:
        yield err("NO_WORK_ORDERS", "沒有需要排程的工單：請先在圖紙頁開立工單")
        return
    build_ms = round((time.perf_counter() - t0) * 1000)

    use_tf = engine == "timefold" and s.scheduler_mode == "real"
    info = await timefold_client.health() if use_tf else None
    fallback_reason = None
    if engine == "timefold" and info is None:
        use_tf = False
        fallback_reason = (
            "SCHEDULER_MODE=mock"
            if s.scheduler_mode == "mock"
            else "Timefold 排程服務未啟動（make scheduler），改用簡易排程："
            "只做一次交期優先派工，未最佳化"
        )
    used = "timefold" if use_tf else "greedy"
    yield sse(
        "meta",
        {
            "request_id": request_id,
            "engine": used,
            "engine_label": ENGINE_LABEL[used],
            "engine_version": info.get("version") if info else None,
            "fallback_reason": fallback_reason,
            "seconds": seconds if use_tf else 0,
            "unimproved_seconds": int(_cfg("unimproved_seconds", 8)) if use_tf else 0,
            "problem": problem.summary(),
            "work_orders": [job_dict(j) for j in problem.jobs],
            "machines": problem.machines,
            "axis": problem.calendar.axis(problem.calendar.day_minutes * 30),
        },
    )

    initial_score, improvements, status = None, 0, "done"
    t_solve = time.perf_counter()
    if use_tf:
        try:
            job_id = await timefold_client.submit(
                problem.to_solver_json(request_id, seconds, int(_cfg("unimproved_seconds", 8)))
            )
        except timefold_client.SchedulerUnavailable as e:
            yield err("SCHEDULER_UNAVAILABLE", f"排程服務無法使用：{e}")
            return
        _active.update(job_id=job_id, request_id=request_id)
        last_improvements, last_emit, last_tick, st = -1, 0.0, 0.0, {}
        try:
            while True:
                st = await timefold_client.status(job_id, solution=True)
                finished = st["status"] != "SOLVING"
                now = time.perf_counter()
                if st["improvements"] != last_improvements and (now - last_emit > 0.45 or finished):
                    last_improvements, last_emit = st["improvements"], now
                    timed = _timed_from(st.get("assignments", []))
                    if len(timed) == len(problem.ops):
                        d = decorate(problem, timed)
                        yield sse(
                            "progress",
                            {
                                "elapsed_ms": st["elapsedMs"],
                                "phase": PHASE_LABEL.get(st["phase"], st["phase"]),
                                **_score_fields(st),
                                "initial_score": st.get("initialScore"),
                                "improvements": st["improvements"],
                                "operations": d["operations"],
                                "kpis": d["kpis"],
                            },
                        )
                elif now - last_tick > 0.9:  # 沒有更好的解時每秒送一次心跳，前端計時才不會停住
                    last_tick = now
                    yield sse(
                        "tick",
                        {
                            "elapsed_ms": st["elapsedMs"],
                            "phase": PHASE_LABEL.get(st["phase"], st["phase"]),
                            "improvements": st["improvements"],
                        },
                    )
                if finished:
                    break
                await asyncio.sleep(_cfg("poll_ms", 400) / 1000)
        except timefold_client.SchedulerUnavailable as e:
            yield err("SCHEDULER_UNAVAILABLE", f"排程服務中斷：{e}")
            return
        finally:
            if st.get("status") == "SOLVING":  # 使用者離開頁面：停止求解，不留背景工作
                with contextlib.suppress(timefold_client.SchedulerUnavailable):
                    await timefold_client.stop(job_id)
            _active.update(job_id=None, request_id=None)
        if st["status"] == "FAILED":
            yield err("SCHEDULE_FAILED", f"Timefold 求解失敗：{st.get('error')}")
            return
        timed = _timed_from(st["assignments"])
        initial_score, improvements = st.get("initialScore"), st["improvements"]
        status = "stopped" if st["status"] == "STOPPED" else "done"
        engine_score = st["score"]
    else:
        timed = await asyncio.to_thread(greedy_schedule, problem)
        engine_score = None
    solve_ms = round((time.perf_counter() - t_solve) * 1000)

    # 各限制條件的明細由後端用同一套規則計算（Timefold 2.x 的 Score analysis 屬商業版），
    # 並核對總分與 Timefold 一致；不一致代表兩邊規則不同步，記錄下來
    ev = evaluate(problem, timed)
    score_check = None if engine_score is None else engine_score == ev["score"]
    if score_check is False:
        log.warning(f"排程分數不一致：Timefold {engine_score}，後端重算 {ev['score']}")
    score = engine_score or ev["score"]
    analysis = ev["analysis"]
    d = decorate(problem, timed)
    if not use_tf:
        yield sse(
            "progress",
            {
                "elapsed_ms": solve_ms,
                "phase": "交期優先派工",
                "score": score,
                "hard": ev["hard"],
                "medium": ev["medium"],
                "soft": ev["soft"],
                "structural": 0,
                "feasible": ev["hard"] >= 0,
                "initial_score": None,
                "improvements": 1,
                "operations": d["operations"],
                "kpis": d["kpis"],
            },
        )

    run = {
        "request_id": request_id,
        "engine": used,
        "engine_version": info.get("version") if info else None,
        "status": status,
        "seconds_limit": seconds if use_tf else 0,
        "solve_ms": solve_ms,
        "score": score,
        "hard": ev["hard"],
        "medium": ev["medium"],
        "soft": ev["soft"],
        "initial_score": initial_score,
        "improvements": improvements,
        "n_work_orders": len(problem.jobs),
        "n_operations": len(problem.ops),
        "kpis": d["kpis"],
        "analysis": analysis,
        "work_orders": d["work_orders"],
        "note": fallback_reason,
        "score_check": score_check,
    }
    run_id = await asyncio.to_thread(get_production_repo().save_run, run, d["operations"])
    await asyncio.to_thread(get_inventory_repo().ensure_built, True)
    yield sse(
        "solution",
        {
            "run_id": run_id,
            "engine": used,
            "status": status,
            "score": score,
            "hard": ev["hard"],
            "medium": ev["medium"],
            "soft": ev["soft"],
            "initial_score": initial_score,
            "score_check": score_check,
            "analysis": analysis,
            "operations": d["operations"],
            "work_orders": d["work_orders"],
            "kpis": d["kpis"],
            "axis": d["axis"],
        },
    )
    total_ms = round((time.perf_counter() - t0) * 1000)
    yield sse(
        "done",
        {
            "request_id": request_id,
            "run_id": run_id,
            "engine": used,
            "status": status,
            "score": score,
            "initial_score": initial_score,
            "improvements": improvements,
            "latency_ms": {"build": build_ms, "solve": solve_ms, "total": total_ms},
            "egress": NO_EGRESS,
            "memory": memory_guard.last_event_since(started_at),
        },
    )
    log.info(
        "schedule",
        extra={
            "fields": {
                "request_id": request_id,
                "run_id": run_id,
                "engine": used,
                "status": status,
                "score": score,
                "initial_score": initial_score,
                "n_ops": len(problem.ops),
                "total_ms": total_ms,
            }
        },
    )


async def stop() -> bool:
    job_id = _active.get("job_id")
    if not job_id:
        return False
    try:
        await timefold_client.stop(job_id)
    except timefold_client.SchedulerUnavailable:
        return False
    return True


def run_detail(run_id: str) -> dict:
    run = get_production_repo().get_run(run_id)
    if not run:
        raise AppError("SCHEDULE_RUN_NOT_FOUND", f"找不到排程結果 {run_id}", 404)
    return _run_detail(run, build_problem())


def reset() -> None:
    """展示還原：清掉圖紙頁開立的工單、所有排程結果與五段防護的攔截紀錄。"""
    get_production_repo().reset()
    get_inventory_repo().ensure_built(force=True)
    get_logs_repo().clear_security()
