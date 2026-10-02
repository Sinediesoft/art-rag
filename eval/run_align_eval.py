"""影像對位與比對評估（docs/adr/012）：在程序內直接執行，不用開後端、不用索引；
結果存 eval/runs/<run_id>-align.json。照片先用 eval/make_align_photos.py 產生（已附在 repo）。

1. 畫作位置：近照（拍到長邊 50%、30%）與整幅照片，位置框四角和正確答案差多少
   （以原圖長邊的 % 計，≤ 2% 算對）
2. 圖紙差異：
   - same：沒改過的圖紙，有沒有冒出假差異（「0 處」的比例）；
   - hole／erase：前視圖注入的改動有沒有找到、另外多報幾處；
   - revA：真正的改版（mfg-001 rev.A，底板 2 孔）三處改動有沒有都找到、另外多報幾處。
3. 兩張照片互比（畫作，pair/）：同光線、不同角度拍的兩張；
   - same：兩張都沒動過，有沒有冒出假差異；
   - paste／smear／seal／fade／tint：改動有沒有找到、被判成形狀還是顏色不同、另外多報幾處。
4. 畫作找不同（觀眾照片 vs 知識庫原圖）：
   - art/ 的照片都沒改過，有沒有冒出假差異；
   - artedit/：在照片上加筆（strokes）或畫本身動了手腳，改動有沒有找到、另外多報幾處。
5. 壓力測試（沒改過的舊模擬照，量假差異）：
   - 圖紙：eval/drawing_photos/known 的 30 張（其中 tilt 是雙線性變形，
     不是相機拍得出來的，拉正後邊緣會差到 25 px）；
   - 畫作：eval/photos/known 的 25 張（模糊、偏暗、反光、裁切、雙線性 tilt）。
6. 延遲：對位＋比對的計算時間（不含讀檔）

和後端 API 同一套：照片先經過 load_image（上傳時的前處理），參數讀 shared/models.yaml。
用法：python eval/run_align_eval.py
"""

import functools
import json
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.analysis import align  # noqa: E402
from app.core.config import get_models_config  # noqa: E402
from app.rag import verify  # noqa: E402
from app.rag.preprocess import load_image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
PHOTOS = ROOT / "eval" / "align_photos"
CORNER_OK = 0.02  # 位置框四角誤差 ≤ 原圖長邊 2% 算對


def ref_path(target: str) -> Path:
    kind, _, tid = target.partition(":")
    for d in ("kb", "kb_staging"):
        p = (
            ROOT
            / d
            / ("images" if kind == "artwork" else "drawings")
            / f"{tid}.{'jpg' if kind == 'artwork' else 'png'}"
        )
        if p.exists():
            return p
    raise FileNotFoundError(target)


def run_one(
    photo_path: Path, target: str
) -> tuple[align.Location | None, align.InkDiff | align.ToneDiff | None, float, Image.Image]:
    cfg = get_models_config()
    domain = "art" if target.startswith("artwork:") else "mfg"
    spec = getattr(cfg.image_compare, domain)
    min_inliers = int(
        (cfg.retrieval if domain == "art" else cfg.drawing_retrieval)["verify_min_inliers"]
    )
    path = ref_path(target)
    ref = Image.open(path).convert("RGB")
    img = load_image(photo_path)
    t0 = time.perf_counter()
    loc = align.locate(
        verify.features(img),
        verify.kb_features(path, path.stat().st_mtime, drawing=domain == "mfg"),
        img.size,
        ref.size,
        min_inliers=min_inliers,
        strict=domain == "mfg",
    )
    d = None
    if loc is not None and spec.diff == "ink":
        d = align.diff_ink(img, loc.h, ref, spec, boxes=verify.drawing_views(ref))
    elif loc is not None and spec.diff == "tone":
        d = align.diff_tone(img, loc.h, ref, spec)
    return loc, d, (time.perf_counter() - t0) * 1000, img


