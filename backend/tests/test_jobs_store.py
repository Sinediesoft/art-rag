"""背景工作的狀態機與資料結構（真的 PostgreSQL，不經 Redis／worker）。

涵蓋：版本化 migration 與回復、建立＋outbox 同一交易、Idempotency-Key、同排程範圍唯一、
GPU 名額、領取／租約／心跳、過期恢復、舊 worker 的 fencing、取消／提前停止／寫回的競態、
outbox 投遞失敗與補投遞、事件續接、保存期限清理。
"""

import threading
import time

import psycopg
import pytest

from app.core import config
from app.core.errors import AppError
from app.jobs import db, migrate, store


@pytest.fixture
def settings(monkeypatch, jobs_env):
    def apply(**kw):
        for k, v in kw.items():
            monkeypatch.setenv(k.upper(), str(v))
        config.get_settings.cache_clear()

    yield apply
    monkeypatch.undo()
    config.get_settings.cache_clear()


def cad(owner="manager", part="mfg-002", key=None, **params):
    return store.create_job(
        kind="cad",
        owner_id=owner,
        params={"strategy": "ortho2cad", **params},
        resource={"part_id": part, "image_id": None},
        scope=f"cad:{part}",
        request_id="req_t",
        idempotency_key=key,
    )


def schedule(owner="planner", key=None, seconds=5):
    return store.create_job(
        kind="schedule",
        owner_id=owner,
        params={"seconds": seconds, "engine": "timefold"},
        resource={},
        scope="schedule:plant",
        request_id="req_t",
        input_version="sv1:abc",
        input_versions={"version": "sv1:abc"},
        idempotency_key=key,
    )


def table_names(jobs_env) -> set[str]:
    rows = jobs_env.sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return {r["table_name"] for r in rows}


def expire(jobs_env, job_id):
    jobs_env.sql(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job_id,)
    )
    jobs_env.sql("UPDATE resource_leases SET expires_at = now() - interval '1 second'")


# ----------------------------------------------------------------------------- migration
def test_migrations_apply_are_idempotent_and_reversible(jobs_env):
    assert migrate.apply() == []  # jobs_env 已套用過
    assert [m["applied"] for m in migrate.status()] == [True]
    tables = table_names(jobs_env)
    assert {"jobs", "job_events", "job_outbox", "job_artifacts", "resource_leases"} <= tables
    # 回復後資料表消失，再套用回來；只動工作系統自己的表
    jobs_env.sql("CREATE TABLE IF NOT EXISTS unrelated_keep (x int)")
    assert migrate.rollback(1) == ["0001_jobs.down.sql"]
    tables = table_names(jobs_env)
    assert "jobs" not in tables and "unrelated_keep" in tables
    assert migrate.apply() == ["0001_jobs.up.sql"]
    jobs_env.sql("DROP TABLE unrelated_keep")


def test_applied_migration_that_was_edited_is_refused(jobs_env):
    jobs_env.sql("UPDATE schema_migrations SET checksum = 'edited' WHERE version = '0001'")
    try:
        with pytest.raises(migrate.MigrationError, match="修改"):
            migrate.apply()
        assert migrate.status()[0]["changed"] is True
    finally:
        import hashlib

        f = migrate.DIR / "0001_jobs.up.sql"
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        jobs_env.sql("UPDATE schema_migrations SET checksum = %s WHERE version = '0001'", (digest,))


# ----------------------------------------------------------------------------- 建立與 outbox
def test_create_writes_job_event_and_outbox_in_one_transaction(jobs):
    job, new = cad()
    assert new and job["status"] == "queued" and job["queue"] == "gpu" and job["attempt"] == 0
    assert job["id"].startswith("cad_")
    assert [e["type"] for e in store.events_after(job["id"], 0)] == ["created"]
    out = jobs.sql("SELECT * FROM job_outbox WHERE job_id = %s", (job["id"],))
    assert len(out) == 1 and out[0]["dispatched_at"] is None and out[0]["queue"] == "gpu"


