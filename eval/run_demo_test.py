"""展示前整合測試（make demo-test）：沿著「圖紙 → 資料庫 → 生產排程」走一遍。

逐步顯示記憶體使用率與記憶體管理釋放了哪些模型。

對執行中的後端（make demo 或 make demo-all）依序呼叫：
  1. 以文字找圖紙（bge-m3）
  2. 製程問答（bge-m3＋Qwen3-VL）
  3. Ortho2CAD 3D 重建（Ortho2CAD 載入後記憶體通常超過 80%；沒啟動就略過）
  4. 依搜尋結果在圖紙上開立工單（以「生管」身分；急件超過額度，送「主管」核准後才開立）
  5. 生產排程（Timefold；不用任何 AI 模型 → 記憶體超過門檻時釋放其他模型）
  6. Text-to-SQL 查排程結果（Qwen3-VL 重新載入）
每一步都記錄耗時、前後記憶體使用率與釋放紀錄，最後印出摘要，存到 eval/runs/<id>-demo.json。

用法：
    python eval/run_demo_test.py                 # 完整流程
    python eval/run_demo_test.py --skip-cad      # 不跑 3D 重建（省約 1 分鐘）
    python eval/run_demo_test.py --cleanup       # 結束後取消這次開立的工單
"""

import argparse
import functools
import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度

# 展示情境：用文字找到圖紙後開立工單（交期、急件與 README 展示腳本相同）
ORDERS = [
    ("有 6 個螺栓孔的法蘭", 80, "2026-10-16", "急件"),
    ("L 型固定支架", 120, "2026-10-20", "一般"),
    ("階梯傳動軸", 50, "2026-10-21", "一般"),
]


def sse(client: httpx.Client, path: str, body: dict, on_event=None) -> list[tuple[str, dict]]:
    events = []
    with client.stream("POST", f"/api/v1{path}", json=body, timeout=600) as r:
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                if "event" in lines:
                    ev = (lines["event"], json.loads(lines["data"]))
                    events.append(ev)
                    if on_event:
                        on_event(*ev)
    return events


