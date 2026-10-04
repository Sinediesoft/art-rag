"""建畫作卡推測（docs/adr/018）的大都會博物館評估集（Q-2026-10-04-04 第 1、2 項）。

從 Met 開放 API（CC0、不用帳號）抽兩類館藏，把館方紀錄轉成我們的正解：
1. 畫作：歐洲繪畫部、中國畫、浮世繪。題材看館方的畫面標籤（tags），媒材看媒材欄（medium），
   中國畫、浮世繪的風格大類看文化欄；歐洲繪畫館方沒有流派標籤，只能用年代判斷「大類和年代對不對得上」。
2. 不是畫：雕塑、瓷器、陶瓷、希臘陶瓶、古埃及雕像、盔甲、樂器、家具、老照片——
   「像畫作」把關要擋下的負例。

iMet（Kaggle FGVC6／7）的標籤就是從這些館藏紀錄整理的；直接用 API：
有「分類」欄分得出畫和器物、圖比較大、CC0。
- 清單與正解寫進 eval/met_set.json（commit，組員重跑得到同一批）；
- 圖存 data/met_eval/<objectID>.jpg（不 commit），run_style_eval.py --met 缺圖時自己補抓；
- 館藏原始紀錄快取在 data/met_eval/raw/，改了下面的對應規則用 --relabel 重算，不用重抓。

--train：另抽一份和評估集完全不重疊的訓練集 eval/met_train.json（Q-2026-10-04-04 第 3 項：
題材、媒材的線性分類頭，見 pipelines/train_style_head.py）。除了三類畫作，再補評估集裡太少的媒材：
美國繪畫、水彩、素描、粉彩（素描版畫部）與濕壁畫。

用法：python eval/make_met_set.py            # 抽樣、抓紀錄與圖（約 1,000 次請求，每秒最多 4 次）
      python eval/make_met_set.py --relabel  # 只依 eval/met_set.json 與快取重算正解
      python eval/make_met_set.py --train [--relabel]   # 訓練集（約 2,000 件）
"""

import argparse
import functools
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = "https://collectionapi.metmuseum.org/public/collection"
SET_PATH = ROOT / "eval" / "met_set.json"
TRAIN_PATH = ROOT / "eval" / "met_train.json"
DATA = ROOT / "data" / "met_eval"
RAW = DATA / "raw"
SEED = 0
TRAIN_SEED = 1
MIN_INTERVAL_S = 0.25  # Met 前面有 Imperva，太快會回 403 的 HTML

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度


@dataclass
class Category:
    key: str
    label: str
    kind: str  # painting｜other
    quota: int
    queries: list[dict] = field(default_factory=list)  # v1.1/search 的參數；department 為整個部門
    department: int | None = None  # /v1/objects?departmentIds=（整個部門的清單）
    keep: str = ""  # 篩選規則的名稱（見 keep()）


CATEGORIES = [
    Category("western", "歐洲繪畫", "painting", 220, department=11, keep="western"),
    Category(
        "chinese",
        "中國畫",
        "painting",
        60,
        [{"departmentId": 6, "q": "hanging scroll"}, {"departmentId": 6, "q": "handscroll"}],
        keep="chinese",
    ),
    Category(
        "ukiyoe",
        "浮世繪",
        "painting",
        60,
        [{"departmentId": 6, "q": "woodblock print"}],
        keep="ukiyoe",
    ),
    Category(
        "sculpture",
        "歐洲雕塑",
        "other",
        30,
        [{"departmentId": 12, "q": "sculpture"}],
        keep="sculpture",
    ),
    Category(
        "porcelain",
        "歐洲瓷器",
        "other",
        25,
        [{"departmentId": 12, "q": "porcelain"}],
        keep="ceramics",
    ),
    Category(
        "asian_ceramics",
        "亞洲陶瓷",
        "other",
        25,
        [{"departmentId": 6, "q": "bowl"}],
        keep="ceramics",
    ),
    Category(
        "greek_vase", "希臘陶瓶", "other", 25, [{"departmentId": 13, "q": "vase"}], keep="vase"
    ),
    Category(
        "egyptian",
        "古埃及雕像與器物",
        "other",
        25,
        [{"departmentId": 10, "q": "statue"}],
        keep="object",
    ),
    Category("armor", "盔甲", "other", 20, [{"departmentId": 4, "q": "armor"}], keep="armor"),
    Category(
        "instrument", "樂器", "other", 20, [{"departmentId": 18, "q": "instrument"}], keep="object"
    ),
    Category("furniture", "家具", "other", 20, [{"departmentId": 1, "q": "chair"}], keep="object"),
    Category(
        "photograph",
        "老照片",
        "other",
        25,
        [{"departmentId": 19, "q": "photograph"}],
        keep="photograph",
    ),
]