def test_failed_insert_leaves_neither_job_nor_outbox(jobs, monkeypatch):
    """outbox 寫入失敗時整個交易回滾：不會有「資料庫有工作、佇列沒訊息」的半成品。"""
    real = store._event

    def boom(conn, job_id, type_, data=None):
        real(conn, job_id, type_, data)
        raise psycopg.errors.DataException("模擬在事件之後失敗")

    monkeypatch.setattr(store, "_event", boom)
    with pytest.raises(psycopg.errors.DataException):
        cad()
    assert jobs.sql("SELECT count(*) AS n FROM jobs")[0]["n"] == 0
    assert jobs.sql("SELECT count(*) AS n FROM job_outbox")[0]["n"] == 0


def test_idempotency_key_same_request_same_job(jobs):
    a, new_a = cad(key="key-aaaaaaaa")
    b, new_b = cad(key="key-aaaaaaaa")
    assert new_a and not new_b and a["id"] == b["id"]
    assert jobs.sql("SELECT count(*) AS n FROM jobs")[0]["n"] == 1
    assert jobs.sql("SELECT count(*) AS n FROM job_outbox")[0]["n"] == 1


def test_idempotency_key_reused_with_other_params_conflicts(jobs):
    cad(key="key-bbbbbbbb")
    with pytest.raises(AppError) as e:
        cad(key="key-bbbbbbbb", part="mfg-001")
    assert e.value.status == 409 and e.value.code == "IDEMPOTENCY_KEY_REUSED"
    with pytest.raises(AppError) as e:
        cad(key="key-bbbbbbbb", strategy="hybrid")
    assert e.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_idempotency_key_is_per_account_and_operation(jobs):
    a, _ = cad(owner="manager", key="key-cccccccc")
    b, new = cad(owner="planner", key="key-cccccccc")  # 另一個帳號用同一個 key：不同的工作
    assert new and a["id"] != b["id"]


