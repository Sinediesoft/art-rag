"""生產排程：示範資料與圖紙一致、行事曆換算、開立工單 → 排程 → 寫回工廠資料庫、記憶體管理。

測試環境 SCHEDULER_MODE=mock（不連 Timefold 服務）；Timefold 的限制條件另有 Java 單元測試
（scheduler/src/test，make scheduler-setup 時執行）。
"""

import re
from datetime import date, datetime

import pytest
from conftest import as_account
from test_api import parse_sse

from app.rag.kb import validate_inventory, validate_parts, validate_production
from app.repositories.inventory_repo import get_inventory_repo
from app.repositories.production_repo import get_production_repo
from app.scheduling.calendar import WorkCalendar
from app.scheduling.problem import build_problem
from app.scheduling.solution import evaluate, greedy_schedule, sequences_from, simulate
from app.services import memory_guard


@pytest.fixture(autouse=True)
def clean_production(mock_env):
    get_production_repo().reset()
    yield
    get_production_repo().reset()


def test_routings_follow_drawing_process_text():
    """途程的工序號與圖紙「加工製程」段落的「工序 10、20…」一一對應。"""
    parts, _ = validate_parts(check_drawing=False)
    _, items, _ = validate_inventory({p["id"] for p in parts})
    site, routings, errors = validate_production({p["id"] for p in parts}, items)
    assert errors == [] and len(site["machines"]) == 11
    assert set(routings) == {f"mfg-00{i}" for i in range(1, 7)}
    for p in parts:
        text = next(d["text"] for d in p["descriptions"] if d["topic"] == "加工製程")
        in_text = [int(n) for n in re.findall(r"工序 (\d+)", text)]
        assert [o["op_seq"] for o in routings[p["id"]]["operations"]] == in_text, p["id"]


def test_calendar_skips_lunch_weekend_and_holiday():
    cal = WorkCalendar(
        date(2026, 10, 1),
        [("08:00", "12:00"), ("13:00", "17:00")],
        [1, 2, 3, 4, 5],
        {"2026-10-09": "補假"},
    )
    assert cal.day_minutes == 480
    assert cal.to_datetime(240) == datetime(2026, 10, 1, 13, 0)  # 午休後
    assert cal.to_datetime(240, is_end=True) == datetime(2026, 10, 1, 12, 0)
    assert cal.to_datetime(480 * 2) == datetime(2026, 10, 5, 8, 0)  # 跳過週末
    assert cal.to_datetime(480 * 6) == datetime(2026, 10, 12, 8, 0)  # 跳過 10/9 補假
    assert cal.end_of_day(date(2026, 10, 10)) == 480 * 6  # 週六交期＝週四下班
    assert cal.from_datetime(datetime(2026, 10, 1, 12, 30)) == 240


def test_problem_from_seed_work_orders():
    p = build_problem()
    jobs = {j.wo_no: j for j in p.jobs}
    assert set(jobs) == {"WO-2609-06", "WO-2609-09", "WO-2609-13", "WO-2610-01"}
    assert [s["wo_no"] for s in p.skipped] == ["WO-2609-02"]  # 委外處理中不佔機台
    # 生產中：從 line 機台的機型那道工序接著做、釘選、免換線、只排剩餘數量
    first = jobs["WO-2609-09"].ops[0]
    assert (first.op_seq, first.pinned_machine, first.setup_min) == (20, "CNC 車床-01", 0)
    assert jobs["WO-2609-09"].qty == 38
    # 委外工序變成前一道自製工序完成後的等待（調質 2 天）
    assert first.lag_after_min == 2 * 480
    # 已開立：從 start_on 開始可開工
    assert jobs["WO-2610-01"].release_min == p.calendar.start_of_day(date(2026, 10, 5))


def test_greedy_matches_simulation_and_has_no_hard_violation():
    p = build_problem()
    timed = greedy_schedule(p)
    assert len(timed) == len(p.ops)
    sim = simulate(p, sequences_from(list(timed.values())))
    assert all(
        sim[k]["start_min"] == t["start_min"] and sim[k]["end_min"] == t["end_min"]
        for k, t in timed.items()
    )
    ev = evaluate(p, timed)
    assert ev["hard"] == 0 and ev["medium"] <= 0 and ev["soft"] < 0
    # 同工單的工序依途程先後、委外等待之後才開始
    for j in p.jobs:
        for a, b in zip(j.ops, j.ops[1:], strict=False):
            assert timed[b.id]["start_min"] >= timed[a.id]["end_min"] + a.lag_after_min


