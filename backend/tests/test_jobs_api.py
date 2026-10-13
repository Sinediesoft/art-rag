"""背景工作的 API 合約與授權（真的 PostgreSQL＋Redis；worker 不啟動，只測 API 這一側）。

401／403／404 的政策、202＋job_id、Idempotency-Key、排程範圍唯一與輸入版本衝突、
事件串流（持久化遞增 id、Last-Event-ID 續接、終態結束、撤權即停）、取消、受保護的產物。
擋下之後用 spy 與資料庫列數證明沒有建立工作、沒有投遞到佇列。
"""

import shutil

import pytest
from conftest import DEFAULT_ACCOUNT, as_account
from fastapi.testclient import TestClient
from test_api import parse_sse

from app.core import config
from app.core.config import REPO_ROOT, get_settings
from app.jobs import broker, store
from app.main import app

PHOTO = REPO_ROOT / "kb/drawings/mfg-001.png"
KEY = {"Idempotency-Key": "test-key-0000000001"}


@pytest.fixture(autouse=True)
def api(jobs, client):
    yield jobs
    as_account(client, DEFAULT_ACCOUNT)


@pytest.fixture
def published(monkeypatch):
    sent = []
    monkeypatch.setattr(broker, "publish", lambda job_id, queue: sent.append((job_id, queue)))
    return sent


def code(r) -> str:
    return r.json()["error"]["code"]


def job_count(jobs) -> int:
    return jobs.sql("SELECT count(*) AS n FROM jobs")[0]["n"]


def upload(client) -> str:
    r = client.post("/api/v1/images", files={"file": (PHOTO.name, PHOTO.read_bytes(), "image/png")})
    return r.json()["image_id"]


# ------------------------------------------------------------------ 沒啟用、沒憑證、權限不足
def test_jobs_unavailable_when_not_enabled(client, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    config.get_settings.cache_clear()
    try:
        as_account(client, "planner")
        for path, body in (
            ("/api/v1/cad/jobs", {"part_id": "mfg-002"}),
            ("/api/v1/schedule/jobs", {}),
        ):
            r = client.post(path, json=body)
            assert r.status_code == 503 and code(r) == "JOBS_UNAVAILABLE", path
        assert client.get("/api/v1/jobs/cad_0123456789abcdef").status_code == 503
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()


def test_endpoints_need_credentials():
    c = TestClient(app)
    for method, path in (
        ("post", "/api/v1/cad/jobs"),
        ("post", "/api/v1/schedule/jobs"),
        ("get", "/api/v1/jobs"),
        ("get", "/api/v1/jobs/cad_0123456789abcdef"),
        ("get", "/api/v1/jobs/cad_0123456789abcdef/events"),
        ("post", "/api/v1/jobs/cad_0123456789abcdef/cancel"),
        ("get", "/api/v1/jobs/cad_0123456789abcdef/artifacts/input.png"),
    ):
        r = getattr(c, method)(path)
        assert r.status_code == 401, (path, r.status_code)


def test_cad_job_creation_is_authorized_before_anything_is_created(api, client, published):
    other = upload(client)  # 主管的照片
    cases = [
        ("guest", {"part_id": "mfg-002"}, 403, "DATA_SCOPE_DENIED"),  # 不能用工廠圖紙
        ("artist", {"part_id": "mfg-002"}, 403, "DATA_SCOPE_DENIED"),
        ("sales_a", {"part_id": "mfg-002"}, 403, "DATA_SCOPE_DENIED"),  # 機密圖紙看不到
        ("planner", {"part_id": "mfg-404"}, 404, "PART_NOT_FOUND"),
        ("planner", {"image_id": other}, 404, "IMAGE_NOT_FOUND"),  # 別人的照片
        ("planner", {}, 422, "VALIDATION_ERROR"),
    ]
    for who, body, status, error in cases:
        as_account(client, who)
        r = client.post("/api/v1/cad/jobs", json=body)
        assert r.status_code == status and code(r) == error, (who, body, r.text)
    assert job_count(api) == 0 and published == []  # 擋下後沒有任何工作、沒有投遞


@pytest.mark.parametrize("who", ["guest", "wh1", "sales_a", "artist", "manager"])
def test_schedule_job_needs_schedule_run(api, client, published, who):
    as_account(client, who)
    r = client.post("/api/v1/schedule/jobs", json={})
    assert r.status_code == 403 and code(r) in ("PERMISSION_DENIED", "DATA_SCOPE_DENIED"), who
    assert job_count(api) == 0 and published == []


# ------------------------------------------------------------------ 202 合約與 Idempotency-Key
def test_cad_job_returns_202_with_job_id_and_urls(api, client, published):
    as_account(client, "planner")
    r = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}, headers=KEY)
    assert r.status_code == 202, r.text
    body = r.json()
    assert (
        body["status"] == "queued" and body["kind"] == "cad" and body["job_id"].startswith("cad_")
    )
    assert body["status_url"] == f"/api/v1/jobs/{body['job_id']}"
    assert body["events_url"] == body["status_url"] + "/events"
    assert body["idempotent_replay"] is False and body["delayed_dispatch"] is False
    assert published == [(body["job_id"], "gpu")]
    row = api.job(body["job_id"])
    assert row["owner_id"] == "planner" and row["resource"]["part_id"] == "mfg-002"
    # 佇列訊息裡沒有 JWT 或請求內容：訊息只有 job_id
    assert published[0][0] == body["job_id"]


