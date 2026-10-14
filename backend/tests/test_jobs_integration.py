"""背景工作的端到端與故障測試（TASK-20261013-001，AC-09～AC-14、AC-19）。

環境（tests/jobs_support.py）：自建的 PostgreSQL 與 Redis、獨立的
`celery -A app.jobs.celery_app worker` 子行程（不是 eager 模式）、假的 Timefold 服務；
模型用 mock（3D 重建用標準模型的程式碼，沙箱照常執行）。
驗證：端到端的 3D 重建與排程、API 行程不執行耗時工作、關頁面不取消、worker kill -9 後恢復並重試、
Redis 中斷與重啟（含訊息遺失）、資料庫中斷、重複投遞只執行一次、
兩個 worker 的 GPU 名額與排程範圍互斥、
取消與提前停止、跨行程撤權。
"""

import copy
import hashlib
import shutil
import time

import httpx
import pytest
import yaml
from conftest import DEFAULT_ACCOUNT, ROOT, as_account

from app.core import config
from app.core.config import get_settings
from app.jobs import broker, coordination, store
from app.repositories.production_repo import get_production_repo
from app.services import cad_service, schedule_service


@pytest.fixture(autouse=True)
def env(jobs, client):
    get_production_repo().reset()
    root = get_settings().cad_jobs_dir
    for d in root.glob("cad_*") if root.is_dir() else []:
        shutil.rmtree(d, ignore_errors=True)
    yield jobs
    get_production_repo().reset()
    as_account(client, DEFAULT_ACCOUNT)


def post(client, who, path, **kw):
    as_account(client, who)
    return client.post(path, **kw)


def create_schedule(client, who="manager", **body):
    r = post(client, who, "/api/v1/schedule/jobs", json={"seconds": 10, **body})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def create_cad(client, part="mfg-002", who="manager"):
    r = post(client, who, "/api/v1/cad/jobs", json={"part_id": part})
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def has_event(job_id, kind):
    return any(e["type"] == kind for e in store.events_after(job_id, 0, 500))


def runs():
    return get_production_repo().recent_runs(50)


def det_run_id(job_id):
    return "plan_" + hashlib.sha256(job_id.encode()).hexdigest()[:12]


# ============================================================ 端到端
def test_cad_job_runs_in_a_separate_worker_and_api_never_executes_it(env, client, monkeypatch):
    env.start_worker("gpu", "gpu1")
    # API 行程不執行耗時重建：建立工作時不會走到管線
    monkeypatch.setattr(
        cad_service, "_reconstruct_stream", lambda *a, **k: pytest.fail("API 行程不該執行重建")
    )
    job_id = create_cad(client)
    assert env.job(job_id)["status"] in ("queued", "running")
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=120)
    assert job["status"] == "succeeded", job["error"]
    res = job["result"]["result"]
    assert res["ok"] and res["iou"] > 0.95 and "code" not in res
    # 受保護產物：授權帳號可預覽／下載，其他帳號 404
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{job_id}/artifacts/model.stl").status_code == 200
    code = client.get(f"/api/v1/jobs/{job_id}/artifacts/code.py")
    assert code.status_code == 200 and b"cadquery" in code.content
    as_account(client, "sales")
    assert client.get(f"/api/v1/jobs/{job_id}/artifacts/model.stl").status_code == 404
    # 事件與輪詢都找得到終態
    as_account(client, "manager")
    events = client.get(f"/api/v1/jobs/{job_id}/events").text
    assert (
        "event: started" in events
        and "event: succeeded" in events
        and "cadquery" not in events.lower()
    )
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "succeeded"
    log = env.workers[0].log_path.read_text(errors="replace")
    assert "artrag.run_job" in log and "succeeded" in log  # 是 worker 行程處理的