def test_concurrent_identical_requests_create_one_job(jobs):
    ids, errors = [], []

    def go():
        try:
            ids.append(cad(key="key-dddddddd")[0]["id"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(ids)) == 1
    assert jobs.sql("SELECT count(*) AS n FROM jobs")[0]["n"] == 1


def test_only_one_active_schedule_job_per_scope(jobs):
    first, _ = schedule()
    with pytest.raises(store.ScheduleBusy) as e:
        schedule()
    assert e.value.status == 409 and e.value.active_job_id == first["id"]
    store.cancel(first["id"])  # 取消之後可以再建
    assert schedule()[1] is True


# ----------------------------------------------------------------------------- 領取、租約
def test_claim_moves_to_running_and_second_claim_is_refused(jobs):
    job, _ = cad()
    c = store.claim(job["id"], "w1")
    assert c.state == "claimed" and c.job["status"] == "running" and c.job["attempt"] == 1
    assert c.resource == "gpu:0" and c.job["lease_owner"] == "w1"
    assert store.claim(job["id"], "w2").state == "not_queued"
    assert [e["type"] for e in store.events_after(job["id"], 0)] == ["created", "started"]


def test_gpu_slots_limit_concurrent_cad_jobs(jobs, settings):
    settings(jobs_gpu_slots=1)
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    assert store.claim(a["id"], "w1").state == "claimed"
    assert store.claim(b["id"], "w2").state == "blocked"  # 名額被占：留在 queued
    assert store.get(b["id"])["status"] == "queued"
    store.finish(a["id"], 1, "w1", "failed", error={"code": "X", "message": "x"})
    assert store.claim(b["id"], "w2").state == "claimed"  # 名額釋放後可領


def test_gpu_slots_setting_allows_more(jobs, settings):
    settings(jobs_gpu_slots=2)
    ids = [cad(part=p)[0]["id"] for p in ("mfg-001", "mfg-002", "mfg-003")]
    states = [store.claim(i, f"w{n}").state for n, i in enumerate(ids)]
    assert states == ["claimed", "claimed", "blocked"]
    held = {r["resource"] for r in jobs.sql("SELECT resource FROM resource_leases")}
    assert held == {"gpu:0", "gpu:1"}


def test_schedule_scope_lease_is_exclusive_with_legacy_holder(jobs):
    job, _ = schedule()
    assert store.acquire_lease("schedule:plant", "legacy:req", 30)  # 舊入口先拿到
    assert store.claim(job["id"], "w1").state == "blocked"
    store.release_lease("schedule:plant", "legacy:req")
    assert store.claim(job["id"], "w1").state == "claimed"
    assert not store.acquire_lease("schedule:plant", "legacy:req2", 30)  # 反過來也不行


def test_heartbeat_extends_lease_and_reports_flags(jobs):
    job, _ = cad()
    store.claim(job["id"], "w1")
    first = jobs.job(job["id"])["lease_expires_at"]
    time.sleep(0.05)
    flags = store.heartbeat(job["id"], 1, "w1")
    assert flags == {"cancel_requested": False, "stop_requested": False, "committing_attempt": None}
    assert jobs.job(job["id"])["lease_expires_at"] > first
    with pytest.raises(store.LeaseLost):
        store.heartbeat(job["id"], 1, "someone-else")
    with pytest.raises(store.LeaseLost):
        store.heartbeat(job["id"], 2, "w1")


# ----------------------------------------------------------------------------- 過期恢復與 fencing
def test_expired_lease_is_requeued_and_stale_worker_is_fenced(jobs):
    job, _ = cad()
    store.claim(job["id"], "w1")
    expire(jobs, job["id"])
    assert store.recover_expired() == [(job["id"], "requeued")]
    row = store.get(job["id"])
    assert row["status"] == "queued" and row["lease_owner"] is None and row["attempt"] == 1
    out = jobs.sql("SELECT reason FROM job_outbox WHERE job_id = %s ORDER BY id", (job["id"],))
    assert [o["reason"] for o in out] == ["created", "recovered"]
    c2 = store.claim(job["id"], "w2")
    assert c2.state == "claimed" and c2.job["attempt"] == 2
    # 舊 worker（attempt 1）醒來：事件、進度、寫回資格、結束全部被擋
    with pytest.raises(store.LeaseLost):
        store.add_event(job["id"], 1, "w1", "progress", {"x": 1}, {"x": 1})
    with pytest.raises(store.LeaseLost):
        store.heartbeat(job["id"], 1, "w1")
    assert store.reserve_commit(job["id"], 1, "w1") is False
    with pytest.raises(store.LeaseLost):
        store.finish(job["id"], 1, "w1", "failed", error={"code": "X", "message": "x"})
    assert store.get(job["id"])["status"] == "running"  # 新 worker 的執行沒被影響
    assert store.reserve_commit(job["id"], 2, "w2") is True
    store.finish(job["id"], 2, "w2", "succeeded", result={"ok": True})
    done = store.get(job["id"])
    assert done["status"] == "succeeded" and done["result"] == {"ok": True}
    with pytest.raises(store.LeaseLost):  # 結束之後舊 worker 還是什麼都不能寫
        store.finish(job["id"], 1, "w1", "failed", error={"code": "X", "message": "x"})
    assert store.get(job["id"])["status"] == "succeeded"


def test_lease_taken_over_by_requeue_frees_the_gpu_slot(jobs, settings):
    settings(jobs_gpu_slots=1)
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    store.claim(a["id"], "w1")
    assert store.claim(b["id"], "w2").state == "blocked"
    expire(jobs, a["id"])
    store.recover_expired()
    assert store.claim(b["id"], "w2").state == "claimed"


def lease(jobs, job_id):
    return jobs.sql("SELECT lease_expires_at > now() AS alive FROM jobs WHERE id = %s", (job_id,))[
        0
    ]["alive"]


def test_old_worker_cannot_keep_working_after_its_gpu_slot_was_taken(jobs, settings):
    """租約過期、資源被別的工作接手、舊 worker 在 recover_expired 收回之前醒來（審查 F2）：
    續約、事件、寫回資格、鎖內確認、結束都不成立；接手的工作不受影響。"""
    settings(jobs_gpu_slots=1)
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    assert store.claim(a["id"], "w1").state == "claimed"
    assert store.claim(b["id"], "w2").state == "blocked"
    expire(jobs, a["id"])  # A 的工作租約與 gpu:0 的租約都過期，但 A 的工作列還是 running
    assert store.claim(b["id"], "w2").state == "claimed"  # B 接手同一個名額
    holder = jobs.sql("SELECT holder FROM resource_leases WHERE resource = 'gpu:0'")
    assert [h["holder"] for h in holder] == [b["id"]]

    assert store.get(a["id"])["status"] == "running"  # 還沒被 recover_expired 收回的窗口
    with pytest.raises(store.LeaseLost):
        store.heartbeat(a["id"], 1, "w1")
    assert not lease(jobs, a["id"])  # 沒有替它延長工作租約（連同資源更新一起回滾）
    assert store.reserve_commit(a["id"], 1, "w1") is False
    with pytest.raises(store.LeaseLost), store.commit_section(a["id"], 1, "w1"):
        pytest.fail("失去資源的 worker 不該進得了寫回區段")
    with pytest.raises(store.LeaseLost):
        store.add_event(a["id"], 1, "w1", "progress", {"x": 1}, {"x": 1})
    with pytest.raises(store.LeaseLost):
        store.finish(a["id"], 1, "w1", "failed", error={"code": "X", "message": "x"})
    assert store.get(a["id"])["committing_attempt"] is None

    # 之後 A 被收回重排；B 的名額、租約、寫回都不受影響
    assert store.recover_expired() == [(a["id"], "requeued")]
    holder = jobs.sql("SELECT holder FROM resource_leases WHERE resource = 'gpu:0'")
    assert [h["holder"] for h in holder] == [b["id"]]
    assert store.heartbeat(b["id"], 1, "w2")["cancel_requested"] is False
    assert store.reserve_commit(b["id"], 1, "w2") is True
    with store.commit_section(b["id"], 1, "w2"):
        store.finish(b["id"], 1, "w2", "succeeded", result={"ok": True})
    assert store.get(b["id"])["status"] == "succeeded"
    assert store.claim(a["id"], "w3").state == "claimed"  # 名額空出來，A 再被領取


def test_expired_lease_nobody_took_over_can_still_be_renewed(jobs, settings):
    """過期但沒有被接手：照常續約（資源租約一起延長），其他工作仍領不到那個名額。"""
    settings(jobs_gpu_slots=1)
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    store.claim(a["id"], "w1")
    expire(jobs, a["id"])
    store.heartbeat(a["id"], 1, "w1")
    assert lease(jobs, a["id"])
    assert store.claim(b["id"], "w2").state == "blocked"
    assert store.reserve_commit(a["id"], 1, "w1") is True
    with store.commit_section(a["id"], 1, "w1"):
        pass


def test_schedule_scope_taken_by_someone_else_fences_the_worker(jobs):
    job, _ = schedule()
    store.claim(job["id"], "w1")
    jobs.sql("UPDATE resource_leases SET holder = 'legacy:other' WHERE resource = 'schedule:plant'")
    with pytest.raises(store.LeaseLost):
        store.heartbeat(job["id"], 1, "w1")
    assert store.reserve_commit(job["id"], 1, "w1") is False
    with pytest.raises(store.LeaseLost):
        store.finish(job["id"], 1, "w1", "failed", error={"code": "X", "message": "x"})


def test_requeued_between_reserve_and_write_back_is_caught_at_the_section(jobs):
    """取得寫回資格之後、拿到寫回鎖之前被收回重排：進入寫回區段時的 fence 擋下，不會寫入。"""
    job, _ = cad()
    store.claim(job["id"], "w1")
    assert store.reserve_commit(job["id"], 1, "w1")
    expire(jobs, job["id"])
    assert store.recover_expired() == [(job["id"], "requeued")]
    with pytest.raises(store.LeaseLost), store.commit_section(job["id"], 1, "w1"):
        pytest.fail("已被收回重排的 worker 不該進得了寫回區段")


def pinned(jobs, resource):
    rows = jobs.sql(
        "SELECT holder, expires_at = 'infinity' AS pinned FROM resource_leases WHERE resource = %s",
        (resource,),
    )
    return rows[0] if rows else None


def test_nobody_can_take_over_the_resource_during_the_write_back(jobs, settings):
    """審查 F1（ad32d74）：取得寫回資格後，即使工作租約過期，別的工作（GPU 名額）與舊同步入口
    （排程範圍）也接手不了資源；互斥涵蓋整段外部寫回，到工作結束為止。"""
    settings(jobs_gpu_slots=1)
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    store.claim(a["id"], "w1")
    assert store.reserve_commit(a["id"], 1, "w1") is True
    assert pinned(jobs, "gpu:0") == {"holder": a["id"], "pinned": True}
    jobs.sql(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (a["id"],)
    )
    assert store.claim(b["id"], "w2").state == "blocked"  # 租約「過期」也領不到
    store.heartbeat(a["id"], 1, "w1")  # 續約不會把它改回有期限的
    assert pinned(jobs, "gpu:0")["pinned"] is True
    with store.commit_section(a["id"], 1, "w1") as section:
        section.fence()
        assert store.claim(b["id"], "w2").state == "blocked"
    store.finish(a["id"], 1, "w1", "succeeded", result={"ok": True})
    assert store.claim(b["id"], "w2").state == "claimed"  # 結束後名額才釋放

    sched, _ = schedule()
    store.claim(sched["id"], "w3")
    assert store.reserve_commit(sched["id"], 1, "w3") is True
    assert store.acquire_lease("schedule:plant", "legacy:review", 30) is False
    jobs.sql(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
        (sched["id"],),
    )
    assert store.acquire_lease("schedule:plant", "legacy:review", 30) is False


def test_fence_stops_a_writer_whose_resource_was_forcibly_taken(jobs, settings):
    """連『永不過期』的標記都被強制改掉、資源被接手（例如人為清除）：進入寫回區段與寫入前的 fence
    都丟 LeaseLost——即使已經在區段裡。"""
    settings(jobs_gpu_slots=1)
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    store.claim(a["id"], "w1")
    assert store.reserve_commit(a["id"], 1, "w1") is True
    with store.commit_section(a["id"], 1, "w1") as section:
        section.fence()
        jobs.sql("UPDATE resource_leases SET expires_at = now() - interval '1 second'")  # 強制
        assert store.claim(b["id"], "w2").state == "claimed"
        with pytest.raises(store.LeaseLost):
            section.fence()
    with pytest.raises(store.LeaseLost), store.commit_section(a["id"], 1, "w1"):
        pytest.fail("資源已被接手，不該進得了寫回區段")


def test_a_writer_that_died_does_not_pin_the_resource_forever(jobs, settings):
    """寫回途中 worker 死了（沒有寫回鎖、工作租約過期）：恢復迴圈收回工作和『永不過期』的租約。"""
    settings(jobs_gpu_slots=1)
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    store.claim(a["id"], "w1")
    assert store.reserve_commit(a["id"], 1, "w1") is True
    jobs.sql(
        "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (a["id"],)
    )
    assert store.claim(b["id"], "w2").state == "blocked"
    assert store.recover_expired() == [(a["id"], "requeued")]
    assert pinned(jobs, "gpu:0") is None
    assert store.claim(b["id"], "w2").state == "claimed"


def test_jobs_failing_too_many_times_stop_retrying(jobs, settings):
    settings(jobs_max_attempts=2)
    job, _ = cad()
    for n in (1, 2):
        c = store.claim(job["id"], f"w{n}")
        assert c.state == "claimed" and c.job["attempt"] == n
        expire(jobs, job["id"])
        store.recover_expired()
    row = store.get(job["id"])
    assert row["status"] == "failed" and row["error"]["code"] == "LEASE_EXPIRED"
    assert store.claim(job["id"], "w3").state == "not_queued"


def test_recover_skips_a_job_whose_worker_is_writing_back(jobs):
    """寫回期間持有 advisory lock：租約剛好過期也不會被接手（舊 worker 不和新 worker 同時寫回）。"""
    job, _ = cad()
    store.claim(job["id"], "w1")
    assert store.reserve_commit(job["id"], 1, "w1")
    expire(jobs, job["id"])
    entered, release = threading.Event(), threading.Event()

    def writer():
        with store.commit_lock(job["id"]):
            entered.set()
            release.wait(10)

    t = threading.Thread(target=writer)
    t.start()
    assert entered.wait(5)
    assert store.recover_expired() == []  # 被寫回鎖擋住
    assert store.get(job["id"])["status"] == "running"
    release.set()
    t.join()
    assert store.recover_expired() == [(job["id"], "requeued")]  # 鎖放掉後才會恢復


def test_recover_honours_a_pending_cancel(jobs):
    job, _ = cad()
    store.claim(job["id"], "w1")
    assert store.cancel(job["id"])[0] == "cancelling"
    expire(jobs, job["id"])
    assert store.recover_expired() == [(job["id"], "cancelled")]
    assert store.get(job["id"])["status"] == "cancelled"


# ----------------------------------------------------------------------------- 取消、停止、寫回
def test_cancel_queued_job_never_runs(jobs):
    job, _ = cad()
    result, row = store.cancel(job["id"])
    assert result == "cancelled" and row["status"] == "cancelled"
    assert store.claim(job["id"], "w1").state == "not_queued"
    assert store.cancel(job["id"])[0] == "already_cancelled"
    assert [e["type"] for e in store.events_after(job["id"], 0)][-1] == "cancelled"


def test_cancel_running_then_worker_stops_and_cannot_commit(jobs):
    job, _ = cad()
    store.claim(job["id"], "w1")
    assert store.cancel(job["id"])[0] == "cancelling"
    assert store.heartbeat(job["id"], 1, "w1")["cancel_requested"] is True
    assert store.reserve_commit(job["id"], 1, "w1") is False  # 取消在前：不能進入寫回
    with pytest.raises(store.LeaseLost):  # 沒有寫回資格不能標成功
        store.finish(job["id"], 1, "w1", "succeeded", result={"ok": True})
    store.finish(job["id"], 1, "w1", "cancelled", note="已取消")
    row = store.get(job["id"])
    assert row["status"] == "cancelled" and row["result"] is None


def test_cancel_after_commit_started_is_refused(jobs):
    job, _ = cad()
    store.claim(job["id"], "w1")
    assert store.reserve_commit(job["id"], 1, "w1")
    assert store.cancel(job["id"])[0] == "too_late"
    assert store.get(job["id"])["cancel_requested"] is False
    store.finish(job["id"], 1, "w1", "succeeded", result={"ok": True})
    assert store.cancel(job["id"])[0] == "finished"
    assert store.get(job["id"])["status"] == "succeeded"


def test_stop_is_not_cancel(jobs):
    job, _ = schedule()
    assert store.request_stop(job["id"])[0] == "not_running"  # 排隊中沒有求解可以停
    store.claim(job["id"], "w1")
    assert store.request_stop(job["id"])[0] == "stopping"
    flags = store.heartbeat(job["id"], 1, "w1")
    assert flags["stop_requested"] is True and flags["cancel_requested"] is False
    assert store.reserve_commit(job["id"], 1, "w1") is True  # 提前停止照常進入寫回
    assert store.request_stop(job["id"])[0] == "too_late"
    store.finish(job["id"], 1, "w1", "succeeded", result={"run_status": "stopped"})
    assert store.get(job["id"])["status"] == "succeeded"


@pytest.mark.parametrize("round_", range(25))
def test_cancel_and_commit_race_has_exactly_one_winner(jobs, round_):
    """取消與進入寫回同時發生：不是「寫回成功且已取消」，也不是兩邊都失敗，一定二選一。"""
    job, _ = cad(part="mfg-001")
    store.claim(job["id"], "w1")
    out = {}
    barrier = threading.Barrier(2)

    def do_cancel():
        barrier.wait()
        out["cancel"] = store.cancel(job["id"])[0]

    def do_commit():
        barrier.wait()
        out["commit"] = store.reserve_commit(job["id"], 1, "w1")

    ts = [threading.Thread(target=f) for f in (do_cancel, do_commit)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    row = store.get(job["id"])
    if out["commit"]:
        assert out["cancel"] == "too_late" and not row["cancel_requested"]
    else:
        assert out["cancel"] == "cancelling" and row["cancel_requested"]
        assert row["committing_attempt"] is None


def test_stale_attempt_cannot_reserve_commit(jobs):
    job, _ = cad()
    store.claim(job["id"], "w1")
    assert store.reserve_commit(job["id"], 7, "w1") is False
    assert store.reserve_commit(job["id"], 1, "other") is False


# ----------------------------------------------------------------------------- outbox
def test_dispatch_marks_sent_and_failures_back_off(jobs):
    a, _ = cad(part="mfg-001")
    b, _ = cad(part="mfg-002")
    sent = []

    def publish(job_id, queue):
        if job_id == b["id"]:
            raise ConnectionError("broker 不通")
        sent.append((job_id, queue))

    assert store.dispatch_pending(publish) == (1, 1)
    assert sent == [(a["id"], "gpu")]
    rows = {r["job_id"]: r for r in jobs.sql("SELECT * FROM job_outbox")}
    assert rows[a["id"]]["dispatched_at"] is not None
    bad = rows[b["id"]]
    assert bad["dispatched_at"] is None and bad["dispatch_attempts"] == 1
    assert "ConnectionError" in bad["last_error"] and bad["available_at"] > bad["created_at"]
    assert store.dispatch_pending(publish) == (0, 0)  # 退讓期間不重試
    jobs.sql("UPDATE job_outbox SET available_at = now() WHERE dispatched_at IS NULL")
    assert store.dispatch_pending(lambda j, q: sent.append((j, q))) == (1, 0)
    assert store.pending_outbox() == 0


def test_stalled_queued_jobs_are_dispatched_again(jobs):
    """broker 重啟後訊息遺失：已投遞但一直沒人領的 queued 工作會再投一次。"""
    job, _ = cad()
    store.dispatch_pending(lambda j, q: None)
    assert store.redispatch_stalled(3600) == 0
    jobs.sql(
        "UPDATE jobs SET dispatched_at = now() - interval '2 hours' WHERE id = %s", (job["id"],)
    )
    assert store.redispatch_stalled(3600) == 1
    assert store.redispatch_stalled(3600) == 0  # 已經有一則等待投遞的
    assert store.pending_outbox() == 1


# ----------------------------------------------------------------------------- 事件與清理
def test_event_ids_are_consecutive_and_resumable(jobs):
    job, _ = cad()
    store.claim(job["id"], "w1")
    for i in range(5):
        store.add_event(job["id"], 1, "w1", "progress", {"n": i}, {"n": i})
    events = store.events_after(job["id"], 0)
    assert [e["seq"] for e in events] == list(range(1, 8))
    assert [e["seq"] for e in store.events_after(job["id"], 4)] == [5, 6, 7]
    assert store.events_after(job["id"], 7) == []
    assert store.get(job["id"])["progress"] == {"n": 4}


def test_purge_removes_old_finished_jobs_only(jobs):
    old, _ = cad(part="mfg-001")
    live, _ = cad(part="mfg-002")
    store.cancel(old["id"])
    jobs.sql("UPDATE jobs SET finished_at = now() - interval '30 days' WHERE id = %s", (old["id"],))
    assert store.purge(7) == [(old["id"], "cad")]
    assert store.get(old["id"]) is None and store.get(live["id"]) is not None
    assert (
        jobs.sql("SELECT count(*) AS n FROM job_events WHERE job_id = %s", (old["id"],))[0]["n"]
        == 0
    )


def test_database_down_is_reported_as_unavailable(jobs_env, monkeypatch):
    monkeypatch.setenv("JOBS_DATABASE_URL", "postgresql://artrag@127.0.0.1:1/none")
    config.get_settings.cache_clear()
    db.close()
    try:
        with pytest.raises(db.JobsUnavailable):
            store.get("cad_0000000000000000")
        assert db.ping() is False
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()
        db.close()


def test_cad_jobs_per_owner_are_capped(jobs, settings):
    settings(jobs_max_active_per_owner=2)
    cad(part="mfg-001")
    cad(part="mfg-002")
    with pytest.raises(AppError) as e:
        cad(part="mfg-003")
    assert e.value.status == 429 and e.value.code == "JOBS_LIMIT_REACHED"
    cad(owner="planner", part="mfg-003")  # 上限是每個帳號各自算
    first = jobs.sql("SELECT id FROM jobs WHERE owner_id = 'manager' ORDER BY created_at LIMIT 1")
    store.cancel(first[0]["id"])  # 取消一個就空出一個
    cad(part="mfg-004")