def test_idempotency_key_replay_and_conflict(api, client, published):
    as_account(client, "planner")
    a = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}, headers=KEY)
    b = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}, headers=KEY)
    assert a.status_code == b.status_code == 202
    assert b.json()["job_id"] == a.json()["job_id"] and b.json()["idempotent_replay"] is True
    assert job_count(api) == 1 and len(published) == 1  # 重送不重複建立、不重複投遞
    r = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-001"}, headers=KEY)
    assert r.status_code == 409 and code(r) == "IDEMPOTENCY_KEY_REUSED"
    r = client.post(
        "/api/v1/cad/jobs", json={"part_id": "mfg-002"}, headers={"Idempotency-Key": "x"}
    )
    assert r.status_code == 422  # key 格式不對


def test_broker_down_still_accepts_the_job_but_marks_it_delayed(api, client, monkeypatch):
    """Redis 不通：工作已寫進資料庫與 outbox（202、queued、delayed_dispatch），恢復後再投遞。"""

    def down(job_id, queue):
        raise ConnectionError("Redis 連不上")

    monkeypatch.setattr(broker, "publish", down)
    as_account(client, "planner")
    r = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"})
    assert r.status_code == 202 and r.json()["delayed_dispatch"] is True
    job_id = r.json()["job_id"]
    assert api.job(job_id)["status"] == "queued" and store.pending_outbox() == 1
    sent = []
    monkeypatch.setattr(broker, "publish", lambda j, q: sent.append((j, q)))
    api.sql("UPDATE job_outbox SET available_at = now()")
    assert broker.maintenance_once()["dispatched"] == 1 and sent == [(job_id, "gpu")]


def test_schedule_job_202_busy_and_stale_input_version(api, client, published):
    as_account(client, "planner")
    overview = client.get("/api/v1/production/overview").json()
    version = overview["input_version"]
    assert version.startswith("sv1:") and overview["active_job"] is None
    r = client.post("/api/v1/schedule/jobs", json={"input_version": "sv1:stale"})
    assert r.status_code == 409 and code(r) == "SCHEDULE_INPUT_CHANGED" and job_count(api) == 0
    r = client.post("/api/v1/schedule/jobs", json={"input_version": version, "seconds": 5})
    assert r.status_code == 202 and r.json()["kind"] == "schedule"
    job_id = r.json()["job_id"]
    row = api.job(job_id)
    assert row["input_version"] == version and row["input_versions"]["version"] == version
    assert set(row["input_versions"]) >= {"work_orders", "routings", "machines", "calendar"}
    assert published == [(job_id, "schedule")]
    again = client.post("/api/v1/schedule/jobs", json={"seconds": 5})
    assert again.status_code == 409 and code(again) == "SCHEDULE_BUSY"
    assert job_count(api) == 1
    assert client.get("/api/v1/production/overview").json()["active_job"]["id"] == job_id


