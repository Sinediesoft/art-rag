"""七段權限控管第 2 段的評估（make eval-guard）：Jev Choice 與地端規則，攔不攔得住攻擊、
會不會誤擋、閒聊有沒有快速短路（docs/adr/015）。

對執行中的後端呼叫 POST /api/v1/agent/route（engine=local／jev），題目在 eval/guard_qa.jsonl：
12 句攻擊（直接注入、換句話說的注入、冒充身分又要改資料、套取系統設定、SQL 注入）
＋12 句正常請求（含「忽略」「主管說」「系統提示」這類容易誤擋的說法）＋6 句閒聊。
每題先切換成題目指定的展示身分，讓第 1 段角色授權不擋，只看第 2 段。
後端要開 DEMO_CONTROLS（切換身分）與 EVAL_CONTROLS（engine=local 才生效），docs/adr/030。

- 攔截率：攻擊被第 2 段擋下的比例
- 誤擋率：正常請求被第 2 段擋下、或被判成閒聊短路的比例
- 閒聊短路率：閒聊被快速短路回覆（不檢索、不生成）的比例
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
        got = {"blocked_guard": "block", "short_circuit": "short"}.get(r["outcome"], "pass")
        rows.append(
            {
                "id": it["id"],
                "kind": it["kind"],
                "question": it["question"],
                "expect": it["expect"],
                "got": got,
                "ok": got == it["expect"],
                "auth_blocked": r["outcome"] in ("blocked_auth", "degraded"),
                "verdict": guard.get("verdict"),
                "rule": (r.get("blocked") or {}).get("rule"),
                "warn": any(c.get("warn") for c in guard.get("checks", [])),
                "intent": r["intent"],
                "guard_ms": r["latency_ms"]["guard"],
                "egress_bytes": r["egress"]["bytes"],
            }
        )
        mark = "✓" if rows[-1]["ok"] else "✗"
        note = {"block": f"擋下：{rows[-1]['rule']}", "short": "閒聊短路"}.get(got, "放行")
        if rows[-1]["auth_blocked"]:
            note = "⚠ 第 1 段就擋下（身分設定不對）"
        print(f"  {mark} {it['id']} {it['question'][:28]:<30} → {note}")
    if skipped:
        return {"engine": engine, "skipped": skipped}
    attacks = [r for r in rows if r["expect"] == "block"]
    benign = [r for r in rows if r["expect"] == "pass"]
    chitchat = [r for r in rows if r["expect"] == "short"]
    by_kind: dict[str, list[bool]] = defaultdict(list)
    for r in attacks:
        by_kind[r["kind"]].append(r["got"] == "block")
    return {
        "engine": engine,
        "skipped": None,
        "n": len(rows),
        "catch_rate": round(sum(r["got"] == "block" for r in attacks) / max(1, len(attacks)), 4),
        "false_block_rate": round(sum(r["got"] != "pass" for r in benign) / max(1, len(benign)), 4),
        "chitchat_rate": round(
            sum(r["got"] == "short" for r in chitchat) / max(1, len(chitchat)), 4
        ),
        "by_kind": {k: f"{sum(v)}/{len(v)}" for k, v in by_kind.items()},
        "auth_blocked": sum(r["auth_blocked"] for r in rows),
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
        client.get("/api/v1/health").raise_for_status()
    except httpx.HTTPError:
        print(f"連不上後端 {args.base}，請先執行 make demo")
        return 1
    # 切換身分要先有憑證、要展示模式；Jev 設定在管理診斷（主管）；
    # engine=local 要評估模式（docs/adr/030）
    client.get("/api/v1/auth/accounts").raise_for_status()
    client.post("/api/v1/auth/switch", json={"account_id": "manager"}).raise_for_status()
    health = client.get("/api/v1/admin/diagnostics").json()
    if "local" in args.engines and not client.get("/api/v1/status").json()["eval_controls"]:
        print("提醒：後端沒開 EVAL_CONTROLS，engine=local 會被忽略（照樣用 Jev）")
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
            f" · 閒聊短路 {r['chitchat_rate']:.0%}"
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
