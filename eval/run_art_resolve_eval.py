"""沒說畫名的畫作問題：智慧助理判斷是哪一幅畫的正確率（docs/adr/038）。

題目在 eval/qa_art_resolve.jsonl：12 題只用描述指稱某一幅（expect＝畫作 ID），6 題不是在問特定一幅
（expect＝null：一般知識、美術館資訊、多幅比較或排名）。dev 用來調說明文字，holdout 只在最後驗證。
整條路徑都走 /agent/route（本地分流判錯意圖、沒呼叫判斷，也照實記成錯）。

用法：uv run --project backend python eval/run_art_resolve_eval.py
      [--engines jev,local] [--split all]
需後端在執行；engine=local 要評估模式（EVAL_CONTROLS=true）。
結果存 eval/runs/experiments/<run_id>-art-resolve.json。
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent


def run(client: httpx.Client, engine: str, items: list[dict]) -> dict:
    rows = []
    for q in items:
        body = {"question": q["question"]}
        if engine == "local":
            body["engine"] = "local"
        r = client.post("/api/v1/agent/route", json=body)
        r.raise_for_status()
        d = r.json()
        got = d["dispatch"].get("artwork_id")
        info = d.get("art_resolve") or {}
        rows.append(
            {
                "id": q["id"],
                "split": q["split"],
                "expect": q["expect"],
                "got": got,
                "ok": got == q["expect"],
                "intent": d["intent"],
                "by": info.get("by"),
                "prob": info.get("prob"),
                "ms": info.get("ms"),
                "fallback_reason": info.get("fallback_reason"),
            }
        )
        row = rows[-1]
        print(
            f"  {'✓' if row['ok'] else '✗'} {q['id']} [{q['split']}] {d['intent']:10} "
            f"expect={q['expect']} got={got} {row['by'] or ''} {row['ms'] or ''}ms"
        )
    pos = [r for r in rows if r["expect"]]
    neg = [r for r in rows if not r["expect"]]
    timed = sorted(r["ms"] for r in rows if r["ms"])
    summary = {
        "n": len(rows),
        "correct": sum(r["ok"] for r in rows),
        "resolved": sum(r["ok"] for r in pos),
        "positives": len(pos),
        "wrong_artwork": sum(bool(r["got"]) and not r["ok"] for r in rows),
        "negatives_ok": sum(r["ok"] for r in neg),
        "negatives": len(neg),
        "p50_ms": timed[len(timed) // 2] if timed else None,
    }
    return {"engine": engine, "summary": summary, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--engines", default="jev,local")
    ap.add_argument("--split", default="all", choices=["dev", "holdout", "all"])
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    items = [
        json.loads(x) for x in (EVAL / "qa_art_resolve.jsonl").read_text("utf-8").splitlines() if x
    ]
    items = [q for q in items if args.split == "all" or q["split"] == args.split]
    client = httpx.Client(base_url=args.base, timeout=120)
    client.get("/api/v1/auth/accounts").raise_for_status()
    results = []
    for engine in args.engines.split(","):
        print(f"▷ {engine}")
        results.append(run(client, engine, items))
    print("\n摘要")
    for res in results:
        s = res["summary"]
        print(
            f"  {res['engine']:6} 答對 {s['correct']}/{s['n']} · "
            f"認出 {s['resolved']}/{s['positives']} · "
            f"認錯成別幅 {s['wrong_artwork']} · 反例 {s['negatives_ok']}/{s['negatives']} · "
            f"判斷 p50 {s['p50_ms']} ms"
        )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = EVAL / "runs" / "experiments" / f"{run_id}-art-resolve.json"
    data = {"run_id": run_id, "split": args.split, "results": results}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"已存 {out.relative_to(EVAL.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