def test_schedule_job_runs_in_a_separate_worker_and_is_written_back(env, client, monkeypatch):
    env.fake.duration = 2
    env.start_worker("schedule", "sched1")
    monkeypatch.setattr(
        schedule_service, "solve_core", lambda *a, **k: pytest.fail("API 行程不該求解")
    )
    job_id = create_schedule(client)
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=60)
    assert job["status"] == "succeeded", job["error"]
    run_id = job["result"]["run_id"]
    assert run_id == det_run_id(job_id) and len(runs()) == 1
    as_account(client, "manager")
    detail = client.get(f"/api/v1/schedule/runs/{run_id}").json()
    assert detail["engine"] == "timefold" and detail["operations"]
    overview = client.get("/api/v1/production/overview").json()
    assert overview["current"]["run_id"] == run_id and overview["active_job"] is None
    # 合法對照組：保存後 Text-to-SQL 查得到排程（工廠資料庫跟上）
    import sqlite3

    from app.repositories.inventory_repo import get_inventory_repo

    conn = sqlite3.connect(get_inventory_repo().path)
    assert conn.execute("SELECT count(*) FROM schedule_ops").fetchone()[0] > 0
    conn.close()


def test_closing_the_page_does_not_cancel_the_job(env, client, live_api):
    env.fake.duration = 3
    env.start_worker("schedule", "sched1")
    h = live_api.headers("manager")
    job_id = httpx.post(
        f"{live_api.base}/api/v1/schedule/jobs", json={"seconds": 10}, headers=h
    ).json()["job_id"]
    with httpx.stream(
        "GET", f"{live_api.base}/api/v1/jobs/{job_id}/events", headers=h, timeout=20
    ) as r:
        for line in r.iter_lines():
            if line.startswith("event: started"):
                break
    # 連線關掉（離開頁面）：工作照常執行完成
    job = env.wait_status(job_id, ("succeeded", "failed", "cancelled"), timeout=60)
    assert job["status"] == "succeeded" and not job["cancel_requested"]
    # 回到頁面：找回工作、續接事件，不用重新建立
    again = httpx.get(f"{live_api.base}/api/v1/jobs", params={"kind": "schedule"}, headers=h).json()
    assert [j["id"] for j in again["items"]] == [job_id]
    types = [
        ln[6:].strip()
        for ln in httpx.get(
            f"{live_api.base}/api/v1/jobs/{job_id}/events", headers={**h, "Last-Event-ID": "2"}
        ).text.splitlines()
        if ln.startswith("event:")
    ]
    assert types[-1] == "succeeded" and "created" not in types and "started" not in types


# ============================================================ worker 當機與重啟
def test_worker_killed_mid_run_is_recovered_and_retried_once(env, client):
    env.fake.duration = 120
    a = env.start_worker("schedule", "schedA")
    job_id = create_schedule(client)
    env.wait_until(lambda: has_event(job_id, "progress"), 60, "第一個進度事件")
    assert env.job(job_id)["status"] == "running" and env.job(job_id)["attempt"] == 1
    a.kill()  # kill -9：沒有機會收尾，租約也不會釋放
    # API 在 worker 掛掉期間仍可查狀態（還是 running，直到租約過期）
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "running"
    env.fake.duration = 1.0  # 重試時很快完成
    env.wait_until(
        lambda: env.sql("SELECT 1 FROM jobs WHERE id=%s AND lease_expires_at < now()", (job_id,)),
        30,
        "租約過期",
    )
    assert broker.maintenance_once()["recovered"] == [
        (job_id, "requeued")
    ]  # 重啟後的維護（API 或另一個 worker）
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"
    env.start_worker("schedule", "schedB")  # worker 重啟
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=90)
    assert job["status"] == "succeeded", job["error"]
    assert job["attempt"] == 2  # 第二次嘗試完成，不宣稱續跑
    types = [e["type"] for e in store.events_after(job_id, 0, 500)]
    assert types.count("started") == 2 and "requeued" in types and types[-1] == "succeeded"
    assert [r["run_id"] for r in runs()] == [det_run_id(job_id)]  # 只寫回一次


