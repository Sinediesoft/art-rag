"""照片建檔（docs/adr/013）：前處理、沒有標準模型的零件、建檔流程（圖紙、畫作）、收錄與還原。

讀標題欄用假的生成端（不呼叫真模型）；收錄寫到暫存的 kb/，重建索引換成假的。
"""

import json
from datetime import date
from pathlib import Path

import pytest
from conftest import as_account
from PIL import Image

from app.analysis import page
from app.core.config import REPO_ROOT, get_settings
from app.rag.chunking import build_part_chunks
from app.rag.kb import artwork_problems, part_problems
from app.repositories.index_store import get_store
from app.services import intake_service

PHOTOS = REPO_ROOT / "eval" / "drawing_photos"
PAINTINGS = REPO_ROOT / "eval" / "photos"
UNKNOWN = PHOTOS / "unknown" / "unknown-01.png"  # 和知識庫同版面、但沒收錄的圖紙（六角支柱）
UNKNOWN_ART = PAINTINGS / "unknown" / "unknown-01.jpg"  # 知識庫沒有的畫


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def upload(client, path: Path) -> str:
    mime = "image/png" if path.suffix == ".png" else "image/jpeg"
    r = client.post("/api/v1/images", files={"file": (path.name, path.read_bytes(), mime)})
    assert r.status_code == 200, r.text
    return r.json()["image_id"]


# ---------------------------------------------------------------- 前處理
def test_blur_score_separates_blurry_photos_in_both_domains():
    """同一個門檻：圖紙、畫作的模糊照都在 0.30 以上；
    低對比的水墨清楚照（拉普拉斯變異數只有 125，舊的指標會擋掉）在以下。"""
    score = lambda p: page.blur_score(Image.open(p))  # noqa: E731
    assert score(PHOTOS / "known" / "mfg-001__blur.jpg") > 0.30
    assert score(PAINTINGS / "known" / "aic-27992__blur.jpg") > 0.30
    assert score(PHOTOS / "known" / "mfg-001__tilt.jpg") < 0.30
    assert score(PAINTINGS / "known" / "npm-000001__dim.jpg") < 0.30


def test_kb_drawing_title_block_matches_layout():
    box = page.title_block_box(Image.open(REPO_ROOT / "kb" / "drawings" / "mfg-002.png"))
    assert box is not None
    assert all(abs(a - b) <= 2 for a, b in zip(box, page.TITLE_BOX, strict=True))


def test_clipped_title_block_is_rejected():
    """標題欄被裁掉一塊（外框貼到邊緣）就請重拍：模型會抄出半截的字（「容機械（虛構）」）。"""
    photo = Image.open(PHOTOS / "known" / "mfg-005__crop.jpg")
    rect = page.clean_drawing(page.rectify(photo, page.find_page(photo), (800, 970)))
    assert page.title_block_box(rect) is None


def test_spacing_between_latin_and_chinese():
    """模型常把「L 型」抄成「L型」：補回知識庫的寫法，料號、列舉值不動。"""
    norm = intake_service._normalize
    assert norm("name", "L型固定支架", "text") == "L 型固定支架"
    assert norm("material", "S45C中碳鋼", "text") == "S45C 中碳鋼"
    assert norm("company", "示範精密機械（虛構）", "text") == "示範精密機械（虛構）"
    assert norm("part_no", "brk-1001", "text") == "BRK-1001"
    assert norm("id_code", " NPM ", "text") == "npm"


def test_photo_is_rectified_onto_kb_layout():
    """反光的照片：拉正、去陰影後，標題欄外框對齊到知識庫版面。"""
    photo = Image.open(PHOTOS / "known" / "mfg-002__glare.jpg")
    quad = page.find_page(photo)
    assert quad is not None
    rect = page.clean_drawing(page.rectify(photo, quad, (800, 970)))
    fit = page.fit_layout(rect, page.title_block_box(rect))
    assert fit is not None and fit.size == (800, 970)
    box = page.title_block_box(fit)
    assert all(abs(a - b) <= 4 for a, b in zip(box, page.TITLE_BOX, strict=True))


