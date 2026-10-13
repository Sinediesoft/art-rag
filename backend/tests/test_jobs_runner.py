"""背景工作的執行（worker 這一側：行程內呼叫 runner.execute，真的 PostgreSQL，Timefold 用假的）。

涵蓋 AC-08～AC-16 的邏輯面：提前停止採用最佳解並寫回、取消不寫回、舊資料版本不覆蓋新工單、
分數與硬限制驗證、撤權（領取後、執行前、寫回前）、租約遺失放棄、3D 重建的原子發布與產物授權。
多行程、broker 中斷、kill -9 等基礎設施面的情境在 test_jobs_integration.py。
"""

import asyncio
import copy
import shutil
import threading
import time

import pytest
from conftest import as_account

from app.cad.sandbox import RunResult
from app.core import config
from app.core.config import get_settings
from app.jobs import runner, store
from app.repositories.production_repo import get_production_repo
from app.services import cad_service, identity, schedule_service, security


@pytest.fixture(autouse=True)
def clean(jobs, client):
    get_production_repo().reset()
    root = get_settings().cad_jobs_dir
    for d in root.glob("cad_*") if root.is_dir() else []:
        shutil.rmtree(d, ignore_errors=True)
    yield jobs
    get_production_repo().reset()
    as_account(client, "manager")


def schedule_job(owner="planner", seconds=5, engine="timefold"):
    versions = schedule_service.current_input_versions()
    job, _ = store.create_job(
        kind="schedule",
        owner_id=owner,
        params={"seconds": seconds, "engine": engine},
        resource={},
        scope="schedule:plant",
        request_id="req_sched",
        input_version=versions["version"],
        input_versions=versions,
    )
    return job["id"]


def cad_job(owner="planner", part="mfg-002", image=None, strategy="ortho2cad"):
    job, _ = store.create_job(
        kind="cad",
        owner_id=owner,
        params={"strategy": strategy},
        resource={"part_id": part, "image_id": image},
        scope=f"cad:{part or image}",
        request_id="req_cad_" + __import__("uuid").uuid4().hex[:8],
    )
    return job["id"]


def after_event(job_id, kind, action, timeout=30):
    """背景執行緒：等工作出現某種事件之後執行 action（模擬求解中途按停止、取消、改資料）。"""
    done = threading.Event()

    def go():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(e["type"] == kind for e in store.events_after(job_id, 0)):
                action()
                break
            time.sleep(0.05)
        done.set()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, done


def event_types(job_id):
    return [e["type"] for e in store.events_after(job_id, 0, 500)]


def revoke(monkeypatch, *, role=None, op=None, domain_of=None, domain=None):
    cfg = copy.deepcopy(config.get_access_config())
    if op:
        cfg["roles"][role]["ops"].remove(op)
    if domain:
        cfg["clearance"][domain_of]["domains"].remove(domain)
    monkeypatch.setattr(identity, "get_access_config", lambda: cfg)


def runs():
    return get_production_repo().recent_runs(20)


# ================================================================= 排程
def test_schedule_job_greedy_runs_validates_and_writes_back(clean):
    jid = schedule_job(engine="greedy")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "succeeded" and job["attempt"] == 1
    res = job["result"]
    run = get_production_repo().get_run(res["run_id"])
    assert run is not None and run["engine"] == "greedy" and res["run_status"] == "done"
    assert res["hard"] == 0 and res["input_version"] == job["input_version"]
    assert get_production_repo().latest_run()["run_id"] == res["run_id"]
    assert get_production_repo().run_ops(res["run_id"])
    types = event_types(jid)
    assert types[:2] == ["created", "started"] and types[-2:] == ["committing", "succeeded"]
    assert "meta" in types and "progress" in types
    # 工廠資料庫（Text-to-SQL 查的衍生檢視）跟上了
    from app.repositories.inventory_repo import get_inventory_repo

    n = get_inventory_repo().path and __import__("sqlite3").connect(get_inventory_repo().path)
    assert n.execute("SELECT count(*) FROM schedule_ops").fetchone()[0] > 0
    n.close()
    # 租約與名額都釋放了
    assert clean.sql("SELECT count(*) AS n FROM resource_leases")[0]["n"] == 0