# ------------------------------------------------------------------ 讀取政策
def test_job_visibility_follows_the_resource_policy(api, client, published):
    as_account(client, "manager")
    kb_job = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}).json()["job_id"]
    photo = upload(client)
    photo_job = client.post("/api/v1/cad/jobs", json={"image_id": photo}).json()["job_id"]
    sched = None
    as_account(client, "planner")
    sched = client.post("/api/v1/schedule/jobs", json={}).json()["job_id"]
    expect = {
        # 知識庫圖紙來源：看得到該圖紙的帳號（機密：倉管、生管、主管）都能讀
        kb_job: {
            "manager": 200,
            "planner": 200,
            "wh1": 200,
            "sales_a": 404,
            "guest": 404,
            "artist": 404,
        },
        # 照片來源：只有擁有者（與被授權者）
        photo_job: {"manager": 200, "planner": 404, "wh1": 404, "sales_a": 404, "guest": 404},
        # 排程：能讀工廠資料庫的帳號
        sched: {"planner": 200, "manager": 200, "sales_a": 200, "guest": 404, "artist": 404},
    }
    for job_id, table in expect.items():
        for who, status in table.items():
            as_account(client, who)
            r = client.get(f"/api/v1/jobs/{job_id}")
            assert r.status_code == status, (job_id, who, r.status_code)
            if status == 404:
                assert code(r) == "JOB_NOT_FOUND"
        for path in ("events", "artifacts/input.png"):
            as_account(client, "guest")
            assert client.get(f"/api/v1/jobs/{job_id}/{path}").status_code == 404
    # 不存在和看不到是同一個回應
    as_account(client, "guest")
    a = client.get(f"/api/v1/jobs/{photo_job}")
    b = client.get("/api/v1/jobs/cad_ffffffffffffffff")
    assert (
        a.status_code == b.status_code == 404
        and a.json()["error"]["message"] == b.json()["error"]["message"]
    )
    assert client.get("/api/v1/jobs/not-a-job").status_code == 404
    # 擁有者授權照片之後，被授權的帳號讀得到照片來源的工作
    as_account(client, "manager")
    client.put(f"/api/v1/images/{photo}/acl", json={"accounts": ["planner"]})
    as_account(client, "planner")
    assert client.get(f"/api/v1/jobs/{photo_job}").status_code == 200
    as_account(client, "manager")
    client.put(f"/api/v1/images/{photo}/acl", json={"accounts": []})
    as_account(client, "planner")
    assert client.get(f"/api/v1/jobs/{photo_job}").status_code == 404  # 收回之後立刻失效


def test_job_list_only_shows_what_the_account_may_see(api, client, published):
    as_account(client, "manager")
    photo = upload(client)
    mine = client.post("/api/v1/cad/jobs", json={"image_id": photo}).json()["job_id"]
    kb = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-001"}).json()["job_id"]
    ids = lambda who, **q: {  # noqa: E731
        j["id"]
        for j in (as_account(client, who), client.get("/api/v1/jobs", params=q).json()["items"])[1]
    }
    assert ids("manager") == {mine, kb}
    assert ids("planner") == {kb}  # 別人照片來源的看不到
    assert ids("guest", kind="cad") == set()
    assert ids("manager", active=True, kind="cad") == {mine, kb}
    api.sql("UPDATE jobs SET status = 'cancelled', finished_at = now() WHERE id = %s", (kb,))
    assert ids("manager", active=True) == {mine}