# ---------------------------------------------------------------- 沒有標準模型的零件
def photo_part(**over) -> dict:
    part = {
        "id": "mfg-099",
        "part_no": "HEX-9001",
        "drawing_no": "D-25-0190",
        "revision": "A",
        "name": {"zh": "六角支柱"},
        "category": "支柱",
        "material": "S45C 中碳鋼",
        "density_g_cm3": 7.85,
        "company": "示範精密機械（虛構）",
        "owner": "生產技術課",
        "confidentiality": "內部",
        "drawing": "kb/drawings/mfg-099.png",
        "dimensions_mm": {"width": 40, "depth": 34.6, "height": 60},
        "descriptions": [
            {"topic": "建檔紀錄", "source": "照片建檔",
             "text": "這張圖紙以照片建檔：標題欄由本地模型讀取，主管確認後收錄。"}
        ],
        "intake": {"method": "photo", "date": "2026-10-02", "confirmed_by": "manager"},
    }  # fmt: skip
    return {**part, **over}


def test_schema_allows_photo_part_without_cad():
    assert part_problems(photo_part()) == []
    no_dims = photo_part()
    del no_dims["dimensions_mm"]
    assert any("dimensions_mm" in p for p in part_problems(no_dims))


def test_photo_part_chunk_uses_drawing_dimensions():
    import build_index

    item = {**photo_part(), "geometry": build_index.photo_geometry(photo_part())}
    meta = build_part_chunks(item)[0]
    assert "40 × 34.6 × 60 mm" in meta["text"] and "圖上標註" in meta["text"]
    assert "重量約" not in meta["text"] and meta["source"].endswith("（照片建檔）")


def test_next_version():
    bump = intake_service._pipeline("bump_version")
    assert bump.next_version("2026.10.3", date(2026, 10, 2)) == "2026.10.4"
    assert bump.next_version("2026.09.6", date(2026, 10, 2)) == "2026.10.1"


# ---------------------------------------------------------------- 建檔流程（圖紙）
def not_in_kb(image_id=None, top_k=None, part_ids=None, domain="mfg", uncertain=False):
    return {
        "route": {"domain": domain, "margin": 0.3, "uncertain": uncertain},
        "drawing_result": (
            {"matched": False, "best_part_id": None, "results": []} if domain == "mfg" else None
        ),
        "artwork_result": (
            {"matched": False, "best_artwork_id": None, "results": []} if domain == "art" else None
        ),
    }


@pytest.fixture
def reads(monkeypatch):
    """LLM_MODE=real，但讀標題欄換成假的生成端，回傳指定的 JSON。"""

    def use(answer: dict):
        class Fake:
            model, strategy = "fake-vl", "hybrid"
            max_tokens = temperature = response_format = None
            usage = type("U", (), {"input_tokens": 1200, "output_tokens": 90})()

            async def stream(self, messages):
                assert messages[-1]["content"][0]["type"] == "image_url"
                yield json.dumps(answer, ensure_ascii=False)

        monkeypatch.setattr(get_settings(), "llm_mode", "real")
        monkeypatch.setattr(intake_service, "get_provider", lambda strategy: Fake())

    monkeypatch.setattr(intake_service, "identify_any", not_in_kb)
    return use


TITLE = {
    "company": "示範精密機械（虛構）",
    "name": "六角支柱",
    "part_no": "hex-9001",
    "drawing_no": "D-25-0190",
    "revision": "A",
    "material": "SCM440 鎖銅鋼",  # 罕見字讀錯，牌號是對的
    "confidentiality": "內部",
    "width": "40.0000",
    "depth": 34.64,
    "height": 60,
}