def test_schedule_job_with_solver_persists_progress(clean):
    clean.fake.duration = 1.5
    jid = schedule_job(seconds=5)
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    run = get_production_repo().get_run(job["result"]["run_id"])
    assert run["engine"] == "timefold" and run["score_check"] == 1
    prog = [e for e in store.events_after(jid, 0, 500) if e["type"] == "progress"]
    assert prog and prog[-1]["data"]["operations"] and prog[-1]["data"]["score"]
    assert job["progress"]["stage"] == "solving"
    assert clean.fake.submitted and not clean.fake.stopped


def test_early_stop_adopts_the_best_legal_solution_and_writes_it_back(clean):
    clean.fake.duration = 120
    jid = schedule_job(seconds=60)
    t, _ = after_event(jid, "progress", lambda: store.request_stop(jid))
    t0 = time.time()
    assert runner.execute(jid, "w1") == "done"
    assert time.time() - t0 < 30  # 沒有等滿 120 秒
    job = store.get(jid)
    assert job["status"] == "succeeded" and job["result"]["run_status"] == "stopped"
    assert clean.fake.stopped == clean.fake.submitted
    run = get_production_repo().get_run(job["result"]["run_id"])
    assert run["status"] == "stopped" and get_production_repo().latest_run()["status"] == "stopped"
    assert "stop_requested" in event_types(jid)


def test_cancel_writes_nothing_and_stops_the_solver(clean):
    clean.fake.duration = 120
    jid = schedule_job(seconds=60)
    t, _ = after_event(jid, "progress", lambda: store.cancel(jid))
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "cancelled" and job["result"] is None
    assert runs() == [] and get_production_repo().latest_run() is None  # 不寫回
    assert clean.fake.stopped == clean.fake.submitted  # 求解被停掉，不留背景工作
    assert event_types(jid)[-1] == "cancelled" and "committing" not in event_types(jid)


def test_stale_solution_never_overwrites_new_work_orders(clean):
    """求解期間有人開了新工單：舊解不能寫回（明確衝突），生產資料庫維持原樣。"""
    clean.fake.duration = 3
    jid = schedule_job(seconds=5)
    t, _ = after_event(
        jid,
        "progress",
        lambda: schedule_service.create_work_order(
            "mfg-002", 10, "2026-10-30", created_by="planner"
        ),
    )
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "SCHEDULE_INPUT_CHANGED"
    assert runs() == []
    assert [w["part_id"] for w in get_production_repo().work_orders()] == ["mfg-002"]  # 新工單還在


def test_input_changed_while_queued_fails_before_calling_the_solver(clean):
    jid = schedule_job()
    schedule_service.create_work_order("mfg-002", 10, "2026-10-30", created_by="planner")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "SCHEDULE_INPUT_CHANGED"
    assert clean.fake.submitted == [] and runs() == []


def test_unrelated_data_change_does_not_block_write_back(clean):
    """revision 變了但排程輸入沒變（例如盤點異動）：用新的 revision 重試寫回，不誤判衝突。"""
    clean.fake.duration = 2

    def unrelated():
        import sqlite3

        conn = sqlite3.connect(get_production_repo().path)
        conn.execute(
            "INSERT INTO meta VALUES ('revision', '1') ON CONFLICT(key) DO UPDATE"
            " SET value = CAST(value AS INTEGER) + 1"
        )
        conn.commit()
        conn.close()

    jid = schedule_job(seconds=5)
    t, _ = after_event(jid, "progress", unrelated)
    assert runner.execute(jid, "w1") == "done"
    assert store.get(jid)["status"] == "succeeded" and len(runs()) == 1


def test_solution_with_mismatching_score_is_not_written(clean):
    clean.fake.corrupt_score = True
    clean.fake.duration = 1
    jid = schedule_job()
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "SCHEDULE_SCORE_MISMATCH"
    assert runs() == []


