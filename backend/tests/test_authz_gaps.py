"""已重現的三項授權缺口的正式回歸測試（TASK-20261013-001，docs/adr/036）。

修補前（用假檔案、有效訪客 JWT、mock solver 在隔離環境重現）：
1. 訪客可以觸發 /schedule/stop
2. 訪客知道圖片 ID 就能讀其他帳號上傳的照片（原圖、色彩、對位、辨識…）
3. 未產生 summary.json 的 3D 重建產物略過文件授權

每一項都有：無憑證 401、有權限不足的帳號被擋（並用 spy 證明擋下後沒有讀圖或停止求解）、
合法角色／擁有者對照組可用。
"""

import json
import shutil

import pytest
from conftest import DEFAULT_ACCOUNT, as_account
from fastapi.testclient import TestClient
from test_api import parse_sse

from app.core.config import REPO_ROOT, get_settings
from app.core.errors import AppError
from app.main import app
from app.repositories.logs_repo import get_logs_repo
from app.services import cad_access, cad_service, identity, schedule_service, uploads

DRAWING = REPO_ROOT / "kb/drawings/mfg-002.png"
PHOTO = REPO_ROOT / "kb/drawings/mfg-001.png"


@pytest.fixture(autouse=True)
def restore_identity(client):
    yield
    as_account(client, DEFAULT_ACCOUNT)


