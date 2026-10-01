"""排程結果：依各機台的工序順序推算起訖時間、計分（與 Timefold 的限制條件相同），換算成實際時間。

時間推算規則（與 scheduler/ 的 Operation 影子變數相同）：
  開始 ＝ max(工單可開工時間, 同機台前一道工序完成, 同工單前一道工序完成＋委外天數)
  換線 ＝ 同機台前一道是同一圖紙的同一道工序（或釘選中的工序）就免換線，否則加上準備時間
  完成 ＝ 開始＋換線＋加工
分數（硬／中／軟，數字越接近 0 越好）：
  硬：機型不符、排程循環（工序互相等待）
  中：交期延遲（延遲分鐘 × 急件權重）
  軟：換線準備時間＋各工單完工時間（越早完工越好）

greedy_schedule() 是沒有 Timefold 時的簡易排程：依交期先後（EDD）把工序逐一放到最早完成的機台，
只做一次建構、不做最佳化。
"""

from collections import defaultdict

from app.scheduling.problem import Op, Problem


def simulate(problem: Problem, sequences: dict[str, list[str]]) -> dict[str, dict]:
    """各機台的工序順序 → 每道工序的起訖。互相等待（循環）的工序標 inconsistent。"""
    ops = {o.id: o for o in problem.ops}
    job_of = {o.id: problem.job(o.wo_no) for o in ops.values()}
    prev_in_job: dict[str, Op | None] = {}
    for j in problem.jobs:
        for i, o in enumerate(j.ops):
            prev_in_job[o.id] = j.ops[i - 1] if i else None
    machine_of, prev_on_machine = {}, {}
    for m, seq in sequences.items():
        for i, oid in enumerate(seq):
            machine_of[oid] = m
            prev_on_machine[oid] = seq[i - 1] if i else None

    timed: dict[str, dict] = {}
    pending = [oid for oid in ops if oid in machine_of]
    progress = True
    while pending and progress:
        progress = False
        rest = []
        for oid in pending:
            o, pm, pj = ops[oid], prev_on_machine[oid], prev_in_job[oid]
            if (pm and pm not in timed) or (pj and pj.id not in timed and pj.id in machine_of):
                rest.append(oid)
                continue
            start = job_of[oid].release_min
            if pm:
                start = max(start, timed[pm]["end_min"])
            if pj and pj.id in timed:
                start = max(start, timed[pj.id]["end_min"] + pj.lag_after_min)
            same = pm is not None and ops[pm].setup_family == o.setup_family
            setup = 0 if (same or o.pinned_machine) else o.setup_min
            timed[oid] = {
                "op_id": oid,
                "machine_id": machine_of[oid],
                "start_min": start,
                "end_min": start + setup + o.run_min,
                "setup_min": setup,
            }
            progress = True
        pending = rest
    for oid in pending:
        timed[oid] = {
            "op_id": oid,
            "machine_id": machine_of[oid],
            "start_min": 0,
            "end_min": 0,
            "setup_min": 0,
            "inconsistent": True,
        }
    return timed


def sequences_from(assignments: list[dict]) -> dict[str, list[str]]:
    seqs: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for a in assignments:
        seqs[a["machine_id"]].append((a["start_min"], a["op_id"]))
    return {m: [oid for _, oid in sorted(v)] for m, v in seqs.items()}


