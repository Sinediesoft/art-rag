"""3D 重建評估拆兩半的後半（docs/5070ti-host.md）。

在 Linux／WSL 執行 eval/cad_generate.py 產生的程式碼，算 IoU。和 cad_service 走同一條路：
extract_code → run_cad（AST 白名單＋受限子行程，scale_to＝索引裡的外形尺寸，
gt_step＝data/index/parts/gt/<id>.step）。
平均 IoU 的算法和 eval/run_cad_eval.py 相同（無法執行的算 0）。

只需要 CadQuery 等少數套件，不用整個後端環境：
  uv venv ~/artrag-cad --python 3.12
  uv pip install --python ~/artrag-cad/bin/python cadquery==2.8.0 cadquery-ocp==7.9.3.1.1 \\
      numpy pillow opencv-python-headless
  ~/artrag-cad/bin/python eval/cad_score.py ortho2cad qwen3-vl_8b-instruct
（參數是 data/cad_codes/ 底下的資料夾。）結果存 eval/runs/experiments/<run_id>-cadscore.json；
不放 eval/runs/ 第一層，/eval/runs 會把它當問答評估讀。
"""

import asyncio
import json
import statistics
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.cad.sandbox import LIMITS_AVAILABLE, extract_code, run_cad  # noqa: E402


def score(label: str, parts: dict, jobs: Path) -> dict:
    rows = []
    for f in sorted((ROOT / "data/cad_codes" / label).glob("mfg-*.py")):
        p = parts[f.stem]
        g = p["geometry"]
        run = asyncio.run(
            run_cad(
                extract_code(f.read_text(encoding="utf-8")),
                jobs / label / f.stem,
                scale_to={k: g[k] for k in ("width", "depth", "height")},
                gt_step=ROOT / f"data/index/parts/gt/{f.stem}.step",
                timeout_s=60,
            )
        )
        rows.append(
            {
                "part_id": f.stem,
                "name": p["name"]["zh"],
                "ok": run.ok,
                "error": run.error,
                "iou": run.data.get("iou") or 0.0,
                "iou_bbox": run.data.get("iou_bbox") or 0.0,
                "repaired": run.data.get("repaired"),
            }
        )
        r = rows[-1]
        print(
            f"  {'✓' if r['ok'] else '✗'} [{label}] {r['part_id']} {r['name']}："
            f"IoU {r['iou']:.3f}／外框對齊 {r['iou_bbox']:.3f}"
            f"{'（已自動修復）' if r['repaired'] else ''}"
            f"{'，' + r['error'][:60] if r['error'] else ''}",
            flush=True,
        )
    return {
        "valid_rate": sum(r["ok"] for r in rows) / max(len(rows), 1),
        "mean_iou": statistics.mean(r["iou"] for r in rows) if rows else 0.0,
        "mean_iou_bbox": statistics.mean(r["iou_bbox"] for r in rows) if rows else 0.0,
        "rows": rows,
    }


def main() -> int:
    if not LIMITS_AVAILABLE:
        print(
            "這台電腦限制不了子行程（Windows），不執行模型產生的程式碼：請在 WSL／Linux／macOS 跑"
        )
        return 1
    labels = sys.argv[1:]
    if not labels:
        print("用法：python eval/cad_score.py <data/cad_codes 底下的資料夾> ...")
        return 1
    index = json.loads((ROOT / "data/index/parts/parts.json").read_text(encoding="utf-8"))
    parts = {p["id"]: p for p in index}  # geometry 是建索引時算的，不在 kb/parts/*.json
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    jobs = Path.home() / "artrag-cad-jobs" / run_id
    results = {}
    for label in labels:
        results[label] = score(label, parts, jobs)
        s = results[label]
        gen = ROOT / "data/cad_codes" / label / "generate.json"
        if gen.exists():
            s["generate"] = json.loads(gen.read_text(encoding="utf-8"))
        print(
            f"{label}：可執行 {s['valid_rate']:.0%}，平均 IoU {s['mean_iou']:.3f}"
            f"（外框對齊 {s['mean_iou_bbox']:.3f}）"
        )
    out = ROOT / "eval/runs/experiments" / f"{run_id}-cadscore.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id, "created_at": datetime.now(UTC).isoformat(), "results": results}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