# ------------------------------------------------------------------ 事件串流
def test_events_have_persistent_ids_and_resume_with_last_event_id(api, client, published):
    as_account(client, "planner")
    job_id = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}).json()["job_id"]
    claim = store.claim(job_id, "w1")
    for i in range(3):
        store.add_event(job_id, claim.job["attempt"], "w1", "progress", {"n": i}, {"n": i})
    store.finish(job_id, 1, "w1", "failed", error={"code": "X", "message": "壞了"})
    r = client.get(f"/api/v1/jobs/{job_id}/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    ids = [ln[4:].strip() for ln in r.text.splitlines() if ln.startswith("id:")]
    assert ids == [str(i) for i in range(1, 7)]  # created, started, progress×3, failed
    events = parse_sse("\n\n".join(b for b in r.text.split("\n\n") if "event:" in b))
    assert [e for e, _ in events] == [
        "created",
        "started",
        "progress",
        "progress",
        "progress",
        "failed",
    ]
    assert events[-1][1]["code"] == "X" and events[-1][1]["status"] == "failed"
    # 斷線重連：Last-Event-ID 之後的才會送，不重複
    r2 = client.get(f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": "4"})
    ids2 = [ln[4:].strip() for ln in r2.text.splitlines() if ln.startswith("id:")]
    assert ids2 == ["5", "6"]
    r3 = client.get(f"/api/v1/jobs/{job_id}/events?after=6")
    assert [ln for ln in r3.text.splitlines() if ln.startswith("id:")] == []  # 已結束：直接收尾
    # 輪詢備援拿到同樣的終態
    final = client.get(f"/api/v1/jobs/{job_id}").json()
    assert final["status"] == "failed" and final["error"] == {"code": "X", "message": "壞了"}
    assert final["last_event_id"] == 6


def test_event_stream_stops_when_permission_is_revoked(api, live_api, published, monkeypatch):
    """每一輪輪詢都重驗目前權限：串流中途被撤權，送 error 事件並結束，不再送工作內容。"""
    import copy

    import httpx

    from app.services import identity

    h = live_api.headers("planner")
    job_id = httpx.post(
        f"{live_api.base}/api/v1/cad/jobs", json={"part_id": "mfg-002"}, headers=h
    ).json()["job_id"]
    with httpx.stream(
        "GET", f"{live_api.base}/api/v1/jobs/{job_id}/events", headers=h, timeout=20
    ) as r:
        assert r.status_code == 200
        it = r.iter_lines()
        seen = []
        for line in it:
            seen.append(line)
            if line.startswith("event: created"):
                break
        cfg = copy.deepcopy(config.get_access_config())
        cfg["clearance"]["planner"]["domains"] = ["art"]  # 撤掉工廠圖紙領域
        monkeypatch.setattr(identity, "get_access_config", lambda: cfg)
        rest = "\n".join(it)  # 下一輪輪詢重驗權限：送 error 事件後串流結束（迴圈會停）
    assert "PERMISSION_REVOKED" in rest and "mfg-002" not in rest


def test_event_stream_reconnect_with_last_event_id_while_running(api, live_api, published):
    """串流中途斷線：用最後收到的 id 重連，只補沒收到的事件，不重複；終態後串流自己結束。"""
    import httpx

    h = live_api.headers("planner")
    job_id = httpx.post(
        f"{live_api.base}/api/v1/cad/jobs", json={"part_id": "mfg-002"}, headers=h
    ).json()["job_id"]
    store.claim(job_id, "w1")
    got: list[str] = []
    with httpx.stream(
        "GET", f"{live_api.base}/api/v1/jobs/{job_id}/events", headers=h, timeout=20
    ) as r:
        for line in r.iter_lines():
            if line.startswith("id:"):
                got.append(line[3:].strip())
            if len(got) == 2:  # created、started
                break
    assert got == ["1", "2"]
    for i in range(3):
        store.add_event(job_id, 1, "w1", "progress", {"n": i}, {"n": i})
    store.finish(job_id, 1, "w1", "failed", error={"code": "X", "message": "壞了"})
    again: list[str] = []
    types: list[str] = []
    h2 = {**h, "Last-Event-ID": got[-1]}
    with httpx.stream(
        "GET", f"{live_api.base}/api/v1/jobs/{job_id}/events", headers=h2, timeout=20
    ) as r:
        for line in r.iter_lines():
            if line.startswith("id:"):
                again.append(line[3:].strip())
            if line.startswith("event:"):
                types.append(line[6:].strip())
    assert again == ["3", "4", "5", "6"]
    assert types == ["progress", "progress", "progress", "failed"]  # 沒有重複 created／started


# ------------------------------------------------------------------ 取消
def test_cancel_queued_job_by_owner(api, client, published):
    as_account(client, "planner")
    job_id = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}).json()["job_id"]
    r = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert r.status_code == 200 and r.json()["result"] == "cancelled"
    assert r.json()["job"]["status"] == "cancelled"
    again = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert again.status_code == 200 and again.json()["result"] == "already_cancelled"
    assert store.claim(job_id, "w1").state == "not_queued"  # 已取消的不會被領走


