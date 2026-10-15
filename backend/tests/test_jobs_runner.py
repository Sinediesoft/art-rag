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
from app.jobs import db, runner, store
from app.jobs import schedule_job as schedule_job_module
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


def schedule_job(owner="manager", seconds=5, engine="timefold"):
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


def cad_job(owner="manager", part="mfg-002", image=None, strategy="ortho2cad"):
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
    # 稽核：誰（工作的擁有者）寫回了哪個排程結果；只有識別碼與結果，沒有參數或模型輸出
    rows = [a for a in get_production_repo().audit(50) if a["op"] == "schedule_run"]
    assert (
        len(rows) == 1 and rows[0]["ref_no"] == res["run_id"] and rows[0]["actor_id"] == "manager"
    )
    assert rows[0]["action"] == "寫入" and rows[0]["request_id"] == job["request_id"]


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
            "mfg-002", 10, "2026-10-30", created_by="manager"
        ),
    )
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "SCHEDULE_INPUT_CHANGED"
    assert runs() == []
    assert [w["part_id"] for w in get_production_repo().work_orders()] == ["mfg-002"]  # 新工單還在


def test_input_changed_while_queued_fails_before_calling_the_solver(clean):
    jid = schedule_job()
    schedule_service.create_work_order("mfg-002", 10, "2026-10-30", created_by="manager")
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
    revoke(monkeypatch, role="manager", op="schedule_run")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "PERMISSION_DENIED"
    assert clean.fake.submitted == [] and runs() == []


def test_revoked_during_solve_is_not_written(clean, monkeypatch):
    clean.fake.duration = 3
    jid = schedule_job(seconds=5)
    t, _ = after_event(
        jid, "progress", lambda: revoke(monkeypatch, role="manager", op="schedule_run")
    )
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "PERMISSION_DENIED"
    assert runs() == []  # 求解完成後寫回前重驗，撤權就不寫
    assert "committing" not in event_types(jid)


def test_deleted_account_is_revoked(clean, monkeypatch):
    jid = schedule_job(owner="manager")
    cfg = copy.deepcopy(config.get_access_config())
    del cfg["accounts"]["manager"]
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


def test_resource_taken_over_right_before_write_back_writes_nothing(clean, monkeypatch):
    """寫回前資源租約已被別的工作接手（審查 F2）：拿不到寫回資格，放棄、什麼都不寫。"""
    jid = schedule_job(engine="greedy")
    real = schedule_service.validate_outcome

    def validate_then_lose_the_scope(o):
        real(o)
        clean.sql("UPDATE resource_leases SET holder = 'job_other' WHERE holder = %s", (jid,))

    monkeypatch.setattr(schedule_service, "validate_outcome", validate_then_lose_the_scope)
    assert runner.execute(jid, "w1") == "lost"
    job = store.get(jid)
    assert job["status"] == "running" and job["committing_attempt"] is None
    assert runs() == [] and job["result"] is None


def in_section(jid):
    """目前是否已取得寫回資格（reserve_commit 之後）：用來只在寫回區段內的授權檢查動手腳。"""
    row = store.get(jid)
    return row is not None and row["committing_attempt"] is not None


def test_schedule_resource_takeover_after_the_final_check_never_writes(clean, monkeypatch):
    """審查 F1（ad32d74）：驗證通過之後、寫入之前停頓，租約過期且舊同步入口接手了排程範圍，
    舊 worker 恢復——寫入交易裡的 fence 擋下，生產資料庫沒有排程結果。"""
    jid = schedule_job(engine="greedy")
    real = schedule_job_module._authorize

    def pause_then_lose_the_scope(job):
        out = real(job)
        if in_section(jid):  # 寫回區段內的最後一次授權檢查：模擬停頓超過租約，別人接手
            clean.sql("UPDATE resource_leases SET expires_at = now() - interval '1 second'")
            assert store.acquire_lease("schedule:plant", "legacy:review", 30) is True
        return out

    monkeypatch.setattr(schedule_job_module, "_authorize", pause_then_lose_the_scope)
    assert runner.execute(jid, "w1") == "lost"
    assert runs() == [] and store.get(jid)["status"] == "running"
    assert clean.sql("SELECT holder FROM resource_leases")[0]["holder"] == "legacy:review"