class Recorder:
    def __init__(self, client: httpx.Client):
        self.client = client
        self.steps: list[dict] = []

    def memory(self) -> dict:
        return self.client.get("/api/v1/memory").json()

    def step(self, name: str, fn):
        before = self.memory()
        seen = {e["at"] for e in before["events"]}
        print(f"\n▶ {name}（記憶體 {before['percent']}%）")
        t0 = time.perf_counter()
        ok, detail = True, ""
        try:
            detail = fn() or ""
        except Exception as e:  # noqa: BLE001 — 單一步驟失敗不中斷整個測試
            ok, detail = False, f"{type(e).__name__}: {e}"
        secs = time.perf_counter() - t0
        after = self.memory()
        events = [e for e in after["events"] if e["at"] not in seen]
        released = list(dict.fromkeys(r["label"] for e in events for r in e["released"]))
        loaded = [m["label"] for m in after["models"] if m["loaded"]]
        print(f"  {'✓' if ok else '✗'} {detail}")
        for e in reversed(events):
            if e["released"] or e["failed"]:
                print(
                    f"  ♻ 記憶體管理（{e['trigger']}）："
                    f"{e['percent_before']}% → {e['percent_after']}%，"
                    f"釋放 {'、'.join(r['label'] for r in e['released']) or '—'}"
                    + (f"；保留 {'、'.join(e['kept'])}" if e["kept"] else "")
                    + (
                        f"；失敗 {'、'.join(f['label'] for f in e['failed'])}"
                        if e["failed"]
                        else ""
                    )
                )
        print(
            f"  {secs:.1f} 秒 · 記憶體 {before['percent']}% → {after['percent']}%"
            f" · 載入中：{'、'.join(loaded) or '無'}"
        )
        self.steps.append(
            {
                "step": name,
                "ok": ok,
                "detail": detail,
                "seconds": round(secs, 1),
                "memory_before": before["percent"],
                "memory_after": after["percent"],
                "released": released,
                "loaded_after": loaded,
                "events": events,
            }
        )
        return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--skip-cad", action="store_true")
    parser.add_argument("--seconds", type=int, default=15, help="Timefold 求解秒數")
    parser.add_argument("--cleanup", action="store_true", help="結束後取消這次開立的工單")
    args = parser.parse_args()

    client = httpx.Client(base_url=args.base, timeout=120)
    try:
        client.get("/api/v1/health").raise_for_status()
    except httpx.HTTPError:
        print(f"連不上後端 {args.base}，請先執行 make demo（或 make demo-all）")
        return 1
    # 切換身分要先有憑證、要展示模式（DEMO_CONTROLS=true）；服務細節在管理診斷（主管，docs/adr/030）
    client.get("/api/v1/auth/accounts").raise_for_status()
    client.post("/api/v1/auth/switch", json={"account_id": "manager"}).raise_for_status()
    h = client.get("/api/v1/admin/diagnostics").json()
    mem = h["memory"]
    print(
        f"後端 {args.base} · LLM_MODE={h['llm_mode']} · 記憶體 {mem['percent']}%"
        f"（門檻 {mem['threshold']}%，"
        f"{'已啟用' if mem['enabled'] else '未啟用'}記憶體管理）"
    )
    for k in ("hybrid", "ortho2cad"):
        s = h["strategies"][k]
        print(f"  {'●' if s['available'] else '○'} {s['label']}：{s['model']}")
    print(f"  {'●' if h['scheduler']['available'] else '○'} 生產排程：{h['scheduler']['detail']}")

    def as_account(account_id: str) -> None:
        """展示身分（docs/adr/011）：開立工單、排程只有生管可以；急件要主管核准。"""
        client.post("/api/v1/auth/switch", json={"account_id": account_id}).raise_for_status()

    as_account("planner")
    rec = Recorder(client)
    state: dict = {"parts": [], "work_orders": []}

    def search():
        out = []
        for q, *_ in ORDERS:
            r = client.get("/api/v1/search/parts", params={"q": q}).json()
            top = r["results"][0]["part"]
            out.append(top)
        state["parts"] = out
        return "、".join(
            f"「{q}」→ {p['name_zh']}（{p['id']}）" for (q, *_), p in zip(ORDERS, out, strict=True)
        )

    def chat():
        part = state["parts"][0]
        answer = []
        events = sse(
            client,
            "/chat",
            {"question": "精車內孔有什麼要求？", "part_id": part["id"]},
            lambda e, d: answer.append(d["text"]) if e == "token" else None,
        )
        done = next((d for e, d in events if e == "done"), None)
        if not done:
            raise RuntimeError(next((d["message"] for e, d in events if e == "error"), "沒有回答"))
        secs = done["latency_ms"]["total"] / 1000
        return f"{part['name_zh']}：「{''.join(answer)[:40]}…」（{done['model']}，{secs:.1f} 秒）"

    def cad():
        part = state["parts"][0]
        events = sse(client, "/cad/reconstruct", {"part_id": part["id"]})
        result = next((d for e, d in events if e == "result"), None)
        if not result:
            raise RuntimeError(next((d["message"] for e, d in events if e == "error"), "沒有結果"))
        done = next(d for e, d in events if e == "done")
        iou = f"IoU {result['iou']:.2f}" if result.get("iou") is not None else "無法計算 IoU"
        secs = done["latency_ms"]["total"] / 1000
        return (
            f"{part['name_zh']}：{'可執行' if result['ok'] else '無法執行'}，{iou}（{secs:.0f} 秒）"
        )

    def create_orders():
        made = []
        for (_, qty, due, priority), part in zip(ORDERS, state["parts"], strict=True):
            body = {
                "part_id": part["id"],
                "qty": qty,
                "due_on": due,
                "priority": priority,
                "note": "demo-test",
            }
            r = client.post("/api/v1/production/work-orders", json=body)
            if r.status_code == 409 and r.json()["error"]["code"] == "APPROVAL_REQUIRED":
                # 急件超過額度：同一套修改資料流程送主管核准，主管核准後才開立
                change = {"op": "wo_create", "params": body}
                p = client.post("/api/v1/changes/preview", json=change).json()
                url = f"/api/v1/changes/{p['pending_id']}/request-approval"
                ap = client.post(url, json={"note": "demo-test"}).json()
                as_account("manager")
                d = client.post(f"/api/v1/approvals/{ap['ap_no']}/approve", json={}).json()
                as_account("planner")
                if d.get("status") != "已核准":
                    raise RuntimeError(d.get("text") or str(d))
                made.append(
                    {"wo_no": d["change_no"], "part_name": part["name_zh"], "qty": qty,
                     "priority": f"{priority}，{ap['ap_no']} 主管核准", "due_on": due}
                )  # fmt: skip
                continue
            r.raise_for_status()
            made.append(r.json())
        state["work_orders"] = made
        return "、".join(
            f"{w['wo_no']} {w['part_name']} {w['qty']} 件（{w['priority']}，交期 {w['due_on']}）"
            for w in made
        )

    def schedule():
        last = {"t": 0.0}

        def show(e, d):
            if e == "meta":
                print(
                    f"    {d['engine_label']}：{d['problem']['n_work_orders']} 張工單、"
                    f"{d['problem']['n_operations']} 道工序"
                )
            elif e == "progress" and time.perf_counter() - last["t"] > 2:
                last["t"] = time.perf_counter()
                print(
                    f"    {d['elapsed_ms'] / 1000:5.1f} 秒 {d['phase']}：{d['score']}"
                    f"（改善 {d['improvements']} 次）"
                )

        events = sse(client, "/schedule/solve", {"seconds": args.seconds}, show)
        sol = next((d for e, d in events if e == "solution"), None)
        if not sol:
            raise RuntimeError(
                next((d["message"] for e, d in events if e == "error"), "沒有排程結果")
            )
        k = sol["kpis"]
        check = {True: "，總分核對一致", False: "，總分核對不一致！", None: ""}[
            sol.get("score_check")
        ]
        init = f"初始解 {sol['initial_score']} → " if sol.get("initial_score") else ""
        return (
            f"{init}{sol['score']}{check}；"
            f"準時 {k['n_work_orders'] - k['n_late']}/{k['n_work_orders']}、"
            f"換線 {k['n_setups']} 次、全部完工 {k['finish_at']}"
        )

    def ask_sql():
        wo = state["work_orders"][0]["wo_no"] if state["work_orders"] else "WO-2610-01"
        q = f"{wo} 排程後什麼時候完工？會不會延遲？"
        answer = []
        events = sse(
            client,
            "/inventory/ask",
            {"question": q},
            lambda e, d: answer.append(d["text"]) if e == "token" else None,
        )
        result = next((d for e, d in events if e == "result"), None)
        if not result:
            raise RuntimeError(next((d["message"] for e, d in events if e == "error"), "沒有結果"))
        done = next(d for e, d in events if e == "done")
        sql = next(d["sql"] for e, d in events if e == "sql" and d["ok"])
        secs = done["latency_ms"]["total"] / 1000
        return (
            f"「{q}」→ {result['row_count']} 筆（{secs:.1f} 秒）\n"
            f"    SQL：{sql}\n    回答：{''.join(answer)[:80]}"
        )

    rec.step("1. 以文字找圖紙", search)
    if state["parts"]:
        rec.step("2. 製程問答", chat)
        if not args.skip_cad and h["strategies"]["ortho2cad"]["available"]:
            rec.step("3. Ortho2CAD 3D 重建", cad)
        else:
            print(
                "\n▷ 3. Ortho2CAD 3D 重建：略過"
                + ("（--skip-cad）" if args.skip_cad else "（Ortho2CAD 未啟動）")
            )
        rec.step("4. 從圖紙開立工單", create_orders)
        rec.step("5. 生產排程", schedule)
        rec.step("6. Text-to-SQL 查排程", ask_sql)

    if args.cleanup:
        for w in state["work_orders"]:
            client.delete(f"/api/v1/production/work-orders/{w['wo_no']}")
        print(f"\n已取消這次開立的 {len(state['work_orders'])} 張工單")

    print("\n摘要")
    print(f"{'步驟':<16}{'結果':<4}{'秒':>6}  記憶體          釋放的模型")
    for s in rec.steps:
        print(
            f"{s['step']:<16}{'✓' if s['ok'] else '✗':<4}{s['seconds']:>6}  "
            f"{s['memory_before']:>5}% → {s['memory_after']:>5}%  {'、'.join(s['released']) or '—'}"
        )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = EVAL / "runs" / f"{run_id}-demo.json"
    out.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": datetime.now(UTC).isoformat(),
                "base": args.base,
                "steps": rec.steps,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n結果存在 {out.relative_to(EVAL.parent)}")
    return 0 if all(s["ok"] for s in rec.steps) else 1


if __name__ == "__main__":
    sys.exit(main())
