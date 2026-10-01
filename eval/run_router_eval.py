"""領域路由評估：所有評估照片都送 /search/any，結果存 eval/runs/<run_id>-router.json。

1. 路由：eval/photos 應判為畫作（art），eval/drawing_photos 應判為工廠圖紙（mfg）；
   記錄每張的 margin（與圖紙原型的相似度 − 與畫作原型的相似度），看兩類之間還有多少間距
2. 端到端：路由之後的辨識——知識庫內的照片要辨識出正確 ID，未收錄的要拒答
   （kb_staging 的項目在 demo-add 之前也算未收錄）

用法：python eval/run_router_eval.py [--base http://localhost:8000]
"""

import argparse
import functools
import json
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度
SETS = {"art": Path(__file__).parent / "photos", "mfg": Path(__file__).parent / "drawing_photos"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def eval_one(client: httpx.Client, api: str, path: Path, domain: str, kb: set[str]) -> dict:
    truth = path.stem.split("__")[0]
    in_kb = truth in kb
    image_id = client.post(
        f"{api}/images", files={"file": (path.name, path.read_bytes(), MIME[path.suffix.lower()])}
    ).json()["image_id"]
    r = client.post(f"{api}/search/any", json={"image_id": image_id}).json()
    route = r["route"]
    if route["domain"] == "art":
        result, best = r["artwork_result"], r["artwork_result"]["best_artwork_id"]
    else:
        result, best = r["drawing_result"], r["drawing_result"]["best_part_id"]
    route_ok = route["domain"] == domain
    return {
        "photo": f"{path.parent.parent.name}/{path.parent.name}/{path.name}",
        "truth_domain": domain,
        "routed": route["domain"],
        "margin": route["margin"],
        "uncertain": route["uncertain"],
        "truth": truth if in_kb else None,
        "predicted": best,
        "route_ok": route_ok,
        "ok": route_ok and ((best == truth) if in_kb else not result["matched"]),
        "latency_ms": r["latency_ms"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()
    api = args.base.rstrip("/") + "/api/v1"
    client = httpx.Client(timeout=120)
    artworks = client.get(f"{api}/artworks").json()
    kb = {
        "art": {a["id"] for a in artworks["items"]},
        "mfg": {p["id"] for p in client.get(f"{api}/parts").json()["items"]},
    }
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    t0 = time.time()
    rows = []
    for domain, folder in SETS.items():
        for path in sorted(p for p in folder.glob("*/*") if p.suffix.lower() in MIME):
            rows.append(eval_one(client, api, path, domain, kb[domain]))
            x = rows[-1]
            print(
                f"  {'✓' if x['ok'] else '✗'} {x['photo']:52s} → {x['routed']}"
                f"（margin {x['margin']:+.3f}{'，不確定' if x['uncertain'] else ''}）"
                f" {x['predicted'] or '拒答'}"
            )

    def margins(domain: str) -> list[float]:
        return [x["margin"] for x in rows if x["truth_domain"] == domain]

    latencies = sorted(x["latency_ms"] for x in rows)
    summary = {
        "n": len(rows),
        "route_accuracy": sum(x["route_ok"] for x in rows) / max(len(rows), 1),
        "confusion": {
            f"{t}->{p}": sum(1 for x in rows if x["truth_domain"] == t and x["routed"] == p)
            for t in SETS
            for p in SETS
        },
        "uncertain": sum(x["uncertain"] for x in rows),
        "art_margin_max": max(margins("art"), default=None),
        "mfg_margin_min": min(margins("mfg"), default=None),
        "end_to_end_accuracy": sum(x["ok"] for x in rows) / max(len(rows), 1),
        "latency_ms_median": statistics.median(latencies) if latencies else None,
        "latency_ms_p95": latencies[int(0.95 * (len(latencies) - 1))] if latencies else None,
    }
    if summary["art_margin_max"] is not None and summary["mfg_margin_min"] is not None:
        summary["gap"] = round(summary["mfg_margin_min"] - summary["art_margin_max"], 4)
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kb_version": artworks["kb_version"],
        "kind": "router",
        "elapsed_s": round(time.time() - t0),
        "summary": summary,
        "rows": rows,
    }
    path = Path(__file__).parent / "runs" / f"{run_id}-router.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    s = summary
    print(
        f"路由正確率 {s['route_accuracy']:.0%}（{s['n']} 張，不確定 {s['uncertain']} 張）；"
        f"畫作 margin ≤ {s['art_margin_max']:+.3f}、圖紙 ≥ {s['mfg_margin_min']:+.3f}"
        f"，間距 {s.get('gap', 0):.3f}；端到端辨識正確率 {s['end_to_end_accuracy']:.0%}"
    )
    print(f"→ {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