def start(client, path: Path = UNKNOWN, domain: str | None = None):
    body = {"image_id": upload(client, path), **({"domain": domain} if domain else {})}
    r = client.post("/api/v1/intake", json=body)
    assert r.status_code == 200
    events = parse_sse(r.text)
    draft = next((d for e, d in events if e == "draft"), None)
    return events, draft


def fields(draft: dict) -> dict:
    return {f["key"]: f for f in draft["fields"]}


def test_blurry_photo_is_rejected_before_the_model(client, reads):
    reads(TITLE)
    events, draft = start(client, PHOTOS / "known" / "mfg-003__blur.jpg")
    assert draft is None and events[-1][0] == "error"
    assert events[-1][1]["code"] == "INTAKE_TOO_BLURRY" and events[-1][1]["blur"] > 0.30
    assert not any(e == "token" for e, _ in events)


def test_known_drawing_is_not_intaken_again(client, monkeypatch):
    found = not_in_kb()
    found["drawing_result"].update(matched=True, best_part_id="mfg-002")
    monkeypatch.setattr(intake_service, "identify_any", lambda *a, **k: found)
    events, draft = start(client, PHOTOS / "known" / "mfg-002__glare.jpg")
    assert draft is None
    assert events[-1][1]["code"] == "INTAKE_ALREADY_IN_KB"
    assert events[-1][1]["part"]["id"] == "mfg-002"


def test_draft_normalizes_and_applies_rules(client, reads):
    reads(TITLE)
    events, draft = start(client)
    assert [d["stage"] for e, d in events if e == "stage"] == [
        "sharpness", "identify", "page", "read", "validate"
    ]  # fmt: skip
    f = fields(draft)
    assert f["part_no"]["value"] == "HEX-9001" and f["part_no"]["source"] == "Qwen3-VL"
    assert f["width"]["value"] == 40.0
    # 材料依牌號對照知識庫既有零件（mfg-003）：校正寫法、補密度
    assert f["material"]["value"] == "SCM440 鉻鉬鋼" and f["material"]["source"] == "規則"
    assert f["density_g_cm3"]["value"] == 7.85 and f["density_g_cm3"]["source"] == "規則"
    # 照片上沒有的欄位要人填
    assert f["category"]["status"] == "missing" and f["owner"]["status"] == "missing"
    assert f["surface"]["status"] == "empty"
    assert draft["can_commit"] is False and set(draft["blockers"]) == {"類別", "負責單位"}
    assert draft["egress"]["images"] == 0 and draft["domain"] == "mfg"
    assert client.get(draft["kb_image_url"]).status_code == 200


def test_rules_flag_wrong_values(client, reads):
    """模型猜錯的典型：機密等級填到隔壁格、料號和知識庫重複、尺寸不合理。"""
    reads({**TITLE, "confidentiality": "第一角法", "part_no": "BRK-1001", "height": 0})
    _, draft = start(client)
    f = fields(draft)
    assert f["confidentiality"]["status"] == "invalid"
    assert f["part_no"]["status"] == "invalid" and "mfg-001" in f["part_no"]["message"]
    assert f["height"]["status"] == "invalid"


def test_model_unavailable_still_gives_an_empty_draft(client, monkeypatch):
    from app.rag.providers import ProviderUnavailable

    class Down:
        model = "down"
        max_tokens = temperature = response_format = None

        async def stream(self, messages):
            raise ProviderUnavailable("連不上")
            yield ""

    monkeypatch.setattr(get_settings(), "llm_mode", "real")
    monkeypatch.setattr(intake_service, "get_provider", lambda strategy: Down())
    monkeypatch.setattr(intake_service, "identify_any", not_in_kb)
    _, draft = start(client)
    assert draft["extraction"]["error"].startswith("本地模型無法使用")
    assert fields(draft)["part_no"]["status"] == "missing"