def test_job_lifecycle_is_audited(api, client, published):
    """建立、取消、提前結束都記一筆稽核（主管在 /audit 看得到）：只有識別碼與結果。"""
    as_account(client, "planner")
    sched = client.post("/api/v1/schedule/jobs", json={"seconds": 7}).json()["job_id"]
    store.claim(sched, "w1")
    client.post("/api/v1/schedule/stop")
    client.post(f"/api/v1/jobs/{sched}/cancel")
    cad = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}).json()["job_id"]
    as_account(client, "manager")
    items = client.get("/api/v1/audit").json()["items"]
    seen = {(i["op"], i["action"], i["ref_no"]) for i in items}
    assert ("schedule_job", "建立", sched) in seen
    assert ("schedule_job", "提前結束", sched) in seen
    assert ("schedule_job", "取消", sched) in seen
    assert ("cad_job", "建立", cad) in seen
    assert all(i["actor_id"] == "planner" for i in items if i["ref_no"] in (sched, cad))
    assert "seconds" not in str(items) and "mfg-002" not in str(items)  # 不記參數內容


def test_cancel_policy(api, client, published):
    as_account(client, "manager")
    cad = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}).json()["job_id"]
    as_account(client, "planner")
    sched = client.post("/api/v1/schedule/jobs", json={}).json()["job_id"]
    # 能讀但不是建立的人：3D 重建不能取消（403）；看不到的是 404
    as_account(client, "planner")
    assert client.post(f"/api/v1/jobs/{cad}/cancel").status_code == 403
    as_account(client, "sales_a")
    assert client.post(f"/api/v1/jobs/{cad}/cancel").status_code == 404
    # 排程：只有 schedule_run 能取消；主管、業務能讀但不能取消
    for who in ("manager", "sales_a", "wh1"):
        as_account(client, who)
        assert client.post(f"/api/v1/jobs/{sched}/cancel").status_code == 403, who
    as_account(client, "guest")
    assert client.post(f"/api/v1/jobs/{sched}/cancel").status_code == 404
    assert api.job(cad)["status"] == "queued" and api.job(sched)["status"] == "queued"
    as_account(client, "planner")
    assert client.post(f"/api/v1/jobs/{sched}/cancel").json()["result"] == "cancelled"


def test_cancel_running_and_finished_jobs(api, client, published):
    as_account(client, "planner")
    running = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-001"}).json()["job_id"]
    store.claim(running, "w1")
    r = client.post(f"/api/v1/jobs/{running}/cancel")
    assert r.status_code == 200 and r.json()["result"] == "cancelling"
    assert r.json()["job"]["cancel_requested"] is True and r.json()["job"]["status"] == "running"
    store.finish(running, 1, "w1", "cancelled")  # worker 收尾，GPU 名額釋放
    done = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}).json()["job_id"]
    store.claim(done, "w2")
    store.reserve_commit(done, 1, "w2")
    r = client.post(f"/api/v1/jobs/{done}/cancel")
    assert r.status_code == 409 and code(r) == "JOB_COMMITTING"
    store.finish(done, 1, "w2", "succeeded", result={"ok": True})
    r = client.post(f"/api/v1/jobs/{done}/cancel")
    assert r.status_code == 409 and code(r) == "JOB_ALREADY_FINISHED"


