"""色彩分析的索引、段落、出處與 API（docs/adr/010）。conftest 已用 mock embedding 建好索引。

用到 store.art 的測試要帶 client：後端啟動時才會載入索引，單獨跑這個檔案時 store.art 是空的，
迴圈會一個都沒檢查就通過。
"""

import json

from app.core.config import REPO_ROOT, get_models_config
from app.repositories.index_store import Hit, get_store


def test_index_has_colors_and_colormaps(client):
    store = get_store()
    assert store.art.items
    for a in store.art.items:
        c = a["colors"]
        assert 1 <= len(c["palette"]) <= 6
        assert abs(sum(p["share"] for p in c["palette"]) - 1) < 0.01
        assert (store.dir / "colormaps" / f"{a['id']}.png").exists()


def test_color_chunk_cites_system_calculation(client):
    store = get_store()
    chunks = {c["chunk_id"]: c for c in store.art.chunks}
    assert store.art.items
    for a in store.art.items:
        c = chunks[f"{a['id']}#color"]
        assert c["topic"] == "色彩分析" and c["source_url"] is None
        assert c["source"] == "系統計算：色彩分析（數位圖檔）" and c["license"] == "CC0"
        assert a["title"]["zh"] in c["text"] and "%" in c["text"]
        assert "可能與原作現況及展場光線下看到的顏色不同" in c["text"]


def test_chat_source_carries_label_for_color_chunk(client):
    from app.services.chat_service import _source

    store = get_store()
    chunk = next(c for c in store.art.chunks if c["chunk_id"].endswith("#color"))
    s = _source(0, Hit(chunk, 0.5), store)
    assert s["source_url"] is None and s["source_label"] == chunk["source"]
    other = next(c for c in store.art.chunks if c["chunk_id"].endswith("#meta"))
    assert "source_label" not in _source(0, Hit(other, 0.5), store)


def test_manifest_records_color_params(mock_env):
    store = get_store()
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    want = get_models_config().color_analysis.model_dump(mode="json")
    assert manifest["color_analysis"] == want
    assert store.check_manifest(manifest) == []
    manifest["color_analysis"]["n_colors"] = 8
    assert any("色彩分析" in p for p in store.check_manifest(manifest))


def test_kb_hash_ignores_computed_colors(client):
    """colors 是建索引算出來的，不能讓 /health 以為知識庫被改過。"""
    assert client.get("/api/v1/health").json()["index_consistent"] is True


def _upload(client, path) -> str:
    r = client.post("/api/v1/images", files={"file": (path.name, path.read_bytes(), "image/jpeg")})
    return r.json()["image_id"]


def test_artwork_colors_api(client):
    r = client.get("/api/v1/artworks/npm-000001/colors")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "original" and body["method"] == "lab-kmeans-v1"
    assert body["summary"] and body["notes"]
    assert "?v=" in body["map_url"]
    png = client.get(body["map_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"


def test_artwork_colors_404(client):
    r = client.get("/api/v1/artworks/nope-1/colors")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ARTWORK_NOT_FOUND"
    r = client.get("/api/v1/artworks/nope-1/colormap.png")
    assert r.status_code == 404


def test_photo_colors_api(client):
    image_id = _upload(client, REPO_ROOT / "eval/photos/unknown/unknown-05.jpg")
    body = client.get(f"/api/v1/images/{image_id}/colors").json()
    assert body["source"] == "photo" and 1 <= len(body["palette"]) <= 6
    assert any("照片" in n for n in body["notes"])
    png = client.get(body["map_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    again = client.get(f"/api/v1/images/{image_id}/colors").json()
    assert again["palette"] == body["palette"]  # 快取或重算，結果都一樣


def test_photo_colors_404(client):
    r = client.get("/api/v1/images/img_doesnotexist0/colors")
    assert r.status_code == 404 and r.json()["error"]["code"] == "IMAGE_NOT_FOUND"