def test_solution_violating_hard_constraints_is_not_written(clean, monkeypatch):
    real = schedule_service.evaluate
    monkeypatch.setattr(schedule_service, "evaluate", lambda p, t: {**real(p, t), "hard": -3})
    jid = schedule_job(engine="greedy")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "SCHEDULE_INVALID_SOLUTION"
    assert runs() == []


def test_write_back_is_idempotent_per_job(clean):
    repo = get_production_repo()
    run = {"engine": "greedy", "status": "done", "score": "0hard/0medium/0soft"}
    op = {
        "op_id": "o1", "wo_no": "W", "part_id": "p", "op_seq": 10, "op_name": "x", "kind": "自製",
        "machine_id": "m", "start_min": 0, "end_min": 5, "start_at": "a", "end_at": "b",
    }  # fmt: skip
    a = repo.save_run(run, [op], run_id="plan_fixed")
    b = repo.save_run(
        {**run, "score": "different"}, [op], run_id="plan_fixed"
    )  # 重送／舊 worker 延遲寫回
    assert a == b == "plan_fixed"
    assert len(repo.recent_runs(10)) == 1 and len(repo.run_ops("plan_fixed")) == 1
    assert repo.get_run("plan_fixed")["score"] == "0hard/0medium/0soft"  # 沒被覆蓋


def test_revoked_before_start_calls_nothing(clean, monkeypatch):
    jid = schedule_job()
    revoke(monkeypatch, role="planner", op="schedule_run")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "PERMISSION_DENIED"
    assert clean.fake.submitted == [] and runs() == []


def test_revoked_during_solve_is_not_written(clean, monkeypatch):
    clean.fake.duration = 3
    jid = schedule_job(seconds=5)
    t, _ = after_event(
        jid, "progress", lambda: revoke(monkeypatch, role="planner", op="schedule_run")
    )
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "PERMISSION_DENIED"
    assert runs() == []  # 求解完成後寫回前重驗，撤權就不寫
    assert "committing" not in event_types(jid)


def test_deleted_account_is_revoked(clean, monkeypatch):
    jid = schedule_job(owner="planner")
    cfg = copy.deepcopy(config.get_access_config())
    del cfg["accounts"]["planner"]
    monkeypatch.setattr(identity, "get_access_config", lambda: cfg)
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "PERMISSION_REVOKED"
    assert clean.fake.submitted == []


def test_lost_lease_abandons_without_writing(clean):
    """租約被別的 worker 接手（這裡直接改資料庫模擬）：舊 worker 偵測到就放棄，什麼都不寫。"""
    clean.fake.duration = 60
    jid = schedule_job(seconds=60)

    def takeover():
        clean.sql(
            "UPDATE jobs SET lease_owner = 'other', attempt = attempt + 1 WHERE id = %s", (jid,)
        )

    t, _ = after_event(jid, "progress", takeover)
    assert runner.execute(jid, "w1") == "lost"
    job = store.get(jid)
    assert job["status"] == "running" and job["lease_owner"] == "other"  # 狀態沒被舊 worker 動過
    assert runs() == [] and job["result"] is None
    assert clean.fake.stopped == clean.fake.submitted  # 求解被放棄


def test_blocked_job_waits_for_the_scope(clean):
    store.acquire_lease("schedule:plant", "legacy:req", 30)  # 舊入口（或別的 worker）占著
    jid = schedule_job(engine="greedy")
    assert runner.execute(jid, "w1") == "blocked"
    assert store.get(jid)["status"] == "queued"
    store.release_lease("schedule:plant", "legacy:req")
    assert runner.execute(jid, "w1") == "done" and store.get(jid)["status"] == "succeeded"


# ================================================================= 3D 重建
needs_limits = pytest.mark.skipif(
    not __import__("app.cad.sandbox", fromlist=["x"]).LIMITS_AVAILABLE,
    reason="這個平台限制不了子行程（Windows）",
)


