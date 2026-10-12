"""圖紙文字與照片檢索評估（TASK-20261010-001）：在程序內直接執行，不用開後端；
要先 make index（真的 Chinese-CLIP、bge-m3，embedding 依 .env）。
結果存 eval/runs/<run_id>-drawing-retrieval.json。

gold set：eval/drawing_retrieval/gold.json
1. 照片：回報的截圖、知識庫原圖、審查提出的方形小局部、三視圖各自的局部與視圖中央的方形小局部
   （知識庫與未收錄零件的圖紙，切出後放大 1.3 或 2 倍）、模擬實拍（局部、傾斜、昏暗、反光、模糊）、
   未收錄的圖紙。
   和 /search/any、智慧助理同一條路：領域路由 → identify_drawing
   （只在目前身分看得到的圖紙裡辨識）
2. 文字：名稱、料號、圖號、描述各一句，送 search_parts_text
   （/search/parts，只檢索看得到的圖紙）
每個身分各跑一次（預設生管＝看得到全部、業務＝看不到機密）。
看不到的圖紙，正確答案和「不存在」一樣是 null。

停止條件（任一成立就以非 0 結束）：
- 指錯圖紙（辨識成功但不是答案，或看不到／不存在的圖紙被辨識成其他圖紙）
- 看不到的圖紙出現在任何結果裡
- 回報的截圖（有權限身分）第一名不是 mfg-006
- 文字查詢第一名不是答案（看得到時）
低品質照片（例如模糊）辨識不到算「沒認出」，不算指錯；比例照實記錄。

用法：python eval/run_drawing_retrieval_eval.py [--accounts planner,sales_a]
"""

import argparse
import functools
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from PIL import Image  # noqa: E402

from app.core.config import kb_version  # noqa: E402
from app.rag.embedders import embed_image  # noqa: E402
from app.rag.preprocess import load_image, to_jpeg_bytes  # noqa: E402
from app.rag.router import route  # noqa: E402
from app.repositories.index_store import get_store  # noqa: E402
from app.services import identity, search_service  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
GOLD = ROOT / "eval" / "drawing_retrieval" / "gold.json"
# 三視圖在圖紙版面（800×970，app/cad/drawing.py；未收錄零件的圖紙也是同一個版面）的位置，
# 前視圖、上視圖連右側與下方的尺寸標註；回報的截圖就是前視圖這樣切
VIEWS = {
    "view_right": (30, 80, 370, 420),
    "view_front": (380, 80, 740, 420),
    "view_top": (380, 430, 740, 770),
}


# 方形的小局部：每個視圖中央 180×180（視圖方框是 300×300，見 verify._DRAWING_VIEWS）
PATCHES = {
    "patch_right": (80, 160, 260, 340),
    "patch_front": (460, 160, 640, 340),
    "patch_top": (460, 510, 640, 690),
}


def view_crop(path: Path, box: tuple[int, int, int, int], factor: float = 1.3) -> Image.Image:
    """切出一塊（版面座標）、放大 factor 倍（預設 1.3，和回報的截圖差不多）。"""
    img = Image.open(path).convert("RGB")
    s = img.width / 800
    crop = img.crop(tuple(round(v * s) for v in box))
    size = (round(crop.width * factor), round(crop.height * factor))
    return crop.resize(size, Image.LANCZOS)


def uploaded(raw: bytes | Image.Image) -> Image.Image:
    """和 POST /images 一樣：EXIF 轉正、縮圖、重新編碼成 JPEG 存檔，辨識時再讀回來。"""
    img = raw if isinstance(raw, Image.Image) else load_image(raw)
    return load_image(to_jpeg_bytes(img))


def photo_cases(gold: dict, part_ids: set[str]) -> list[dict]:
    """展開 gold set 的照片：每張 {name, variant, expect, image}。"""
    out = []
    for spec in gold["photos"]:
        if "path" in spec:
            p = ROOT / spec["path"]
            out.append(
                {
                    "name": p.name,
                    "variant": spec["variant"],
                    "expect": spec["expect"],
                    "image": uploaded(p.read_bytes()),
                }
            )
            continue
        if spec.get("generate") == "views":
            for p in sorted(x for g in spec["from"] for x in ROOT.glob(g)):
                for variant, box in VIEWS.items():
                    out.append(
                        {
                            "name": f"{p.stem}__{variant}",
                            "variant": variant,
                            "expect": p.stem if p.stem in part_ids else None,
                            "image": uploaded(view_crop(p, box)),
                        }
                    )
            continue
        if spec.get("generate") == "patches":
            for p in sorted(x for g in spec["from"] for x in ROOT.glob(g)):
                for variant, box in PATCHES.items():
                    for factor in spec["factors"]:
                        out.append(
                            {
                                "name": f"{p.stem}__{variant}_x{factor:g}",
                                "variant": variant,
                                "expect": p.stem if p.stem in part_ids else None,
                                "image": uploaded(view_crop(p, box, factor)),
                            }
                        )
            continue
        if "crop" in spec:
            p = ROOT / spec["crop"]
            box, factor = tuple(spec["box"]), spec["factor"]
            out.append(
                {
                    "name": f"{p.stem}__crop{list(box)}_x{factor:g}",
                    "variant": spec["variant"],
                    "expect": p.stem if p.stem in part_ids else None,
                    "image": uploaded(view_crop(p, box, factor)),
                }
            )
            continue
        for p in sorted(ROOT.glob(spec["glob"])):
            stem = p.stem.split("__")[0]
            variant = p.stem.split("__")[1] if "__" in p.stem else spec["variant"]
            out.append(
                {
                    "name": p.name,
                    "variant": variant,
                    "expect": stem if stem in part_ids else None,
                    "image": uploaded(p.read_bytes()),
                }
            )
    return out


