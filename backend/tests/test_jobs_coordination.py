"""跨行程的協調（TASK-20261013-001，AC-14）：模型使用登記、交接票共用、舊入口與背景工作互斥、舊入口開關。"""

import asyncio

import pytest
from conftest import DEFAULT_ACCOUNT, as_account
from test_api import parse_sse

from app.agent import handoff
from app.core import config
from app.jobs import coordination, legacy_lease, store
from app.services import memory_guard, schedule_service


@pytest.fixture(autouse=True)
def restore(client):
    yield
    as_account(client, DEFAULT_ACCOUNT)


# ------------------------------------------------------------------ 模型使用登記
@pytest.fixture
def two_processes(jobs, monkeypatch):
    """用兩個不同的 HOLDER 模擬兩個行程共用同一個 Redis。"""
    mine = coordination.HOLDER

    def as_other():
        monkeypatch.setattr(coordination, "HOLDER", "other-process")

    def as_me():
        monkeypatch.setattr(coordination, "HOLDER", mine)

    return as_other, as_me


def test_models_in_use_by_another_process_are_visible(two_processes):
    other, me = two_processes
    other()
    coordination.publish_usage({"ortho2cad": 1, "qwen": 0})
    me()
    assert coordination.others_in_use() == {"ortho2cad"}
    g = memory_guard.MemoryGuard()
    assert g.local_busy() == set() and g.busy() == {"ortho2cad"}
    other()
    coordination.publish_usage({})  # 流程結束：登記消失
    me()
    assert coordination.others_in_use() == set()


def test_guard_does_not_unload_a_model_another_process_is_using(two_processes, monkeypatch):
    other, me = two_processes
    released: list[str] = []
    loaded = {"clip": True, "ortho2cad": True}

    def make(key):
        def release():
            released.append(key)
            return "ok"

        return memory_guard.ManagedModel(key, key, 4000, "test", lambda: loaded[key], release)

    monkeypatch.setattr(memory_guard, "_models", lambda: [make(k) for k in loaded])
    monkeypatch.setattr(memory_guard.time, "sleep", lambda s: None)
    monkeypatch.setattr(memory_guard, "has_gpu", lambda: False)
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: 90.0)

    class S:
        memory_guard = True
        memory_high_pct = 80.0

    monkeypatch.setattr(memory_guard, "get_settings", lambda: S())
    other()
    coordination.publish_usage({"ortho2cad": 1})  # worker 正在 3D 重建
    me()
    g = memory_guard.MemoryGuard()
    event = g.check("背景監控", set())
    assert released == ["clip"]  # API 行程判斷記憶體吃緊，也只釋放沒人用的
    assert "ortho2cad" in event["kept"]


def test_entering_and_leaving_a_flow_publishes_and_clears(jobs, monkeypatch):
    g = memory_guard.MemoryGuard()
    g.enter("reconstruct", {"ortho2cad"})
    monkeypatch.setattr(coordination, "HOLDER", "viewer")
    assert coordination.others_in_use() == {"ortho2cad"}
    monkeypatch.undo()
    g.exit("reconstruct", {"ortho2cad"})
    monkeypatch.setattr(coordination, "HOLDER", "viewer")
    assert coordination.others_in_use() == set()


# ------------------------------------------------------------------ 交接票
def test_handoff_tickets_are_shared_between_api_processes(jobs, monkeypatch):
    handoff.clear()
    token = handoff.issue("planner", "fp-1", "drawing_qa", "mfg-001", None, "jev", "jev", "req_1")
    handoff._tickets.clear()  # 另一個 API 行程的記憶體裡沒有這張票
    t = handoff.redeem(token, "planner", "fp-1", "mfg-001", None)
    assert t is not None and t.engine == "jev" and t.guard_engine == "jev"
    # 帳號、問句、對象任一不符都不認——換行程不會讓檢查變少
    assert handoff.redeem(token, "sales_a", "fp-1", "mfg-001", None) is None
    assert handoff.redeem(token, "planner", "fp-2", "mfg-001", None) is None
    assert handoff.redeem(token, "planner", "fp-1", "mfg-002", None) is None
    assert handoff.redeem("forged-token", "planner", "fp-1", "mfg-001", None) is None
    handoff.clear()
    assert handoff.redeem(token, "planner", "fp-1", "mfg-001", None) is None