@needs_limits
def test_cad_job_publishes_artifacts_atomically_and_keeps_code_out_of_events(clean, client):
    jid = cad_job(part="mfg-002")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "succeeded", job["error"]
    result = job["result"]["result"]
    assert result["ok"] and result["iou"] > 0.95 and "code" not in result  # 程式碼不在結果裡
    assert result["files"]["code.py"] == f"/api/v1/jobs/{jid}/artifacts/code.py"
    final = get_settings().cad_jobs_dir / jid
    assert {p.name for p in final.iterdir()} >= {"model.stl", "model.step", "code.py", "input.png"}
    assert not (get_settings().cad_jobs_dir / ".staging" / f"{jid}-a1").exists()
    assert "import cadquery" in (final / "code.py").read_text(encoding="utf-8")
    # 事件裡沒有任何一段模型程式碼
    for e in store.events_after(jid, 0, 500):
        assert "cadquery" not in str(e["data"]).lower()
    names = {a["name"] for a in store.artifacts(jid)}
    assert names >= {"model.stl", "code.py", "input.png"}
    # 受保護端點：授權的帳號拿得到程式碼和模型，沒權限的 404
    as_account(client, "planner")
    r = client.get(f"/api/v1/jobs/{jid}/artifacts/code.py")
    assert r.status_code == 200 and b"cadquery" in r.content
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/model.stl").status_code == 200
    # 相容讀取：舊網址與 summary
    assert client.get(f"/api/v1/cad/jobs/{jid}").json()["result"]["ok"] is True
    as_account(client, "sales_a")
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/code.py").status_code == 404
    assert client.get(f"/api/v1/cad/jobs/{jid}/model.step").status_code == 404


def test_cad_job_exec_failure_is_failed_with_safe_message_and_inspectable_artifacts(
    clean, client, monkeypatch
):
    async def boom(code, job_dir, **kw):
        (job_dir / "code.py").write_text(code, encoding="utf-8")
        return RunResult(False, 'NameError: boom\n  File "code.py", line 2\n    secret = 1', {})

    monkeypatch.setattr(cad_service, "run_cad", boom)
    jid = cad_job(part="mfg-001")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"] == {
        "code": "CAD_EXEC_FAILED",
        "message": "NameError: boom",
    }
    assert "secret" not in str(job["result"]) and "File" not in str(job["error"])
    assert {a["name"] for a in store.artifacts(jid)} == {"code.py", "input.png"}
    as_account(client, "planner")
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/code.py").status_code == 200
    as_account(client, "guest")
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/code.py").status_code == 404


def test_cad_cancel_during_generation_publishes_nothing(clean, monkeypatch):
    async def slow(code):
        for i in range(0, len(code), 4):
            await asyncio.sleep(0.2)
            yield code[i : i + 4]

    ran = []
    monkeypatch.setattr(cad_service, "_mock_stream", slow)
    real_run = cad_service.run_cad
    monkeypatch.setattr(
        cad_service, "run_cad", lambda *a, **k: (ran.append(1), real_run(*a, **k))[1]
    )
    jid = cad_job(part="mfg-002")
    t, _ = after_event(jid, "meta", lambda: store.cancel(jid))
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "cancelled" and job["result"] is None
    assert ran == []  # 沙箱沒有執行
    assert not (get_settings().cad_jobs_dir / jid).exists()
    assert not (get_settings().cad_jobs_dir / ".staging" / f"{jid}-a1").exists()
    assert store.artifacts(jid) == []


def test_cad_cancel_during_sandbox_kills_the_execution(clean, monkeypatch):
    state = {"started": False, "cancelled": False}

    async def hang(code, job_dir, **kw):
        state["started"] = True
        try:
            await asyncio.sleep(120)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    monkeypatch.setattr(cad_service, "run_cad", hang)
    jid = cad_job(part="mfg-002")
    t, _ = after_event(jid, "stage", lambda: store.cancel(jid))  # executing 階段的事件
    t0 = time.time()
    assert runner.execute(jid, "w1") == "done"
    assert time.time() - t0 < 30 and state == {"started": True, "cancelled": True}
    assert store.get(jid)["status"] == "cancelled"
    assert not (get_settings().cad_jobs_dir / jid).exists()


