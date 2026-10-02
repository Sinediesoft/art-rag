"""色彩分析的索引、段落、出處與 API（docs/adr/010）。conftest 已用 mock embedding 建好索引。

用到 store.art 的測試要帶 client：後端啟動時才會載入索引，單獨跑這個檔案時 store.art 是空的，
迴圈會一個都沒檢查就通過。
"""

import json
import re

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


def test_manifest_without_color_analysis_is_rejected(mock_env):
    """舊索引（色彩分析上線前建的）沒有 color_analysis：不能被當成一致而載入。"""
    store = get_store()
    manifest = json.loads(store.manifest_path.read_text(encoding="utf-8"))
    del manifest["color_analysis"]
    assert any("色彩分析參數與 models.yaml 不一致" in p for p in store.check_manifest(manifest))


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
    assert re.search(r"/colormap\.png\?v=[0-9a-f]{8}$", body["map_url"])
    png = client.get(body["map_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"


def test_colormap_cache_key_follows_color_content(client, monkeypatch):
    """?v= 由色彩分析本身算出：只改 color_analysis 參數重建索引（kb_hash 不變）時網址也要變。"""
    from app.services import color_service

    a = get_store().art.items[0]
    first = color_service.artwork_colors(a["id"])["map_url"]
    assert color_service.artwork_colors(a["id"])["map_url"] == first  # 內容不變，網址不變
    changed = {**a, "colors": {**a["colors"], "summary": a["colors"]["summary"] + "（改）"}}
    monkeypatch.setattr(color_service, "_artwork_or_404", lambda artwork_id: changed)
    assert color_service.artwork_colors(a["id"])["map_url"] != first


def test_artwork_colors_404(client):
    r = client.get("/api/v1/artworks/nope-1/colors")
    assert r.status_code == 404 and r.json()["error"]["code"] == "ARTWORK_NOT_FOUND"
    r = client.get("/api/v1/artworks/nope-1/colormap.png")
    assert r.status_code == 404


def test_artwork_colormap_missing_file_is_404_not_500(client):
    """索引有這幅畫、色塊圖檔卻不見了：FileResponse 會丟 RuntimeError（500），要回 404。"""
    png = get_store().dir / "colormaps" / "npm-000001.png"
    hidden = png.with_suffix(".hidden")
    png.rename(hidden)
    try:
        r = client.get("/api/v1/artworks/npm-000001/colormap.png")
    finally:
        hidden.rename(png)
    assert r.status_code == 404 and r.json()["error"]["code"] == "ARTWORK_NOT_FOUND"
    assert client.get("/api/v1/artworks/npm-000001/colormap.png").status_code == 200


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


def test_photo_colormap_404(client):
    r = client.get("/api/v1/images/img_doesnotexist0/colormap.png")
    assert r.status_code == 404 and r.json()["error"]["code"] == "IMAGE_NOT_FOUND"


def test_text_search_ignores_color_chunks(client):
    """色彩段落只給問答用（依色彩找畫不在範圍內，ADR 010），以文搜圖的文字排名不能看它。

    用色彩段落的原文當查詢：mock 向量相同、相似度是 1，沒排除的話這幅畫的 text_score 會是 1。"""
    from app.services import search_service

    store = get_store()
    chunk = next(c for c in store.art.chunks if c["chunk_id"].endswith("#color"))
    r = search_service.search_text(chunk["text"], top_k=len(store.art.items))
    hit = next(x for x in r["results"] if x["artwork"]["id"] == chunk["artwork_id"])
    assert hit["text_score"] < 0.5


def test_eval_runs_skips_color_runs(client):
    """GET /eval/runs 讀真的 eval/runs/：*-color.json 格式不同，沒排除會讓回應驗證失敗（500）。

    放在這個檔案是因為這個 500 是色彩評估的結果檔造成的。"""
    assert any((REPO_ROOT / "eval" / "runs").glob("*-color.json"))
    r = client.get("/api/v1/eval/runs")
    assert r.status_code == 200