# ---------------------------------------------------------------- 建檔流程（畫作）
@pytest.fixture
def art(monkeypatch):
    """路由判為畫作、知識庫沒有；畫作不讀照片，生成端被呼叫就算失敗。"""
    monkeypatch.setattr(intake_service, "identify_any", lambda *a, **k: not_in_kb(domain="art"))

    def never(strategy):
        raise AssertionError("畫作建檔不該呼叫生成端")

    monkeypatch.setattr(intake_service, "get_provider", never)


def test_blurry_painting_is_rejected(client, art):
    events, draft = start(client, PAINTINGS / "known" / "aic-27992__blur.jpg", domain="art")
    assert draft is None and events[-1][1]["code"] == "INTAKE_TOO_BLURRY"


def test_painting_draft_is_filled_by_hand(client, art):
    events, draft = start(client, UNKNOWN_ART, domain="art")
    assert [d["stage"] for e, d in events if e == "stage"] == ["sharpness", "identify", "validate"]
    assert draft["domain"] == "art" and draft["extraction"]["model"] is None
    f = fields(draft)
    assert all(x["source"] in (None, "規則") for x in f.values())  # 沒有模型讀的欄位
    assert {"畫名", "作者", "年代", "典藏單位", "典藏頁網址", "照片授權"} <= set(draft["blockers"])
    # 典藏單位還沒填：知識庫 ID 用預設代碼
    assert f["id_code"]["value"] == "photo" and f["id_no"]["value"] == "001"
    assert f["image_license"]["options"] == ["CC0", "公有領域", "CC BY 4.0"]
    assert {"基本資料", "典藏與授權", "介紹（選填）", "知識庫 ID"} <= {
        x["group"] for x in f.values()
    }
    assert client.get(draft["kb_image_url"]).status_code == 200


def test_painting_rules(client, art):
    _, draft = start(client, UNKNOWN_ART, domain="art")
    did = draft["draft_id"]
    # 典藏單位是知識庫已有的 → 來源代碼跟著既有畫作；館藏編號當編號；npm-000002 在 kb_staging 已用過
    r = client.put(
        f"/api/v1/intake/{did}",
        json={"values": {"collection": "國立故宮博物院", "source_id": "故畫 000002",
                         "image_license": "CC BY 4.0", "description": "太短了"}},
    )  # fmt: skip
    f = fields(r.json())
    assert f["id_code"]["value"] == "npm" and f["id_code"]["source"] == "規則"
    assert f["id_no"]["value"] == "000002" and f["id_no"]["status"] == "invalid"
    assert f["image_attribution"]["status"] == "missing" and f["image_attribution"]["required"]
    assert f["description"]["status"] == "invalid"
    assert f["description_source_url"]["status"] == "missing"
    # 已有的畫名＋作者 → 擋下；網址格式
    r = client.put(
        f"/api/v1/intake/{did}",
        json={"values": {"title": "谿山行旅圖", "artist": "范寬", "source_url": "npm.gov.tw"}},
    )
    f = fields(r.json())
    assert f["title"]["status"] == "invalid" and "npm-000001" in f["title"]["message"]
    assert f["source_url"]["status"] == "invalid"


def test_wrong_domain_and_known_painting(client, monkeypatch):
    monkeypatch.setattr(intake_service, "identify_any", lambda *a, **k: not_in_kb(domain="mfg"))
    events, _ = start(client, UNKNOWN_ART, domain="art")
    assert events[-1][1]["code"] == "INTAKE_WRONG_DOMAIN"
    found = not_in_kb(domain="art")
    found["artwork_result"].update(matched=True, best_artwork_id="npm-000001")
    monkeypatch.setattr(intake_service, "identify_any", lambda *a, **k: found)
    events, _ = start(client, UNKNOWN_ART, domain="art")
    assert events[-1][1]["code"] == "INTAKE_ALREADY_IN_KB"
    assert events[-1][1]["artwork"]["id"] == "npm-000001"


