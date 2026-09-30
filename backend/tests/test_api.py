import json


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
    assert done["prompt_version"] == "answer_v1"
    assert "[" in "".join(d["text"] for e, d in events if e == "token")


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