def corner_error(loc: align.Location, row: dict, photo_size, ref_size) -> float:
    """位置框四角和正確答案的最大誤差，以原圖長邊的比例計。答案的 H 是對原始照片，
    load_image 可能縮過圖，先換算回原始照片座標。"""
    raw = Image.open(PHOTOS / row["file"]).size
    s = raw[0] / photo_size[0]
    h = np.array(row["h"]) @ np.diag([s, s, 1.0])
    want = align.polygon_from_h(h, photo_size, ref_size)
    got = np.array(loc.polygon) * ref_size
    return float(np.abs(got - want).max() / max(ref_size))


def overlaps(bbox, box, size) -> bool:
    w, h = size
    x0, y0, x1, y1 = bbox[0] * w, bbox[1] * h, bbox[2] * w, bbox[3] * h
    return not (x0 > box[2] or x1 < box[0] or y0 > box[3] or y1 < box[1])


def run_pair(row: dict) -> tuple[dict, float]:
    """照片 B（file）對齊到照片 A（ref_file）後比形狀與顏色；方框是照片 A 上的座標。"""
    cfg = get_models_config()
    a, b = load_image(PHOTOS / row["ref_file"]), load_image(PHOTOS / row["file"])
    s = a.width / Image.open(PHOTOS / row["ref_file"]).width
    boxes = [[v * s for v in box] for box in row["boxes"]]
    t0 = time.perf_counter()
    loc = align.locate(
        verify.features(b),
        verify.features(a),
        b.size,
        a.size,
        min_inliers=int(cfg.retrieval["verify_min_inliers"]),
    )
    d = align.diff_tone(b, loc.h, a, cfg.image_compare.pair) if loc else None
    ms = (time.perf_counter() - t0) * 1000
    r = {"file": row["file"], "case": row["case"], "located": loc is not None, "ms": round(ms)}
    if d is not None:
        hits = [[g.kind for g in d.regions if overlaps(g.bbox, box, a.size)] for box in boxes]
        r.update(
            status=d.status,
            regions=len(d.regions),
            found=sum(bool(h) for h in hits),
            expected=len(boxes),
            hit_kinds=sorted({k for h in hits for k in h}),
            false_regions=sum(
                1 for g in d.regions if not any(overlaps(g.bbox, box, a.size) for box in boxes)
            ),
            changed_ratio=d.changed_ratio,
        )
    return r, ms


def run_edit(row: dict) -> tuple[dict, float]:
    """畫作動了手腳的照片 vs 知識庫原圖：方框是原圖座標。"""
    loc, d, ms, _ = run_one(PHOTOS / row["file"], row["target"])
    ref_size = Image.open(ref_path(row["target"])).size
    r = {
        "file": row["file"],
        "case": row["case"],
        "variant": row["variant"],
        "located": loc is not None,
        "ms": round(ms),
    }
    if d is not None:
        boxes = row["boxes"]
        hits = [[g.kind for g in d.regions if overlaps(g.bbox, b, ref_size)] for b in boxes]
        r.update(
            status=d.status,
            regions=len(d.regions),
            found=sum(bool(h) for h in hits),
            expected=len(boxes),
            hit_kinds=sorted({k for h in hits for k in h}),
            false_regions=sum(
                1 for g in d.regions if not any(overlaps(g.bbox, b, ref_size) for b in boxes)
            ),
        )
    return r, ms


def edit_summary(edits: list[dict]) -> dict:
    out = {}
    for c in ("strokes", "paste", "smear", "seal", "fade", "tint"):
        rs = [r for r in edits if r["case"] == c]
        out[c] = {
            "photos": len(rs),
            "located": sum(r["located"] for r in rs),
            "found": sum(r.get("found", 0) for r in rs),
            "expected": sum(r.get("expected", 0) for r in rs),
            "kinds": {
                k: sum(k in r.get("hit_kinds", []) for r in rs) for k in ("shape", "color", "both")
            },
            "global_change": sum(r.get("status") == "global_change" for r in rs),
            "false_regions_total": sum(r.get("false_regions", 0) for r in rs),
            "false_regions_max": max((r.get("false_regions", 0) for r in rs), default=0),
        }
    return out