def upload(client, path=PHOTO) -> str:
    r = client.post("/api/v1/images", files={"file": (path.name, path.read_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["image_id"]


def code(r) -> str:
    return r.json()["error"]["code"]


# ------------------------------------------------------------------ 1. /schedule/stop
def test_schedule_stop_needs_credentials():
    r = TestClient(app).post("/api/v1/schedule/stop")
    assert r.status_code == 401 and code(r) == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    "account", ["guest", "wh1", "wh2", "sales_a", "sales_b", "artist", "manager"]
)
def test_schedule_stop_denied_without_schedule_run(client, monkeypatch, account):
    """沒有 schedule_run 的帳號（含主管：主管不自動取得生管的操作）不能停止求解，
    而且擋下之後 stop() 完全沒被呼叫。"""
    called = []

    async def spy():
        called.append(1)
        return True

    monkeypatch.setattr(schedule_service, "stop", spy)
    as_account(client, account)
    r = client.post("/api/v1/schedule/stop")
    assert r.status_code == 403, (account, r.text)
    assert code(r) in ("PERMISSION_DENIED", "DATA_SCOPE_DENIED")
    assert called == []


def test_schedule_stop_allowed_for_planner(client, monkeypatch):
    called = []

    async def spy():
        called.append(1)
        return True

    monkeypatch.setattr(schedule_service, "stop", spy)
    as_account(client, "planner")
    r = client.post("/api/v1/schedule/stop")
    assert r.status_code == 200 and r.json() == {"ok": True} and called == [1]


# ------------------------------------------------------------------ 2. 上傳照片的擁有者與 ACL
OWNER_ONLY_GETS = [
    "/api/v1/images/{id}",
    "/api/v1/images/{id}/colors",
    "/api/v1/images/{id}/colormap.png",
    "/api/v1/images/{id}/style",
    "/api/v1/images/{id}/align?target=artwork:npm-000001",
    "/api/v1/images/{id}/align.png?target=artwork:npm-000001",
]
OWNER_ONLY_POSTS = [
    ("/api/v1/search/image", lambda i: {"image_id": i}),
    ("/api/v1/search/any", lambda i: {"image_id": i}),
    ("/api/v1/search/drawing", lambda i: {"image_id": i}),
    ("/api/v1/chat", lambda i: {"question": "這是什麼？", "image_id": i}),
    ("/api/v1/agent/route", lambda i: {"question": "這是什麼？", "image_id": i}),
    ("/api/v1/cad/reconstruct", lambda i: {"image_id": i}),
]


@pytest.fixture
def no_reads(monkeypatch):
    """擋下的請求不能讀到圖檔：記錄 uploads.load／load_unchecked 成功讀出位元組的次數。"""
    reads = []
    for name in ("load", "load_unchecked"):
        real = getattr(uploads, name)

        def spy(*a, _real=real, _name=name, **k):
            out = _real(*a, **k)  # 授權失敗會在這裡丟例外，不算讀取
            reads.append(_name)
            return out

        monkeypatch.setattr(uploads, name, spy)
    return reads


def test_other_accounts_cannot_use_someones_upload(client, no_reads):
    image_id = upload(client)  # 主管上傳
    missing = "img_0000000000000000"
    for who in ("planner", "sales_a", "wh1", "artist", "guest"):
        as_account(client, who)
        base = client.get(f"/api/v1/images/{missing}")
        assert base.status_code == 404
        for tmpl in OWNER_ONLY_GETS:
            r = client.get(tmpl.format(id=image_id))
            # 不存在和不是你的是同一個回應（不透露別人的照片存不存在）
            assert r.status_code == 404 and code(r) == "IMAGE_NOT_FOUND", (who, tmpl, r.text)
            assert r.json()["error"]["message"] == base.json()["error"]["message"]
        for path, body in OWNER_ONLY_POSTS:
            r = client.post(path, json=body(image_id))
            assert r.status_code in (403, 404), (who, path, r.status_code)
            assert r.status_code == 403 or code(r) == "IMAGE_NOT_FOUND", (who, path, r.text)
    assert no_reads == []  # 擋下之後沒有任何一次讀取上傳的原檔


def test_owner_and_explicit_acl_can_use_the_upload(client):
    image_id = upload(client)  # 主管（擁有者）
    assert client.get(f"/api/v1/images/{image_id}").status_code == 200
    assert client.get(f"/api/v1/images/{image_id}/colors").status_code == 200
    as_account(client, "planner")
    assert client.get(f"/api/v1/images/{image_id}").status_code == 404
    # 非擁有者不能改 ACL，而且回的是和不存在一樣的 404
    r = client.put(f"/api/v1/images/{image_id}/acl", json={"accounts": ["planner"]})
    assert r.status_code == 404 and code(r) == "IMAGE_NOT_FOUND"
    as_account(client, "manager")
    r = client.put(f"/api/v1/images/{image_id}/acl", json={"accounts": ["planner", "manager"]})
    assert r.status_code == 200 and r.json()["accounts"] == ["planner"]  # 擁有者本人不用列
    as_account(client, "planner")
    assert client.get(f"/api/v1/images/{image_id}").status_code == 200
    assert client.get(f"/api/v1/images/{image_id}/colors").status_code == 200
    as_account(client, "sales_a")  # 沒被授權的還是不行
    assert client.get(f"/api/v1/images/{image_id}").status_code == 404
    as_account(client, "manager")  # 收回
    client.put(f"/api/v1/images/{image_id}/acl", json={"accounts": []})
    as_account(client, "planner")
    assert client.get(f"/api/v1/images/{image_id}").status_code == 404
    assert client.get(f"/api/v1/images/{image_id}/colors").status_code == 404


def test_acl_rejects_unknown_accounts(client):
    image_id = upload(client)
    r = client.put(f"/api/v1/images/{image_id}/acl", json={"accounts": ["nobody"]})
    assert r.status_code == 422


def test_legacy_upload_without_owner_is_denied_until_adopted(client):
    """舊資料（沒有擁有者）安全拒絕，不預設公開；管理者遷移後才能用。"""
    repo = get_logs_repo()
    image_id = "img_legacy000000001"
    shutil.copy(PHOTO, get_settings().uploads_dir / "legacy1.png")
    repo._exec(
        "INSERT INTO uploads (image_id, path, created_at, owner_id, acl)"
        " VALUES (?, ?, ?, NULL, NULL)",
        (image_id, "legacy1.png", repo.now()),
    )
    for who in ("manager", "planner", "guest"):
        as_account(client, who)
        assert client.get(f"/api/v1/images/{image_id}").status_code == 404, who
    assert repo.adopt_uploads("manager") >= 1
    as_account(client, "manager")
    r = client.get(f"/api/v1/images/{image_id}")
    assert r.status_code == 200
    as_account(client, "planner")
    assert client.get(f"/api/v1/images/{image_id}").status_code == 404


def test_derived_results_are_not_served_from_another_accounts_cache(client):
    """色彩、畫作卡、對位的結果以 image_id 快取：擁有者算過之後，其他帳號還是拿不到。"""
    image_id = upload(client)
    assert client.get(f"/api/v1/images/{image_id}/colors").status_code == 200
    assert client.get(f"/api/v1/images/{image_id}/colormap.png").status_code == 200
    assert client.get(f"/api/v1/images/{image_id}/style").status_code == 200
    as_account(client, "planner")
    for tmpl in OWNER_ONLY_GETS[1:]:
        assert client.get(tmpl.format(id=image_id)).status_code == 404, tmpl


def test_intake_and_batch_use_the_same_upload_policy(client):
    image_id = upload(client)
    as_account(client, "planner")
    events = parse_sse(
        client.post("/api/v1/intake", json={"image_id": image_id, "domain": "mfg"}).text
    )
    assert events[-1][0] == "error" and events[-1][1]["code"] == "IMAGE_NOT_FOUND"
    rows = parse_sse(
        client.post("/api/v1/batch/identify", json={"image_ids": [image_id], "domain": "mfg"}).text
    )
    row = next(d for e, d in rows if e == "row")
    assert row["status"] == "error" and row["item"] is None and row["closest"] is None


# ------------------------------------------------------------------ 3. 3D 重建的檔案授權
def make_job(job_id, *, access=None, summary=None, files=("input.png", "code.py")):
    d = get_settings().cad_jobs_dir / job_id
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for name in files:
        (d / name).write_bytes(b"x")
    if access is not None:
        (d / cad_access.ACCESS_FILE).write_text(json.dumps(access), encoding="utf-8")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def cad_status(client, who, job_id, name="input.png") -> int:
    as_account(client, who)
    return client.get(f"/api/v1/cad/jobs/{job_id}/{name}").status_code


def test_unfinished_cad_job_files_follow_the_part_policy(client):
    """還沒有 summary.json 的工作（執行中或失敗）一樣驗圖紙授權：機密圖紙來源的業務看不到。"""
    make_job("cad_a000000000000001", access={"owner_id": "manager", "part_id": "mfg-002"})
    assert cad_status(client, "manager", "cad_a000000000000001") == 200
    assert cad_status(client, "planner", "cad_a000000000000001", "code.py") == 200
    assert cad_status(client, "wh1", "cad_a000000000000001") == 200
    assert cad_status(client, "sales_a", "cad_a000000000000001") == 404  # 機密圖紙
    assert cad_status(client, "guest", "cad_a000000000000001") == 403  # 不能用工廠圖紙
    assert cad_status(client, "artist", "cad_a000000000000001") == 403


def test_photo_sourced_cad_job_needs_upload_access(client):
    image_id = upload(client)
    make_job("cad_a000000000000002", access={"owner_id": "manager", "image_id": image_id})
    assert cad_status(client, "manager", "cad_a000000000000002") == 200
    for who in ("planner", "sales_a", "wh1"):
        assert cad_status(client, who, "cad_a000000000000002") == 404, who
    as_account(client, "manager")
    client.put(f"/api/v1/images/{image_id}/acl", json={"accounts": ["planner"]})
    assert cad_status(client, "planner", "cad_a000000000000002") == 200
    assert cad_status(client, "wh1", "cad_a000000000000002") == 404


def test_photo_cad_job_creator_loses_access_when_the_share_is_revoked(client):
    """存取紀錄的 owner_id 是建立工作的人，不是照片擁有者：獲分享後建立工作，分享被撤銷就讀不到
    （架構審查 F1，0378910）。照片紀錄過期清掉後只剩記下的照片擁有者（source_owner_id）。"""
    as_account(client, "manager")
    image_id = upload(client)
    client.put(f"/api/v1/images/{image_id}/acl", json={"accounts": ["planner"]})
    access = {"owner_id": "planner", "image_id": image_id, "source_owner_id": "manager"}
    make_job("cad_a000000000000007", access=access)
    assert cad_status(client, "planner", "cad_a000000000000007") == 200
    as_account(client, "manager")
    client.put(f"/api/v1/images/{image_id}/acl", json={"accounts": []})
    assert cad_status(client, "planner", "cad_a000000000000007") == 404
    assert cad_status(client, "planner", "cad_a000000000000007", "code.py") == 404
    assert cad_status(client, "manager", "cad_a000000000000007") == 200
    get_logs_repo()._exec("DELETE FROM uploads WHERE image_id = ?", (image_id,))
    assert cad_status(client, "manager", "cad_a000000000000007") == 200
    assert cad_status(client, "planner", "cad_a000000000000007") == 404
    # 沒有記下照片擁有者的紀錄、照片也不在了：建立者讀不到
    make_job("cad_a000000000000008", access={"owner_id": "planner", "image_id": "img_gone"})
    assert cad_status(client, "planner", "cad_a000000000000008") == 404


def test_cad_job_json_summary_has_the_same_policy(client):
    summary = {"meta": {"part": {"id": "mfg-002"}}, "result": {"ok": True}, "done": {}}
    make_job(
        "cad_a000000000000003",
        access={"owner_id": "manager", "part_id": "mfg-002"},
        summary=summary,
    )
    as_account(client, "planner")
    assert client.get("/api/v1/cad/jobs/cad_a000000000000003").status_code == 200
    as_account(client, "sales_a")
    assert client.get("/api/v1/cad/jobs/cad_a000000000000003").status_code == 404
    as_account(client, "guest")
    assert client.get("/api/v1/cad/jobs/cad_a000000000000003").status_code == 403


def test_legacy_cad_folders_without_access_record(client):
    # 舊的已完成資料夾（只有摘要）：知識庫圖紙來源照圖紙規則可讀（相容讀取）
    kb = {"meta": {"part": {"id": "mfg-002"}, "image_id": None}, "result": {}, "done": {}}
    make_job("cad_a000000000000004", summary=kb, files=("input.png",))
    assert cad_status(client, "planner", "cad_a000000000000004") == 200
    assert cad_status(client, "sales_a", "cad_a000000000000004") == 404
    # 舊的照片來源：不知道擁有者 → 安全拒絕（主管也不行），要管理者遷移
    photo = {"meta": {"part": {"id": "mfg-001"}, "image_id": "img_old"}, "result": {}, "done": {}}
    make_job("cad_a000000000000005", summary=photo, files=("input.png",))
    assert cad_status(client, "manager", "cad_a000000000000005") == 404
    # 沒有存取紀錄也沒有摘要（舊的執行中資料夾）：沒有人能讀
    make_job("cad_a000000000000006", files=("input.png", "code.py"))
    for who in ("manager", "planner", "wh1"):
        assert cad_status(client, who, "cad_a000000000000006") == 404, who
    # 格式不對的 id 和不存在一樣
    assert cad_status(client, "manager", "cad_nope") == 404


def test_reconstruct_stream_records_access_before_any_other_file(client):
    """既有的 SSE 入口：資料夾建好後第一個寫入的是存取紀錄；在第一個事件之前就已經在了。"""
    import asyncio

    async def first_events():
        gen = cad_service.reconstruct_stream(
            "req-x", part_id="mfg-002", account=identity.get_account("planner")
        )
        try:
            return await anext(gen)
        finally:
            await gen.aclose()

    event = asyncio.run(first_events())
    job_id = json.loads(event.split("data: ", 1)[1])["job_id"]
    d = get_settings().cad_jobs_dir / job_id
    access = json.loads((d / cad_access.ACCESS_FILE).read_text(encoding="utf-8"))
    assert access["owner_id"] == "planner" and access["part_id"] == "mfg-002"
    assert cad_status(client, "planner", job_id) == 200  # 沒有摘要的進行中檔案，擁有者讀得到
    assert cad_status(client, "sales_a", job_id) == 404


# ---------------------------------------------------------------- 照片建檔草稿（照片的衍生資料）
def test_intake_draft_belongs_to_its_creator_and_the_manager(client, monkeypatch):
    """草稿裡有上傳照片的副本：建檔的人和能收錄的身分（主管）看得到，其他帳號 404。"""
    from app.services import intake_service

    draft_id = "intake_" + "a" * 16
    folder = get_settings().intake_dir / draft_id
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / "photo.jpg").write_bytes(b"x")
    draft = {
        "draft_id": draft_id,
        "domain": "mfg",
        "status": "draft",
        "created_at": "2026-10-13T00:00:00+00:00",
        "created_by": "planner",
        "image_id": "img_x",
        "proposed_id": "mfg-007",
        "item_id": None,
        "blur": 0.1,
        "route": {},
        "checks": [],
        "extraction": {"model": None, "ms": 0},
        "values": {},
        "sources": {},
        "notes": {},
        "commit": None,
    }
    (folder / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
    monkeypatch.setattr(intake_service, "_view", lambda d: {"draft_id": d["draft_id"]})
    for who, allowed in (
        ("planner", True),
        ("manager", True),
        ("sales_a", False),
        ("guest", False),
    ):
        as_account(client, who)
        assert (client.get(f"/api/v1/intake/{draft_id}/photo.jpg").status_code == 200) is allowed
        acc = identity.get_account(who)
        if allowed:
            assert intake_service.get_draft(draft_id, acc) == {"draft_id": draft_id}
        else:
            with pytest.raises(AppError) as e:
                intake_service.get_draft(draft_id, acc)
            assert e.value.status == 404
    as_account(client, "sales_a")
    assert client.delete(f"/api/v1/intake/{draft_id}").status_code == 404
    assert (folder / "draft.json").is_file()  # 別人刪不掉


def test_chat_with_someone_elses_photo_runs_no_stage(client, monkeypatch):
    """第 1 段的資源授權：別人的照片 404，後面的檢索、Jev、生成端都沒有被呼叫。"""
    from app.agent import jev
    from app.services import chat_service

    image_id = upload(client)
    calls = []
    monkeypatch.setattr(chat_service, "retrieve", lambda *a, **k: calls.append("retrieve") or [])
    monkeypatch.setattr(
        chat_service,
        "get_provider",
        lambda *a, **k: calls.append("generate") or pytest.fail("生成端"),
    )

    async def no_jev(*a, **k):
        calls.append("jev")
        raise jev.JevUnavailable("測試")

    monkeypatch.setattr(jev, "ask", no_jev)
    as_account(client, "planner")
    r = client.post("/api/v1/chat", json={"question": "這張圖紙是什麼？", "image_id": image_id})
    assert r.status_code == 404 and code(r) == "IMAGE_NOT_FOUND"
    assert calls == []