# 訓練集（--train）：和評估集不重疊，題材、媒材的線性分類頭用；
# 評估集裡太少的媒材（水彩、素描、粉彩、濕壁畫）另外補
TRAIN_CATEGORIES = [
    Category("western", "歐洲繪畫", "painting", 900, department=11, keep="western"),
    Category(
        "american",
        "美國繪畫",
        "painting",
        200,
        [{"departmentId": 1, "q": "oil on canvas"}],
        keep="american",
    ),
    Category(
        "chinese",
        "中國畫",
        "painting",
        250,
        [
            {"departmentId": 6, "q": "hanging scroll"},
            {"departmentId": 6, "q": "handscroll"},
            {"departmentId": 6, "q": "album leaf"},
        ],
        keep="chinese",
    ),
    Category(
        "ukiyoe",
        "浮世繪",
        "painting",
        200,
        [{"departmentId": 6, "q": "woodblock print"}],
        keep="ukiyoe",
    ),
    Category(
        "watercolor",
        "水彩",
        "painting",
        150,
        [{"departmentId": 9, "q": "watercolor"}],
        keep="watercolor",
    ),
    Category(
        "drawing",
        "素描",
        "painting",
        150,
        [{"departmentId": 9, "q": "chalk"}, {"departmentId": 9, "q": "graphite"}],
        keep="drawing",
    ),
    Category(
        "pastel",
        "粉彩",
        "painting",
        100,
        [{"departmentId": 9, "q": "pastel"}, {"departmentId": 1, "q": "pastel"}],
        keep="pastel",
    ),
    Category("fresco", "濕壁畫", "painting", 60, [{"q": "fresco"}], keep="fresco"),
]

# 器物類別排除這些分類：本身就是平面圖像（混進負例會把「該過的畫」算成「沒擋下」）
FLAT_CLASSES = (
    "Paintings", "Prints", "Drawings", "Miniatures", "Photographs", "Illustrated Books",
    "Calligraphy", "Manuscripts", "Textiles", "Enamels", "Pastels",
)  # fmt: skip


def keep(rule: str, o: dict) -> bool:
    cls, culture, medium = (
        o.get("classification") or "",
        o.get("culture") or "",
        (o.get("medium") or "").lower(),
    )
    if rule == "western":
        return cls in ("Paintings", "Miniatures", "Pastels & Oil Sketches on Paper")
    if rule == "chinese":
        return culture.startswith("China") and cls == "Paintings"
    if rule == "ukiyoe":
        return (
            culture.startswith("Japan")
            and cls == "Prints"
            and "woodblock print" in medium
            and "book" not in medium
        )
    if rule == "sculpture":
        return cls.startswith("Sculpture")
    if rule == "ceramics":
        return cls.startswith("Ceramics")
    if rule == "vase":
        return cls == "Vases"
    if rule == "armor":
        return cls.startswith("Armor")
    if rule == "photograph":
        return cls == "Photographs"
    if rule == "american":  # 美國裝飾藝術部的油畫，分類欄是空的
        return cls in ("", "Paintings") and re.search(r"\boil\b", medium) is not None
    if rule == "watercolor":
        return cls == "Drawings" and re.search(r"watercolou?r", medium) is not None
    if rule == "drawing":
        return (
            cls == "Drawings"
            and re.search(r"graphite|charcoal|chalk|pencil|\bink\b", medium) is not None
            and re.search(r"watercolou?r|gouache|pastel", medium) is None
        )
    if rule == "pastel":
        return "pastel" in medium and cls != "Prints"
    if rule == "fresco":  # 搜 fresco 多半是壁畫的草圖、版畫，只收媒材真的是濕壁畫的
        return "fresco" in medium and cls not in ("Drawings", "Prints")
    # object：分類常常是空的（古埃及、美國裝飾藝術部），所以也看媒材——
    # 「家具」那一類搜 chair 曾混進 6 幅油畫與素描（分類空白、媒材 Oil on wood、Graphite…）
    return not cls.startswith(FLAT_CLASSES) and not media_truth(medium)