def test_schedule_lease_expiry_during_the_write_back_cannot_be_taken_over(clean, monkeypatch):
    """同樣的停頓，但是用真正的接手路徑：工作租約過期，舊同步入口想接手排程範圍——被拒絕，
    寫回照常完成（互斥涵蓋整段寫回）。"""
    jid = schedule_job(engine="greedy")
    real = schedule_job_module._authorize
    seen = []

    def pause(job):
        out = real(job)
        if in_section(jid):
            clean.sql(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (jid,),
            )
            seen.append(store.acquire_lease("schedule:plant", "legacy:review", 30))
        return out

    monkeypatch.setattr(schedule_job_module, "_authorize", pause)
    assert runner.execute(jid, "w1") == "done"
    assert seen == [False]
    assert store.get(jid)["status"] == "succeeded" and len(runs()) == 1


def kill_commit_connection(clean, section, jid):
    """模擬「最後一次 fence 通過之後，持有寫回鎖的連線被中斷」：鎖隨連線釋放、工作租約過期，
    恢復迴圈把工作收回重排（審查 F1，509bf1d）。"""
    clean.sql(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (jid,)
    )
    killed = clean.sql(
        "SELECT pg_terminate_backend(%s) AS killed", (section._conn.info.backend_pid,)
    )
    assert killed[0]["killed"]
    assert store.recover_expired() == [(jid, "requeued")]


def wait_for_jobs_db():
    """中斷連線後工作資料庫的連線池會短暫 fail-fast（db.FAIL_FAST_S）。"""
    from app.jobs import db

    for _ in range(50):
        if db.ping():
            return
        time.sleep(0.1)
    raise AssertionError("工作資料庫一直連不上")


def test_schedule_old_attempt_committing_first_is_what_the_job_reports(clean, monkeypatch):
    """排程寫回的同一個故障邊界：attempt 1 在寫入交易裡的 fence 通過後，持有寫回鎖的連線中斷、
    工作被收回；attempt 1 仍提交了它的解。之後的 attempt 2 不覆蓋（run_id 由 job_id 決定，先提交
    的為準），工作結果、排程詳情、目前排程都是同一列——不會出現「工作結果是 B、排程是 A」。"""
    jid = schedule_job(engine="greedy")
    real_write_back = schedule_service.write_back
    original_fence = store.CommitSection.fence
    fences, writes = [], []

    def write_back(o, *a, **kw):
        writes.append(1)
        if (
            len(writes) == 1
        ):  # attempt 1：讓兩次嘗試的解分得出來（實務上 Timefold 每次的解可能不同）
            o.score = "0hard/0medium/-424242soft"
        return real_write_back(o, *a, **kw)

    def fence(section):
        original_fence(section)
        fences.append(section.attempt)
        if section.attempt == 1 and fences.count(1) == 2:  # 第二次＝寫入交易裡、INSERT 之前
            kill_commit_connection(clean, section, jid)

    monkeypatch.setattr(schedule_service, "write_back", write_back)
    monkeypatch.setattr(store.CommitSection, "fence", fence)
    assert runner.execute(jid, "w1") == "lost"  # 寫入了，但結束被擋（工作已被收回）
    assert clean.job(jid)["status"] == "queued"
    wait_for_jobs_db()
    out = None
    for _ in range(20):
        out = runner.execute(jid, "w2")
        if out != "retry":
            break
        time.sleep(0.2)
    assert out == "done"
    job = clean.job(jid)
    assert job["status"] == "succeeded" and job["attempt"] == 2
    stored = get_production_repo().get_run(job["result"]["run_id"])
    assert len(runs()) == 1  # attempt 2 沒有再寫一列，也沒有覆蓋
    assert stored["score"] == "0hard/0medium/-424242soft" and len(writes) == 2
    assert job["result"]["score"] == stored["score"]  # 工作結果就是存下的那一列
    assert get_production_repo().latest_run()["run_id"] == stored["run_id"]


# ---- 排程寫入生產資料庫之後、工作結案之前的故障（架構審查 F2，0378910）
def schedule_audits():
    return [a for a in get_production_repo().audit(50) if a["op"] == "schedule_run"]


def expire(clean, jid):
    clean.sql(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (jid,)
    )


def recover():
    """恢復迴圈的一輪（和 broker.maintenance_once 一樣帶排程的補做）。"""
    return store.recover_expired(reconcile={"schedule": schedule_job_module.reconcile})


