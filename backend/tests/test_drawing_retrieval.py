"""圖紙文字與照片查詢誤判「查無資料」（TASK-20261010-001）。

回報：上傳 mfg-006〈立式軸承座〉前視圖的局部截圖
（eval/drawing_retrieval/mfg-006__reported-screenshot.png），
工廠模組顯示「查無資料」。實測（真模型、生管）照片辨識正確（mfg-006，inlier 93、線條重合 1.0），
/agent/route 分派圖紙問答＋預設問句「這張圖紙的重點是什麼？」，失敗在 /chat 第 6 段地端生成閘門：
問句剩下「圖紙」「重點」兩個關鍵詞，覆蓋率 0/2 → 降級（機密圖紙不送 Jev，一定走地端閘門）。

另外用 gold set（make eval-drawing-retrieval）找到同一條鏈上的三個問題，一起在這裡回歸：
- 只截一個扁長的視圖（法蘭、軸、薄板的前視圖）時，幾何驗證的分布條件擋掉正確的對應
- 未收錄零件的局部（上視圖）線條重合 0.553，剛好過 0.55 被指成別張圖紙
- 圖號（D-24-0117）只靠 bge-m3 排不到第一名

mock 向量下 Chinese-CLIP 的分數沒有意義：照片的測試把相似度門檻設成 -1、領域路由固定判成圖紙，
ORB 幾何驗證與線條重合用真的照片和圖檔計算（和 test_security_pipeline 的以圖辨識測試同一個作法）。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.agent import guard
from app.core import config
from app.main import app
from app.rag import verify
from app.rag.prompt import artwork_card, part_card
from app.repositories.index_store import get_store
from app.services import chat_service, identity, search_service

ROOT = config.REPO_ROOT
REPORTED = ROOT / "eval/drawing_retrieval/mfg-006__reported-screenshot.png"
UNKNOWN = ROOT / "eval/drawing_photos/unknown/unknown-02.png"
DEFAULT_Q = "這張圖紙的重點是什麼？"  # agent_service.DEFAULT_QUESTIONS["drawing_qa"]
MFG_006 = ("mfg-006", "立式軸承座", "PBK-6006", "D-24-0618")
# 三視圖在圖紙版面（800×970）的位置，連尺寸標註（和 eval/run_drawing_retrieval_eval.py 一樣）
VIEW_FRONT = (380, 80, 740, 420)
VIEW_TOP = (380, 430, 740, 770)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def client_as(account_id: str) -> TestClient:
    token = identity.issue(identity.get_account(account_id))
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def upload(c: TestClient, path: Path | None = None, img: Image.Image | None = None) -> str:
    if img is not None:
        import io

        buf = io.BytesIO()
        img.save(buf, "PNG")
        data, name = buf.getvalue(), "crop.png"
    else:
        data, name = path.read_bytes(), path.name
    r = c.post("/api/v1/images", files={"file": (name, data, "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["image_id"]


def view_crop(path: Path, box: tuple[int, int, int, int]) -> Image.Image:
    img = Image.open(path).convert("RGB")
    s = img.width / 800
    crop = img.crop(tuple(round(v * s) for v in box))
    return crop.resize((round(crop.width * 1.3), round(crop.height * 1.3)), Image.LANCZOS)


@pytest.fixture
def drawing_photo(monkeypatch):
    """照片走真的 ORB＋線條重合；CLIP 門檻 -1、領域路由固定判成圖紙。記下讀了哪些知識庫圖紙。"""
    read: list[str] = []
    real_kb = verify.kb_features

    def kb_features(path, mtime, drawing=False):
        read.append(Path(path).stem)
        return real_kb(path, mtime, drawing=drawing)

    def spy(real):
        def wrapped(img, h, ref_path):
            read.append(Path(ref_path).stem)
            return real(img, h, ref_path)

        return wrapped

    class Mfg:
        domain, uncertain, margin = "mfg", False, 0.3

        def summary(self):
            return {
                "domain": "mfg",
                "margin": 0.3,
                "art_score": 0.5,
                "mfg_score": 0.8,
                "min_margin": 0.05,
                "uncertain": False,
            }

    monkeypatch.setattr(verify, "kb_features", kb_features)
    for name in ("ink_match", "ink_overlap"):  # 讀圖檔的兩個入口（修正前只有 ink_overlap）
        if hasattr(verify, name):
            monkeypatch.setattr(verify, name, spy(getattr(verify, name)))
    monkeypatch.setitem(config.get_models_config().drawing_retrieval, "image_threshold", -1.0)
    monkeypatch.setattr(search_service, "route", lambda vec: Mfg())
    return read


@pytest.fixture
def realistic_retrieval(monkeypatch):
    """第 3 段：取這張圖紙看得到的全部段落，相似度設成真的 bge-m3 對預設問句的實測值（0.37–0.41）。
    mock 向量是雜湊亂數、相似度幾乎是 0，第 4 段會把段落全當成無關，量不到第 6 段。"""

    def retrieve(question, artwork_id, part_id=None, scope=None):
        store = chat_service.get_store()
        owners = chat_service.visible_parts(scope) if part_id else None
        qvec = chat_service.embed_text([question])[0]
        hits = store.mfg.search_chunks(qvec, 99, part_id, owners=owners)
        out = [chat_service._source(i, h, store) for i, h in enumerate(hits)]
        return [{**s, "score": 0.41 - 0.01 * i} for i, s in enumerate(out)]

    monkeypatch.setattr(chat_service, "retrieve", retrieve)


def leaked(text: str, part=MFG_006) -> list[str]:
    return [x for x in part if x in text]


# ---------------------------------------------------------------- 回報的截圖：有權限
def test_reported_screenshot_is_identified_as_mfg_006(client, drawing_photo):
    c = client_as("planner")
    image_id = upload(c, REPORTED)
    for path in ("/api/v1/search/drawing", "/api/v1/search/any"):
        body = c.post(path, json={"image_id": image_id}).json()
        r = body if path.endswith("drawing") else body["drawing_result"]
        assert r["matched"] and r["best_part_id"] == "mfg-006", r
        top = r["results"][0]
        # 只截了前視圖：涵蓋比例低，要用局部照片的門檻（線條幾乎完全重合）
        assert top["coverage"] < r["partial_cover"] and top["overlap"] >= r["partial_min_overlap"]
        assert top["part"]["part_no"] == "PBK-6006" and top["part"]["drawing_no"] == "D-24-0618"


def test_reported_screenshot_photo_only_answers_instead_of_degrading(
    client, drawing_photo, realistic_retrieval
):
    """修正前：照片辨識對了，/chat 第 6 段把預設問句判成不可答 → 「查無資料」。"""
    c = client_as("planner")
    image_id = upload(c, REPORTED)
    r = c.post("/api/v1/agent/route", json={"question": "", "image_id": image_id}).json()
    assert r["photo"] == {
        "kind": "drawing",
        "id": "mfg-006",
        "label": "立式軸承座",
        "domain": "mfg",
    }
    assert (r["intent"], r["gate"], r["outcome"]) == ("drawing_qa", "direct", "pass")
    d = r["dispatch"]
    assert d["part_id"] == "mfg-006" and d["question"] == DEFAULT_Q and r["route_ticket"]
    assert r["auth"]["filter"]["doc_level"] == "機密"  # 第 1 段確認看得到（前端用它顯示圖紙）
    res = c.post(
        "/api/v1/chat",
        json={"question": DEFAULT_Q, "part_id": "mfg-006", "route_ticket": r["route_ticket"]},
    )
    events = parse_sse(res.text)
    src = next(x for e, x in events if e == "sources")
    done = next(x for e, x in events if e == "done")
    assert src["part_id"] == "mfg-006" and src["sources"]
    assert {s["part_id"] for s in src["sources"]} == {"mfg-006"}
    assert done["degraded"] is False
    # 七段都實際執行、都通過；第 2 段沿用交接票
    assert [(s["stage"], s["status"]) for s in done["pipeline"]] == [
        (n, "pass") for n in range(1, 8)
    ]
    assert "交接票" in done["pipeline"][1]["detail"]
    gate = next(s for s in done["pipeline"] if s["stage"] == 6)
    assert "概覽問題" in gate["detail"]


def test_photo_with_the_same_name_goes_straight_to_the_drawing(client, drawing_photo):
    """「立式軸承座」＋同一張圖紙的照片：文字沒有多說要做什麼，和只附照片一樣直接交給問答。
    修正前落在澄清按鈕（圖紙查找／問答／庫存三選一）。只有文字的名稱照舊要澄清（route_qa r44）。"""
    c = client_as("planner")
    image_id = upload(c, REPORTED)
    r = c.post("/api/v1/agent/route", json={"question": "立式軸承座", "image_id": image_id}).json()
    assert (r["intent"], r["gate"], r["outcome"]) == ("drawing_qa", "direct", "pass")
    assert r["dispatch"]["part_id"] == "mfg-006" and r["dispatch"]["question"] == "立式軸承座"
    assert r["router"]["detail"]["photo_named"] is True and r["route_ticket"]
    for text in ("立式軸承座", "PBK-6006", "D-24-0618"):
        alone = c.post("/api/v1/agent/route", json={"question": text}).json()
        assert alone["gate"] == "clarify" and alone["dispatch"]["part_id"] == "mfg-006"
        # 澄清後選「圖紙查找」：交給這張圖紙
        picked = c.post(
            "/api/v1/agent/route", json={"question": text, "forced_intent": "drawing_search"}
        ).json()
        assert picked["outcome"] == "pass" and picked["dispatch"]["part_id"] == "mfg-006"
        assert picked["auth"]["filter"]["doc_level"] == "機密"
    # 照片和文字講的不是同一件：照舊交給本地分流判斷，不直接分派
    other = c.post(
        "/api/v1/agent/route", json={"question": "連接法蘭", "image_id": image_id}
    ).json()
    assert other["router"]["detail"].get("photo_named") is None


def test_found_drawing_but_unanswerable_question_keeps_the_drawing(
    client, drawing_photo, realistic_retrieval
):
    """圖紙找到了、問題答不出來：第 1 段通過（文件看得到），第 6 段降級；
    sources 帶著這張圖紙，前端據此保留「找到圖紙」，不當成圖紙不存在。"""
    c = client_as("planner")
    q = "這張圖紙的包裝方式與出貨箱規格？"  # 不是要內部資料（第 2 段放行），但段落裡沒有
    res = c.post("/api/v1/chat", json={"question": q, "part_id": "mfg-006"})
    events = parse_sse(res.text)
    src = next(x for e, x in events if e == "sources")
    done = next(x for e, x in events if e == "done")
    stages = {s["stage"]: s["status"] for s in done["pipeline"]}
    assert (
        done["degraded"] is True
        and stages[1] == "pass"
        and stages[6] == "block"
        and 7 not in stages
    )
    assert src["part_id"] == "mfg-006" and src["sources"] == []
    assert done["tokens"] == {"input": 0, "output": 0}


# ---------------------------------------------------------------- 回報的截圖：沒有權限
@pytest.mark.parametrize("account", ["sales_a", "guest"])
def test_reported_screenshot_without_clearance_looks_like_an_unknown_drawing(
    client, drawing_photo, account
):
    """看不到機密圖紙的身分：和知識庫沒有的圖紙一樣，不讀 mfg-006 的特徵與圖檔、不回名稱與編號。"""
    c = client_as(account)
    visible = identity.visible_part_ids(identity.get_account(account), get_store().parts)
    hidden = upload(c, REPORTED)
    unknown = upload(c, UNKNOWN)
    drawing_photo.clear()
    a = c.post("/api/v1/agent/route", json={"question": "", "image_id": hidden})
    b = c.post("/api/v1/agent/route", json={"question": "", "image_id": unknown})
    assert set(drawing_photo) <= visible and "mfg-006" not in drawing_photo
    ra, rb = a.json(), b.json()
    assert (
        ra["photo"]
        == rb["photo"]
        == {"kind": "unknown", "id": None, "label": "知識庫中沒有這張圖紙", "domain": "mfg"}
    )
    for k in ("intent", "gate", "outcome", "dispatch", "route_ticket"):
        assert ra[k] == rb[k], k
    assert leaked(a.text) == []
    # /chat 只附照片：一樣是「知識庫中沒有這張圖紙」
    chat = c.post("/api/v1/chat", json={"question": "這是什麼零件？", "image_id": hidden})
    if account == "guest":
        assert chat.status_code in (200, 403)
    events = parse_sse(chat.text) if chat.status_code == 200 else []
    if events:
        assert events[0] == ("error", {**events[0][1], "code": "NOT_IN_KB"})
    assert leaked(chat.text) == []
    if account == "sales_a":
        r = c.post("/api/v1/search/drawing", json={"image_id": hidden})
        assert r.status_code == 200 and r.json()["matched"] is False and leaked(r.text) == []


# ---------------------------------------------------------------- 局部照片的幾何驗證與門檻
def test_thin_view_crop_is_recognised(client, drawing_photo):
    """只截一個扁長的視圖：法蘭的前視圖，正確的對應點垂直分布只有約 60 px。
    修正前分布條件要兩個方向都 ≥ 80 px，正確的對應被當成「擠在一小塊」擋掉。"""
    c = client_as("planner")
    image_id = upload(c, img=view_crop(ROOT / "kb/drawings/mfg-002.png", VIEW_FRONT))
    r = c.post("/api/v1/search/drawing", json={"image_id": image_id}).json()
    assert r["matched"] and r["best_part_id"] == "mfg-002", r["results"][:2]


def test_partial_crop_of_an_unknown_drawing_is_not_matched(client, drawing_photo):
    """未收錄零件（U 型槽鋼座）的上視圖局部：和 mfg-001 的線條重合約 0.55，
    修正前剛好過 0.55 被指成〈L 型固定支架〉；局部照片要 ≥ partial_min_overlap。"""
    c = client_as("planner")
    image_id = upload(c, img=view_crop(UNKNOWN, VIEW_TOP))
    r = c.post("/api/v1/search/drawing", json={"image_id": image_id}).json()
    assert r["matched"] is False and r["best_part_id"] is None, r["results"][:2]


def test_plausible_accepts_thin_spread_but_not_a_cluster():
    h = [[0.6, 0, 0], [0, 0.6, 0], [0, 0, 1]]
    import numpy as np

    h = np.array(h, dtype=float)
    thin = np.array([[100, 300], [380, 300], [240, 330], [120, 350]], dtype=float)  # 280×50
    cluster = np.array([[100, 300], [150, 310], [130, 340]], dtype=float)  # 50×40
    line = np.array([[100, 300], [400, 302], [250, 301]], dtype=float)  # 300×2
    assert verify._plausible(h, thin)
    assert not verify._plausible(h, cluster) and not verify._plausible(h, line)


# ---------------------------------------------------------------- 第 6 段：概覽問題
def _mfg_006_card() -> dict:
    p = get_store().get_part("mfg-006")
    return {"level": "機密", "text": part_card(p), "domain": "mfg"}


def _kept() -> list[dict]:
    return [
        {"title": "立式軸承座", "topic": "用途與組裝", "text": "本件用於支撐輸送滾筒軸的軸承。"}
    ]


@pytest.mark.parametrize(
    "question", [DEFAULT_Q, "請介紹這張圖紙", "這張圖紙的概要", "這個零件的重點是什麼？"]
)
def test_overview_of_a_specified_drawing_is_answerable(question):
    ok, detail = guard.local_answerable(question, _kept(), _mfg_006_card())
    assert ok and "概覽問題" in detail


def test_overview_of_a_specified_artwork_is_answerable():
    a = get_store().artworks[0]
    card = {"level": "公開", "text": artwork_card(a), "domain": "art"}
    assert guard.local_answerable("請介紹這幅畫", _kept(), card)[0]


@pytest.mark.parametrize(
    ("question", "kept", "card"),
    [
        # 概覽字眼以外的詞照樣要比對：「成本」不在段落與作品資料裡
        ("這張圖紙的成本重點是什麼？", "kept", "card"),
        # 沒有指定文件：不能用概覽規則
        (DEFAULT_Q, "kept", None),
        # 指定了文件但沒有段落通過第 4、5 段
        (DEFAULT_Q, None, "card"),
    ],
)
def test_overview_rule_does_not_open_the_gate_otherwise(question, kept, card):
    ok, _ = guard.local_answerable(
        question, _kept() if kept else [], _mfg_006_card() if card else None
    )
    assert not ok


def test_overview_terms_only_drop_overview_words_and_document_kinds():
    assert guard.overview_terms(DEFAULT_Q) == set()
    assert guard.overview_terms("這張圖紙的成本重點") == {"成本"}
    assert guard.overview_terms("立式軸承座的重點") == guard.terms("立式軸承座")


# ---------------------------------------------------------------- 文字查詢：名稱、料號、圖號
PARTS = {
    "mfg-001": ("L 型固定支架", "BRK-1001", "D-24-0117"),
    "mfg-002": ("連接法蘭", "FLG-2002", "D-24-0203"),
    "mfg-003": ("階梯傳動軸", "SFT-3003", "D-24-0311"),
    "mfg-004": ("步進馬達安裝板", "MTP-4004", "D-24-0425"),
    "mfg-005": ("T 型槽螺帽", "TSN-5005", "D-24-0502"),
    "mfg-006": ("立式軸承座", "PBK-6006", "D-24-0618"),
}


@pytest.mark.parametrize("account", ["planner", "sales_a"])
def test_text_search_puts_the_named_drawing_first(client, account):
    """問句寫了名稱、料號或圖號：那張圖紙排第一名（看得到時）；看不到的和不存在一樣，不出現在結果。
    修正前只有 bge-m3：真模型下 D-24-0117、D-24-0311、D-24-0502 第一名都是別張。"""
    c = client_as(account)
    visible = identity.visible_part_ids(identity.get_account(account), get_store().parts)
    for pid, terms in PARTS.items():
        for q in (*terms, f"找{terms[0]}的圖紙"):
            res = c.get("/api/v1/search/parts", params={"q": q}).json()
            ids = [x["part"]["id"] for x in res["results"]]
            if pid in visible:
                assert ids[0] == pid and res["results"][0]["exact"], (q, ids)
            else:
                assert pid not in ids and not any(x["exact"] for x in res["results"]), (q, ids)
                # query 是使用者自己打的字，不算；其他欄位不能有這張圖紙的 ID、名稱、料號、圖號
                rest = json.dumps({**res, "query": ""}, ensure_ascii=False)
                assert leaked(rest, (pid, *terms)) == []


def test_exact_parts_ignore_spacing_and_width():
    parts = get_store().parts
    assert search_service.exact_parts("L型固定支架", parts) == {"mfg-001"}
    assert search_service.exact_parts("ｐｂｋ－６００６", parts) == {"mfg-006"}
    assert search_service.exact_parts("法蘭", parts) == set()  # 只寫一部分：交給語意檢索