# ---- 館方紀錄 → 我們的正解（標籤名稱對應 shared/models.yaml 的 style_guess） ----

# 風格大類的年代範圍（歐洲繪畫只能用年代判斷）：
# 館藏的年代區間和大類範圍（前後放寬 TOLERANCE 年）重疊就算對得上。
# 中世紀晚期（1300 年前後的金底畫）歸「文藝復興」最接近；1300 年以前的不評。
ERA_RANGES = {
    "文藝復興": (1300, 1600),
    "巴洛克與洛可可": (1600, 1790),
    "新古典、浪漫與寫實主義": (1760, 1870),
    "印象派一脈": (1860, 1910),
    "現代藝術": (1900, 2000),
}
TOLERANCE = 10

# 媒材：媒材欄（小寫）符合就列入可接受的答案，可以多個（Oil and tempera → 油畫、蛋彩畫）
MEDIA_RULES = [
    (r"woodblock", "木刻版畫"),
    (r"fresco", "濕壁畫"),
    (r"tempera", "蛋彩畫"),
    (r"\boil\b", "油畫"),
    (r"pastel", "粉彩畫"),
    (r"watercolou?r|gouache", "水彩畫"),
    (r"\bink\b", "水墨畫"),  # 木刻版畫的「ink and color on paper」不算（見 media_truth）
    (r"graphite|charcoal|chalk|pencil", "素描"),
]

# 題材：館方畫面標籤（Getty AAT 詞彙）→ 題材；一幅畫可以有多個可接受的答案（館方標籤本來就是多標籤）
GENRE_TAGS = {
    "Portraits": "肖像畫",
    "Self-portraits": "肖像畫",
    "Landscapes": "風景畫",
    "Mountains": "風景畫",
    "Forests": "風景畫",
    "Rivers": "風景畫",
    "Seascapes": "海景畫",
    "Waves": "海景畫",
    "Ships": "海景畫",
    "Harbors": "海景畫",
    "Still Life": "靜物畫",
    "Interiors": "室內畫",
    "Cityscapes": "城市街景",
    "Cities": "城市街景",
    "Streets": "城市街景",
}  # 不收地名（Paris 也可能是神話裡的帕里斯）、Men／Women（人物不等於肖像）
RELIGIOUS_TAGS = {
    "Christ", "Madonna and Child", "Virgin Mary", "Saints", "Angels", "Cross", "Crucifixion",
    "Annunciation", "Adoration of the Magi", "Holy Family", "Apostles", "Assumption of the Virgin",
    "Nativity", "Pietà", "Resurrection", "Moses", "Bible", "Old Testament", "New Testament",
    "Buddha", "Buddhism", "Bodhisattva", "Arhats", "Lohans",
}  # fmt: skip
MYTH_TAGS = {
    "Venus", "Cupid", "Apollo", "Diana", "Mars", "Jupiter", "Hercules", "Bacchus", "Goddess",
    "Gods", "Muses", "Nymphs", "Satyrs", "Mythology", "Greek Mythology", "Roman Mythology",
    "Mercury", "Minerva", "Juno",
    "Neptune", "Psyche", "Medusa", "Battles",
}  # fmt: skip
BIRD_FLOWER_TAGS = {
    "Birds",
    "Flowers",
    "Orchids",
    "Bamboo",
    "Plum Blossoms",
    "Peonies",
    "Lotus",
    "Chrysanthemums",
}


EAST_ASIAN = ("chinese", "ukiyoe")