def assert_one_run_one_audit(job):
    assert job["status"] == "succeeded", job
    stored = get_production_repo().get_run(job["result"]["run_id"])
    assert len(runs()) == 1 and stored is not None
    assert job["result"]["score"] == stored["score"]  # 工作結果就是存下的那一列
    audits = schedule_audits()
    assert len(audits) == 1 and audits[0]["ref_no"] == stored["run_id"]
    assert audits[0]["actor_id"] == job["owner_id"]


def test_schedule_finish_failing_once_after_commit_is_retried_not_failed(clean, monkeypatch):
    """生產資料庫已提交後，工作資料庫的成功更新暫時失敗一次：worker 退讓重試，工作成功、
    結果與一筆稽核都在；不會變成「failed、沒有結果、重送 skipped」的永久矛盾。"""
    jid = schedule_job(engine="greedy")
    real_finish = store.finish
    failed = []

    def finish(job_id, attempt, worker_id, status, **kw):
        if status == "succeeded" and not failed:
            assert len(runs()) == 1 and len(schedule_audits()) == 1  # 排程與稽核已一起提交
            failed.append(1)
            raise db.JobsUnavailable("暫時失敗")
        return real_finish(job_id, attempt, worker_id, status, **kw)

    monkeypatch.setattr(store, "finish", finish)
    assert runner.execute(jid, "w1") == "done" and failed
    assert_one_run_one_audit(clean.job(jid))
    assert runner.execute(jid, "w2") == "skipped"
    assert len(runs()) == 1 and len(schedule_audits()) == 1


