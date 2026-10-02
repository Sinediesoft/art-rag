"""工廠圖紙評估（make eval-cad）：對執行中的後端跑兩件事，結果存 eval/runs/<run_id>-cad.json。

1. 圖紙辨識：eval/drawing_photos/known（應辨識為對應圖紙）與 unknown（應拒答）
   ——kb_staging 的圖紙（mfg-007）在 demo-add 之前也算「未收錄」
2. 3D 重建：每張知識庫圖紙送 Ortho2CAD（/cad/reconstruct），記錄程式碼可執行率、
   與標準模型的 IoU（論文的評估法：對齊質心與主軸、正規化尺度）、外形尺寸誤差、延遲
   --baseline 另外跑未微調的 Qwen3-VL（strategy=hybrid）當對照組

用法：python eval/run_cad_eval.py [--base http://localhost:8000] [--skip-photos] [--baseline]
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
PHOTOS = Path(__file__).parent / "drawing_photos"


def sse_events(client: httpx.Client, url: str, body: dict) -> list[tuple[str, dict]]:
    events = []
    with client.stream("POST", url, json=body, timeout=600) as r:
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                if "event" in lines:
                    events.append((lines["event"], json.loads(lines["data"])))
    return events


def eval_photos(client: httpx.Client, api: str, kb_ids: set[str]) -> dict:
    rows = []
    for path in sorted(PHOTOS.glob("*/*.jpg")):
        truth = path.stem.split("__")[0]
        in_kb = truth in kb_ids
        with path.open("rb") as f:
            image_id = client.post(
                f"{api}/images", files={"file": (path.name, f, "image/jpeg")}
            ).json()["image_id"]
        r = client.post(f"{api}/search/drawing", json={"image_id": image_id}).json()
        rows.append(
            {
                "photo": f"{path.parent.name}/{path.name}",
                "truth": truth if in_kb else None,
                "predicted": r["best_part_id"],
                "ok": (r["best_part_id"] == truth) if in_kb else not r["matched"],
                "latency_ms": r["latency_ms"],
            }
        )
        print(f"  {'✓' if rows[-1]['ok'] else '✗'} {rows[-1]['photo']:36s} → {r['best_part_id']}")
    known = [r for r in rows if r["truth"]]
    unknown = [r for r in rows if not r["truth"]]
    return {
        "top1": sum(r["ok"] for r in known) / max(len(known), 1),
        "reject_rate": sum(r["ok"] for r in unknown) / max(len(unknown), 1),
        "n_known": len(known),
        "n_unknown": len(unknown),
        "rows": rows,
    }


def eval_reconstruction(client: httpx.Client, api: str, parts: list[dict], strategy: str) -> dict:
    rows = []
    # 照片建檔的零件沒有標準模型（docs/adr/013），算不出 IoU，不列入
    for p in (p for p in parts if p.get("model_url")):
        body = {"part_id": p["id"], "strategy": strategy}
        events = sse_events(client, f"{api}/cad/reconstruct", body)
        err = next((d for e, d in events if e == "error"), None)
        res = next((d for e, d in events if e == "result"), None)
        done = next((d for e, d in events if e == "done"), None)
        g = p["geometry"]
        dims = (res or {}).get("dims") or {}
        dim_err = (
            max(abs(dims[k] - g[k]) / g[k] for k in ("width", "depth", "height")) if dims else None
        )
        rows.append(
            {
                "part_id": p["id"],
                "name": p["name_zh"],
                "ok": bool(res and res["ok"]),
                "error": (err or {}).get("message") or (res or {}).get("error"),
                "iou": (res or {}).get("iou") or 0.0,
                "iou_bbox": (res or {}).get("iou_bbox") or 0.0,
                "max_dim_error": dim_err,
                "job_id": (done or {}).get("job_id"),
                "first_token_ms": ((done or {}).get("latency_ms") or {}).get("first_token"),
                "total_ms": ((done or {}).get("latency_ms") or {}).get("total"),
                "output_tokens": ((done or {}).get("tokens") or {}).get("output"),
            }
        )
        r = rows[-1]
        print(
            f"  {'✓' if r['ok'] else '✗'} [{strategy}] {p['id']} {p['name_zh']}："
            f"IoU {r['iou']:.3f}／外框對齊 {r['iou_bbox']:.3f}"
            f"，{(r['total_ms'] or 0) / 1000:.0f} 秒{'，' + r['error'] if r['error'] else ''}"
        )
    ious = [r["iou"] for r in rows]
    totals = [r["total_ms"] for r in rows if r["total_ms"]]
    return {
        "valid_rate": sum(r["ok"] for r in rows) / max(len(rows), 1),
        "mean_iou": statistics.mean(ious) if ious else 0.0,
        "mean_iou_bbox": statistics.mean(r["iou_bbox"] for r in rows) if rows else 0.0,
        "median_total_ms": statistics.median(totals) if totals else None,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--skip-photos", action="store_true")
    parser.add_argument("--baseline", action="store_true", help="另跑未微調的 Qwen3-VL 對照組")
    args = parser.parse_args()
    api = args.base.rstrip("/") + "/api/v1"
    client = httpx.Client(timeout=120)
    parts = client.get(f"{api}/parts").json()
    kb_version, items = parts["kb_version"], parts["items"]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kb_version": kb_version,
        "kind": "cad",
        "health": {
            k: v["model"] for k, v in client.get(f"{api}/health").json()["strategies"].items()
        },
    }
    t0 = time.time()
    if not args.skip_photos:
        print("圖紙辨識：")
        out["identification"] = eval_photos(client, api, {p["id"] for p in items})
    print("3D 重建：")
    out["reconstruction"] = {"ortho2cad": eval_reconstruction(client, api, items, "ortho2cad")}
    if args.baseline:
        out["reconstruction"]["hybrid"] = eval_reconstruction(client, api, items, "hybrid")
    out["elapsed_s"] = round(time.time() - t0)
    path = Path(__file__).parent / "runs" / f"{run_id}-cad.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    ident = out.get("identification")
    if ident:
        print(
            f"圖紙辨識 Top-1 {ident['top1']:.0%}（{ident['n_known']} 張），"
            f"拒答 {ident['reject_rate']:.0%}（{ident['n_unknown']} 張）"
        )
    for k, v in out["reconstruction"].items():
        print(
            f"{k}：可執行 {v['valid_rate']:.0%}，平均 IoU {v['mean_iou']:.3f}"
            f"（外框對齊 {v['mean_iou_bbox']:.3f}）"
        )
    print(f"→ {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