def media_truth(medium: str, cat: str = "") -> list[str]:
    m = medium.lower()
    out = [name for pat, name in MEDIA_RULES if re.search(pat, m)]
    if "木刻版畫" in out and "水墨畫" in out:
        out.remove("水墨畫")
    if "水墨畫" in out and cat not in EAST_ASIAN:
        # 「水墨畫」指東亞水墨；西洋的 pen and ink、ink wash 是素描
        out = [x for x in out if x != "水墨畫"] + ([] if "素描" in out else ["素描"])
    return out


def genre_truth(tags: list[str], cat: str) -> list[str]:
    out = {GENRE_TAGS[t] for t in tags if t in GENRE_TAGS}
    if any(t.startswith("Saint ") or t in RELIGIOUS_TAGS for t in tags):
        out.add("宗教畫")
    if any(t in MYTH_TAGS for t in tags):
        out.add("歷史神話畫")
    if cat == "chinese":
        if "風景畫" in out:
            out |= {"山水畫", "風景畫"}  # 中國山水：兩個都算對
        if any(t in BIRD_FLOWER_TAGS for t in tags):
            out.add("花鳥畫")
    return sorted(out)


def era_truth(begin: int | None, end: int | None) -> list[str]:
    if begin is None:
        return []
    end = end if end is not None and end >= begin else begin
    return [
        g for g, (lo, hi) in ERA_RANGES.items() if begin <= hi + TOLERANCE and end >= lo - TOLERANCE
    ]


def truth_of(cat: Category, o: dict) -> dict:
    if cat.kind != "painting":
        return {}
    tags = [t["term"] for t in o.get("tags") or []]
    t: dict = {
        "media": media_truth(o.get("medium") or "", cat.key),
        "genre": genre_truth(tags, cat.key),
    }
    if cat.key == "chinese":
        t |= {"style_group": ["中國傳統繪畫"], "style": ["中國傳統繪畫"]}
    elif cat.key == "ukiyoe":
        t |= {"style_group": ["日本浮世繪"], "style": ["浮世繪"]}
    else:
        t["style_era"] = era_truth(o.get("objectBeginDate"), o.get("objectEndDate"))
    return {k: v for k, v in t.items() if v}


def entry(cat: Category, o: dict) -> dict:
    return {
        "id": o["objectID"],
        "category": cat.key,
        "kind": cat.kind,
        "title": o.get("title"),
        "artist": o.get("artistDisplayName") or None,
        "date": o.get("objectDate"),
        "classification": o.get("classification") or None,
        "culture": o.get("culture") or None,
        "medium": o.get("medium"),
        "tags": [t["term"] for t in o.get("tags") or []],
        "image_url": o["primaryImageSmall"],
        "object_url": o.get("objectURL"),
        "truth": truth_of(cat, o),
    }


# ---- 抓資料 ----


class Met:
    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=30, headers={"User-Agent": "art-rag-eval/0.1 (school project)"}
        )
        self.last = 0.0

    def _get(self, url: str, params: dict | None = None) -> httpx.Response | None:
        for attempt in range(4):
            wait = MIN_INTERVAL_S - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                r = self.client.get(url, params=params)
            except httpx.HTTPError as e:
                print(f"  連線失敗（{e.__class__.__name__}），{5 * (attempt + 1)} 秒後重試")
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 200 and (
                r.headers.get("content-type", "").startswith(("application/json", "image/"))
            ):
                return r
            print(
                f"  {r.status_code} {r.headers.get('content-type')}，{5 * (attempt + 1)} 秒後重試"
            )
            time.sleep(5 * (attempt + 1))
        return None

    def object(self, oid: int) -> dict | None:
        path = RAW / f"{oid}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        r = self._get(f"{API}/v1/objects/{oid}")
        if r is None:
            return None
        path.write_text(r.text, encoding="utf-8")
        return r.json()

    def ids(self, cat: Category) -> list[int]:
        if cat.department is not None:
            r = self._get(f"{API}/v1/objects", {"departmentIds": cat.department})
            # API 回傳的順序每次不同，排序後再抽樣才能重現
            return sorted(r.json()["objectIDs"]) if r else []
        out: list[int] = []
        for q in cat.queries:
            for offset in (0, 500):  # 每頁最多 500 筆；前 1,000 筆就夠抽
                r = self._get(
                    f"{API}/v1.1/search", q | {"hasImages": "true", "limit": 500, "offset": offset}
                )
                out += (r.json().get("objectIDs") or []) if r else []
        return sorted(set(out))

    def image(self, url: str, path: Path) -> bool:
        if path.exists():
            return True
        r = self._get(url)
        if r is None:
            return False
        path.write_bytes(r.content)
        return True