def test_stale_worker_waking_up_cannot_overwrite_the_new_result(env, client):
    """worker 沒死、只是停住而租約過期：接手的新 worker 完成後，舊 worker 醒來什麼都寫不進去。"""
    env.fake.duration = 1.0
    job_id = create_schedule(client)
    old = store.claim(job_id, "stale-worker")
    assert old.state == "claimed"
    env.sql("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id=%s", (job_id,))
    env.sql("UPDATE resource_leases SET expires_at = now() - interval '1 second'")
    assert broker.maintenance_once()["recovered"] == [(job_id, "requeued")]
    env.start_worker("schedule", "sched1")
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=60)
    assert job["status"] == "succeeded" and job["attempt"] == 2
    good = job["result"]["run_id"]
    # 舊 worker（attempt 1）延遲醒來：寫事件、標結果都被擋，生產資料庫與工作列都不變
    with pytest.raises(store.LeaseLost):
        store.finish(job_id, 1, "stale-worker", "succeeded", result={"run_id": "plan_bad"})
    with pytest.raises(store.LeaseLost):
        store.add_event(job_id, 1, "stale-worker", "progress", {}, {})
    assert env.job(job_id)["result"]["run_id"] == good and [r["run_id"] for r in runs()] == [good]


# ============================================================ broker 與資料庫中斷
def test_broker_outage_accepts_the_job_and_delivers_after_recovery(env, client):
    env.fake.duration = 1
    env.start_worker("schedule", "sched1")
    env.redis.stop()
    r = post(client, "manager", "/api/v1/schedule/jobs", json={"seconds": 10})
    assert r.status_code == 202 and r.json()["delayed_dispatch"] is True  # 明確、可恢復的狀態
    job_id = r.json()["job_id"]
    assert env.job(job_id)["status"] == "queued" and store.pending_outbox() == 1
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"
    time.sleep(1.5)
    assert env.job(job_id)["status"] == "queued"  # broker 不通期間不會偷偷在別處執行
    env.redis.start()
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=90)
    assert job["status"] == "succeeded", job["error"]
    assert store.pending_outbox() == 0 and len(runs()) == 1


def test_message_lost_when_the_broker_restarts_is_dispatched_again(env, client):
    env.fake.duration = 1
    job_id = create_schedule(client)  # 訊息進了 Redis，但還沒有 worker 領
    assert store.pending_outbox() == 0
    env.redis.stop()
    env.redis.start()  # Redis 沒有持久化：訊息遺失
    env.start_worker("schedule", "sched1")
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=90)  # 靠 redispatch_stalled 補投
    assert job["status"] == "succeeded", job["error"]
    reasons = [
        r["reason"] for r in env.sql("SELECT reason FROM job_outbox WHERE job_id=%s", (job_id,))
    ]
    assert "created" in reasons and "stalled" in reasons
    assert len(runs()) == 1


def test_database_outage_is_a_clear_recoverable_503(env, client):
    env.fake.duration = 1
    job_id = create_schedule(client)
    env.pg.stop()
    try:
        as_account(client, "manager")
        for method, path in (
            ("post", "/api/v1/schedule/jobs"),
            ("post", "/api/v1/cad/jobs"),
            ("get", f"/api/v1/jobs/{job_id}"),
            ("get", "/api/v1/jobs"),
        ):
            r = getattr(client, method)(
                path, **({"json": {"part_id": "mfg-002"}} if method == "post" else {})
            )
            assert r.status_code == 503 and r.json()["error"]["code"] == "JOBS_UNAVAILABLE", path
    finally:
        env.pg.start()
    env.start_worker("schedule", "sched1")
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=90)  # 資料庫恢復後工作照常完成
    assert job["status"] == "succeeded"
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200


# ============================================================ 重複投遞與多 worker
def test_duplicate_deliveries_to_two_workers_run_the_job_once(env, client):
    env.fake.duration = 2
    job_id = create_schedule(client)
    for _ in range(3):  # 佇列重送：同一個工作再投遞三次
        env.sql(
            "INSERT INTO job_outbox (job_id, queue, reason) VALUES (%s, 'schedule', 'dup')",
            (job_id,),
        )
    env.start_worker("schedule", "sched1")
    env.start_worker("schedule", "sched2")
    job = env.wait_status(job_id, ("succeeded", "failed"), timeout=60)
    time.sleep(2)  # 讓重複的訊息都被兩個 worker 處理過
    job = env.job(job_id)
    assert job["status"] == "succeeded" and job["attempt"] == 1
    assert len(env.fake.submitted) == 1
    assert [r["run_id"] for r in runs()] == [det_run_id(job_id)]
    assert [e["type"] for e in store.events_after(job_id, 0, 500)].count("started") == 1


