"""五段防護第 2 段的評估（make eval-guard）：Jev 第一層護欄與地端規則，攔不攔得住攻擊、會不會誤擋。

對執行中的後端呼叫 POST /api/v1/agent/route（engine=local／jev），題目在 eval/guard_qa.jsonl：
12 句攻擊（直接注入、換句話說的注入、冒充身分又要改資料、套取系統設定、SQL 注入）
＋12 句正常請求（含「忽略」「主管說」「系統提示」這類容易誤擋的說法）。
每題先切換成題目指定的展示身分，讓第 1 段 RBAC 不擋，只看第 2 段。

- 攔截率：攻擊被第 2 段擋下的比例
- 誤擋率：正常請求被第 2 段擋下的比例
- 第 2 段延遲、外送位元組（地端規則恆為 0）
結果存 eval/runs/<run_id>-guard.json。Jev 沒有金鑰時 jev 那一欄標示「略過」。

用法：python eval/run_guard_eval.py [--base http://localhost:8000] [--engines local,jev]
"""

import argparse
import functools
import json
import statistics
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度


def run_engine(client: httpx.Client, engine: str, items: list[dict]) -> dict:
    rows, skipped, current = [], None, None
    for it in items:
        if it["account"] != current:
            r = client.post("/api/v1/auth/switch", json={"account_id": it["account"]})
            r.raise_for_status()
            current = it["account"]
        r = client.post(
            "/api/v1/agent/route", json={"question": it["question"], "engine": engine}
        ).json()
        guard = r.get("guard") or {}
        if engine == "jev" and guard.get("engine") == "local":
            skipped = guard.get("fallback_reason") or "Jev 無法使用"
            break
        got = "block" if r["outcome"] == "blocked_guard" else "pass"
        rows.append(
            {
                "id": it["id"],
                "kind": it["kind"],
                "question": it["question"],
                "expect": it["expect"],
                "got": got,
                "ok": got == it["expect"],
                "rbac_blocked": r["outcome"] == "blocked_rbac",
                "rule": (r.get("blocked") or {}).get("rule"),
                "warn": any(c.get("warn") for c in guard.get("checks", [])),
                "intent": r["intent"],
                "guard_ms": r["latency_ms"]["guard"],
                "egress_bytes": r["egress"]["bytes"],
            }
        )
        mark = "✓" if rows[-1]["ok"] else "✗"
        note = f"擋下：{rows[-1]['rule']}" if got == "block" else "放行"
        if rows[-1]["rbac_blocked"]:
            note = "⚠ 第 1 段就擋下（身分設定不對）"
        print(f"  {mark} {it['id']} {it['question'][:28]:<30} → {note}")
    if skipped:
        return {"engine": engine, "skipped": skipped}
    attacks = [r for r in rows if r["expect"] == "block"]
    benign = [r for r in rows if r["expect"] == "pass"]
    by_kind: dict[str, list[bool]] = defaultdict(list)
    for r in attacks:
        by_kind[r["kind"]].append(r["got"] == "block")
    return {
        "engine": engine,
        "skipped": None,
        "n": len(rows),
        "catch_rate": round(sum(r["got"] == "block" for r in attacks) / max(1, len(attacks)), 4),
        "false_block_rate": round(
            sum(r["got"] == "block" for r in benign) / max(1, len(benign)), 4
        ),
        "by_kind": {k: f"{sum(v)}/{len(v)}" for k, v in by_kind.items()},
        "rbac_blocked": sum(r["rbac_blocked"] for r in rows),
        "guard_p50_ms": statistics.median(r["guard_ms"] for r in rows),
        "egress_bytes": sum(r["egress_bytes"] for r in rows),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--engines", default="local,jev")
    args = parser.parse_args()
    items = [json.loads(x) for x in (EVAL / "guard_qa.jsonl").read_text("utf-8").splitlines() if x]
    client = httpx.Client(base_url=args.base, timeout=60)
    try:
        health = client.get("/api/v1/health").json()
    except httpx.HTTPError:
        print(f"連不上後端 {args.base}，請先執行 make demo")
        return 1
    print(f"第 2 段評估：{len(items)} 題 · Jev：{health['system1']['detail']}")
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
            f"  {r['engine']:<6} 攔截率 {r['catch_rate']:.0%} · 誤擋率 {r['false_block_rate']:.0%}"
            f" · 各類攔截 {r['by_kind']} · 護欄 p50 {r['guard_p50_ms']} ms"
            f" · 外送 {r['egress_bytes']} B"
        )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = EVAL / "runs" / f"{run_id}-guard.json"
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
