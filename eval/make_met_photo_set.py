"""建以圖搜圖的「觀眾實拍照」評估集（Q-2026-10-04-06 第 1、2 項，docs/adr/002）。

The Met Dataset（Ypsilantis et al., NeurIPS 2021 Datasets and Benchmarks，http://cmp.felk.cvut.cz/met/）
的 Met queries：觀眾在大都會館內用手機拍的照片 1,132 張（研究團隊自拍＋Flickr 上的 CC 照片），
每張標了拍到的館藏編號（MET_id）。這裡把每張照片拍到的那件館藏從 Met 開放 API 查回來，
分成畫作／版畫素描／器物，寫成 eval/met_photo_set.json（commit，組員重跑得到同一批）。

- 標註（testset.json、valset.json）是 CC BY 4.0；照片本身不 commit：
  Met Dataset 附的照片長邊只有 500 px，Flickr 的照片 run_met_photo_eval.py 會另外抓原圖。
- 下載站用 ptak.felk.cvut.cz：2026-10-04 官網 cmp.felk.cvut.cz 從這台連不上，
  ptak 是同一個實驗室（捷克理工大學 CMP）放資料檔的主機。
- 館藏原始紀錄和 make_met_set.py 共用快取 data/met_eval/raw/。

用法：python eval/make_met_photo_set.py
"""

import functools
import json
import re
import sys
import tarfile
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from make_met_set import RAW, Met  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
MIRROR = "http://ptak.felk.cvut.cz/met/dataset"
DATA = ROOT / "data" / "met_dataset"
SET_PATH = ROOT / "eval" / "met_photo_set.json"


def download(url: str, path: Path) -> None:
    if path.exists():
        return
    print(f"下載 {url}")
    tmp = path.with_suffix(path.suffix + ".part")
    with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.replace(path)


def fetch_dataset() -> None:
    """標註兩份＋Met queries 的照片（test_met.tar.gz，32 MB，驗證集與測試集的照片都在裡面）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    for name in ("testset.json", "valset.json"):
        download(f"{MIRROR}/ground_truth/{name}", DATA / name)
    if not (DATA / "test_met").is_dir():
        download(f"{MIRROR}/test_met.tar.gz", DATA / "test_met.tar.gz")
        with tarfile.open(DATA / "test_met.tar.gz") as t:
            t.extractall(DATA, filter="data")


def kind_of(o: dict) -> str:
    """painting：會掛在牆上、我們知識庫收的那種畫（油畫、蛋彩、中國畫、細密畫、粉彩）；
    print_drawing：版畫、素描、水彩（平面，但多半在特展輪替，知識庫目前沒收）；object：其他。"""
    cls = o.get("classification") or ""
    medium = (o.get("medium") or "").lower()
    if cls.startswith("Paintings") or cls in ("Miniatures", "Pastels & Oil Sketches on Paper"):
        return "painting"
    if cls == "" and re.search(r"\boil\b|tempera", medium):  # 美國館的油畫分類欄是空的
        return "painting"
    if cls.startswith(("Prints", "Drawings")):
        return "print_drawing"
    return "object"


def main() -> int:
    fetch_dataset()
    queries = []
    for split, name in (("val", "valset.json"), ("test", "testset.json")):
        for q in json.loads((DATA / name).read_text(encoding="utf-8")):
            if q.get("MET_id") is None:  # 其他館藏、非藝術品的干擾照：這裡不用
                continue
            queries.append(
                {
                    "file": q["path"],
                    "met_id": int(q["MET_id"]),
                    "split": split,
                    "photographer": q["photographer"],
                    "flickr_url": None if q["url"] in (None, "None") else q["url"],
                }
            )
    ids = sorted({q["met_id"] for q in queries})
    print(
        f"Met queries {len(queries)} 張、{len(ids)} 件館藏；"
        f"查 Met 開放 API（快取 {RAW.relative_to(ROOT)}/）"
    )
    RAW.mkdir(parents=True, exist_ok=True)
    met = Met()
    objects = {}
    for i, oid in enumerate(ids, 1):
        o = met.object(oid)
        if o:
            objects[oid] = {
                "title": o.get("title"),
                "artist": o.get("artistDisplayName") or None,
                "date": o.get("objectDate"),
                "department": o.get("department"),
                "classification": o.get("classification") or None,
                "medium": o.get("medium"),
                "kind": kind_of(o),
                "public_domain": bool(o.get("isPublicDomain")),
                "image_url": o.get("primaryImage") or None,
                "object_url": o.get("objectURL"),
            }
        if i % 100 == 0:
            print(f"  {i}/{len(ids)}")
    missing = [oid for oid in ids if oid not in objects]
    for q in queries:
        q["kind"] = objects[q["met_id"]]["kind"] if q["met_id"] in objects else "missing"

    out = {
        "_說明": (
            "以圖搜圖觀眾實拍照評估集（Q-2026-10-04-06，docs/adr/002），"
            "由 eval/make_met_photo_set.py 產生。"
            "照片與正解來自 The Met Dataset 的 Met queries（標註 CC BY 4.0，"
            "Ypsilantis et al., NeurIPS 2021 Datasets and Benchmarks）；"
            "館藏資料來自 Met 開放 API（CC0）。"
            "kind：painting＝知識庫收的那種畫，print_drawing＝版畫素描水彩，object＝器物雕塑等，"
            "missing＝API 查不到。照片不 commit。"
        ),
        "source": "http://cmp.felk.cvut.cz/met/",
        "queries": queries,
        "objects": {str(k): v for k, v in objects.items()},
    }
    SET_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    by_kind = Counter(q["kind"] for q in queries)
    print(f"→ {SET_PATH.relative_to(ROOT)}：照片 {len(queries)} 張 {dict(by_kind)}")
    paint = {oid for oid, o in objects.items() if o["kind"] == "painting"}
    print(
        f"   畫作 {len(paint)} 件（有圖 {sum(bool(objects[o]['image_url']) for o in paint)}）；"
        f"API 查不到 {len(missing)} 件"
    )
    print("   畫作的部門：", Counter(objects[o]["department"] for o in paint).most_common())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