def run_photos(cases: list[dict], visible: set[str]) -> list[dict]:
    rows = []
    for c in cases:
        t0 = time.perf_counter()
        img = c["image"]
        vec = embed_image(img)
        r = route(vec)
        got, cand, top = None, [], None
        if r.domain == "mfg":
            res = search_service.identify_drawing("eval", img=img, vec=vec, part_ids=visible)
            got = res["best_part_id"] if res["matched"] else None
            cand = [x["part"]["id"] for x in res["results"]]
            top = res["results"][0] if res["results"] else None
        expect = c["expect"] if c["expect"] in visible else None
        rows.append(
            {
                "name": c["name"],
                "variant": c["variant"],
                "expect": expect,
                "expect_in_kb": c["expect"],
                "domain": r.domain,
                "margin": round(r.margin, 4),
                "got": got,
                "candidates": cand,
                "top_inliers": top["inliers"] if top else None,
                "top_overlap": top["overlap"] if top else None,
                "ok": got == expect,
                "wrong": got is not None and got != expect,
                "leak": bool(set(cand) - visible),
                "ms": round((time.perf_counter() - t0) * 1000),
            }
        )
    return rows


def run_texts(gold: dict, visible: set[str], scope_text: str) -> list[dict]:
    rows = []
    for t in gold["texts"]:
        res = search_service.search_parts_text(t["q"], None, visible, scope_text)
        ids = [x["part"]["id"] for x in res["results"]]
        expect = t["expect"] if t["expect"] in visible else None
        top = ids[0] if ids else None
        rows.append(
            {
                "q": t["q"],
                "by": t["by"],
                "expect": expect,
                "top": top,
                "results": ids,
                # 看得到：第一名要是答案；看不到：答案不能出現在任何名次（和不存在一樣）
                "ok": top == expect if expect else t["expect"] not in ids,
                "leak": bool(set(ids) - visible),
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default=None, help="逗號分隔，預設照 gold.json")
    args = ap.parse_args()
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    store = get_store()
    store.load()
    part_ids = {p["id"] for p in store.parts}
    cases = photo_cases(gold, part_ids)
    accounts = args.accounts.split(",") if args.accounts else gold["accounts"]
    report = {
        "run_id": f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:4]}",
        "kb_version": kb_version(),
        "accounts": {},
    }
    failed = False
    for acc_id in accounts:
        acc = identity.get_account(acc_id)
        visible = identity.visible_part_ids(acc, store.parts)
        from app.agent import guard

        scope = guard.MetaFilter.of(acc, "mfg")
        print(f"\n== {acc.label}（{acc_id}）看得到 {sorted(visible)}")
        photos = run_photos(cases, visible)
        texts = run_texts(gold, visible, scope.text)
        for r in photos:
            mark = "OK " if r["ok"] else ("錯！" if r["wrong"] else "沒認出")
            print(
                f"  [{mark}] {r['name']:42} 答案 {r['expect']!s:8} 得到 {r['got']!s:8} "
                f"inliers {r['top_inliers']!s:5} overlap {r['top_overlap']!s:5}"
            )
        for r in texts:
            print(
                f"  [{'OK ' if r['ok'] else '錯！'}] 文字 {r['by']} {r['q']!r:34} "
                f"答案 {r['expect']!s:8} 第一名 {r['top']!s:8}"
            )
        known = [r for r in photos if r["expect"]]
        unknown = [r for r in photos if not r["expect"]]
        by_variant: dict[str, list[int]] = {}
        for r in known:
            v = by_variant.setdefault(r["variant"], [0, 0])
            v[0] += r["ok"]
            v[1] += 1
        summary = {
            "photo_top1": f"{sum(r['ok'] for r in known)}/{len(known)}",
            "photo_by_variant": {k: f"{a}/{b}" for k, (a, b) in sorted(by_variant.items())},
            "photo_rejected": f"{sum(r['ok'] for r in unknown)}/{len(unknown)}",
            "photo_wrong": sum(r["wrong"] for r in photos),
            "text_ok": f"{sum(r['ok'] for r in texts)}/{len(texts)}",
            "leaks": sum(r["leak"] for r in photos + texts),
        }
        reported = next(r for r in photos if r["variant"] == "reported")
        stop = (
            summary["photo_wrong"]
            or summary["leaks"]
            or not all(r["ok"] for r in texts)
            or not reported["ok"]
        )
        failed |= bool(stop)
        print(f"  摘要：{json.dumps(summary, ensure_ascii=False)}")
        report["accounts"][acc_id] = {"summary": summary, "photos": photos, "texts": texts}
    out = ROOT / "eval" / "runs" / f"{report['run_id']}-drawing-retrieval.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n結果：{out.relative_to(ROOT)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
