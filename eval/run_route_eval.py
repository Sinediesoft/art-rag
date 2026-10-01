"""智慧助理的路由評估（make eval-route）：Jev 與本地路由在 eval/route_qa.jsonl
的中文句子上誰比較準。

對執行中的後端呼叫 POST /api/v1/agent/route（engine=local／jev），
結果存 eval/runs/<run_id>-route.json。

- 意圖正確率：第一名意圖＝標準答案（標了 gate 的題目改看閘門，例如只說「法蘭」應該出澄清按鈕）
- 修改誤判：查詢被判成修改、或修改被判成查詢（代價最高的錯：一個會進入權限判定，一個會漏掉）
- 修改操作正確率：修改類題目的操作（盤點、報廢、調撥…）是否正確
- 直接處理率：意圖正確而且不必再問（沒有出澄清按鈕）的比例；出澄清按鈕不算錯，但多一次點選
- 閘門分布、平均信心、延遲、外送位元組（本地路由恆為 0）
Jev 沒有金鑰時只跑本地路由，Jev 那一欄標示「未設定金鑰，略過」。
門檻要依這份結果再調（shared/agent.yaml）。

用法：python eval/run_route_eval.py [--base http://localhost:8000] [--engines local,jev]
"""

import argparse
import functools
import json
import statistics
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度


def run_engine(client: httpx.Client, engine: str, items: list[dict]) -> dict:
    rows, skipped = [], None
    for it in items:
        r = client.post(
            "/api/v1/agent/route", json={"question": it["question"], "engine": engine}
        ).json()
        if engine == "jev" and r["engine"] != "jev":
            skipped = r.get("fallback_reason") or "Jev 無法使用"
            break
        expect_gate = it.get("gate")
        ok = r["gate"] == expect_gate if expect_gate else r["intent"] == it["intent"]
        write_err = (r["intent"] == "modify") != (it["intent"] == "modify") and not expect_gate
        op_ok = (r.get("modify_op") or "") == it.get("op", "") if it.get("op") else None
        rows.append(
            {
                "id": it["id"],
                "question": it["question"],
                "expected": expect_gate or it["intent"],
                "got": r["gate"] if expect_gate else r["intent"],
                "ok": ok,
                "write_misfire": write_err,
                "op_expected": it.get("op"),
                "op_got": r.get("modify_op"),
                "op_ok": op_ok,
                "confidence": r["confidence"],
                "gate": r["gate"],
                "ms": r["latency_ms"]["system1"],
                "egress_bytes": r["egress"]["bytes"],
            }
        )
        mark = "✓" if ok else "✗"
        got = f"{rows[-1]['got']} ({r['confidence']:.2f})"
        print(f"  {mark} {it['id']} {it['question'][:24]:<26} → {got}")
    if skipped:
        return {"engine": engine, "skipped": skipped}
    ops = [r for r in rows if r["op_ok"] is not None]
    return {
        "engine": engine,
        "skipped": None,
        "n": len(rows),
        "accuracy": round(sum(r["ok"] for r in rows) / len(rows), 4),
        "auto_rate": round(
            sum(r["ok"] and r["gate"] != "clarify" for r in rows if r["expected"] != "clarify")
            / max(1, sum(r["expected"] != "clarify" for r in rows)),
            4,
        ),
        "write_misfires": sum(r["write_misfire"] for r in rows),
        "op_accuracy": round(sum(r["op_ok"] for r in ops) / len(ops), 4) if ops else None,
        "gates": dict(Counter(r["gate"] for r in rows)),
        "mean_confidence": round(statistics.mean(r["confidence"] for r in rows), 3),
        "p50_ms": statistics.median(r["ms"] for r in rows),
        "egress_bytes": sum(r["egress_bytes"] for r in rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--engines", default="local,jev")
    args = parser.parse_args()
    items = [json.loads(x) for x in (EVAL / "route_qa.jsonl").read_text("utf-8").splitlines() if x]
    client = httpx.Client(base_url=args.base, timeout=60)
    try:
        health = client.get("/api/v1/health").json()
    except httpx.HTTPError:
        print(f"連不上後端 {args.base}，請先執行 make demo")
        return 1
    print(f"路由評估：{len(items)} 題 · System 1：{health['system1']['detail']}")
    results = []
    for engine in args.engines.split(","):
        print(f"\n▷ {engine}")
        results.append(run_engine(client, engine, items))

    print("\n摘要")
    for r in results:
        if r["skipped"]:
            print(f"  {r['engine']:<6} 略過：{r['skipped']}")
            continue
        print(
            f"  {r['engine']:<6} 正確率 {r['accuracy']:.0%}（{r['n']} 題）"
            f" · 直接處理 {r['auto_rate']:.0%}"
            f" · 修改誤判 {r['write_misfires']} 題"
            f" · 操作正確率 {r['op_accuracy']:.0%} · 平均信心 {r['mean_confidence']}"
            f" · p50 {r['p50_ms']} ms · 外送 {r['egress_bytes']} B · 閘門 {r['gates']}"
        )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = EVAL / "runs" / f"{run_id}-route.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": datetime.now(UTC).isoformat(),
                "n_items": len(items),
                "system1": health["system1"],
                "engines": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已存 {out.relative_to(EVAL.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
