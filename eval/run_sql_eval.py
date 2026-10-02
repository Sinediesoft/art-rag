"""工廠庫存 Text-to-SQL 評估（make eval-sql）：對執行中的後端跑 eval/sql_qa.jsonl，
結果存 eval/runs/<run_id>-sql.json。

- 題目與 prompt 內的 few-shot 範例不同（範例在 shared/prompts/sql_v1_examples.json）
- 執行正確率（execution accuracy）：模型的 SQL 與標準 SQL 在同一個資料庫上的查詢結果相同。
  比對方式：標準答案的每一欄，都要在模型結果中找到值集合相同的一欄——允許模型多輸出欄位
  （例如多給品名）、忽略列順序與重複列；數值四捨五入到小數 2 位
- 另記錄可執行率、平均嘗試次數（1＝一次就對，錯誤訊息回饋給模型後修正成功的次數另計）、延遲

用法：python eval/run_sql_eval.py [--base http://localhost:8000] [--strategy hybrid|mock]
"""

import argparse
import functools
import json
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
sys.path.insert(0, str(ROOT / "backend"))
print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度

from app.repositories.inventory_repo import get_inventory_repo  # noqa: E402


def sse_events(client: httpx.Client, body: dict) -> list[tuple[str, dict]]:
    events = []
    with client.stream("POST", "/api/v1/inventory/ask", json=body, timeout=300) as r:
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                if "event" in lines:
                    events.append((lines["event"], json.loads(lines["data"])))
    return events


def _norm(v):
    if isinstance(v, float):
        v = round(v, 2)
        return int(v) if v.is_integer() else v
    return v


def results_match(gold: list[list], pred: list[list]) -> bool:
    if not gold:
        return not pred
    if not pred:
        return False
    gold_cols = [{_norm(r[i]) for r in gold} for i in range(len(gold[0]))]
    pred_cols = [{_norm(r[j]) for r in pred} for j in range(len(pred[0]))]
    used: set[int] = set()
    for g in gold_cols:
        j = next((j for j, p in enumerate(pred_cols) if j not in used and p == g), None)
        if j is None:
            return False
        used.add(j)
    return True


def p95(values: list[float]) -> float | None:
    values = sorted(values)
    return values[min(len(values) - 1, round(0.95 * (len(values) - 1)))] if values else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--strategy", default="hybrid")
    args = parser.parse_args()
    repo = get_inventory_repo()
    repo.ensure_built()
    qa = [json.loads(line) for line in (EVAL / "sql_qa.jsonl").read_text().splitlines() if line]
    client = httpx.Client(base_url=args.base, timeout=300)
    # 資料範圍（docs/adr/012）：訪客不能讀工廠圖紙與工廠資料庫，用看得到全部資料的主管身分評估
    client.post("/api/v1/auth/switch", json={"account_id": "manager"}).raise_for_status()
    health = client.get("/api/v1/health").json()
    rows = []
    for q in qa:
        gold = repo.run_readonly(q["gold_sql"], 1000, 5000).rows
        t0 = time.perf_counter()
        events = sse_events(client, {"question": q["question"], "strategy": args.strategy})
        sqls = [d for e, d in events if e == "sql"]
        result = next((d for e, d in events if e == "result"), None)
        done = next((d for e, d in events if e == "done"), {})
        error = next((d for e, d in events if e == "error"), None)
        ok = result is not None and results_match(gold, result["rows"])
        rows.append(
            {
                "id": q["id"],
                "type": q["type"],
                "question": q["question"],
                "executable": result is not None,
                "correct": ok,
                "attempts": len(sqls),
                "sql": sqls[-1]["sql"] if sqls else None,
                "errors": [s["error"] for s in sqls if s["error"]],
                "error": error,
                "rows": result["row_count"] if result else None,
                "latency_ms": done.get("latency_ms"),
                "wall_s": round(time.perf_counter() - t0, 1),
                "model": done.get("model"),
                "egress": done.get("egress"),
            }
        )
        r = rows[-1]
        mark = "✓" if ok else ("✗" if r["executable"] else "⚠")
        print(f"{mark} {q['id']} [{r['attempts']} 次，{r['wall_s']} 秒] {q['question']}")
        if not ok:
            print(f"    模型 SQL：{(r['sql'] or '').replace(chr(10), ' ')}")
            print(f"    標準答案：{gold[:5]}　模型結果：{(result or {}).get('rows', [])[:5]}")

    by_type: dict[str, list[bool]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r["correct"])
    totals = [r["latency_ms"]["total"] for r in rows if r["latency_ms"]]
    sql_ms = [r["latency_ms"]["sql"] for r in rows if r["latency_ms"]]
    summary = {
        "n": len(rows),
        "execution_accuracy": sum(r["correct"] for r in rows) / len(rows),
        "executable_rate": sum(r["executable"] for r in rows) / len(rows),
        "first_try_rate": sum(r["executable"] and r["attempts"] == 1 for r in rows) / len(rows),
        "repaired": sum(r["executable"] and r["attempts"] > 1 for r in rows),
        "avg_attempts": statistics.mean(r["attempts"] for r in rows),
        "by_type": {k: sum(v) / len(v) for k, v in sorted(by_type.items())},
        "p50_total_ms": statistics.median(totals) if totals else None,
        "p95_total_ms": p95(totals),
        "p50_sql_ms": statistics.median(sql_ms) if sql_ms else None,
        "egress_bytes": sum((r["egress"] or {}).get("bytes", 0) for r in rows),
    }
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "strategy": args.strategy,
        "model": next((r["model"] for r in rows if r["model"]), None),
        "hybrid_model": health["strategies"]["hybrid"]["model"],
        "inventory_as_of": repo.as_of,
        "summary": summary,
        "rows": rows,
        "type_counts": Counter(r["type"] for r in rows),
    }
    path = EVAL / "runs" / f"{run_id}-sql.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"\n執行正確率 {summary['execution_accuracy']:.0%}（{sum(r['correct'] for r in rows)}/"
        f"{len(rows)}）、可執行率 {summary['executable_rate']:.0%}、一次就能執行 "
        f"{summary['first_try_rate']:.0%}、修正後成功 {summary['repaired']} 題；"
        f"P50 {summary['p50_total_ms']} ms、P95 {summary['p95_total_ms']} ms；外送 "
        f"{summary['egress_bytes']} bytes"
    )
    print(f"各題型：{summary['by_type']}")
    print(f"結果存到 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