def pair_summary(pairs: list[dict]) -> dict:
    out = {}
    for c in ("same", "paste", "smear", "seal", "fade", "tint"):
        rs = [r for r in pairs if r["case"] == c]
        out[c] = {
            "pairs": len(rs),
            "located": sum(r["located"] for r in rs),
            "zero_regions": sum(r.get("status") == "same" for r in rs),
            "global_change": sum(r.get("status") == "global_change" for r in rs),
            "found": sum(r.get("found", 0) for r in rs),
            "expected": sum(r.get("expected", 0) for r in rs),
            "kinds": {
                k: sum(k in r.get("hit_kinds", []) for r in rs) for k in ("shape", "color", "both")
            },
            "false_regions_total": sum(r.get("false_regions", 0) for r in rs),
            "false_regions_max": max((r.get("false_regions", 0) for r in rs), default=0),
        }
    return out


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    t0 = time.time()
    truth = json.loads((PHOTOS / "truth.json").read_text(encoding="utf-8"))
    rows, latencies = [], []
    for row in truth:
        if row["domain"] in ("pair", "artedit"):
            continue
        loc, d, ms, img = run_one(PHOTOS / row["file"], row["target"])
        latencies.append(ms)
        ref_size = Image.open(ref_path(row["target"])).size
        r = {
            "file": row["file"],
            "case": row["case"],
            "variant": row.get("variant"),
            "located": loc is not None,
            "ms": round(ms),
        }
        if loc is not None:
            r["inliers"], r["coverage"] = loc.inliers, loc.coverage
        if row["domain"] == "art":
            r["corner_error"] = (
                round(corner_error(loc, row, img.size, ref_size), 4) if loc else None
            )
            r["ok"] = r["corner_error"] is not None and r["corner_error"] <= CORNER_OK
            if d is not None:  # 照片都沒改過：有差異就是假差異
                r.update(status=d.status, regions=len(d.regions))
        elif d is not None:
            boxes = row["boxes"]
            hit = [any(overlaps(g.bbox, b, ref_size) for g in d.regions) for b in boxes]
            r.update(
                status=d.status,
                regions=len(d.regions),
                found=sum(hit),
                expected=len(boxes),
                false_regions=sum(
                    1 for g in d.regions if not any(overlaps(g.bbox, b, ref_size) for b in boxes)
                ),
            )
        rows.append(r)
        detail = json.dumps({k: v for k, v in r.items() if k != "file"}, ensure_ascii=False)
        print(f"  {row['file']:<40} {detail}")

    stress = []
    for p in sorted((ROOT / "eval/drawing_photos/known").glob("*.jpg")):
        target = f"part:{p.stem.split('__')[0]}"
        try:
            ref_path(target)
        except FileNotFoundError:
            continue
        loc, d, ms, _ = run_one(p, target)
        stress.append(
            {
                "file": p.name,
                "located": loc is not None,
                "status": d.status if d else None,
                "regions": len(d.regions) if d else None,
            }
        )

    painting_stress = []
    for p in sorted((ROOT / "eval/photos/known").glob("*.jpg")):
        loc, d, ms, _ = run_one(p, f"artwork:{p.stem.split('__')[0]}")
        painting_stress.append(
            {
                "file": p.name,
                "variant": p.stem.split("__")[1],
                "located": loc is not None,
                "status": d.status if d else None,
                "regions": len(d.regions) if d else None,
                "kinds": sorted({g.kind for g in d.regions}) if d else [],
            }
        )

    def pick(case=None, domain=None):
        return [
            r
            for r in rows
            if (case is None or r["case"] == case)
            and (domain is None or ("corner_error" in r) == (domain == "art"))
        ]

    art = {
        c: {
            "photos": len(pick(c)),
            "located": sum(r["located"] for r in pick(c)),
            "ok": sum(r["ok"] for r in pick(c)),
            "corner_error_median": round(
                statistics.median(
                    [r["corner_error"] for r in pick(c) if r["corner_error"] is not None]
                    or [float("nan")]
                ),
                4,
            ),
        }
        for c in ("whole", "crop50", "crop30")
    }
    art_photos = [r for r in rows if "corner_error" in r and r["located"]]
    art_diff = {
        "photos": len(art_photos),
        "zero_regions": sum(r.get("status") == "same" for r in art_photos),
        "global_change": sum(r.get("status") == "global_change" for r in art_photos),
        "false_regions_total": sum(r.get("regions") or 0 for r in art_photos),
        "false_regions_max": max((r.get("regions") or 0 for r in art_photos), default=0),
    }
    same = pick("same")
    mfg = {
        "same": {
            "photos": len(same),
            "located": sum(r["located"] for r in same),
            "zero_regions": sum(r.get("regions") == 0 and r.get("status") == "same" for r in same),
            "global_change": sum(r.get("status") == "global_change" for r in same),
            "false_regions_max": max((r.get("regions") or 0) for r in same),
        }
    }
    for c in ("hole", "erase", "revA"):
        rs = pick(c)
        mfg[c] = {
            "photos": len(rs),
            "located": sum(r["located"] for r in rs),
            "all_found": sum(r.get("found") == r.get("expected") for r in rs),
            "changes_found": sum(r.get("found", 0) for r in rs),
            "changes_expected": sum(r.get("expected", 0) for r in rs),
            "false_regions_total": sum(r.get("false_regions", 0) for r in rs),
            "false_regions_max": max((r.get("false_regions", 0) for r in rs), default=0),
        }
    pairs, pair_lat = [], []
    for row in truth:
        if row["domain"] == "pair":
            r, ms = run_pair(row)
            pairs.append(r)
            pair_lat.append(ms)
            detail = json.dumps({k: v for k, v in r.items() if k != "file"}, ensure_ascii=False)
            print(f"  {row['file']:<40} {detail}")
    pl = sorted(pair_lat)
    edits, edit_lat = [], []
    for row in truth:
        if row["domain"] == "artedit":
            r, ms = run_edit(row)
            edits.append(r)
            edit_lat.append(ms)
            detail = json.dumps({k: v for k, v in r.items() if k != "file"}, ensure_ascii=False)
            print(f"  {row['file']:<40} {detail}")

    lat = sorted(latencies)
    summary = {
        "art": art,
        "mfg": mfg,
        "stress_old_photos": {
            "photos": len(stress),
            "located": sum(s["located"] for s in stress),
            "zero_regions": sum(s["status"] == "same" for s in stress),
            "with_regions": [
                f"{s['file']}（{s['status']}，{s['regions']} 處）"
                for s in stress
                if s["status"] not in ("same", None)
            ],
        },
        "art_diff_unmodified": art_diff,
        "art_edits": edit_summary(edits),
        "stress_painting_photos": {
            v: {
                "photos": sum(s["variant"] == v for s in painting_stress),
                "located": sum(s["variant"] == v and s["located"] for s in painting_stress),
                "zero_regions": sum(
                    s["variant"] == v and s["status"] == "same" for s in painting_stress
                ),
                "with_regions": [
                    f"{s['file']}（{s['status']}，{s['regions']} 處 {'/'.join(s['kinds'])}）"
                    for s in painting_stress
                    if s["variant"] == v and s["status"] not in ("same", None)
                ],
            }
            for v in sorted({s["variant"] for s in painting_stress})
        },
        "art_diff_latency_ms_p50": round(statistics.median(edit_lat)) if edit_lat else None,
        "pair": pair_summary(pairs),
        "pair_latency_ms_p50": round(statistics.median(pl)) if pl else None,
        "pair_latency_ms_p95": round(pl[int(0.95 * (len(pl) - 1))]) if pl else None,
        "latency_ms_p50": round(statistics.median(lat)),
        "latency_ms_p95": round(lat[int(0.95 * (len(lat) - 1))]),
    }
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kind": "align",
        "params": get_models_config().image_compare.model_dump(mode="json"),
        "elapsed_s": round(time.time() - t0),
        "summary": summary,
        "photos": rows,
        "pairs": pairs,
        "art_edits": edits,
        "stress": stress,
        "stress_painting": painting_stress,
    }
    path = ROOT / "eval" / "runs" / f"{run_id}-align.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n畫作位置（四角誤差 ≤ 原圖長邊 2% 算對）：")
    for c, a in art.items():
        print(
            f"  {c:<7} 對上 {a['located']}/{a['photos']}、位置正確 {a['ok']}/{a['photos']}、"
            f"誤差中位數 {a['corner_error_median'] * 100:.2f}%"
        )
    a = art_diff
    print(
        f"畫作找不同（照片 vs 原圖）沒改過：0 處差異 {a['zero_regions']}/{a['photos']}、"
        f"整體變了 {a['global_change']}、假差異共 {a['false_regions_total']} 處"
        f"（單張最多 {a['false_regions_max']}）"
    )
    for c, m in summary["art_edits"].items():
        kinds = "、".join(f"{k} {v}" for k, v in m["kinds"].items() if v)
        print(
            f"  {c:<7}：對上 {m['located']}/{m['photos']}、找到 {m['found']}/{m['expected']}"
            f"（{kinds or '—'}）、假差異共 {m['false_regions_total']} 處"
            f"（單張最多 {m['false_regions_max']}）"
        )
    for v, m in summary["stress_painting_photos"].items():
        listed = "、".join(m["with_regions"]) or "無"
        print(
            f"  壓力測試 {v:<5}：對上 {m['located']}/{m['photos']}、"
            f"0 處差異 {m['zero_regions']}；有差異：{listed}"
        )
    s = mfg["same"]
    print(
        f"圖紙沒改過：對上 {s['located']}/{s['photos']}、"
        f"0 處差異 {s['zero_regions']}/{s['photos']}、"
        f"整體變了 {s['global_change']}、最多 {s['false_regions_max']} 處假差異"
    )
    for c in ("hole", "erase", "revA"):
        m = mfg[c]
        print(
            f"圖紙 {c:<5}：對上 {m['located']}/{m['photos']}、"
            f"全部找到 {m['all_found']}/{m['photos']}"
            f"（改動 {m['changes_found']}/{m['changes_expected']}）、"
            f"假差異共 {m['false_regions_total']} 處（單張最多 {m['false_regions_max']}）"
        )
    st = summary["stress_old_photos"]
    listed = "、".join(st["with_regions"]) or "無"
    zero = f"{st['zero_regions']}/{st['photos']}"
    print(f"壓力測試（舊模擬照，沒改過）：0 處差異 {zero}；有差異：{listed}")
    print("兩張照片互比（畫作，同光線）：")
    for c, m in summary["pair"].items():
        if c == "same":
            print(
                f"  same  ：對上 {m['located']}/{m['pairs']}、"
                f"0 處差異 {m['zero_regions']}/{m['pairs']}、整體變了 {m['global_change']}、"
                f"假差異共 {m['false_regions_total']} 處（單張最多 {m['false_regions_max']}）"
            )
        else:
            kinds = "、".join(f"{k} {v}" for k, v in m["kinds"].items() if v)
            print(
                f"  {c:<6}：對上 {m['located']}/{m['pairs']}、"
                f"找到 {m['found']}/{m['expected']}（{kinds or '—'}）、"
                f"假差異共 {m['false_regions_total']} 處（單張最多 {m['false_regions_max']}）"
            )
    print(f"延遲 P50 {summary['latency_ms_p50']} ms、P95 {summary['latency_ms_p95']} ms")
    p50, p95 = summary["pair_latency_ms_p50"], summary["pair_latency_ms_p95"]
    print(f"兩張照片互比延遲 P50 {p50} ms、P95 {p95} ms")
    print(f"畫作找不同（照片 vs 原圖）延遲 P50 {summary['art_diff_latency_ms_p50']} ms")
    print(f"→ {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