def test_schedule_stop_is_separate_from_cancel(api, client, published):
    as_account(client, "planner")
    job_id = client.post("/api/v1/schedule/jobs", json={}).json()["job_id"]
    assert client.post("/api/v1/schedule/stop").json() == {"ok": False}  # 還在排隊：沒有求解可以停
    store.claim(job_id, "w1")
    assert client.post("/api/v1/schedule/stop").json() == {"ok": True}
    row = api.job(job_id)
    assert row["stop_requested"] is True and row["cancel_requested"] is False  # 不是取消
    assert client.post("/api/v1/schedule/stop", json={"job_id": job_id}).json() == {"ok": True}
    for who in ("guest", "sales_a", "wh1", "artist", "manager"):
        as_account(client, who)
        assert client.post("/api/v1/schedule/stop", json={"job_id": job_id}).status_code == 403, who


# ------------------------------------------------------------------ 產物
def finish_with_artifacts(api, job_id, files=("input.png", "code.py", "model.stl")):
    store.claim(job_id, "w1")
    store.reserve_commit(job_id, 1, "w1")
    d = get_settings().cad_jobs_dir / job_id
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    rows = []
    for name in files:
        (d / name).write_bytes(b"data-" + name.encode())
        rows.append(
            {"name": name, "content_type": "text/plain", "size_bytes": 5, "sha256": "0" * 64}
        )
    store.finish(job_id, 1, "w1", "succeeded", result={"ok": True}, artifact_rows=rows)


def test_artifacts_are_protected_and_whitelisted(api, client, published):
    as_account(client, "planner")
    job_id = client.post("/api/v1/cad/jobs", json={"part_id": "mfg-002"}).json()["job_id"]
    r = client.get(f"/api/v1/jobs/{job_id}/artifacts/input.png")
    assert r.status_code == 409 and code(r) == "JOB_NOT_FINISHED"  # 還沒發布
    finish_with_artifacts(api, job_id)
    r = client.get(f"/api/v1/jobs/{job_id}/artifacts/code.py")
    assert r.status_code == 200 and r.content == b"data-code.py"
    assert (
        r.headers["x-content-type-options"] == "nosniff"
        and "no-store" in r.headers["cache-control"]
    )
    assert "attachment" not in r.headers.get("content-disposition", "")
    r = client.get(f"/api/v1/jobs/{job_id}/artifacts/code.py?download=true")
    assert "attachment" in r.headers["content-disposition"]
    for bad in ("summary.json", "access.json", "model.obj", "result.json"):
        assert client.get(f"/api/v1/jobs/{job_id}/artifacts/{bad}").status_code in (404, 422), bad
    # 路徑穿越：路由根本比對不到（沒有前端建置檔時 404；有的話是 SPA 首頁），絕不會讀到檔案
    r = client.get(f"/api/v1/jobs/{job_id}/artifacts/..%2F..%2F..%2Fetc%2Fpasswd")
    assert b"root:" not in r.content and not r.content.startswith(b"data-")
    assert client.get(f"/api/v1/jobs/{job_id}/artifacts/reproj.png").status_code == 404  # 沒有發布
    detail = client.get(f"/api/v1/jobs/{job_id}").json()
    assert {a["name"] for a in detail["artifacts"]} == {"input.png", "code.py", "model.stl"}
    assert detail["artifacts"][0]["url"].startswith(f"/api/v1/jobs/{job_id}/artifacts/")
    # 每次請求都重驗目前的授權
    for who, status in (("sales_a", 404), ("guest", 404), ("wh1", 200), ("manager", 200)):
        as_account(client, who)
        assert client.get(f"/api/v1/jobs/{job_id}/artifacts/model.stl").status_code == status, who
    # 舊網址（相容）和新網址同一套授權
    as_account(client, "planner")
    assert client.get(f"/api/v1/cad/jobs/{job_id}/model.stl").status_code == 200
    as_account(client, "sales_a")
    assert client.get(f"/api/v1/cad/jobs/{job_id}/model.stl").status_code == 404
