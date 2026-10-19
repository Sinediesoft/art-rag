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


# ---- 協調不可用：「未知」不等於「沒人在用」（架構審查 F3，0378910）
@pytest.fixture
def guarded(two_processes, monkeypatch):
    """記憶體 95%、兩個已載入的模型：共用推論服務上的 ortho2cad、本行程自己的 clip。
    釋放函式是替身，只記錄被要求卸載的模型，不碰真的模型服務。"""
    released: list[str] = []

    def model(key, shared):
        return memory_guard.ManagedModel(
            key, key, 4000, "test", lambda: True, lambda: released.append(key) or "ok",
            shared=shared,
        )  # fmt: skip

    monkeypatch.setattr(
        memory_guard, "_models", lambda: [model("ortho2cad", True), model("clip", False)]
    )
    monkeypatch.setattr(memory_guard.time, "sleep", lambda s: None)
    monkeypatch.setattr(memory_guard, "has_gpu", lambda: False)
    monkeypatch.setattr(memory_guard, "memory_percent", lambda: 95.0)
    settings = memory_guard.get_settings().model_copy(update={"memory_guard": True})
    monkeypatch.setattr(memory_guard, "get_settings", lambda: settings)
    return memory_guard.MemoryGuard(), released


@pytest.fixture
def grace(monkeypatch):
    """暫時改 Redis 重啟寬限期（測試環境預設 0）。"""

    def set_grace(seconds: float) -> None:
        monkeypatch.setenv("JOBS_MODELS_RESTART_GRACE_S", str(seconds))
        config.get_settings.cache_clear()

    yield set_grace
    monkeypatch.undo()
    config.get_settings.cache_clear()


def test_registry_outage_keeps_shared_models_and_reports_degraded(jobs, guarded, two_processes):
    """Redis 不通：其他行程的使用狀況「未知」，共用模型一律保留，只放本行程自己的模型；
    釋放紀錄與記憶體狀態都標出降級。恢復後回到依登記判斷。"""
    g, released = guarded
    other, me = two_processes
    other()
    coordination.publish_usage({"ortho2cad": 1})
    me()
    jobs.redis.stop()
    try:
        assert coordination.others_in_use() is None
        event = g.check("背景監控", set())
        assert released == ["clip"] and "ortho2cad" in event["kept"]
        assert event["coordination"] == "unavailable"
        assert coordination.status()["state"] == "unavailable"
        released.clear()
        g.check("手動釋放", set(), force=True)  # 手動釋放也不例外
        assert "ortho2cad" not in released
    finally:
        jobs.redis.start()
    # Redis 回來、登記也還在（這個測試的 Redis 沒有重啟寬限期）：依登記保留
    other()
    coordination.publish_usage({"ortho2cad": 1})
    me()
    released.clear()
    event = g.check("背景監控", set())
    assert "ortho2cad" not in released and event["coordination"] == "ok"
    # 確認閒置（沒有任何登記）才釋放共用模型
    other()
    coordination.publish_usage({})
    me()
    released.clear()
    g.check("背景監控", set())
    assert set(released) == {"ortho2cad", "clip"}


def test_redis_restart_window_is_unknown_until_everyone_reregisters(
    jobs, guarded, two_processes, grace
):
    """Redis 重啟會清掉所有登記：啟動未滿寬限期時，「沒有登記」不代表沒人在用——共用模型不卸載、
    狀態是 warming；寬限期過了（每個活著的行程都已續約）才依登記判斷。"""
    g, released = guarded
    other, me = two_processes
    other()
    coordination.publish_usage({"ortho2cad": 1})
    me()
    jobs.redis.stop()
    jobs.redis.start()  # 重啟：登記全沒了，worker 還在用 ortho2cad
    grace(3600)
    assert coordination.others_in_use() is None
    event = g.check("背景監控", set())
    assert released == ["clip"] and event["coordination"] == "warming"
    assert coordination.status()["state"] == "warming"
    assert "ortho2cad" not in released
    # worker 在寬限期內續約回來；寬限期過了以登記為準
    other()
    coordination.publish_usage({"ortho2cad": 1})
    me()
    grace(0)
    released.clear()
    g.check("背景監控", set())
    assert released == ["clip"] and coordination.status()["state"] == "ok"


