"""排程問題：工單 × 製程途程 × 機台 → 一組待排的工序（Timefold 的規劃實體）。

資料來源（整合系統的三個部分）：
- 圖紙：kb/parts（品名）＋kb/production/routings（由「加工製程」段落整理的工序、機型與工時）
- 資料庫：kb/inventory 的既有工單（已開立／生產中）＋production.sqlite3 使用者在圖紙頁開立的工單
- 機台與行事曆：kb/production/site.json

規則：
- 生產中的工單：目前在 line 機台上做途程中第一道該機型的工序，之前的工序已完成；
  這道工序釘選（pinned）在該機台最前面、不必換線，只排剩餘數量
- 委外處理中的工單不佔機台，不排（列在 skipped）
- 委外工序（熱處理、表面處理）不是規劃實體，換成前一道自製工序完成後的等待時間（lag）
- 時間一律是工作分鐘（見 calendar.py）；交期＝交期當天下班
"""

import math
from dataclasses import asdict, dataclass, field
from datetime import date

from app.core.errors import AppError
from app.rag.kb import validate_inventory, validate_parts, validate_production
from app.repositories.production_repo import get_production_repo
from app.scheduling.calendar import WorkCalendar


@dataclass
class Outsource:
    op_seq: int
    name: str
    minutes: int


@dataclass
class Op:
    id: str
    wo_no: str
    part_id: str
    op_seq: int
    name: str
    machine_type: str
    setup_min: int
    run_min: int
    setup_family: str
    lag_after: list[Outsource] = field(default_factory=list)
    pinned_machine: str | None = None

    @property
    def lag_after_min(self) -> int:
        return sum(o.minutes for o in self.lag_after)


@dataclass
class Job:
    wo_no: str
    part_id: str
    part_no: str
    part_name: str
    qty: int
    priority: str
    weight: int
    status: str
    source: str  # 既有工單（kb/inventory）／系統開立（圖紙頁）
    release_on: str
    due_on: str
    release_min: int
    due_min: int
    note: str | None
    ops: list[Op]


@dataclass
class Problem:
    calendar: WorkCalendar
    machines: list[dict]
    machine_types: list[dict]
    jobs: list[Job]
    skipped: list[dict]
    weights: dict[str, int]

    @property
    def ops(self) -> list[Op]:
        return [o for j in self.jobs for o in j.ops]

    def job(self, wo_no: str) -> Job:
        return next(j for j in self.jobs if j.wo_no == wo_no)

    def to_solver_json(self, problem_id: str, seconds: int, unimproved_seconds: int) -> dict:
        """送給 Timefold 排程服務的問題（只有工作分鐘數，行事曆留在後端）。"""
        return {
            "problemId": problem_id,
            "spentLimitSeconds": seconds,
            "unimprovedSpentLimitSeconds": unimproved_seconds,
            "machines": [{"id": m["machine_id"], "type": m["machine_type"]} for m in self.machines],
            "jobs": [
                {
                    "id": j.wo_no,
                    "releaseMin": j.release_min,
                    "dueMin": j.due_min,
                    "weight": j.weight,
                }
                for j in self.jobs
            ],
            "operations": [
                {
                    "id": o.id,
                    "jobId": o.wo_no,
                    "seq": o.op_seq,
                    "machineType": o.machine_type,
                    "setupMin": o.setup_min,
                    "runMin": o.run_min,
                    "lagAfterMin": o.lag_after_min,
                    "setupFamily": o.setup_family,
                    "pinnedMachineId": o.pinned_machine,
                }
                for o in self.ops
            ],
        }

    def summary(self) -> dict:
        return {
            "plan_start": str(self.calendar.start),
            "day_minutes": self.calendar.day_minutes,
            "n_work_orders": len(self.jobs),
            "n_operations": len(self.ops),
            "n_machines": len(self.machines),
            "n_pinned": sum(1 for o in self.ops if o.pinned_machine),
            "total_work_min": sum(o.setup_min + o.run_min for o in self.ops),
            "skipped": self.skipped,
        }


def load_setup() -> tuple[dict, dict[str, dict], dict[str, dict], list[dict], WorkCalendar]:
    """讀取並驗證知識庫：(site, routings, parts, 庫存項目, 行事曆)。資料有誤時回 503。"""
    parts, part_errors = validate_parts(check_drawing=False)
    _, items, inv_errors = validate_inventory({p["id"] for p in parts})
    site, routings, errors = validate_production({p["id"] for p in parts}, items)
    if errors or site is None:
        raise AppError(
            "SCHEDULE_DATA_INVALID",
            "生產排程資料有誤：" + "；".join((errors or ["找不到設定"])[:3]),
            503,
        )
    cal = site["calendar"]
    calendar = WorkCalendar(
        date.fromisoformat(site["plan_start"]),
        cal["shifts"],
        cal["workdays"],
        {h["date"]: h["name"] for h in cal.get("holidays", [])},
    )
    return site, routings, {p["id"]: p for p in parts}, items, calendar