# ---------------------------------------------------------------- 修改與收錄
@pytest.fixture
def kb(tmp_path, monkeypatch):
    """收錄寫到暫存的 kb/；背景重建索引改成直接呼叫、假的重建。"""
    for d in ("parts", "drawings", "artworks", "images"):
        (tmp_path / d).mkdir()
    (tmp_path / "VERSION").write_text("2026.10.1\n", encoding="utf-8")
    monkeypatch.setattr(intake_service, "KB_DIR", tmp_path)
    monkeypatch.setattr(intake_service, "_start", lambda fn, *args: fn(*args))
    monkeypatch.setattr(intake_service, "_rebuild_index", lambda: None)
    monkeypatch.setattr(intake_service, "_in_index", lambda domain, item_id: True)
    return tmp_path


def ready_draft(client, reads) -> dict:
    reads(TITLE)
    _, draft = start(client)
    r = client.put(
        f"/api/v1/intake/{draft['draft_id']}",
        json={"values": {"category": "支柱", "owner": "生產技術課", "name_en": "Hex Standoff"}},
    )
    assert r.status_code == 200, r.text
    return r.json()


def commit_as_manager(client, draft_id: str):
    as_account(client, "manager")
    r = client.post(f"/api/v1/intake/{draft_id}/commit")
    as_account(client, "guest")
    return r


def test_human_edits_are_marked(client, reads):
    draft = ready_draft(client, reads)
    f = fields(draft)
    assert f["category"]["source"] == "人" and draft["can_commit"] is True
    # 人改了材料：密度依新的牌號重新補
    r = client.put(
        f"/api/v1/intake/{draft['draft_id']}", json={"values": {"material": "A6061-T6 鋁合金"}}
    )
    f = fields(r.json())
    assert f["material"]["source"] == "人" and f["density_g_cm3"]["value"] == 2.7


def test_only_manager_can_commit(client, reads, kb):
    draft = ready_draft(client, reads)
    as_account(client, "planner")
    r = client.post(f"/api/v1/intake/{draft['draft_id']}/commit")
    as_account(client, "guest")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PERMISSION_DENIED"
    assert list((kb / "parts").iterdir()) == []