def test_handoff_ticket_is_ignored_when_redis_is_down(jobs):
    token = handoff.issue("planner", "fp-1", "drawing_qa", "mfg-001", None, "jev", "jev", "req_1")
    handoff._tickets.clear()
    jobs.redis.stop()
    try:
        assert (
            handoff.redeem(token, "planner", "fp-1", "mfg-001", None) is None
        )  # 沒票＝/chat 重跑第 2 段
        t2 = handoff.issue("planner", "fp-1", "drawing_qa", "mfg-001", None, "jev", "jev", "req_2")
        assert handoff.redeem(t2, "planner", "fp-1", "mfg-001", None) is not None  # 同行程仍可核銷
    finally:
        jobs.redis.start()
        handoff.clear()


# ------------------------------------------------------------------ 舊入口與背景工作互斥
def test_legacy_schedule_solve_cannot_run_while_a_job_holds_the_scope(jobs, monkeypatch):
    monkeypatch.setenv("LEGACY_SYNC_JOBS", "on")
    config.get_settings.cache_clear()
    versions = schedule_service.current_input_versions()
    job, _ = store.create_job(
        kind="schedule", owner_id="planner", params={}, resource={}, scope="schedule:plant",
        request_id="r", input_version=versions["version"], input_versions=versions,
    )  # fmt: skip
    store.claim(job["id"], "w1")  # 背景工作執行中
    events = parse_sse(
        "".join(asyncio.run(_collect(schedule_service.solve_stream("req_legacy", 3))))
    )
    assert events[0][0] == "error" and events[0][1]["code"] == "SCHEDULE_BUSY"
    store.finish(job["id"], 1, "w1", "failed", error={"code": "X", "message": "x"})
    # 反過來：舊入口求解期間，背景工作領不到排程範圍
    lease = legacy_lease.acquire("req_x")
    assert isinstance(lease, legacy_lease.Lease)
    job2, _ = store.create_job(
        kind="schedule", owner_id="planner", params={}, resource={}, scope="schedule:plant",
        request_id="r2", input_version=versions["version"], input_versions=versions,
    )  # fmt: skip
    assert store.claim(job2["id"], "w2").state == "blocked"
    legacy_lease.release(lease)
    assert store.claim(job2["id"], "w2").state == "claimed"


async def _collect(gen):
    return [e async for e in gen]


# ------------------------------------------------------------------ 舊入口開關
@pytest.mark.parametrize("mode,enabled,allowed", [
    ("auto", False, True),   # 單機開發：沒有工作系統，舊入口照常
    ("auto", True, False),   # 有工作系統：舊入口關閉，改用背景工作
    ("on", True, True),
    ("off", False, False),
])  # fmt: skip
def test_legacy_in_process_entries_follow_the_setting(
    client, jobs, monkeypatch, mode, enabled, allowed
):
    monkeypatch.setenv("LEGACY_SYNC_JOBS", mode)
    if not enabled:
        monkeypatch.setenv("REDIS_URL", "")
    config.get_settings.cache_clear()
    try:
        as_account(client, "planner")
        for path, body in (
            ("/api/v1/cad/reconstruct", {"part_id": "mfg-002"}),
            ("/api/v1/schedule/solve", {"seconds": 3}),
        ):
            r = client.post(path, json=body)
            if allowed:
                assert r.status_code == 200 and r.headers["content-type"].startswith(
                    "text/event"
                ), path
            else:
                assert r.status_code == 409 and r.json()["error"]["code"] == "USE_JOBS_API", path
                assert (
                    "/jobs" in r.json()["error"]["message"]
                    or "jobs" in r.json()["error"]["message"]
                )
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()
