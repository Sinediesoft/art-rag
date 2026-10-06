import json

from app.core.config import REPO_ROOT

KB_PHOTOS = (
    (REPO_ROOT / "kb/drawings/mfg-006.png", "image/png"),
    (REPO_ROOT / "kb/images/npm-000001.jpg", "image/jpeg"),
)


def upload(client, path, mime: str) -> str:
    r = client.post("/api/v1/images", files={"file": (path.name, path.read_bytes(), mime)})
    return r.json()["image_id"]


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["index_consistent"] is True


def test_health_when_database_is_down(client, monkeypatch):
    """PostgreSQL 容器停了：ping 失敗就不查任何最近紀錄，狀態頁照樣回得出來（degraded、db=false）。
    查了會等連線池逾時（每次 5 秒，卡住整個後端）再 500。"""
    from app.api import routes

    class DownRepo:
        def ping(self):
            return False

        def __getattr__(self, name):
            def query(*args, **kwargs):
                raise AssertionError(f"資料庫連不上還呼叫 {name}()")

            return query

    monkeypatch.setattr(routes, "get_logs_repo", DownRepo)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] is False and body["status"] == "degraded"
    assert body["recent_chats"] == body["recent_routes"] == []


def test_artwork_detail_and_404(client):
    items = client.get("/api/v1/artworks").json()["items"]
    r = client.get(f"/api/v1/artworks/{items[0]['id']}")
    assert r.status_code == 200 and r.json()["descriptions"]
    r = client.get("/api/v1/artworks/nope-1")
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "ARTWORK_NOT_FOUND" and body["request_id"].startswith("req_")


def test_upload_rejects_wrong_type(client):
    r = client.post("/api/v1/images", files={"file": ("a.gif", b"GIF89a", "image/gif")})
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "IMAGE_TYPE_NOT_ALLOWED"


def test_chat_streams_sources_tokens_done(client):
    r = client.post(
        "/api/v1/chat",
        json={"question": "這幅畫的作者是誰？", "artwork_id": "npm-000001", "strategy": "hybrid"},
    )
    events = parse_sse(r.text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "sources" and kinds[-1] == "done" and "token" in kinds
    done = events[-1][1]
    assert done["prompt_version"] == "answer_v3"
    assert "[" in "".join(d["text"] for e, d in events if e == "token")


def test_search_any_returns_only_the_routed_domain(client):
    """領域路由：只回傳判定領域的辨識結果。
    mock embedding 下判斷本身沒有意義，只驗證格式與一致性。"""
    for path, mime in KB_PHOTOS:
        image_id = upload(client, path, mime)
        body = client.post("/api/v1/search/any", json={"image_id": image_id}).json()
        route = body["route"]
        assert route["domain"] in {"art", "mfg"} and route["min_margin"] > 0
        picked, other = (
            (body["artwork_result"], body["drawing_result"])
            if route["domain"] == "art"
            else (body["drawing_result"], body["artwork_result"])
        )
        assert picked["query_image_id"] == image_id and other is None


def test_chat_with_photo_only_goes_through_router(client):
    image_id = upload(client, *KB_PHOTOS[0])
    r = client.post("/api/v1/chat", json={"question": "這是什麼？", "image_id": image_id})
    event, data = parse_sse(r.text)[0]
    if event == "error":  # mock 向量多半過不了相似度門檻：要回報對應領域的「知識庫中沒有」
        assert data["code"] == "NOT_IN_KB"
    else:
        assert event == "sources" and data["route"]["domain"] in {"art", "mfg"}


def test_chat_rearrange_is_off_by_default_and_reported_when_on(client, monkeypatch):
    from app.services import chat_service

    # mock 向量是雜湊亂數，相似度都在門檻以下：已指定畫作時檢索只留最相關的 1 段，
    # 只有 1 段時篩選直接跳過。這裡改成不設門檻、取這幅畫的前 3 段，篩選才會交給生成端判斷
    def top3(question, artwork_id, part_id=None, levels=None):
        store = chat_service.get_store()
        qvec = chat_service.embed_text([question])[0]
        hits = store.art.search_chunks(qvec, 3, owner_id=artwork_id)
        return [chat_service._source(i, h, store) for i, h in enumerate(hits)]

    monkeypatch.setattr(chat_service, "retrieve", top3)
    body = {"question": "畫家的簽名藏在哪裡？", "artwork_id": "npm-000001", "strategy": "hybrid"}
    off = parse_sse(client.post("/api/v1/chat", json=body).text)[0][1]
    assert off["rearrange"] is None
    on = parse_sse(client.post("/api/v1/chat", json={**body, "rearrange": True}).text)[0][1]
    # mock 生成端的輸出不是「1,3」格式 → 退回原本的段落，但要回報篩選資訊
    info = on["rearrange"]
    assert info["candidates"] == 3 and info["fallback"]
    assert info["kept"] == len(on["sources"]) == 3


def test_unknown_strategy_without_fallback_errors(client):
    r = client.post(
        "/api/v1/chat",
        json={"question": "?", "artwork_id": "npm-000001", "strategy": "lora"},
    )
    # LLM_MODE=mock 時所有策略都走 mock；只驗證格式正確
    assert parse_sse(r.text)[-1][0] in {"done", "error"}


def test_spa_route_blocks_path_traversal(client):
    for path in ("/%2e%2e/%2e%2e/.env", "/..%2f..%2f.env", "/%2e%2e/%2e%2e/backend/app/main.py"):
        r = client.get(path)
        assert "API_KEY" not in r.text and "FastAPI" not in r.text


def test_feedback(client):
    r = client.post("/api/v1/feedback", json={"request_id": "req_x", "rating": "up"})
    assert r.json() == {"ok": True}
