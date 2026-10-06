"""畫作卡推測評估（docs/adr/018）：在程序內直接執行，不用開後端、不用索引（要有 Chinese-CLIP）；
結果存 eval/runs/<run_id>-style.json。正解在 eval/style_truth.json。

1. 畫作（知識庫內外 10 幅原圖＋eval/photos/known 25 張模擬照）：
   - 風格大類第一名對不對（畫面上顯示的是大類）；
   - 細分流派第一名、前三名有沒有正解（只當參考）；
   - 題材、媒材第一名、前三名；
   - 第一名對了卻標「看不太出來」、錯了卻沒標（標示有沒有用）。
2. 「像畫作」把關：畫作全部要過；圖紙照片（eval/drawing_photos）全部要擋下。
3. 延遲：一張照片的推測時間（照片向量另計，和以圖搜圖共用；不含第一次算標籤向量）。
4. --met：大都會博物館評估集（eval/met_set.json，由 eval/make_met_set.py 產生；
   缺的圖自動補抓到 data/met_eval/）：
   - 畫作 340 幅：中國畫、浮世繪的風格大類；歐洲繪畫的大類和年代對不對得上（館方沒有流派標籤）；
     題材、媒材；
   - 不是畫 215 件（雕塑、陶瓷、器物 190 件，老照片 25 張）要被把關擋下；
   - 門檻掃描：painting_min 各值下畫作通過幾幅、負例擋下幾件；
     min_confidence 各值下每欄留下多少、留下的對多少；
   - 領域路由（ADR 007）把這些照片分到哪個領域（要有本機索引 data/index，沒有就跳過）。

和後端 API 同一套：照片先經過 load_image（上傳時的前處理），標籤與門檻讀 shared/models.yaml。
用法：python eval/run_style_eval.py [--met]
"""

import argparse
import functools
import json
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.analysis import style  # noqa: E402
from app.core.config import get_models_config  # noqa: E402
from app.rag.embedders import embed_image  # noqa: E402
from app.rag.preprocess import load_image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
TRUTH = json.loads((ROOT / "eval" / "style_truth.json").read_text(encoding="utf-8"))


def painting_cases() -> list[tuple[str, Path, str]]:
    """(案例名稱, 圖檔, 正解 key)：原圖與模擬照。"""
    cases = []
    for key in TRUTH:
        if key.startswith("_"):
            continue
        if key.startswith("unknown-"):
            cases.append((key, ROOT / "eval" / "photos" / "unknown" / f"{key}.jpg", key))
            continue
        for d in ("kb", "kb_staging"):
            p = ROOT / d / "images" / f"{key}.jpg"
            if p.exists():
                cases.append((key, p, key))
    for p in sorted((ROOT / "eval" / "photos" / "known").glob("*.jpg")):
        cases.append((p.stem, p, p.stem.split("__")[0]))
    return cases


def judge(result: dict, truth: dict) -> dict:
    """只評正解裡有的欄位（大都會評估集有些欄沒有正解）；style_era＝風格大類和年代對不對得上。"""
    fields = {f["key"]: f for f in result["fields"]}
    out: dict = {"painting_score": result["painting_score"], "is_painting": result["is_painting"]}
    if not result["is_painting"]:
        return out
    s = fields["style"]
    for k in ("style_group", "style_era"):
        if k in truth:
            out[k] = {
                "pred": s["name"],
                "prob": s["prob"],
                "uncertain": s["uncertain"],
                "ok": s["name"] in truth[k],
            }
    fine = [c["name"] for c in s["candidates"]]
    if "style" in truth:
        out["style"] = {
            "pred": fine,
            "ok1": fine[0] in truth["style"],
            "ok3": any(n in truth["style"] for n in fine),
        }
    for k in ("genre", "media"):
        if k not in truth:
            continue
        f = fields[k]
        names = [c["name"] for c in f["candidates"]]
        out[k] = {
            "pred": names,
            "prob": f["prob"],
            "uncertain": f["uncertain"],
            "ok": names[0] in truth[k],
            "ok3": any(n in truth[k] for n in names),
            "source": f.get("source", "zero_shot"),
        }
    return out