def test_commit_writes_kb_and_bumps_version(client, reads, kb):
    draft = ready_draft(client, reads)
    r = commit_as_manager(client, draft["draft_id"])
    assert r.status_code == 200, r.text
    done = client.get(f"/api/v1/intake/{draft['draft_id']}").json()
    assert done["status"] == "done" and done["commit"]["error"] is None
    pid = done["item_id"]
    assert pid == r.json()["item_id"] and done["item_url"] == f"/drawings/{pid}"
    part = json.loads((kb / "parts" / f"{pid}.json").read_text(encoding="utf-8"))
    assert part_problems(part) == [] and "cad" not in part
    assert part["material"] == "SCM440 鉻鉬鋼" and part["name"]["en"] == "Hex Standoff"
    assert part["dimensions_mm"] == {"width": 40.0, "depth": 34.64, "height": 60.0}
    assert part["intake"]["confirmed_by"] == "manager"
    assert "part_no" in part["intake"]["fields_from_model"]
    assert "category" not in part["intake"]["fields_from_model"]
    assert (kb / "drawings" / f"{pid}.png").is_file()
    assert (kb / "VERSION").read_text(encoding="utf-8").strip() != "2026.10.1"
    # 收錄過的草稿不能再改
    r = client.put(f"/api/v1/intake/{draft['draft_id']}", json={"values": {"owner": "x"}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INTAKE_CLOSED"


def test_duplicate_check_only_looks_at_drawings_the_requester_can_see(client, monkeypatch):
    """查「知識庫是不是已經有」只在建檔人看得到的圖紙裡比（docs/adr/019）：
    業務看不到機密圖紙，辨識時不讀它們，也不會在「已收錄」訊息裡看到名稱。"""
    seen: list = []

    def fake(image_id, top_k=None, part_ids=None):
        seen.append(part_ids)
        return not_in_kb()

    monkeypatch.setattr(intake_service, "identify_any", fake)
    as_account(client, "sales_a")
    start(client, PHOTOS / "known" / "mfg-002__glare.jpg")
    as_account(client, "guest")
    assert seen == [{"mfg-004", "mfg-005"}]


def test_commit_rechecks_duplicates_within_the_managers_scope(client, reads, kb, monkeypatch):
    draft = ready_draft(client, reads)
    seen: list = []

    def dup(image_id, top_k=None, img=None, vec=None, part_ids=None):
        seen.append(part_ids)
        return {"matched": True, "best_part_id": "mfg-002", "results": []}

    monkeypatch.setattr(intake_service, "identify_drawing", dup)
    r = commit_as_manager(client, draft["draft_id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "INTAKE_ALREADY_IN_KB"
    assert seen == [{p["id"] for p in get_store().parts}]  # 主管看得到全部
    assert list((kb / "parts").iterdir()) == []


ART = {
    "title": "溪岸圖",
    "artist": "董源",
    "date_text": "五代",
    "medium": "絹本 設色",
    "collection": "大都會藝術博物館（The Met）",
    "source_id": "1977.78",
    "source_url": "https://www.metmuseum.org/art/collection/search/39668",
    "image_license": "CC0",
    "style_tags": "山水、五代",
}


def test_commit_painting_without_description(client, art, kb):
    """沒填介紹：系統依欄位寫一段「基本資料」（CC0、出處是典藏頁）；照片本身是知識庫的圖。"""
    _, draft = start(client, UNKNOWN_ART, domain="art")
    r = client.put(f"/api/v1/intake/{draft['draft_id']}", json={"values": ART})
    d = r.json()
    assert d["can_commit"] is True, d["blockers"]
    assert d["item_id"] == "met-197778"  # 大都會的畫用 met；館藏編號去掉符號
    r = commit_as_manager(client, draft["draft_id"])
    assert r.status_code == 200, r.text
    done = client.get(f"/api/v1/intake/{draft['draft_id']}").json()
    assert done["status"] == "done" and done["item_url"] == "/artworks/met-197778"
    a = json.loads((kb / "artworks" / "met-197778.json").read_text(encoding="utf-8"))
    assert artwork_problems(a) == []
    assert a["image"] == {"path": "kb/images/met-197778.jpg", "license": "CC0"}
    desc = a["descriptions"][0]
    assert desc["topic"] == "基本資料" and desc["license"] == "CC0"
    assert desc["source_url"] == ART["source_url"] and "董源" in desc["text"]
    assert a["style_tags"] == ["山水", "五代"] and a["intake"]["generated_description"] is True
    assert (kb / "images" / "met-197778.jpg").is_file()


def test_failed_index_rolls_back(client, reads, kb, monkeypatch):
    monkeypatch.setattr(intake_service, "_in_index", lambda domain, item_id: False)
    draft = ready_draft(client, reads)
    commit_as_manager(client, draft["draft_id"])
    d = client.get(f"/api/v1/intake/{draft['draft_id']}").json()
    assert d["status"] == "failed" and d["commit"]["error"] and d["can_commit"] is True
    assert list((kb / "parts").iterdir()) == [] and list((kb / "drawings").iterdir()) == []
    assert (kb / "VERSION").read_text(encoding="utf-8") == "2026.10.1\n"


def test_draft_not_found_and_discard(client, reads):
    assert client.get("/api/v1/intake/intake_0000000000000000").status_code == 404
    # 草稿 ID 格式不對一律當作找不到，不拿去組檔案路徑
    for path in ("/api/v1/intake/not-a-draft", "/api/v1/intake/x/photo.jpg"):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["error"]["code"] == "INTAKE_DRAFT_NOT_FOUND"
    draft = ready_draft(client, reads)
    did = draft["draft_id"]
    assert client.get(f"/api/v1/intake/{did}/secret.txt").status_code == 422
    assert client.delete(f"/api/v1/intake/{did}").status_code == 200
    assert client.get(f"/api/v1/intake/{did}").status_code == 404