def evaluate(problem: Problem, timed: dict[str, dict]) -> dict:
    """計分與各限制條件的明細（Timefold 不可用時，或用來核對 Timefold 的分數）。"""
    mtype = {m["machine_id"]: m["machine_type"] for m in problem.machines}
    ops = {o.id: o for o in problem.ops}
    mismatch = [
        oid for oid, t in timed.items() if mtype.get(t["machine_id"]) != ops[oid].machine_type
    ]
    loops = [oid for oid, t in timed.items() if t.get("inconsistent")]
    unassigned = [o.id for o in problem.ops if o.id not in timed]
    late, completion = [], 0
    for j in problem.jobs:
        last = timed.get(j.ops[-1].id)
        if not last or last.get("inconsistent"):
            continue
        done = last["end_min"] + j.ops[-1].lag_after_min
        completion += done
        if done > j.due_min:
            late.append((j, done - j.due_min))
    setup = sum(t["setup_min"] for t in timed.values())
    hard = -(len(mismatch) + len(loops) + len(unassigned))
    medium = -sum(j.weight * m for j, m in late)
    soft = -(setup + completion)
    analysis = [
        _row("機型不符", "hard", -len(mismatch), len(mismatch), "工序只能排在對應機型的機台"),
        _row(
            "排程循環",
            "hard",
            -len(loops),
            len(loops),
            "工序之間不能互相等待（同機台順序與工序先後矛盾）",
        ),
        _row("未排入", "hard", -len(unassigned), len(unassigned), "每道工序都要排到機台上"),
        _row(
            "交期延遲",
            "medium",
            medium,
            len(late),
            "工單在交期當天下班前完工（含委外）；延遲分鐘 × 權重（急件 "
            f"{problem.weights.get('急件', 1)} 倍）",
        ),
        _row(
            "換線準備",
            "soft",
            -setup,
            sum(1 for t in timed.values() if t["setup_min"]),
            "同一台機台連續做同一道工序可省下準備時間",
        ),
        _row(
            "完工時間",
            "soft",
            -completion,
            len(problem.jobs),
            "每張工單越早完工越好（各工單完工的工作分鐘加總）",
        ),
    ]
    return {
        "hard": hard,
        "medium": medium,
        "soft": soft,
        "score": format_score(hard, medium, soft),
        "analysis": analysis,
    }


def _row(name: str, level: str, score: int, matches: int, description: str) -> dict:
    return {
        "constraint": name,
        "level": level,
        "score": score,
        "matches": matches,
        "description": description,
    }


def format_score(hard: int, medium: int, soft: int) -> str:
    return f"{hard}hard/{medium}medium/{soft}soft"


def greedy_schedule(problem: Problem) -> dict[str, dict]:
    """簡易排程（EDD 派工）：交期早的先排，每道工序放到能最早完成的同機型機台。"""
    by_type: dict[str, list[str]] = defaultdict(list)
    for m in problem.machines:
        by_type[m["machine_type"]].append(m["machine_id"])
    free = {m["machine_id"]: 0 for m in problem.machines}
    last_family: dict[str, str | None] = {m: None for m in free}
    timed: dict[str, dict] = {}
    next_idx = {j.wo_no: 0 for j in problem.jobs}
    ready = {j.wo_no: j.release_min for j in problem.jobs}

    def place(o: Op, machine: str, start: int) -> None:
        setup = 0 if (last_family[machine] == o.setup_family or o.pinned_machine) else o.setup_min
        end = start + setup + o.run_min
        timed[o.id] = {
            "op_id": o.id,
            "machine_id": machine,
            "start_min": start,
            "end_min": end,
            "setup_min": setup,
        }
        free[machine], last_family[machine] = end, o.setup_family
        ready[o.wo_no] = end + o.lag_after_min
        next_idx[o.wo_no] += 1

    for j in problem.jobs:  # 釘選的工序（生產中）先放
        if j.ops and j.ops[0].pinned_machine:
            place(j.ops[0], j.ops[0].pinned_machine, 0)
    while True:
        cands = [j for j in problem.jobs if next_idx[j.wo_no] < len(j.ops)]
        if not cands:
            break
        j = min(cands, key=lambda j: (j.due_min, ready[j.wo_no], j.wo_no))
        o = j.ops[next_idx[j.wo_no]]

        def finish(m: str, o: Op = o) -> int:
            setup = 0 if last_family[m] == o.setup_family else o.setup_min
            return max(free[m], ready[o.wo_no]) + setup + o.run_min

        machine = min(by_type[o.machine_type], key=lambda m: (finish(m), m))
        place(o, machine, max(free[machine], ready[o.wo_no]))
    return timed


