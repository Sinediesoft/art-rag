"""以圖搜圖「同系列、不同版本」評估（Q-2026-10-04-05 第 2 項，docs/adr/002）：
在程序內執行，不用開後端、不用索引（要有 Chinese-CLIP）；結果存 eval/runs/<run_id>-versions.json。

ADR 002 的門檻（CLIP ≥ image_threshold、ORB inlier ≥ verify_min_inliers）是用構圖完全不同的畫
校正的。真正危險的是同一位畫家畫好幾版的作品：觀眾拍到別館的那一版，系統卻回答「這是知識庫裡的那幅」
並附上錯的出處，比回「知識庫中沒有這幅畫」糟得多。
這支用 eval/version_set.json 的照片（Wikimedia Commons，正解依各檔所在的分類）量：
1. 別版、習作、同系列（relation＝replica／study／series／motif）有沒有被認成知識庫的畫：應該 0 張；
2. 同一幅畫的其他照片（same）認不認得出來：對照組，改門檻時不能把這些也擋掉。
翻拍（kind＝repro）另做 5 種模擬照（eval/make_synthetic_photos.py 的 VARIANTS）；
美術館現場實拍（photo）直接用。

和 search_service.identify 同一套：照片先經過 load_image（上傳時的前處理），
知識庫圖的 CLIP 向量照建索引的算法，ORB 特徵用 verify.kb_features。
每張照片和每幅目標畫作都比、不看 CLIP 名次，所以「被認錯」的數字是上限
（identify 另外只驗 CLIP 前 verify_top_n 名）。
目標含 kb_staging 的〈睡蓮〉（make demo-add 才會進知識庫）。

圖不 commit：第一次跑從 Commons 抓到 data/version_eval/（長邊 1600 px），
模擬照放 data/version_eval/synth/。
用法：python eval/run_version_eval.py
"""

import functools
import io
import json
import random
import statistics
import sys
import time
import uuid
import zlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "eval"))

from make_synthetic_photos import VARIANTS  # noqa: E402

from app.core.config import get_models_config  # noqa: E402
from app.rag import verify  # noqa: E402
from app.rag.embedders import embed_image  # noqa: E402
from app.rag.preprocess import load_image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
SET = json.loads((ROOT / "eval" / "version_set.json").read_text(encoding="utf-8"))
DATA = ROOT / "data" / "version_eval"
API = "https://commons.wikimedia.org/w/api.php"
UA = {"User-Agent": "art-rag-eval/0.1 (school project; https://github.com/Sinediesoft/art-rag)"}
FETCH_EDGE = 1600  # 和 make_synthetic_photos 一樣，先縮到 1600 再加工
MIN_INTERVAL_S = 0.5


def fetch_missing(items: list[dict]) -> None:
    """缺的圖從 Commons 抓（API 要縮圖網址 → 下載 → 轉成 JPEG）。"""
    missing = [it for it in items if not (DATA / f"{it['id']}.jpg").exists()]
    if not missing:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"從 Wikimedia Commons 抓 {len(missing)} 張圖到 {DATA.relative_to(ROOT)}/")
    with httpx.Client(headers=UA, timeout=60, follow_redirects=True) as c:
        for it in missing:
            for attempt in range(4):
                try:
                    r = c.get(
                        API,
                        params={
                            "action": "query",
                            "titles": "File:" + it["file"],
                            "prop": "imageinfo",
                            "iiprop": "url",
                            "iiurlwidth": FETCH_EDGE,
                            "format": "json",
                        },
                    )
                    page = next(iter(r.json()["query"]["pages"].values()))
                    info = page["imageinfo"][0]
                    time.sleep(MIN_INTERVAL_S)
                    img = c.get(info.get("thumburl") or info["url"])
                    img.raise_for_status()
                    im = Image.open(io.BytesIO(img.content)).convert("RGB")
                    im.thumbnail((FETCH_EDGE, FETCH_EDGE))
                    im.save(DATA / f"{it['id']}.jpg", quality=92)
                    print(f"  ✓ {it['id']}")
                    break
                except (httpx.HTTPError, KeyError, StopIteration, OSError) as e:
                    wait = 5 * (attempt + 1)
                    print(f"  {it['id']} 失敗（{e.__class__.__name__}），{wait} 秒後重試")
                    time.sleep(wait)
            else:
                raise SystemExit(f"抓不到 {it['file']}，請確認網路或 Commons 上的檔名")
            time.sleep(MIN_INTERVAL_S)


def cases(items: list[dict]) -> list[tuple[dict, str, Path]]:
    """(項目, 變體名稱, 照片路徑)：每張原圖一筆；翻拍另加 5 種模擬照（固定 seed，重跑結果相同）。"""
    out = []
    synth = DATA / "synth"
    synth.mkdir(parents=True, exist_ok=True)
    for it in items:
        src = DATA / f"{it['id']}.jpg"
        out.append((it, "原圖" if it["kind"] == "repro" else "實拍", src))
        if it["kind"] != "repro":
            continue
        base = None
        for name, fn in VARIANTS.items():
            path = synth / f"{it['id']}__{name}.jpg"
            if not path.exists():
                if base is None:
                    base = Image.open(src).convert("RGB")
                    base.thumbnail((1600, 1600))
                random.seed(zlib.crc32(f"{it['id']}__{name}".encode()))
                photo = fn(base.copy())
                photo.thumbnail((1024, 1024))
                photo.save(path, quality=80)
            out.append((it, name, path))
    return out