def test_same_schedule_scope_never_runs_twice_even_with_many_workers(env, client):
    env.fake.duration = 2
    env.start_worker("schedule", "sched1")
    env.start_worker("schedule", "sched2")
    first = create_schedule(client)
    as_account(client, "manager")
    second = client.post("/api/v1/schedule/jobs", json={})
    assert second.status_code == 409 and second.json()["error"]["code"] == "SCHEDULE_BUSY"
    env.wait_status(first, "succeeded", timeout=60)
    assert len(env.fake.submitted) == 1
    # 第一個完成後可以再排一次；兩次寫回是不同的列，互不覆蓋
    again = create_schedule(client)
    env.wait_status(again, "succeeded", timeout=60)
    assert {r["run_id"] for r in runs()} == {det_run_id(first), det_run_id(again)}


def test_gpu_heavy_jobs_run_one_at_a_time_across_two_workers(env, client):
    env.start_worker("gpu", "gpu1")
    env.start_worker("gpu", "gpu2")
    ids = [create_cad(client, part) for part in ("mfg-001", "mfg-002", "mfg-003")]
    peak, seen_models = 0, False
    deadline = time.time() + 180
    while time.time() < deadline:
        rows = env.sql("SELECT id, status FROM jobs ORDER BY id")
        peak = max(peak, sum(1 for r in rows if r["status"] == "running"))
        leases = env.sql("SELECT resource FROM resource_leases")
        assert len(leases) <= 1  # GPU 名額最多一個
        seen_models = seen_models or ("ortho2cad" in (coordination.others_in_use() or set()))
        if all(r["status"] in ("succeeded", "failed") for r in rows):
            break
        time.sleep(0.03)
    final = {r["id"]: r["status"] for r in env.sql("SELECT id, status FROM jobs")}
    assert set(final) == set(ids) and set(final.values()) == {"succeeded"}, final
    assert peak == 1  # 三個工作、兩個 worker，同時只有一個在跑
    assert seen_models  # worker 登記的「使用中模型」其他行程（這裡的 API 行程）看得到


# ============================================================ 取消與提前停止
def test_stop_and_cancel_with_a_real_worker(env, client):
    env.fake.duration = 120
    env.start_worker("schedule", "sched1")
    # 提前停止：採用目前最佳的合法解並寫回
    a = create_schedule(client)
    env.wait_until(lambda: has_event(a, "progress"), 60, "進度")
    as_account(client, "manager")
    assert client.post("/api/v1/schedule/stop").json() == {"ok": True}
    job = env.wait_status(a, ("succeeded", "failed", "cancelled"), timeout=60)
    assert job["status"] == "succeeded" and job["result"]["run_status"] == "stopped"
    assert len(runs()) == 1 and runs()[0]["status"] == "stopped"
    # 取消：放棄、不寫回
    b = create_schedule(client)
    env.wait_until(lambda: has_event(b, "progress"), 60, "進度")
    r = client.post(f"/api/v1/jobs/{b}/cancel")
    assert r.status_code == 200 and r.json()["result"] == "cancelling"
    job = env.wait_status(b, ("succeeded", "failed", "cancelled"), timeout=60)
    assert job["status"] == "cancelled" and job["result"] is None
    assert len(runs()) == 1  # 沒有新增
    assert set(env.fake.stopped) == set(env.fake.submitted)
    assert env.job(b)["finished_at"] is not None and has_event(b, "cancelled")
    as_account(client, "guest")
    assert client.post("/api/v1/schedule/stop").status_code == 403


# ============================================================ 跨行程撤權
@pytest.fixture
def shared_copy(tmp_path, monkeypatch):
    """shared/ 的副本：API 與 worker 都改看它，改 access.yaml（撤權）不動版控中的檔案。"""
    dst = tmp_path / "shared"
    shutil.copytree(ROOT / "shared", dst)
    monkeypatch.setenv("ARTRAG_SHARED_DIR", str(dst))
    config.get_settings.cache_clear()
    yield dst
    monkeypatch.undo()
    config.get_settings.cache_clear()


def edit_access(shared, mutate):
    path = shared / "access.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(cfg)
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    time.sleep(0.05)  # 確保修改時間不同