def rate(rows: list[dict], key: str, sub: str) -> str:
    vals = [r[key][sub] for r in rows if key in r]
    return f"{sum(vals)}/{len(vals)}"


def flags(rows: list[dict], key: str, ok: str = "ok") -> dict:
    """第一名對了卻標「看不太出來」（多餘的保留）、錯了卻沒標（該保留卻沒保留）。"""
    rs = [r[key] for r in rows if key in r]
    return {
        "uncertain": sum(r["uncertain"] for r in rs),
        "right_but_uncertain": sum(r[ok] and r["uncertain"] for r in rs),
        "wrong_not_flagged": sum(not r[ok] and not r["uncertain"] for r in rs),
    }


def block(rs: list[dict]) -> dict:
    out = {
        "n": len(rs),
        "passed_gate": sum(r["is_painting"] for r in rs),
        "painting_score_min": round(min(r["painting_score"] for r in rs), 4),
        "style_group": rate(rs, "style_group", "ok"),
        "style_top1": rate(rs, "style", "ok1"),
        "style_top3": rate(rs, "style", "ok3"),
        "genre_top1": rate(rs, "genre", "ok"),
        "genre_top3": rate(rs, "genre", "ok3"),
        "media_top1": rate(rs, "media", "ok"),
        "media_top3": rate(rs, "media", "ok3"),
        "flags": {k: flags(rs, k) for k in ("style_group", "genre", "media")},
    }
    if any("style_era" in r for r in rs):
        out["style_era"] = rate(rs, "style_era", "ok")
        out["flags"]["style_era"] = flags(rs, "style_era")
    return out


GATE_SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
CONF_SWEEP = (0.3, 0.4, 0.5, 0.6, 0.7)


def conf_sweep(rows: list[dict], key: str) -> list[dict]:
    """min_confidence 各值下：留下（不標「看不太出來」）幾筆、留下的對幾筆。"""
    rs = [r[key] for r in rows if key in r]
    out = []
    for t in CONF_SWEEP:
        kept = [r for r in rs if r["prob"] >= t]
        out.append(
            {
                "min_confidence": t,
                "n": len(rs),
                "kept": len(kept),
                "kept_ok": sum(r["ok"] for r in kept),
            }
        )
    return out


def try_router():
    """領域路由要用本機索引的畫作／圖紙原型；沒有索引（或版本不符）就跳過，不擋評估。"""
    try:
        from app.core.config import get_settings
        from app.rag import router
        from app.repositories import index_store

        index_store._store = index_store.IndexStore(
            get_settings().index_dir
        )  # 不連資料庫，直接讀 data/index
        router.route(np.zeros(512, dtype=np.float32) + 1 / np.sqrt(512))
        return router.route
    except Exception as e:  # noqa: BLE001
        print(f"（領域路由跳過：{e.__class__.__name__}: {e}）")
        return None