def test_run_cad_cancellation_kills_the_subprocess(tmp_path):
    """sandbox.run_cad 被取消時殺掉子行程，不讓模型產生的程式碼繼續在背景吃 CPU。"""
    import psutil

    from app.cad import sandbox

    code = "import time\ntime.sleep(60)\n"

    async def go():
        me = psutil.Process()
        before = {p.pid for p in me.children(recursive=True)}  # 測試環境本來就有的（redis 等）
        task = asyncio.create_task(sandbox.run_cad(code, tmp_path, trusted=True, timeout_s=60))
        deadline = time.time() + 20
        new: list[psutil.Process] = []
        while time.time() < deadline and not new:
            await asyncio.sleep(0.1)
            new = [p for p in me.children(recursive=True) if p.pid not in before]
        assert new, "子行程沒有出現"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)
        return new

    alive = [p for p in asyncio.run(go()) if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
    assert alive == []


def test_cad_revoked_before_start_reads_and_generates_nothing(clean, monkeypatch):
    called = []
    real = cad_service._reconstruct_stream
    monkeypatch.setattr(
        cad_service, "_reconstruct_stream", lambda *a, **k: (called.append(1), real(*a, **k))[1]
    )
    jid = cad_job(owner="planner", part="mfg-002")
    revoke(monkeypatch, domain_of="planner", domain="mfg")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "DATA_SCOPE_DENIED"
    assert called == []


def test_cad_revoked_before_publish_publishes_nothing(clean, monkeypatch):
    calls = []
    real = security.cad_check_create

    def check(ctx, part_id, image_id):
        calls.append(1)
        if len(calls) >= 2:  # 領取後的第一次通過；寫回前（第二次起）已被撤權
            raise security.AppError("DATA_SCOPE_DENIED", "已被撤權", 403)
        return real(ctx, part_id, image_id)

    monkeypatch.setattr(security, "cad_check_create", check)
    jid = cad_job(part="mfg-002")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "DATA_SCOPE_DENIED"
    assert not (get_settings().cad_jobs_dir / jid).exists() and store.artifacts(jid) == []
    assert "committing" not in event_types(jid)


def test_cad_lost_lease_publishes_nothing(clean, monkeypatch):
    async def slow(code):
        for i in range(0, len(code), 4):
            await asyncio.sleep(0.2)
            yield code[i : i + 4]

    monkeypatch.setattr(cad_service, "_mock_stream", slow)
    jid = cad_job(part="mfg-002")

    def takeover():
        clean.sql(
            "UPDATE jobs SET lease_owner = 'other', attempt = attempt + 1 WHERE id = %s", (jid,)
        )

    t, _ = after_event(jid, "meta", takeover)
    assert runner.execute(jid, "w1") == "lost"
    assert store.get(jid)["status"] == "running"
    assert not (get_settings().cad_jobs_dir / jid).exists()


def test_cad_job_for_someone_elses_photo_is_refused_at_run_time(clean, client):
    """建立後照片的授權被收回：worker 執行前重驗，不讀圖。"""
    as_account(client, "manager")
    import json

    from conftest import ROOT

    p = ROOT / "kb/drawings/mfg-001.png"
    image = client.post(
        "/api/v1/images", files={"file": (p.name, p.read_bytes(), "image/png")}
    ).json()["image_id"]
    client.put(f"/api/v1/images/{image}/acl", json={"accounts": ["planner"]})
    jid = cad_job(owner="planner", part=None, image=image)
    client.put(f"/api/v1/images/{image}/acl", json={"accounts": []})  # 排隊期間被收回
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "IMAGE_NOT_FOUND"
    assert json.dumps(job["error"]).count("img_") == 0