def build(categories: list[Category], seed: int, exclude: set[int]) -> list[dict]:
    met = Met()
    rng = random.Random(seed)
    entries: list[dict] = []
    seen = set(exclude)  # 訓練集排除評估集；同一件也不重複收進兩類
    for cat in categories:
        ids = met.ids(cat)
        rng.shuffle(ids)
        got, tried = [], 0
        for oid in ids:
            if len(got) >= cat.quota:
                break
            if oid in seen:
                continue
            tried += 1
            o = met.object(oid)
            if (
                not o
                or not o.get("isPublicDomain")
                or not o.get("primaryImageSmall")
                or not keep(cat.keep, o)
            ):
                continue
            e = entry(cat, o)
            if met.image(e["image_url"], DATA / f"{oid}.jpg"):
                got.append(e)
                seen.add(oid)
        print(
            f"{cat.key:<15} {cat.label}：{len(got)}/{cat.quota}"
            f"（看了 {tried} 筆，候選 {len(ids)} 筆）"
        )
        entries += got
    return entries


def own_met_ids() -> set[int]:
    """知識庫（kb、kb_staging）與 eval/style_truth.json 裡來自大都會的畫：
    訓練集不能收，否則評估時等於拿考題練習。"""
    keys = {
        p.stem for d in ("kb", "kb_staging") for p in (ROOT / d / "artworks").glob("met-*.json")
    }
    keys |= set(json.loads((ROOT / "eval" / "style_truth.json").read_text(encoding="utf-8")))
    return {int(k.split("-")[1]) for k in keys if re.fullmatch(r"met-\d+", k)}


def relabel(path: Path) -> list[dict]:
    old = json.loads(path.read_text(encoding="utf-8"))["items"]
    cats = {c.key: c for c in CATEGORIES + TRAIN_CATEGORIES}  # 正解只看 key 與 kind
    out = []
    for e in old:
        o = json.loads((RAW / f"{e['id']}.json").read_text(encoding="utf-8"))
        out.append(entry(cats[e["category"]], o))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--relabel", action="store_true", help="只依快取的館藏紀錄重算正解")
    ap.add_argument("--train", action="store_true", help="訓練集 eval/met_train.json（排除評估集）")
    args = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    path = TRAIN_PATH if args.train else SET_PATH
    if args.relabel:
        items = relabel(path)
    elif args.train:
        exclude = {e["id"] for e in json.loads(SET_PATH.read_text(encoding="utf-8"))["items"]}
        exclude |= own_met_ids()
        items = build(TRAIN_CATEGORIES, TRAIN_SEED, exclude)
    else:
        items = build(CATEGORIES, SEED, set())
    head = (
        "畫作卡推測（docs/adr/018）題材、媒材線性分類頭的訓練集（和評估集不重疊），"
        if args.train
        else "畫作卡推測（docs/adr/018）的大都會博物館評估集，"
    )
    out = {
        "_說明": (
            head + "由 eval/make_met_set.py 產生（Met 開放 API，CC0）。"
            "truth：style_group／style 只有中國畫與浮世繪有（看文化欄）；"
            "style_era 是歐洲繪畫依年代可接受的風格大類；"
            "genre 依館方畫面標籤、media 依媒材欄，每欄列所有可接受的答案；"
            "kind=other 的是「不是畫」的負例，沒有 truth。"
        ),
        "source": "https://metmuseum.github.io/",
        "seed": TRAIN_SEED if args.train else SEED,
        "items": items,
    }
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_paint = sum(e["kind"] == "painting" for e in items)
    print(f"→ {path.relative_to(ROOT)}：畫作 {n_paint}、不是畫 {len(items) - n_paint}")
    for k in ("style_group", "style_era", "genre", "media"):
        print(f"   有 {k} 正解：{sum(k in e['truth'] for e in items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