def met_eval(spec) -> dict:
    """大都會評估集：eval/met_set.json；缺的圖從 Met 補抓到 data/met_eval/。"""
    from make_met_set import DATA, SET_PATH, Met

    items = json.loads(SET_PATH.read_text(encoding="utf-8"))["items"]
    met = None
    route = try_router()
    rows = []
    for i, e in enumerate(items, 1):
        path = DATA / f"{e['id']}.jpg"
        if not path.exists():
            met = met or Met()
            if not met.image(e["image_url"], path):
                print(f"  抓不到圖 {e['id']}，跳過")
                continue
        vec = embed_image(load_image(path.read_bytes()))
        r = style.guess(vec, spec)
        row = judge(r, e["truth"]) | {"id": e["id"], "category": e["category"], "kind": e["kind"]}
        if route:
            rt = route(vec)
            row["route"] = {
                "domain": rt.domain,
                "margin": round(rt.margin, 4),
                "uncertain": rt.uncertain,
            }
        rows.append(row)
        if i % 100 == 0:
            print(f"  大都會評估集 {i}/{len(items)}")

    paintings = [r for r in rows if r["kind"] == "painting"]
    objects = [r for r in rows if r["kind"] == "other" and r["category"] != "photograph"]
    photos = [r for r in rows if r["category"] == "photograph"]

    def scores(rs: list[dict]) -> dict:
        s = sorted(r["painting_score"] for r in rs)
        return {
            "min": round(s[0], 4),
            "median": round(statistics.median(s), 4),
            "max": round(s[-1], 4),
        }

    per_cat = {}
    for cat in dict.fromkeys(r["category"] for r in rows):
        rs = [r for r in rows if r["category"] == cat]
        c = {
            "kind": rs[0]["kind"],
            "n": len(rs),
            "passed_gate": sum(r["is_painting"] for r in rs),
        } | scores(rs)
        if route:
            c["route"] = {d: sum(r["route"]["domain"] == d for r in rs) for d in ("art", "mfg")} | {
                "uncertain": sum(r["route"]["uncertain"] for r in rs)
            }
        per_cat[cat] = c
    summary = {
        "paintings": block(paintings),
        "per_category": per_cat,
        "gate_sweep": [
            {
                "painting_min": t,
                "paintings_passed": sum(r["painting_score"] >= t for r in paintings),
                "objects_blocked": sum(r["painting_score"] < t for r in objects),
                "photos_blocked": sum(r["painting_score"] < t for r in photos),
            }
            for t in GATE_SWEEP
        ],
        "gate_counts": {
            "paintings": len(paintings),
            "objects": len(objects),
            "photos": len(photos),
        },
        # 線性分類頭的欄位用各自的門檻，不看 min_confidence，掃描不適用（None）
        "conf_sweep": {
            k: None
            if any(r[k].get("source") == "head" for r in paintings if k in r)
            else conf_sweep(paintings, k)
            for k in ("style_group", "style_era", "genre", "media")
        },
    }
    return {"summary": summary, "rows": rows}


