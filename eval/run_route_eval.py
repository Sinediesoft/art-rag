"""智慧助理的路由評估（make eval-route）：七段權限控管的第 1、2 段（docs/adr/015）在
eval/route_qa.jsonl 的中文句子上準不準、會不會誤擋。

對執行中的後端呼叫 POST /api/v1/agent/route（engine＝第 2 段由誰判斷：local 地端規則／jev），
結果存 eval/runs/<run_id>-route.json。要交給哪個模組一律由本地分流判斷，兩種 engine 的意圖結果相同；
Jev 只判斷正常查詢／Prompt 注入／無關閒聊（Jev Choice）。

- 意圖正確率：第一名意圖＝標準答案（標了 gate 的題目改看閘門，例如只說「法蘭」應該出澄清按鈕）
- 修改誤判：查詢被判成修改、或修改被判成查詢（代價最高的錯：一個會進入權限判定，一個會漏掉）
- 修改操作正確率：修改類題目的操作（盤點、報廢、調撥…）是否正確
- 直接處理率：意圖正確而且不必再問（沒有出澄清按鈕）的比例；出澄清按鈕不算錯，但多一次點選
- 第 2 段誤擋：題目都是正常的請求，被第 2 段（Jev Choice 或地端規則）擋下就是誤擋
- 第 2 段誤短路：系統範圍內的請求被判成閒聊、快速短路回覆（超出範圍的題目短路才是對的）
- 閘門分布、平均信心、第 1、2 段延遲、外送位元組（地端規則恆為 0）
每題先切換成做得了這件事的展示身分（修改庫存＝倉管、改訂單＝業務、工單與排程＝生管、其他＝主管），
讓第 1 段角色授權不擋，第 2 段每題都會執行。Jev 沒有金鑰時 jev 那一欄標示「略過」。
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


def account_for(it: dict) -> str:
    """做得了這件事的展示身分：讓第 1 段角色授權不擋，第 2 段每題都會執行。"""
    op = it.get("op", "")
    if it["intent"] == "modify":
        return {"stock": "wh1", "so": "sales_a", "wo": "planner"}.get(op.split("_")[0], "manager")
    return "planner" if it["intent"] == "schedule" else "manager"


def run_engine(client: httpx.Client, engine: str, items: list[dict]) -> dict:
    rows, skipped, current = [], None, None
    for it in items:
        if (account := account_for(it)) != current:
            client.post("/api/v1/auth/switch", json={"account_id": account}).raise_for_status()
            current = account
        r = client.post(
            "/api/v1/agent/route", json={"question": it["question"], "engine": engine}
        ).json()
        guard = r.get("guard") or {}
        if engine == "jev" and guard.get("engine") == "local":
            skipped = guard.get("fallback_reason") or "Jev 無法使用"
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
                "outcome": r["outcome"],
                "verdict": guard.get("verdict"),
                "false_short": r["outcome"] == "short_circuit" and it["intent"] != "out_of_scope",
                "blocked_rule": (r.get("blocked") or {}).get("rule"),
                "ms": r["latency_ms"]["router"],
                "guard_ms": r["latency_ms"]["guard"],
                "egress_bytes": r["egress"]["bytes"],
            }
        )
        mark = "✓" if ok else "✗"
        got = f"{rows[-1]['got']} ({r['confidence']:.2f})"
        blocked = ""
        if r["outcome"] == "blocked_guard":
            blocked = f" ⚠ 第 2 段擋下：{rows[-1]['blocked_rule']}"
        elif rows[-1]["false_short"]:
            blocked = " ⚠ 第 2 段判成閒聊（誤短路）"
        elif r["outcome"] == "short_circuit":
            blocked = " · 閒聊短路"
        print(f"  {mark} {it['id']} {it['question'][:24]:<26} → {got}{blocked}")
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
        "false_blocks": sum(r["outcome"] == "blocked_guard" for r in rows),
        "false_shorts": sum(r["false_short"] for r in rows),
        "auth_blocks": sum(r["outcome"] in ("blocked_auth", "degraded") for r in rows),
        "p50_ms": statistics.median(r["ms"] for r in rows),
        "guard_p50_ms": statistics.median(r["guard_ms"] for r in rows),
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
    print(f"路由評估：{len(items)} 題 · Jev：{health['system1']['detail']}")
    results = []
    for engine in args.engines.split(","):
        print(f"\n▷ {engine}")
        results.append(run_engine(client, engine, items))

    print("\n摘要")
    for r in results:
        if r["skipped"]:
            print(f"  {r['engine']:<6} 略過：{r['skipped']}")
            continue
        # 題目裡沒有標修改操作（op）的題目時，操作正確率是 None
        op_acc = "—" if r["op_accuracy"] is None else f"{r['op_accuracy']:.0%}"
        print(
            f"  {r['engine']:<6} 正確率 {r['accuracy']:.0%}（{r['n']} 題）"
            f" · 直接處理 {r['auto_rate']:.0%}"
            f" · 修改誤判 {r['write_misfires']} 題"
            f" · 操作正確率 {op_acc} · 平均信心 {r['mean_confidence']}"
            f" · 第 2 段誤擋 {r['false_blocks']} 題 · 誤短路 {r['false_shorts']} 題"
            f" · 分流 p50 {r['p50_ms']} ms"
            f" · 護欄 p50 {r['guard_p50_ms']} ms · 外送 {r['egress_bytes']} B · 閘門 {r['gates']}"
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