def _job_ops(
    wo_no: str,
    routing: dict,
    qty: int,
    calendar: WorkCalendar,
    in_progress_machine: dict | None = None,
) -> list[Op]:
    """依途程展開工序；in_progress_machine 給生產中的工單（從該機型的第一道工序開始、釘選）。"""
    steps = routing["operations"]
    start = 0
    if in_progress_machine:
        start = next(
            (
                i
                for i, s in enumerate(steps)
                if s["kind"] == "自製" and s["machine_type"] == in_progress_machine["machine_type"]
            ),
            0,
        )
    ops: list[Op] = []
    for i, s in enumerate(steps[start:]):
        if s["kind"] == "委外":
            if ops:
                minutes = s["outsource_days"] * calendar.day_minutes
                ops[-1].lag_after.append(Outsource(s["op_seq"], s["name"], minutes))
            continue
        pinned = in_progress_machine is not None and i == 0
        ops.append(
            Op(
                id=f"{wo_no}#{s['op_seq']}",
                wo_no=wo_no,
                part_id=routing["part_id"],
                op_seq=s["op_seq"],
                name=s["name"],
                machine_type=s["machine_type"],
                setup_min=0 if pinned else math.ceil(s["setup_min"]),
                run_min=max(1, math.ceil(s["run_min_per_pc"] * qty)),
                setup_family=f"{routing['part_id']}#{s['op_seq']}",
                pinned_machine=in_progress_machine["machine_id"] if pinned else None,
            )
        )
    return ops


def build_problem() -> Problem:
    site, routings, parts, items, calendar = load_setup()
    machines = {m["machine_id"]: m for m in site["machines"]}
    weights = site["priority_weights"]
    jobs: list[Job] = []
    skipped: list[dict] = []

    def make_job(w: dict, source: str, qty: int, release_on: str, priority: str) -> None:
        pid = w["part_id"]
        part = parts.get(pid)
        if pid not in routings or part is None:
            skipped.append({"wo_no": w["wo_no"], "part_id": pid, "reason": "這張圖紙沒有製程途程"})
            return
        in_progress = machines.get(w.get("line", "")) if w["status"] == "生產中" else None
        ops = _job_ops(w["wo_no"], routings[pid], qty, calendar, in_progress)
        release = 0 if in_progress else calendar.start_of_day(date.fromisoformat(release_on))
        jobs.append(
            Job(
                wo_no=w["wo_no"],
                part_id=pid,
                part_no=part["part_no"],
                part_name=part["name"]["zh"],
                qty=qty,
                priority=priority,
                weight=int(weights.get(priority, 1)),
                status=w["status"],
                source=source,
                release_on=max(release_on, str(calendar.start)),
                due_on=w["due_on"],
                release_min=release,
                due_min=calendar.end_of_day(date.fromisoformat(w["due_on"])),
                note=w.get("note"),
                ops=ops,
            )
        )

    for it in items:
        for w in it["work_orders"]:
            w = {**w, "part_id": it["part_id"]}
            if w["status"] == "已完工":
                continue
            if w["status"] == "委外處理中":
                skipped.append(
                    {
                        "wo_no": w["wo_no"],
                        "part_id": it["part_id"],
                        "reason": w.get("note") or "委外處理中，不佔機台",
                    }
                )
                continue
            make_job(w, "既有工單", w["qty_planned"] - w["qty_done"], w["start_on"], "一般")
    for w in get_production_repo().work_orders():
        make_job({**w, "status": "已開立"}, "系統開立", w["qty"], w["release_on"], w["priority"])

    # 同一台機台有兩張生產中的工單時只釘選第一張，其餘照常排
    seen: set[str] = set()
    for o in (o for j in jobs for o in j.ops if o.pinned_machine):
        if o.pinned_machine in seen:
            o.pinned_machine = None
        else:
            seen.add(o.pinned_machine)
    jobs.sort(key=lambda j: (j.due_min, j.wo_no))
    return Problem(
        calendar=calendar,
        machines=site["machines"],
        machine_types=site["machine_types"],
        jobs=jobs,
        skipped=skipped,
        weights=weights,
    )


def job_dict(j: Job) -> dict:
    d = asdict(j)
    d.pop("ops")
    d["n_ops"] = len(j.ops)
    d["work_min"] = sum(o.setup_min + o.run_min for o in j.ops)
    return d