# ---- 登記格式錯誤也是「未知」（Code Review F1，543a269）
BAD_REGISTRATIONS = [
    '"ortho2cad"',  # 合法 JSON 字串：直接 set() 會拆成 {"o", "r", "t", ...}
    "",  # 內容空白
    "   ",
    "not json",
    "null",
    "123",
    "true",
    '{"ortho2cad": 1}',  # 物件
    '["ortho2cad", 1]',  # 含非字串
    "[null]",
    '[""]',  # 空字串名稱
    '[["ortho2cad"]]',  # 巢狀
]


@pytest.mark.parametrize("raw", BAD_REGISTRATIONS)
def test_malformed_registration_is_unknown_and_keeps_shared_models(jobs, guarded, raw):
    """真實 Redis 裡另一個行程的登記不是模型名稱陣列：整次查詢是未知（None）、狀態 unavailable，
    共用模型不卸載（手動釋放也一樣），不會用拆字或部分結果宣稱 ok。"""
    g, released = guarded
    coordination.client().set(coordination.PREFIX + "broken-process", raw, ex=60)
    assert coordination.others_in_use() is None
    st = coordination.status()
    assert st["state"] == "unavailable" and st["detail"] == "模型使用登記格式錯誤"
    event = g.check("手動釋放", set(), force=True)
    assert released == ["clip"] and "ortho2cad" in event["kept"]
    assert event["coordination"] == "unavailable"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('["ortho2cad"]', {"ortho2cad"}),
        ('["ortho2cad", "qwen"]', {"ortho2cad", "qwen"}),
        ("[]", set()),
    ],
)
def test_well_formed_registrations_are_confirmed(jobs, guarded, raw, expected):
    """對照組：模型名稱陣列（含空陣列）是確認的結果、狀態 ok；登記在用的保留，確認閒置的才釋放。"""
    g, released = guarded
    coordination.client().set(coordination.PREFIX + "other-process", raw, ex=60)
    assert coordination.others_in_use() == expected
    assert coordination.status()["state"] == "ok"
    event = g.check("手動釋放", set(), force=True)
    assert event["coordination"] == "ok"
    assert ("ortho2cad" in released) == ("ortho2cad" not in expected)
    assert "clip" in released


def test_one_malformed_registration_spoils_the_whole_query(jobs, two_processes):
    """一筆壞登記就整次查詢未知：不回其他正常登記的部分結果；壞登記消失後恢復 ok。"""
    other, me = two_processes
    other()
    coordination.publish_usage({"qwen": 1})
    me()
    r = coordination.client()
    r.set(coordination.PREFIX + "broken-process", '"ortho2cad"', ex=60)
    assert coordination.others_in_use() is None
    r.delete(coordination.PREFIX + "broken-process")
    assert coordination.others_in_use() == {"qwen"}
    assert coordination.status()["state"] == "ok"


def test_registration_key_of_the_wrong_type_is_unknown(jobs):
    """登記的 key 不是字串型別（WRONGTYPE）：未知，不是略過。"""
    coordination.client().hset(coordination.PREFIX + "broken-process", "ortho2cad", "1")
    assert coordination.others_in_use() is None
    assert coordination.status()["state"] == "unavailable"


def test_registration_gone_between_scan_and_get_is_skipped(monkeypatch):
    """scan 列出之後才過期或結束登記（GET 回 None）：那個登記已經不在，不算格式錯誤。"""

    class Reply:
        def info(self, section):
            return {"uptime_in_seconds": 999999}

        def scan_iter(self, **_kw):
            yield coordination.PREFIX + "finished-process"
            yield coordination.PREFIX + "other-process"

        def get(self, key):
            return None if key.endswith("finished-process") else '["qwen"]'

    monkeypatch.setattr(coordination, "client", lambda: Reply())
    monkeypatch.setattr(coordination, "restart_grace_s", lambda: 0)
    assert coordination.others_in_use() == {"qwen"}
    assert coordination.status()["state"] == "ok"