def print_met(m: dict, spec) -> None:
    b, n = m["paintings"], m["gate_counts"]
    print(f"\n大都會評估集（畫作 {n['paintings']}、器物 {n['objects']}、老照片 {n['photos']}）")
    print(
        f"畫作：把關通過 {b['passed_gate']}/{b['n']}；"
        f"風格大類（中國畫＋浮世繪）{b['style_group']}；"
        f"大類和年代對得上（歐洲繪畫）{b.get('style_era', '—')}；"
        f"題材 {b['genre_top1']}（top3 {b['genre_top3']}）；"
        f"媒材 {b['media_top1']}（top3 {b['media_top3']}）"
    )
    for f, x in b["flags"].items():
        print(
            f"    {f}：標看不太出來 {x['uncertain']}、"
            f"對了卻標 {x['right_but_uncertain']}、錯了沒標 {x['wrong_not_flagged']}"
        )
    print("各類別（通過把關／件數；像畫作的程度 最低～中位數～最高；路由 art／mfg）：")
    for cat, c in m["per_category"].items():
        rt = f"；路由 {c['route']['art']}／{c['route']['mfg']}" if "route" in c else ""
        print(
            f"    {cat:<15} {'畫' if c['kind'] == 'painting' else '非'}"
            f" {c['passed_gate']:>3}/{c['n']:<3}"
            f" {c['min']:.2f}～{c['median']:.2f}～{c['max']:.2f}{rt}"
        )
    print(f"painting_min 掃描（目前 {spec.painting_min}）：")
    for g in m["gate_sweep"]:
        print(
            f"    {g['painting_min']:.1f}：畫作通過 {g['paintings_passed']}/{n['paintings']}、"
            f"器物擋下 {g['objects_blocked']}/{n['objects']}、"
            f"老照片擋下 {g['photos_blocked']}/{n['photos']}"
        )
    print(f"min_confidence 掃描（目前 {spec.min_confidence}；留下的對幾筆／留下幾筆／全部）：")
    for k, sw in m["conf_sweep"].items():
        if sw is None:
            print(f"    {k:<12} 線性分類頭用各自的門檻，不適用")
            continue
        cells = "  ".join(
            f"{s['min_confidence']:.1f}: {s['kept_ok']}/{s['kept']}/{s['n']}" for s in sw
        )
        print(f"    {k:<12} {cells}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--met", action="store_true", help="另跑大都會評估集（eval/met_set.json）")
    args = ap.parse_args()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    spec = get_models_config().style_guess
    style.guess(
        embed_image(load_image((ROOT / "kb" / "images" / "npm-000001.jpg").read_bytes()))
    )  # 先算標籤向量

    rows, lat = [], []
    for name, path, key in painting_cases():
        vec = embed_image(load_image(path.read_bytes()))
        t0 = time.perf_counter()
        r = style.guess(vec)
        lat.append((time.perf_counter() - t0) * 1000)
        j = judge(r, TRUTH[key]) | {"case": name, "truth": key, "photo": "__" in name}
        rows.append(j)
        if not j["is_painting"]:
            print(f"{name:<22} ✗ 被當成不是畫作（{j['painting_score']:.2f}）")
            continue
        mark = lambda ok: "O" if ok else "X"  # noqa: E731
        g, s, ge, me = j["style_group"], j["style"], j["genre"], j["media"]
        q = lambda f: "?" if f["uncertain"] else ""  # noqa: E731 — 標了「看不太出來」
        print(
            f"{name:<22} 大類 {mark(g['ok'])} {g['pred']}({g['prob']:.2f}{q(g)})"
            f" | 細分 {mark(s['ok1'])} {'、'.join(s['pred'])}"
            f" | 題材 {mark(ge['ok'])} {ge['pred'][0]}({ge['prob']:.2f}{q(ge)})"
            f" | 媒材 {mark(me['ok'])} {me['pred'][0]}({me['prob']:.2f}{q(me)})"
        )

    drawings = sorted((ROOT / "eval" / "drawing_photos").rglob("*.*"))
    drawings = [p for p in drawings if p.suffix.lower() in (".jpg", ".png")]
    gate_scores = {}
    for p in drawings:
        r = style.guess(embed_image(load_image(p.read_bytes())))
        gate_scores[str(p.relative_to(ROOT))] = r["painting_score"]

    originals = [r for r in rows if not r["photo"]]
    photos = [r for r in rows if r["photo"]]
    summary = {
        "originals": block(originals),
        "known_photos": block(photos),
        "unknown_only": block([r for r in originals if r["truth"].startswith("unknown-")]),
        "gate_non_paintings": {
            "n": len(gate_scores),
            "blocked": sum(v < spec.painting_min for v in gate_scores.values()),
            "painting_score_max": round(max(gate_scores.values()), 4) if gate_scores else None,
        },
        "latency_ms_p50": round(statistics.median(lat), 1),
        "latency_ms_p95": round(sorted(lat)[int(len(lat) * 0.95) - 1], 1),
    }
    met = met_eval(spec) if args.met else None
    if met:
        summary["met"] = met["summary"]
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "config": spec.model_dump(mode="json"),
        "summary": summary,
        "rows": rows,
        "non_painting_scores": gate_scores,
    } | ({"met_rows": met["rows"]} if met else {})
    path = ROOT / "eval" / "runs" / f"{run_id}-style.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    for label, k in (
        ("原圖（知識庫內外 10 幅）", "originals"),
        ("其中知識庫外 5 幅", "unknown_only"),
        ("模擬照 25 張", "known_photos"),
    ):
        b = summary[k]
        print(
            f"{label}：把關通過 {b['passed_gate']}/{b['n']}；風格大類 {b['style_group']}；"
            f"細分 top1 {b['style_top1']}、top3 {b['style_top3']}；"
            f"題材 {b['genre_top1']}（top3 {b['genre_top3']}）；"
            f"媒材 {b['media_top1']}（top3 {b['media_top3']}）"
        )
        for f, m in b["flags"].items():
            print(
                f"    {f}：標看不太出來 {m['uncertain']}、"
                f"對了卻標 {m['right_but_uncertain']}、錯了沒標 {m['wrong_not_flagged']}"
            )
    g = summary["gate_non_paintings"]
    print(f"圖紙照片擋下 {g['blocked']}/{g['n']}（像畫作的程度最高 {g['painting_score_max']}）")
    print(f"延遲 P50 {summary['latency_ms_p50']} ms、P95 {summary['latency_ms_p95']} ms")
    if met:
        print_met(met["summary"], spec)
    print(f"→ {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
