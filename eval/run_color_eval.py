"""色彩分析評估（docs/adr/010）：在程序內直接執行，不用開後端；
結果存 eval/runs/<run_id>-color.json。

1. 決定性：每幅原圖連算兩次，分析結果與色塊圖是否完全相同
2. 照片 vs 原圖：eval/photos/known 的模擬照（blur／crop／dim／glare／tilt）與原圖的色盤色差
   （雙向、依占比加權的 CIEDE2000）與冷暖比例差（百分點，取暖／冷／中性中差最多的）
3. 檢索：eval/qa.jsonl 主題為「色彩分析」的題目有沒有取到 <id>#color；其他題有沒有混進色彩段落、
   原本取到的段落有沒有被擠掉（和拿掉色彩段落的檢索結果比）。要先 make index，embedding 依 .env。
   「被擠掉的段落」分兩種計：全部（displaced，原始數字）、
   主題屬於該題 topics 的（displaced_on_topic，答題要用的段落）。
   只有第二種算傷到檢索，是停止條件；第一種只是記錄（色彩段落分數夠高時會擠掉第 5 名）。
   對照組是記憶體裡的 Collection（numpy 內積）；設了 DATABASE_URL 時正式結果走 pgvector，
   兩者排序相同（tests/test_postgres.py 驗證過）
4. 延遲：每張圖的分析時間

用法：python eval/run_color_eval.py
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

from app.analysis.color import analyze, ciede2000  # noqa: E402
from app.core.config import kb_version  # noqa: E402
from app.rag.preprocess import load_image  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
PHOTOS = ROOT / "eval" / "photos" / "known"


def original_path(artwork_id: str) -> Path:
    for d in ("kb", "kb_staging"):
        p = ROOT / d / "images" / f"{artwork_id}.jpg"
        if p.exists():
            return p
    raise FileNotFoundError(artwork_id)


def timed(path: Path) -> tuple[dict, bytes, float]:
    img = load_image(path)
    t0 = time.perf_counter()
    result, png = analyze(img)
    return result, png, (time.perf_counter() - t0) * 1000


def palette_distance(a: dict, b: dict) -> float:
    """雙向：每個主色到對方最近主色的 CIEDE2000，依占比加權，兩個方向取平均。"""
    la, lb = (np.array([p["lab"] for p in x["palette"]]) for x in (a, b))
    wa, wb = (np.array([p["share"] for p in x["palette"]]) for x in (a, b))
    d = ciede2000(la[:, None], lb[None])
    return float((wa @ d.min(1) / wa.sum() + wb @ d.min(0) / wb.sum()) / 2)


def temperature_gap(a: dict, b: dict) -> float:
    return max(abs(a["temperature"][k] - b["temperature"][k]) for k in a["temperature"]) * 100


def retrieval_rows() -> list[dict]:
    from app.repositories.index_store import Collection, get_store
    from app.services.chat_service import retrieve

    store = get_store()
    store.load()
    full = store.art
    keep = [i for i, c in enumerate(full.chunks) if not c["chunk_id"].endswith("#color")]
    without = Collection(
        "artwork_id",
        full.items,
        full.image_vecs,
        [full.chunks[i] for i in keep],
        full.chunk_vecs[keep],
    )
    topic_of = {c["chunk_id"]: c.get("topic") for c in full.chunks}
    rows = []
    for line in (ROOT / "eval" / "qa.jsonl").read_text(encoding="utf-8").splitlines():
        q = json.loads(line)
        if not store.get_artwork(q["artwork_id"]):
            continue  # kb_staging 的畫在 demo-add 之前不在索引裡
        got = [s["chunk_id"] for s in retrieve(q["question"], q["artwork_id"])]
        store.art = without
        try:
            base = [s["chunk_id"] for s in retrieve(q["question"], q["artwork_id"])]
        finally:
            store.art = full
        displaced = [c for c in base if c not in got]
        rows.append(
            {
                "id": q["id"],
                "color_question": "色彩分析" in q.get("topics", []),
                "got_color": any(c.endswith("#color") for c in got),
                "displaced": displaced,
                # 被擠掉的段落裡，主題屬於這題 topics 的（那才是答題要用的）
                "displaced_on_topic": [
                    c for c in displaced if topic_of.get(c) in q.get("topics", [])
                ],
                "chunks": got,
            }
        )
    return rows


def main() -> int:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    t0 = time.time()
    latencies: list[float] = []

    originals = {}
    determinism = []
    for aid in sorted({p.stem.split("__")[0] for p in PHOTOS.glob("*.jpg")}):
        r1, png1, ms = timed(original_path(aid))
        r2, png2, _ = timed(original_path(aid))
        originals[aid] = r1
        latencies.append(ms)
        determinism.append({"artwork_id": aid, "same": r1 == r2 and png1 == png2})
        print(
            f"  {aid}  {r1['summary']}  {'✓ 結果固定' if determinism[-1]['same'] else '✗ 兩次不同'}"
        )

    photos = []
    for path in sorted(PHOTOS.glob("*.jpg")):
        aid, kind = path.stem.split("__")
        r, _, ms = timed(path)
        latencies.append(ms)
        photos.append(
            {
                "photo": path.name,
                "artwork_id": aid,
                "kind": kind,
                "delta_e": round(palette_distance(r, originals[aid]), 2),
                "temperature_gap_pp": round(temperature_gap(r, originals[aid]), 1),
                "latency_ms": round(ms),
            }
        )
        x = photos[-1]
        print(
            f"  {x['photo']:28s} ΔE00 {x['delta_e']:5.2f}  "
            f"冷暖差 {x['temperature_gap_pp']:4.1f} 個百分點"
        )

    by_kind = {
        k: {
            "delta_e_mean": round(
                statistics.mean(x["delta_e"] for x in photos if x["kind"] == k), 2
            ),
            "temperature_gap_pp_mean": round(
                statistics.mean(x["temperature_gap_pp"] for x in photos if x["kind"] == k), 1
            ),
        }
        for k in sorted({x["kind"] for x in photos})
    }

    rows = retrieval_rows()
    color_q = [r for r in rows if r["color_question"]]
    other_q = [r for r in rows if not r["color_question"]]
    lat = sorted(latencies)
    summary = {
        "deterministic": all(d["same"] for d in determinism),
        "photos": len(photos),
        "delta_e_mean": round(statistics.mean(x["delta_e"] for x in photos), 2),
        "temperature_gap_pp_mean": round(
            statistics.mean(x["temperature_gap_pp"] for x in photos), 1
        ),
        "by_kind": by_kind,
        "color_questions": len(color_q),
        "color_hit": sum(r["got_color"] for r in color_q),
        "other_questions": len(other_q),
        "other_with_color": sum(r["got_color"] for r in other_q),
        "other_displaced": sum(bool(r["displaced"]) for r in other_q),
        "other_displaced_on_topic": sum(bool(r["displaced_on_topic"]) for r in other_q),
        "latency_ms_p50": round(statistics.median(lat)),
        "latency_ms_p95": round(lat[int(0.95 * (len(lat) - 1))]),
    }
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kb_version": kb_version(),
        "kind": "color",
        "elapsed_s": round(time.time() - t0),
        "summary": summary,
        "determinism": determinism,
        "photos": photos,
        "retrieval": rows,
    }
    path = ROOT / "eval" / "runs" / f"{run_id}-color.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    s = summary
    print(
        f"結果固定 {'是' if s['deterministic'] else '否'}；"
        f"照片 vs 原圖 ΔE00 平均 {s['delta_e_mean']}、"
        f"冷暖差平均 {s['temperature_gap_pp_mean']} 個百分點；"
        f"顏色題取到色彩段落 {s['color_hit']}/{s['color_questions']}；"
        f"其他題混進色彩段落 {s['other_with_color']}/{s['other_questions']}、"
        f"段落被擠掉 {s['other_displaced']}"
        f"（其中與題目主題相關 {s['other_displaced_on_topic']}）；"
        f"延遲 P50 {s['latency_ms_p50']} ms、P95 {s['latency_ms_p95']} ms"
    )
    print(f"→ {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