def stats(values: list[float]) -> dict:
    if not values:
        return {}
    return {
        "min": round(min(values), 4),
        "median": round(statistics.median(values), 4),
        "max": round(max(values), 4),
    }


def block(rows: list[dict]) -> dict:
    """一組照片的結果；same 看認得出來幾張，其他看被認成知識庫畫作幾張。"""
    same = [r for r in rows if r["relation"] == "same"]
    other = [r for r in rows if r["relation"] != "same"]
    return {
        "same": {
            "n": len(same),
            "recognized": sum(r["ok"] for r in same),
            "inliers": stats([r["inliers"] for r in same]),
            "clip": stats([r["clip"] for r in same]),
        },
        "versions": {
            "n": len(other),
            "false_accepts": sum(r["predicted"] is not None for r in other),
            "clip_passed": sum(r["clip"] >= r["threshold"] for r in other),
            "inliers": stats([r["inliers"] for r in other]),
            "clip": stats([r["clip"] for r in other]),
        },
    }


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    cfg = get_models_config().retrieval
    threshold, min_inliers = float(cfg["image_threshold"]), int(cfg["verify_min_inliers"])
    items = SET["items"]
    fetch_missing(items)

    targets = {}
    for tid, rel in SET["targets"].items():
        path = ROOT / rel
        targets[tid] = (
            embed_image(load_image(path)),
            verify.kb_features(path, path.stat().st_mtime),
        )

    rows = []
    for it, variant, path in cases(items):
        img = load_image(path.read_bytes())
        vec, feats = embed_image(img), verify.features(img)
        per = {
            tid: {"clip": float(vec @ tvec), "inliers": verify.count_inliers(feats, tfeat)}
            for tid, (tvec, tfeat) in targets.items()
        }
        accepted = [
            t for t, s in per.items() if s["clip"] >= threshold and s["inliers"] >= min_inliers
        ]
        predicted = max(accepted, key=lambda t: per[t]["inliers"]) if accepted else None
        own = per[it["target"]]
        ok = predicted == it["target"] if it["relation"] == "same" else predicted is None
        rows.append(
            {
                "id": it["id"],
                "variant": variant,
                "target": it["target"],
                "relation": it["relation"],
                "kind": it["kind"],
                "work": it["work"],
                "clip": round(own["clip"], 4),
                "inliers": own["inliers"],
                "threshold": threshold,
                "predicted": predicted,
                "ok": ok,
                "all_targets": {
                    t: {"clip": round(v["clip"], 4), "inliers": v["inliers"]}
                    for t, v in per.items()
                },
            }
        )
        r = rows[-1]
        print(
            f"{'O' if ok else 'X'} {r['id']:<12} {variant:<5} {r['relation']:<8}"
            f" CLIP {r['clip']:.3f}  inlier {r['inliers']:>4}  → {predicted or '查無'}"
        )

    summary = {
        "all": block(rows),
        "by_target": {t: block([r for r in rows if r["target"] == t]) for t in targets},
        "by_relation": {
            rel: {
                "n": len(g),
                "ok": sum(r["ok"] for r in g),
                "inliers": stats([r["inliers"] for r in g]),
                "clip": stats([r["clip"] for r in g]),
            }
            for rel in ("same", "replica", "study", "series", "motif")
            if (g := [r for r in rows if r["relation"] == rel])
        },
        "photos_only": block([r for r in rows if r["kind"] == "photo"]),
        "failures": [
            {k: r[k] for k in ("id", "variant", "relation", "work", "clip", "inliers", "predicted")}
            for r in rows
            if not r["ok"]
        ],
    }
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "config": {
            "image_threshold": threshold,
            "verify_min_inliers": min_inliers,
            "verify_top_n": int(cfg["verify_top_n"]),
            "set": SET["source"],
        },
        "summary": summary,
        "rows": rows,
    }
    path = ROOT / "eval" / "runs" / f"{run_id}-versions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    a = summary["all"]
    print()
    print(f"門檻：CLIP ≥ {threshold}、inlier ≥ {min_inliers}")
    print(
        f"同一幅畫的其他照片：認得出來 {a['same']['recognized']}/{a['same']['n']}"
        f"（inlier {a['same']['inliers']}）"
    )
    v = a["versions"]
    print(
        f"別版、習作、同系列：被認成知識庫的畫 {v['false_accepts']}/{v['n']}"
        f"（CLIP 過門檻 {v['clip_passed']}；inlier {v['inliers']}）"
    )
    for rel, b in summary["by_relation"].items():
        print(f"  {rel:<8} {b['ok']}/{b['n']} 正確  inlier {b['inliers']}  CLIP {b['clip']}")
    p = summary["photos_only"]
    print(
        f"只看現場實拍：同一幅 {p['same']['recognized']}/{p['same']['n']}；"
        f"別版被認錯 {p['versions']['false_accepts']}/{p['versions']['n']}"
    )
    if summary["failures"]:
        print("錯的：")
        for f in summary["failures"]:
            print(
                f"  {f['id']} {f['variant']}（{f['work']}）CLIP {f['clip']}"
                f" inlier {f['inliers']} → {f['predicted'] or '查無'}"
            )
    print(f"\n已存 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