def decorate(problem: Problem, timed: dict[str, dict]) -> dict:
    """把工作分鐘換算成實際時間，並整理出甘特圖、工單完工與 KPI。"""
    cal = problem.calendar
    ops_out, jobs_out = [], []
    busy: dict[str, int] = defaultdict(int)
    for j in problem.jobs:
        done = None
        for o in j.ops:
            t = timed.get(o.id)
            if not t:
                continue
            ops_out.append(
                {
                    "op_id": o.id,
                    "wo_no": j.wo_no,
                    "part_id": j.part_id,
                    "op_seq": o.op_seq,
                    "op_name": o.name,
                    "kind": "自製",
                    "machine_id": t["machine_id"],
                    "start_min": t["start_min"],
                    "end_min": t["end_min"],
                    "setup_min": t["setup_min"],
                    "run_min": o.run_min,
                    "start_at": cal.to_datetime(t["start_min"]).strftime("%Y-%m-%d %H:%M"),
                    "end_at": cal.to_datetime(t["end_min"], is_end=True).strftime("%Y-%m-%d %H:%M"),
                    "pinned": bool(o.pinned_machine),
                }
            )
            busy[t["machine_id"]] += t["end_min"] - t["start_min"]
            cursor = t["end_min"]
            for lag in o.lag_after:  # 委外：前一道自製工序完成後的等待時間
                ops_out.append(
                    {
                        "op_id": f"{j.wo_no}#{lag.op_seq}",
                        "wo_no": j.wo_no,
                        "part_id": j.part_id,
                        "op_seq": lag.op_seq,
                        "op_name": lag.name,
                        "kind": "委外",
                        "machine_id": None,
                        "start_min": cursor,
                        "end_min": cursor + lag.minutes,
                        "setup_min": 0,
                        "run_min": lag.minutes,
                        "start_at": cal.to_datetime(cursor).strftime("%Y-%m-%d %H:%M"),
                        "end_at": cal.to_datetime(cursor + lag.minutes, is_end=True).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                        "pinned": False,
                    }
                )
                cursor += lag.minutes
            done = cursor
        first = timed.get(j.ops[0].id) if j.ops else None
        late = max(0, done - j.due_min) if done is not None else None
        jobs_out.append(
            {
                "wo_no": j.wo_no,
                "part_id": j.part_id,
                "part_name": j.part_name,
                "qty": j.qty,
                "priority": j.priority,
                "source": j.source,
                "status": j.status,
                "due_on": j.due_on,
                "due_min": j.due_min,
                "release_min": j.release_min,
                "start_min": first["start_min"] if first else None,
                "end_min": done,
                "start_at": cal.to_datetime(first["start_min"]).strftime("%Y-%m-%d %H:%M")
                if first
                else None,
                "end_at": cal.to_datetime(done, is_end=True).strftime("%Y-%m-%d %H:%M")
                if done is not None
                else None,
                "late_min": late,
                "on_time": late == 0 if late is not None else None,
            }
        )
    ends = [j["end_min"] for j in jobs_out if j["end_min"] is not None]
    makespan = max(ends, default=0)
    machine_end = max((o["end_min"] for o in ops_out if o["kind"] == "自製"), default=0)
    late_jobs = [j for j in jobs_out if j["late_min"]]
    kpis = {
        "n_work_orders": len(jobs_out),
        "n_late": len(late_jobs),
        "on_time_rate": round(1 - len(late_jobs) / len(jobs_out), 3) if jobs_out else None,
        "total_late_min": sum(j["late_min"] or 0 for j in jobs_out),
        "total_setup_min": sum(o["setup_min"] for o in ops_out),
        "n_setups": sum(1 for o in ops_out if o["setup_min"]),
        "makespan_min": makespan,
        "finish_at": cal.to_datetime(makespan, is_end=True).strftime("%Y-%m-%d %H:%M")
        if ends
        else None,
        "utilization": {
            m["machine_id"]: round(busy.get(m["machine_id"], 0) / machine_end, 3)
            if machine_end
            else 0
            for m in problem.machines
        },
    }
    return {
        "operations": sorted(ops_out, key=lambda o: (o["start_min"], o["op_id"])),
        "work_orders": jobs_out,
        "kpis": kpis,
        "axis": cal.axis(
            max([makespan, machine_end] + [j.due_min for j in problem.jobs]) + cal.day_minutes
        ),
    }