def test_maintenance_loop_renews_the_usage_registration(jobs, two_processes):
    """各行程的維護執行緒定期續約「使用中的模型」：Redis 重啟清掉登記後，寬限期內自動登記回來。"""
    from app.jobs import broker

    other, me = two_processes
    g = memory_guard.guard
    g.enter("reconstruct", {"ortho2cad"})
    try:
        jobs.redis.flush()  # 登記消失（等同重啟）
        other()
        assert coordination.others_in_use() == set()
        me()
        m = broker.Maintenance()
        m.start()
        try:
            other()
            assert jobs.wait_until(
                lambda: "ortho2cad" in (coordination.others_in_use() or set()), 10, "重新登記"
            )
            me()
        finally:
            m.stop()
            m.join(5)
    finally:
        me()
        g.exit("reconstruct", {"ortho2cad"})


def test_memory_status_reports_the_coordination_state(jobs, client):
    r = client.get("/api/v1/memory")
    assert r.status_code == 200
    assert r.json()["coordination"] == {"mode": "redis", "state": "ok", "detail": None}
    jobs.redis.stop()
    try:
        c = client.get("/api/v1/memory").json()["coordination"]
        assert c["mode"] == "redis" and c["state"] == "unavailable"
    finally:
        jobs.redis.start()


# ------------------------------------------------------------------ 交接票
def test_handoff_tickets_are_shared_between_api_processes(jobs, monkeypatch):
    handoff.clear()
    token = handoff.issue("manager", "fp-1", "drawing_qa", "mfg-001", None, "jev", "jev", "req_1")
    handoff._tickets.clear()  # 另一個 API 行程的記憶體裡沒有這張票
    t = handoff.redeem(token, "manager", "fp-1", "mfg-001", None)
    assert t is not None and t.engine == "jev" and t.guard_engine == "jev"
    # 帳號、問句、對象任一不符都不認——換行程不會讓檢查變少
    assert handoff.redeem(token, "sales", "fp-1", "mfg-001", None) is None
    assert handoff.redeem(token, "manager", "fp-2", "mfg-001", None) is None
    assert handoff.redeem(token, "manager", "fp-1", "mfg-002", None) is None
    assert handoff.redeem("forged-token", "manager", "fp-1", "mfg-001", None) is None
    handoff.clear()
    assert handoff.redeem(token, "manager", "fp-1", "mfg-001", None) is None


def test_handoff_ticket_is_ignored_when_redis_is_down(jobs):
    token = handoff.issue("manager", "fp-1", "drawing_qa", "mfg-001", None, "jev", "jev", "req_1")
    handoff._tickets.clear()
    jobs.redis.stop()
    try:
        assert (
            handoff.redeem(token, "manager", "fp-1", "mfg-001", None) is None
        )  # 沒票＝/chat 重跑第 2 段
        t2 = handoff.issue("manager", "fp-1", "drawing_qa", "mfg-001", None, "jev", "jev", "req_2")
        assert handoff.redeem(t2, "manager", "fp-1", "mfg-001", None) is not None  # 同行程仍可核銷
    finally:
        jobs.redis.start()
        handoff.clear()


# ------------------------------------------------------------------ 舊入口與背景工作互斥
def test_legacy_schedule_solve_cannot_run_while_a_job_holds_the_scope(jobs, monkeypatch):
    monkeypatch.setenv("LEGACY_SYNC_JOBS", "on")
    config.get_settings.cache_clear()
    versions = schedule_service.current_input_versions()
    job, _ = store.create_job(
        kind="schedule", owner_id="manager", params={}, resource={}, scope="schedule:plant",
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
        kind="schedule", owner_id="manager", params={}, resource={}, scope="schedule:plant",
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
        as_account(client, "manager")
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
