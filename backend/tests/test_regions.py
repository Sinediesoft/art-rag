"""區域標註（docs/adr/029）：畫面上圈出的區域、綁在區域上的段落。"""

import copy

import pytest

from app.core.config import REPO_ROOT
from app.rag.chunking import build_chunks, region_bbox, where_on_painting
from app.rag.kb import artwork_problems, region_problems
from app.repositories.index_store import Hit, get_store
from app.services import chat_service

IMAGE = REPO_ROOT / "kb" / "images" / "npm-000001.jpg"  # 511×1024
SIG = [[0.8, 0.8], [0.95, 0.8], [0.95, 0.9], [0.8, 0.9]]

ART = {
    "id": "test-1",
    "title": {"zh": "測試畫"},
    "artist": {"zh": "某人"},
    "date_text": "2026",
    "collection": "測試館",
    "image": {"path": "kb/images/npm-000001.jpg", "license": "CC0"},
    "source_url": "https://example.org/",
    "descriptions": [
        {
            "lang": "zh",
            "topic": "落款",
            "region": "sig",
            "text": "這裡藏著畫家本人的簽名，字很小，要湊近畫面、很仔細才看得到。",
            "source": "藝術家本人標註",
            "license": "CC BY 4.0",
            "attribution": "某人",
        },
        {
            "lang": "zh",
            "topic": "構圖",
            "text": "整幅畫由上方的主峰與下方的樹林、山路組成，中間用雲霧隔開。",
            "source_url": "https://example.org/",
            "license": "CC0",
        },
    ],
    "regions": {
        "image_size": [511, 1024],
        "items": [{"id": "sig", "label": "簽名", "points": SIG}],
    },
}


def art() -> dict:
    return copy.deepcopy(ART)


def test_valid_region_has_no_problems():
    assert region_problems(art(), IMAGE) == []
    assert artwork_problems(art()) == []


def test_passage_must_point_to_an_existing_region():
    a = art()
    a["descriptions"][0]["region"] = "nope"
    assert any("nope" in p for p in region_problems(a))


def test_passage_with_region_needs_regions():
    a = art()
    del a["regions"]
    assert any("sig" in p for p in region_problems(a))


def test_region_without_passage_is_rejected():
    a = art()
    a["regions"]["items"].append({"id": "tree", "label": "樹", "points": SIG})
    assert any("tree" in p for p in region_problems(a))


def test_duplicate_region_id_is_rejected():
    a = art()
    a["regions"]["items"].append({"id": "sig", "label": "簽名 2", "points": SIG})
    assert any("重複" in p for p in region_problems(a))


def test_degenerate_polygon_is_rejected():
    a = art()
    a["regions"]["items"][0]["points"] = [[0.1, 0.1], [0.5, 0.5], [0.9, 0.9]]  # 三點同一條線
    assert any("面積" in p for p in region_problems(a))


def test_replaced_image_needs_regions_redrawn():
    a = art()
    a["regions"]["image_size"] = [1022, 2048]
    assert any("重新圈" in p for p in region_problems(a, IMAGE))
    assert region_problems(a) == []  # 不給圖（照片建檔收錄前，圖還沒寫）就不比尺寸


def test_schema_rejects_points_outside_the_image():
    a = art()
    a["regions"]["items"][0]["points"][0] = [1.2, 0.5]
    assert artwork_problems(a)


@pytest.mark.parametrize(
    ("center", "where"),
    [
        ((0.1, 0.1), "左上"),
        ((0.5, 0.1), "上方"),
        ((0.5, 0.5), "中央"),
        ((0.9, 0.5), "右側"),
        ((0.9, 0.9), "右下"),
    ],
)
def test_where_on_painting(center, where):
    assert where_on_painting(center) == where


def test_region_passage_carries_region_and_position():
    chunks = build_chunks(art())
    bound = [c for c in chunks if "region" in c]
    assert len(bound) == 1
    c = bound[0]
    assert region_bbox(SIG) == [0.8, 0.8, 0.95, 0.9]
    assert c["region"] == {
        "id": "sig",
        "label": "簽名",
        "points": SIG,
        "bbox": [0.8, 0.8, 0.95, 0.9],
    }
    # 方位詞由程式加在段落前面：純文字問「右下角」也搜得到，模型也知道這段講的是哪裡
    assert c["text"] == "〔畫面右下・簽名〕" + ART["descriptions"][0]["text"]
    plain = next(c for c in chunks if c["topic"] == "構圖")
    assert plain["text"] == ART["descriptions"][1]["text"]


def test_first_person_story_names_the_speaker():
    """藝術家寫「我第一次看到時…」：段落寫成「藝術家・甲說：「…」」，模型才不會把「我」當成畫家。"""
    a = art()
    a["descriptions"][0]["speaker"] = "藝術家・甲"
    assert artwork_problems(a) == []
    c = next(c for c in build_chunks(a) if "region" in c)
    assert c["text"] == "〔畫面右下・簽名〕藝術家・甲說：「" + ART["descriptions"][0]["text"] + "」"


def test_kb_artwork_detail_returns_regions(client):
    a = client.get("/api/v1/artworks/npm-000001").json()
    ids = {r["id"] for r in a["regions"]["items"]}
    assert ids and any(d.get("region") in ids for d in a["descriptions"])
    assert client.get("/api/v1/artworks/aic-27992").json()["regions"] is None


def test_chat_source_carries_region(mock_env):
    store = get_store()
    bound = next(c for c in store.chunks if c.get("region"))
    src = chat_service._source(0, Hit(bound, 0.5), store)
    assert src["region"]["label"] and len(src["region"]["bbox"]) == 4
    plain = next(c for c in store.chunks if not c.get("region"))
    assert "region" not in chat_service._source(0, Hit(plain, 0.5), store)