def test_permission_revoked_while_queued_is_enforced_by_the_worker_process(
    env, client, shared_copy
):
    job_id = create_schedule(client)  # 建立時主管有權限（docs/adr/036：排程在主管）
    edit_access(shared_copy, lambda cfg: cfg["roles"]["manager"]["ops"].remove("schedule_run"))
    env.start_worker("schedule", "sched1")  # worker 是另一個行程，讀同一份（已修改的）設定
    job = env.wait_status(job_id, ("succeeded", "failed", "cancelled"), timeout=60)
    assert job["status"] == "failed" and job["error"]["code"] == "PERMISSION_DENIED"
    assert env.fake.submitted == [] and runs() == []  # 沒有呼叫求解、沒有寫入
    as_account(client, "manager")  # 讀取也驗目前授權：主管還能讀工廠資料庫，所以看得到失敗結果
    assert client.get(f"/api/v1/jobs/{job_id}").json()["error"]["code"] == "PERMISSION_DENIED"
    assert client.post("/api/v1/schedule/jobs", json={}).status_code == 403  # 也不能再建立


def test_cad_permission_revoked_while_queued_never_reads_or_generates(env, client, shared_copy):
    job_id = create_cad(client, "mfg-002")
    edit_access(shared_copy, lambda cfg: cfg["clearance"]["manager"]["domains"].remove("mfg"))
    env.start_worker("gpu", "gpu1")
    job = env.wait_status(job_id, ("succeeded", "failed", "cancelled"), timeout=60)
    assert job["status"] == "failed" and job["error"]["code"] == "DATA_SCOPE_DENIED"
    assert not (get_settings().cad_jobs_dir / job_id).exists() and store.artifacts(job_id) == []
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404  # 撤權後連讀都不行


def test_access_config_reloads_when_the_file_changes(shared_copy):
    from app.services import identity

    assert identity.get_account("manager").can("schedule_run")
    edit_access(shared_copy, lambda cfg: cfg["roles"]["manager"]["ops"].remove("schedule_run"))
    assert not identity.get_account("manager").can("schedule_run")  # 不用重啟
    cfg = copy.deepcopy(config.get_access_config())
    assert "schedule_run" not in cfg["roles"]["manager"]["ops"]


def test_api_restart_keeps_jobs_queryable_and_workers_pick_them_up(env, client):
    """API 行程重啟（連線池、broker 連線重建）：狀態仍可查，工作照常由 worker 完成。"""
    from app.jobs import db

    env.fake.duration = 1
    job_id = create_schedule(client)
    db.close()  # 連線池重建＝新的 API 行程
    broker._url["broker"] = None
    as_account(client, "manager")
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "queued"
    env.start_worker("schedule", "sched1")
    env.wait_status(job_id, "succeeded", timeout=60)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == "succeeded"


def test_health_status_lists_live_workers_and_dependencies(env, client):
    """健康狀態：PostgreSQL、Redis、待投遞數、各狀態工作數，以及活著的 worker（kill 後會消失）。"""
    as_account(client, "manager")
    body = client.get("/api/v1/status").json()["jobs"]
    assert body["enabled"] and body["database_ok"] and body["broker_ok"] and body["workers"] == []
    w = env.start_worker("schedule", "sched1")
    env.wait_until(
        lambda: client.get("/api/v1/status").json()["jobs"]["workers"], 30, "worker 登記"
    )
    workers = client.get("/api/v1/status").json()["jobs"]["workers"]
    assert workers[0]["queues"] == ["schedule"] and workers[0]["pid"] == w.proc.pid
    w.kill()
    env.wait_until(
        lambda: not client.get("/api/v1/status").json()["jobs"]["workers"], 40, "worker 消失"
    )


def test_publish_recovers_after_the_broker_restarts_without_losing_the_first_message(env, client):
    """Redis 重啟後，這個行程裡 Celery 的舊連線已經失效：第一則投遞要自己重建連線，不能白白失敗。"""
    import redis

    broker.publish("cad_0000000000000001", "gpu")  # 建立連線池
    env.redis.stop()
    env.redis.start()
    broker.publish("cad_0000000000000002", "gpu")  # 沒有例外
    r = redis.Redis(port=env.redis.port, decode_responses=True)
    assert r.llen("gpu") == 1  # 重啟後佇列是空的，這一則進去了