def test_schedule_finish_unavailable_after_commit_is_reconciled_by_recovery(clean, monkeypatch):
    """結案一直失敗（工作資料庫在整個重試期間都不通）：工作留在 running、不收成 failed；
    租約過期後恢復迴圈依生產資料庫補做結案——嘗試次數已用完也一樣是成功，不是 LEASE_EXPIRED。"""
    jid = schedule_job(engine="greedy")
    clean.sql("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (jid,))
    real_finish = store.finish

    def finish(job_id, attempt, worker_id, status, **kw):
        if status == "succeeded":
            raise db.JobsUnavailable("工作資料庫不通")
        return real_finish(job_id, attempt, worker_id, status, **kw)

    monkeypatch.setattr(store, "finish", finish)
    monkeypatch.setattr(schedule_job_module, "FINALIZE_TRIES", 2)
    assert runner.execute(jid, "w1") == "deferred"
    job = clean.job(jid)
    assert job["status"] == "running" and job["result"] is None and job["error"] is None
    assert len(runs()) == 1 and len(schedule_audits()) == 1
    monkeypatch.setattr(store, "finish", real_finish)
    assert recover() == []  # 租約還沒過期：不動
    expire(clean, jid)
    assert recover() == [(jid, "reconciled")]
    job = clean.job(jid)
    assert_one_run_one_audit(job)
    assert job["result"]["reconciled"] is True and job["error"] is None
    assert job["result"]["input_version"] == job["input_version"]
    assert clean.sql("SELECT count(*) AS n FROM resource_leases")[0]["n"] == 0  # 名額放掉了
    assert event_types(jid)[-1] == "succeeded"
    assert runner.execute(jid, "w2") == "skipped" and recover() == []
    assert len(runs()) == 1 and len(schedule_audits()) == 1


class WorkerCrash(BaseException):
    """模擬 worker 行程在某一點被殺掉（不是一般例外，runner 不會收尾）。"""


def test_worker_crash_after_production_commit_is_reconciled_once(clean, monkeypatch):
    """生產提交後、工作成功更新前 worker 當掉：恢復迴圈依生產資料庫補做，只有一筆排程與一筆稽核；
    再投遞、再跑恢復迴圈都不會多寫。"""
    jid = schedule_job(engine="greedy")

    def crash(*_a, **_kw):
        raise WorkerCrash

    monkeypatch.setattr(schedule_job_module, "_finalize", crash)
    with pytest.raises(WorkerCrash):
        runner.execute(jid, "w1")
    assert clean.job(jid)["status"] == "running"
    assert len(runs()) == 1 and len(schedule_audits()) == 1  # 稽核和排程同一個交易
    expire(clean, jid)
    assert recover() == [(jid, "reconciled")]
    assert_one_run_one_audit(clean.job(jid))
    assert runner.execute(jid, "w2") == "skipped" and recover() == []
    assert len(runs()) == 1 and len(schedule_audits()) == 1


def test_recovery_without_a_committed_run_keeps_the_normal_rules(clean):
    """生產資料庫裡沒有這個工作的排程（寫入前就中斷）：補做查無 → 照原規則回佇列或失敗。"""
    jid = schedule_job(engine="greedy")
    store.claim(jid, "w1")
    expire(clean, jid)
    assert recover() == [(jid, "requeued")]
    clean.sql("UPDATE jobs SET max_attempts = 2 WHERE id = %s", (jid,))
    store.claim(jid, "w1")
    expire(clean, jid)
    assert recover() == [(jid, "failed")]
    assert clean.job(jid)["error"]["code"] == "LEASE_EXPIRED" and runs() == []


def test_recovery_defers_when_the_production_db_cannot_confirm(clean, monkeypatch):
    """生產資料庫查不了（被鎖太久、檔案壞掉）：不猜，這一輪不動（不重跑、不失敗），下一輪再查。"""
    jid = schedule_job(engine="greedy")
    clean.sql("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (jid,))
    store.claim(jid, "w1")
    expire(clean, jid)
    repo = get_production_repo()
    real = repo.committed_run

    def broken(run_id):
        raise __import__("sqlite3").OperationalError("database is locked")

    monkeypatch.setattr(repo, "committed_run", broken)
    assert recover() == [(jid, "deferred")]
    assert clean.job(jid)["status"] == "running"
    monkeypatch.setattr(repo, "committed_run", real)
    assert recover() == [(jid, "failed")]  # 確認沒有寫入、嘗試已用完 → 才失敗


def test_recovery_waits_for_an_in_flight_production_write(clean, monkeypatch):
    """舊嘗試已通過寫入交易裡的 fence、持有寫回鎖的連線才中斷：恢復迴圈的補做查詢在 SQLite
    寫入鎖上等那個交易提交，看到排程就補做成功——不會先判定「沒寫入」重排或失敗、之後才被提交推翻。
    （另一個行程用另一個 ProductionRepo；同一行程內的鎖不能替代 SQLite 的寫入鎖。）"""
    from app.repositories.production_repo import ProductionRepo

    jid = schedule_job(engine="greedy")
    clean.sql("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (jid,))
    other = ProductionRepo(get_production_repo().path)  # 恢復迴圈所在的「另一個行程」
    reconciling = threading.Event()
    outcome: list = []

    def reconcile_elsewhere(job):
        reconciling.set()  # 這時已持有工作列的鎖（FOR UPDATE）
        stored = other.committed_run(schedule_service.run_id_for_job(job["id"]))
        return None if stored is None else schedule_job_module._result(job, stored)

    def recover_elsewhere():
        outcome.append(store.recover_expired(reconcile={"schedule": reconcile_elsewhere}))

    original_fence = store.CommitSection.fence
    fences = []

    def fence(section):
        original_fence(section)
        fences.append(section.attempt)
        if len(fences) == 2:  # 寫入交易裡、INSERT 之前：連線中斷，恢復迴圈開始
            expire(clean, jid)
            clean.sql("SELECT pg_terminate_backend(%s)", (section._conn.info.backend_pid,))
            threading.Thread(target=recover_elsewhere, daemon=True).start()
            assert reconciling.wait(10)

    monkeypatch.setattr(store.CommitSection, "fence", fence)
    monkeypatch.setattr(schedule_job_module, "FINALIZE_TRIES", 1)
    assert runner.execute(jid, "w1") in ("lost", "deferred")
    deadline = time.time() + 30
    while not outcome and time.time() < deadline:
        time.sleep(0.05)
    assert outcome == [[(jid, "reconciled")]]
    job = clean.job(jid)
    assert_one_run_one_audit(job)
    assert job["error"] is None and job["result"]["reconciled"] is True


def test_schedule_audit_commits_with_the_run_or_not_at_all(clean, monkeypatch):
    """稽核和排程在同一個交易：稽核寫不進去，排程也沒有寫入（工作如實失敗），不會只有其中一個。"""
    from app.repositories.production_repo import ProductionRepo

    jid = schedule_job(engine="greedy")

    def broken(self, conn, row, once=False):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(ProductionRepo, "_insert_audit", broken)
    assert runner.execute(jid, "w1") == "done"
    job = clean.job(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "INTERNAL_ERROR"
    assert runs() == [] and schedule_audits() == []


def test_inventory_rebuild_failure_after_commit_does_not_fail_the_job(clean, monkeypatch):
    """工廠資料庫（衍生檢視）重建失敗：排程已經提交，工作照樣成功（下次讀取會再重建）。"""
    from app.repositories.inventory_repo import get_inventory_repo

    def broken(force=False):
        raise OSError("disk full")

    monkeypatch.setattr(get_inventory_repo(), "ensure_built", broken)
    jid = schedule_job(engine="greedy")
    assert runner.execute(jid, "w1") == "done"
    assert_one_run_one_audit(clean.job(jid))


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
    assert {p.name for p in (get_settings().cad_jobs_dir / jid).iterdir()} == {"a1"}
    final = get_settings().cad_jobs_dir / jid / "a1"  # 每次嘗試發布到自己的資料夾
    assert {p.name for p in final.iterdir()} >= {"model.stl", "model.step", "code.py", "input.png"}
    assert not (get_settings().cad_jobs_dir / ".staging" / f"{jid}-a1").exists()
    assert "import cadquery" in (final / "code.py").read_text(encoding="utf-8")
    # 事件裡沒有任何一段模型程式碼
    for e in store.events_after(jid, 0, 500):
        assert "cadquery" not in str(e["data"]).lower()
    names = {a["name"] for a in store.artifacts(jid)}
    assert names >= {"model.stl", "code.py", "input.png"}
    # 受保護端點：授權的帳號拿得到程式碼和模型，沒權限的 404
    as_account(client, "manager")
    r = client.get(f"/api/v1/jobs/{jid}/artifacts/code.py")
    assert r.status_code == 200 and b"cadquery" in r.content
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/model.stl").status_code == 200
    # 相容讀取：舊網址與 summary
    assert client.get(f"/api/v1/cad/jobs/{jid}").json()["result"]["ok"] is True
    as_account(client, "sales")
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
    assert job["result"]["published_attempt"] == 1  # 正常結束的執行失敗有發布紀錄
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/code.py").status_code == 200
    assert client.get(f"/api/v1/cad/jobs/{jid}/code.py").status_code == 200  # 舊網址一致
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
    jid = cad_job(owner="manager", part="mfg-002")
    revoke(monkeypatch, domain_of="manager", domain="mfg")
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


@needs_limits
def test_cad_resource_taken_over_right_before_publish_publishes_nothing(clean, monkeypatch):
    from app.jobs import cad_job as cad_module

    jid = cad_job(part="mfg-002")
    real = cad_module._authorize

    def authorize_then_lose_the_slot(job):
        real(job)
        clean.sql("UPDATE resource_leases SET holder = 'job_other' WHERE holder = %s", (jid,))

    monkeypatch.setattr(cad_module, "_authorize", authorize_then_lose_the_slot)
    assert runner.execute(jid, "w1") == "lost"
    assert store.get(jid)["status"] == "running"
    assert not (get_settings().cad_jobs_dir / jid).exists()


@needs_limits
def test_cad_resource_takeover_after_the_final_check_publishes_nothing(clean, client, monkeypatch):
    """審查 F1（ad32d74）：CAD 驗證通過後停頓，GPU 名額被另一個 3D 重建接手，舊 worker 恢復——
    不發布產物，相容 API 也讀不到它的 code.py。"""
    from app.jobs import cad_job as cad_module

    jid = cad_job(part="mfg-002")
    other = cad_job(part="mfg-001")
    real = cad_module._authorize

    def pause_then_lose_the_slot(job):
        out = real(job)
        if in_section(jid):
            clean.sql("UPDATE resource_leases SET expires_at = now() - interval '1 second'")
            assert store.claim(other, "w2").state == "claimed"  # 另一個 3D 重建領到唯一的名額
        return out

    monkeypatch.setattr(cad_module, "_authorize", pause_then_lose_the_slot)
    assert runner.execute(jid, "w1") == "lost"
    assert store.get(jid)["status"] == "running"
    assert not (get_settings().cad_jobs_dir / jid).exists()
    as_account(client, "manager")
    assert client.get(f"/api/v1/cad/jobs/{jid}/code.py").status_code != 200
    assert store.get(other)["status"] == "running"  # 接手的工作不受影響


@needs_limits
def test_cad_lease_expiry_during_the_write_back_cannot_be_taken_over(clean, monkeypatch):
    from app.jobs import cad_job as cad_module

    jid = cad_job(part="mfg-002")
    other = cad_job(part="mfg-001")
    real = cad_module._authorize
    seen = []

    def pause(job):
        out = real(job)
        if in_section(jid):
            clean.sql(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (jid,),
            )
            seen.append(store.claim(other, "w2").state)
        return out

    monkeypatch.setattr(cad_module, "_authorize", pause)
    assert runner.execute(jid, "w1") == "done"
    assert seen == ["blocked"] and store.get(jid)["status"] == "succeeded"
    assert (get_settings().cad_jobs_dir / jid / "a1" / "code.py").is_file()


def test_old_cad_attempt_cannot_overwrite_a_newer_attempt_after_the_lock_connection_dies(
    clean, client, monkeypatch, tmp_path
):
    """審查 F1（509bf1d）：attempt 1 最後一次 fence 通過後，持有寫回鎖的連線被中斷，工作被收回並由
    attempt 2 成功發布；attempt 1 恢復後的發布只會落在它自己的資料夾（a1），讀取端只認資料庫記錄
    的 attempt 2——資料庫、產物清單的雜湊、新舊下載網址拿到的檔案全都是 attempt 2 的內容。"""
    import hashlib

    from app.jobs import cad_job as cad_module
    from app.jobs.control import JobControl
    from app.services import cad_access

    jid = cad_job(part="mfg-001")
    claimed = store.claim(jid, "w1")
    assert claimed.state == "claimed"
    monkeypatch.setattr(cad_module, "_authorize", lambda _job: None)
    old, new = b"print('old worker')\n", b"print('new worker')\n"
    first = tmp_path / "staging1"
    first.mkdir()
    (first / "code.py").write_bytes(old)
    original_fence = store.CommitSection.fence
    fences = []

    def fence(section):
        original_fence(section)
        fences.append(section.attempt)
        if section.attempt == 1 and fences.count(1) == 2:  # 第二次＝發布之前的最後一次
            kill_commit_connection(clean, section, jid)
            retry = store.claim(jid, "w2")
            assert retry.state == "claimed" and retry.job["attempt"] == 2
            second = tmp_path / "staging2"
            second.mkdir()
            (second / "code.py").write_bytes(new)
            cad_module._commit(retry.job, JobControl(jid, 2, "w2"), second, {}, {"ok": True}, {})

    monkeypatch.setattr(store.CommitSection, "fence", fence)
    with pytest.raises(store.LeaseLost):  # attempt 1：發布到自己的資料夾，結束被擋
        cad_module._commit(claimed.job, JobControl(jid, 1, "w1"), first, {}, {"ok": True}, {})

    job = clean.job(jid)
    assert job["status"] == "succeeded" and job["attempt"] == 2
    assert (cad_access.attempt_dir(jid, 2) / "code.py").read_bytes() == new  # 沒被刪、沒被換
    assert (cad_access.attempt_dir(jid, 1) / "code.py").read_bytes() == old  # 舊的只在自己那裡
    sha = clean.sql(
        "SELECT sha256 FROM job_artifacts WHERE job_id = %s AND name = 'code.py'", (jid,)
    )
    assert [r["sha256"] for r in sha] == [hashlib.sha256(new).hexdigest()]
    wait_for_jobs_db()
    assert cad_access.published_dir(jid) == cad_access.attempt_dir(jid, 2)
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/code.py").content == new
    assert client.get(f"/api/v1/cad/jobs/{jid}/code.py").content == new


def test_late_attempt_after_retries_ran_out_is_never_readable(clean, client, monkeypatch, tmp_path):
    """審查 F1（d4632b0）：重試上限 1，attempt 1 最後一次 fence 後持鎖連線被中斷，恢復迴圈把工作判定
    失敗（LEASE_EXPIRED），舊嘗試之後才把資料夾發布到磁碟上——沒有 fenced finish 的發布紀錄，
    新舊入口（產物、摘要、檔案）一律 404，對「產物是否已發布」的答案一致。"""
    from app.jobs import cad_job as cad_module
    from app.jobs.control import JobControl
    from app.services import cad_access

    jid = cad_job(part="mfg-001")
    clean.sql("UPDATE jobs SET max_attempts = 1 WHERE id = %s", (jid,))
    claimed = store.claim(jid, "w1")
    monkeypatch.setattr(cad_module, "_authorize", lambda _job: None)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "code.py").write_bytes(b"print('late old worker')\n")
    (staging / "summary.json").write_text('{"meta": {}, "result": {"ok": true}, "done": {}}')
    original_fence = store.CommitSection.fence
    fences = []

    def fence(section):
        original_fence(section)
        fences.append(section.attempt)
        if len(fences) == 2:  # 發布之前的最後一次
            clean.sql(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                (jid,),
            )
            killed = clean.sql(
                "SELECT pg_terminate_backend(%s) AS killed", (section._conn.info.backend_pid,)
            )
            assert killed[0]["killed"]
            assert store.recover_expired() == [(jid, "failed")]

    monkeypatch.setattr(store.CommitSection, "fence", fence)
    with pytest.raises(store.LeaseLost):
        cad_module._commit(claimed.job, JobControl(jid, 1, "w1"), staging, {}, {"ok": True}, {})
    job = clean.job(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "LEASE_EXPIRED"
    assert job["result"] is None  # 沒有發布紀錄
    assert (cad_access.attempt_dir(jid, 1) / "code.py").is_file()  # 晚到的檔案確實在磁碟上
    wait_for_jobs_db()
    assert cad_access.published_dir(jid) is None
    as_account(client, "manager")
    assert client.get(f"/api/v1/cad/jobs/{jid}/code.py").status_code == 404
    assert client.get(f"/api/v1/cad/jobs/{jid}").status_code == 404
    assert client.get(f"/api/v1/jobs/{jid}/artifacts/code.py").status_code == 404


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
    client.put(f"/api/v1/images/{image}/acl", json={"accounts": ["warehouse"]})
    jid = cad_job(owner="warehouse", part=None, image=image)
    client.put(f"/api/v1/images/{image}/acl", json={"accounts": []})  # 排隊期間被收回
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "IMAGE_NOT_FOUND"
    assert json.dumps(job["error"]).count("img_") == 0


def test_unexpected_errors_never_leak_their_text(clean, monkeypatch):
    """例外訊息可能夾帶路徑、帳密、模型輸出：工作的錯誤只留固定的短訊息，細節只在伺服器日誌。"""
    secret = "password=hunter2 /Users/someone/.env SECRET_MODEL_OUTPUT"

    def boom(*a, **k):
        raise RuntimeError(secret)

    monkeypatch.setattr(cad_service, "_reconstruct_stream", boom)
    jid = cad_job(part="mfg-002")
    assert runner.execute(jid, "w1") == "done"
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "INTERNAL_ERROR"
    blob = str(job["error"]) + str(store.events_after(jid, 0, 100))
    assert "hunter2" not in blob and ".env" not in blob and "SECRET_MODEL_OUTPUT" not in blob


def test_workers_never_call_jev_or_the_cloud(clean, monkeypatch):
    """背景工作不外送：成功的排程與 3D 重建都沒有呼叫 Jev，也沒有連到非本機的位址。"""
    from app.agent import jev

    calls = []

    async def no_jev(*a, **k):
        calls.append("jev")
        raise jev.JevUnavailable("測試")

    monkeypatch.setattr(jev, "ask", no_jev)
    import httpx

    real_send = httpx.AsyncClient.send

    async def guard_send(self, request, **kw):
        host = request.url.host
        assert host in ("127.0.0.1", "localhost", "::1"), f"連到非本機位址 {host}"
        return await real_send(self, request, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "send", guard_send)
    clean.fake.duration = 1
    jid = schedule_job()
    assert runner.execute(jid, "w1") == "done" and store.get(jid)["status"] == "succeeded"
    assert calls == []


def test_worker_authorization_follows_the_pluggable_account_source(clean):
    """權限來源是可替換的介面（之後接 SSO／IdP）：worker 重驗權限問的是它。"""
    real = identity.get_account("manager")

    class NoScheduling:
        def get_account(self, account_id):
            from dataclasses import replace

            return replace(real, ops=tuple(o for o in real.ops if o != "schedule_run"))

    jid = schedule_job(engine="greedy")
    security.set_account_source(NoScheduling())
    try:
        assert runner.execute(jid, "w1") == "done"
    finally:
        security.set_account_source(None)
    job = store.get(jid)
    assert job["status"] == "failed" and job["error"]["code"] == "PERMISSION_DENIED"
    assert runs() == []


# ------------------------------------------------- 3D 重建結果快取（TASK-20261014-002，ADR 042）
@pytest.fixture
def cad_cache_env(tmp_path, monkeypatch):
    """真模型模式＋假的 Ortho2CAD（回標準模型的程式碼）、模型身分與獨立的快取檔。"""
    from app.core.config import REPO_ROOT
    from app.rag.providers import Usage
    from app.services import cad_cache

    monkeypatch.setattr(get_settings(), "llm_mode", "local")
    ident = {"alias": "ortho2cad", "model": {"path": "m", "size": 1, "mtime_ns": 1}}
    monkeypatch.setattr(
        cad_cache, "model_identity", lambda alias: ident | {"mmproj": ident["model"]}
    )
    cache = cad_cache.CadCache(tmp_path / "cache.sqlite3")
    monkeypatch.setattr(cad_cache, "get_cache", lambda: cache)
    code = (REPO_ROOT / "kb" / "cad" / "mfg-002.py").read_text(encoding="utf-8")
    calls: list[tuple] = []

    class FakeOrtho2CAD:
        model = "ortho2cad"

        def __init__(self):
            self.usage, self.max_tokens, self.temperature, self.seed = Usage(), None, None, None

        async def stream(self, messages):
            calls.append((self.temperature, self.seed))
            yield code

    monkeypatch.setattr(cad_service, "get_provider", lambda strategy: FakeOrtho2CAD())
    return {"cache": cache, "calls": calls, "code": code.strip()}


@needs_limits
def test_cad_cache_hit_in_job_still_publishes_through_published_attempt(clean, cad_cache_env):
    from app.services import cad_access

    first = cad_job(part="mfg-002")
    assert runner.execute(first, "w1") == "done"
    row = store.get(first)
    assert row["status"] == "succeeded"
    assert row["result"]["result"]["cache"]["status"] == "stored"
    assert len(cad_cache_env["calls"]) == 1

    second = cad_job(part="mfg-002")
    assert runner.execute(second, "w1") == "done"
    row = store.get(second)
    assert row["status"] == "succeeded" and len(cad_cache_env["calls"]) == 1  # 沒有生成
    result = row["result"]["result"]
    assert result["cache"]["status"] == "hit" and result["iou"] > 0.95
    assert "code" not in result  # 事件與結果不帶程式碼（照舊）
    # 發布照舊：只認 fenced finish 記下的那一次嘗試，產物在那次嘗試自己的資料夾
    assert cad_access.published_attempt(row) == row["attempt"]
    d = cad_access.published_dir(second)
    assert d == cad_access.attempt_dir(second, row["attempt"])
    assert (d / "model.stl").is_file() and (d / "code.py").read_text() == cad_cache_env["code"]
    assert {a["name"] for a in store.artifacts(second)} >= {"model.stl", "code.py", "input.png"}
    assert "token" not in event_types(second)


@needs_limits
def test_cad_job_reject_then_regenerate_samples_a_new_seed(clean, cad_cache_env):
    from app.services import cad_cache

    jid = cad_job(part="mfg-002")
    runner.execute(jid, "w1")
    out = cad_cache.reject_job(identity.get_account("warehouse"), jid, "req_reject")
    assert out == {"job_id": jid, "rejected": True, "removed": True, "audit": "recorded"}
    job, _ = store.create_job(
        kind="cad",
        owner_id="manager",
        params={"strategy": "ortho2cad", "regenerate": True},
        resource={"part_id": "mfg-002", "image_id": None},
        scope="cad:mfg-002",
        request_id="req_regen",
    )
    assert runner.execute(job["id"], "w1") == "done"
    assert cad_cache_env["calls"][-1] == (0.7, 1)  # temperature＋第一個沒試過的 seed
    cache = store.get(job["id"])["result"]["result"]["cache"]
    # 假的模型每個 seed 都回同一份（被否決的）程式碼：標示先前被否決、不寫入
    assert cache["status"] == "rejected_before" and cache["candidate"]["seed"] == 1
    # 看不到這張圖紙的身分不能作廢
    with pytest.raises(security.AppError) as e:
        cad_cache.reject_job(identity.get_account("sales"), jid, "req_x")
    assert e.value.code == "CAD_JOB_NOT_FOUND"