def test_simulate_detects_cycles():
    """機台順序和工序先後互相矛盾時要標成循環（Timefold 的 structural score 對應這種情況）。"""
    p = build_problem()
    a, b = next(
        (x, y)
        for j in p.jobs
        for x, y in zip(j.ops, j.ops[1:], strict=False)
        if x.machine_type == y.machine_type and not x.pinned_machine
    )
    machine = next(m["machine_id"] for m in p.machines if m["machine_type"] == a.machine_type)
    timed = simulate(p, {machine: [b.id, a.id]})  # b 排在 a 前面，但 b 要等 a 完成
    assert timed[a.id].get("inconsistent") and timed[b.id].get("inconsistent")
    assert evaluate(p, timed)["hard"] < 0


def test_create_work_order_schedule_and_query_with_sql(client):
    # 1. 圖紙頁：途程與建議
    plan = client.get("/api/v1/production/parts/mfg-002").json()
    assert plan["has_routing"] and [o["op_seq"] for o in plan["routing"]] == [
        10,
        20,
        30,
        40,
        50,
        60,
    ]
    assert plan["suggestion"]["qty"] >= 1 and plan["suggestion"]["due_on"] >= plan["plan_start"]
    # 2. 開立工單：只有生管可以；急件超過額度要主管核准（和智慧助理同一套規則）
    body = {"part_id": "mfg-002", "qty": 80, "due_on": "2026-10-16", "priority": "急件"}
    as_account(client, "guest")
    r = client.post("/api/v1/production/work-orders", json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    as_account(client, "planner")
    r = client.post("/api/v1/production/work-orders", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "APPROVAL_REQUIRED"
    change = {"op": "wo_create", "params": body}
    preview = client.post("/api/v1/changes/preview", json=change).json()
    assert preview["next"] == "approval" and preview["pending_id"]
    ap = client.post(f"/api/v1/changes/{preview['pending_id']}/request-approval", json={}).json()
    as_account(client, "manager")
    decided = client.post(f"/api/v1/approvals/{ap['ap_no']}/approve", json={}).json()
    assert decided["status"] == "已核准"
    # 單號避開知識庫（含 kb_staging 的 WO-2610-03）；開立人是申請的生管
    wo = get_production_repo().get_work_order(decided["change_no"])
    assert wo["wo_no"] == "WO-2610-04" and wo["created_by"] == "planner"
    as_account(client, "planner")
    # 工廠資料庫（Text-to-SQL 查的）立刻看得到，尚未排程
    repo = get_inventory_repo()
    row = repo.run_readonly(
        "SELECT line, priority, qty_planned FROM work_orders WHERE wo_no = 'WO-2610-04'", 5, 1000
    )
    assert row.rows == [["待排程", "急件", 80]]
    overview = client.get("/api/v1/production/overview").json()
    assert "WO-2610-04" in {w["wo_no"] for w in overview["work_orders"]}
    assert overview["engine"]["engine"] == "greedy" and overview["current"] is None

    # 3. 排程（mock：簡易排程），SSE：meta → progress → solution → done
    events = parse_sse(client.post("/api/v1/schedule/solve", json={"seconds": 5}).text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta" and kinds[-1] == "done" and {"progress", "solution"} <= set(kinds)
    meta = events[0][1]
    assert meta["engine"] == "greedy" and meta["fallback_reason"]
    solution = next(d for e, d in events if e == "solution")
    assert solution["hard"] == 0 and len(solution["analysis"]) == 6
    assert {o["kind"] for o in solution["operations"]} == {"自製", "委外"}
    assert events[-1][1]["egress"] == {"images": 0, "chunks": 0, "bytes": 0}

    # 4. 結果寫回：目前排程、圖紙頁、工廠資料庫的 schedule_ops 與 v_wo_plan
    current = client.get("/api/v1/production/overview").json()["current"]
    assert current["run_id"] == solution["run_id"] and current["missing"] == []
    plan = client.get("/api/v1/production/parts/mfg-002").json()
    assert next(w for w in plan["work_orders"] if w["wo_no"] == "WO-2610-04")["plan"]["end_at"]
    ops = repo.run_readonly(
        "SELECT s.op_seq, m.machine_type FROM schedule_ops s JOIN machines m"
        " ON m.machine_id = s.machine_id WHERE s.wo_no = 'WO-2610-04' ORDER BY s.op_seq",
        20,
        1000,
    )
    assert ops.rows == [[10, "下料"], [20, "車削"], [40, "車削"], [50, "綜合加工"]]
    plan_rows = dict(repo.run_readonly("SELECT wo_no, on_time FROM v_wo_plan", 50, 1000).rows)
    assert plan_rows["WO-2610-04"] in {"準時", "延遲"} and plan_rows["WO-2609-02"] == "未排程"
    line = repo.run_readonly(
        "SELECT line FROM work_orders WHERE wo_no = 'WO-2610-04'", 5, 1000
    ).rows[0][0]
    assert line != "待排程"

    # 5. 取消：只能取消圖紙頁開立的工單
    assert client.delete("/api/v1/production/work-orders/WO-2610-04").status_code == 200
    assert client.delete("/api/v1/production/work-orders/WO-2609-06").status_code == 404
    current = client.get("/api/v1/production/overview").json()["current"]
    assert current["removed"] == ["WO-2610-04"]


def test_work_order_validation(client):
    as_account(client, "planner")
    bad_due = {"part_id": "mfg-001", "qty": 10, "due_on": "2026-09-01"}
    assert client.post("/api/v1/production/work-orders", json=bad_due).status_code == 422
    unknown = {"part_id": "nope-1", "qty": 10, "due_on": "2026-10-20"}
    assert client.post("/api/v1/production/work-orders", json=unknown).status_code == 404
    too_many = {"part_id": "mfg-001", "qty": 0, "due_on": "2026-10-20"}
    assert client.post("/api/v1/production/work-orders", json=too_many).status_code == 422


def test_health_reports_scheduler_and_memory(client):
    h = client.get("/api/v1/health").json()
    assert h["scheduler"]["engine"] == "greedy"
    assert 0 < h["memory"]["percent"] <= 100
    assert {m["key"] for m in h["memory"]["models"]} == {
        "clip",
        "bge",
        "qwen",
        "ortho2cad",
        "timefold",
    }


# ---------------------------------------------------------------- 記憶體管理
@pytest.fixture
def fake_models(monkeypatch):
    """三個假模型：都已載入，釋放時記錄下來。"""
    released: list[str] = []
    loaded = {"clip": True, "qwen": True, "ortho2cad": True}

    def make(key):
        def release():
            released.append(key)
            loaded[key] = False
            return "ok"

        return memory_guard.ManagedModel(key, key, 4000, "test", lambda: loaded[key], release)

    monkeypatch.setattr(memory_guard, "_models", lambda: [make(k) for k in loaded])
    monkeypatch.setattr(memory_guard.time, "sleep", lambda s: None)
    # 只有系統記憶體一個池（Mac 的情況）；有 NVIDIA 顯示卡的兩個池見 test_memory_gpu.py
    monkeypatch.setattr(memory_guard, "has_gpu", lambda: False)
    g = memory_guard.MemoryGuard()
    return g, released, loaded


def test_guard_releases_models_not_used_by_current_flow(fake_models, monkeypatch):
    g, released, _ = fake_models
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: 86.0)
    monkeypatch.setattr(memory_guard, "get_settings", lambda: _settings(True))
    g.enter("schedule", {"timefold"})
    event = g.check("進入「生產排程」", {"timefold"})
    assert sorted(released) == ["clip", "ortho2cad", "qwen"]
    assert event["percent_before"] == 86.0 and len(event["released"]) == 3
    g.exit("schedule", {"timefold"})


def test_guard_keeps_models_in_use_and_below_threshold(fake_models, monkeypatch):
    g, released, _ = fake_models
    monkeypatch.setattr(memory_guard, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: 60.0)
    assert g.check("背景監控", set()) is None and released == []  # 低於門檻不動
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: 90.0)
    g.enter("reconstruct", {"ortho2cad"})  # 另一個請求正在 3D 重建
    g.enter("sql", {"qwen"})  # 巢狀不改變目前流程
    assert g.current_flow == "reconstruct"
    event = g.check("進入「庫存查詢」", {"qwen"})
    assert released == ["clip"] and set(event["kept"]) == {"qwen", "ortho2cad"}


def test_guard_disabled_does_nothing(fake_models, monkeypatch):
    g, released, _ = fake_models
    monkeypatch.setattr(memory_guard, "get_settings", lambda: _settings(False))
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: 99.0)
    assert g.check("背景監控", set()) is None and released == []
    assert g.check("手動釋放", set(), force=True) is not None and len(released) == 3


def _settings(enabled: bool):
    class S:
        memory_guard = enabled
        memory_high_pct = 80.0

    return S()


def test_guard_anticipates_models_about_to_load(fake_models, monkeypatch):
    """78% 時進入庫存查詢：Qwen3-VL 還沒載入，預估載入後超過 80%，先釋放其他模型。"""
    g, released, loaded = fake_models
    loaded["qwen"] = False
    monkeypatch.setattr(memory_guard, "get_settings", lambda: _settings(True))
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: 78.0)
    assert g.check("背景監控", {"qwen"}) is None  # 只看目前使用率：還沒超過
    event = g.check("進入「庫存查詢」", {"qwen"}, anticipate=True)
    assert sorted(released) == ["clip", "ortho2cad"] and "預估載入" in event["trigger"]
