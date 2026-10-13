"""刁難題評估：錯誤前提、知識庫沒有的事、算術、易混淆、否定句、外語、跨作品、顏色、誘導、注入。

題目在 eval/qa_adversarial.jsonl（2026-10-11 依知識庫段落人工出題）。每題：
- keywords：每一組至少出現一個才算答對（例如錯誤前提題要更正成正確事實）
- forbidden：出現任何一個就算錯（例如把色碼當成物體顏色、洩漏內部電話）
split：dev 用來找問題、調 prompt；holdout 只在最後驗證，不能拿來調。

用法：uv run --project backend python eval/run_adversarial_eval.py [--split dev|holdout|all]
需後端在執行（make demo）。結果存 eval/runs/experiments/<run_id>-adversarial.json。
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL))
from run_eval import run_chat  # noqa: E402


def judge(q: dict, ans: str) -> tuple[bool, str]:
    missing = [g for g in q["keywords"] if not any(k in ans for k in g)]
    hit = [k for k in q.get("forbidden", []) if k in ans]
    if hit:
        return False, f"出現不該有的「{hit[0]}」"
    if missing:
        return False, f"缺少 {missing[0][0]}"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--split", default="dev", choices=["dev", "holdout", "all"])
    ap.add_argument("--file", default="qa_adversarial.jsonl", help="eval/ 底下的題目檔")
    ap.add_argument("--only", nargs="*", help="只跑這些題號")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    qs = [json.loads(x) for x in (EVAL / args.file).read_text("utf-8").splitlines() if x]
    qs = [q for q in qs if args.split == "all" or q["split"] == args.split]
    if args.only:
        qs = [q for q in qs if q["id"] in args.only]
    client = httpx.Client(base_url=args.base, timeout=60)
    client.get("/api/v1/auth/accounts").raise_for_status()
    rows = []
    for q in qs:
        body = {"question": q["question"], "strategy": "hybrid"}
        if q.get("artwork_id"):
            body["artwork_id"] = q["artwork_id"]
        res = run_chat(client, body)
        ans = res["answer"].replace("\n", " ")
        ok, why = judge(q, ans) if not res["error"] else (False, f"錯誤 {res['error']}")
        rows.append(
            {
                "id": q["id"],
                "split": q["split"],
                "kind": q["kind"],
                "question": q["question"],
                "ok": ok,
                "why": why,
                "answer": ans,
                "topics": [s.get("topic") for s in res["sources"]],
                "prompt_version": (res["done"] or {}).get("prompt_version"),
            }
        )
        print(f"{'✓' if ok else '✗'} {q['id']} [{q['kind']}] {why}")
        print(f"    問：{q['question']}\n    答：{ans[:160]}")
    n_ok = sum(r["ok"] for r in rows)
    by_kind: dict = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], [0, 0])
        k[0] += r["ok"]
        k[1] += 1
    kinds = " · ".join(f"{k} {a}/{b}" for k, (a, b) in by_kind.items())
    print(f"\n摘要：{n_ok}/{len(rows)} 答對 · {kinds}")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    out = EVAL / "runs" / "experiments" / f"{run_id}-adversarial.json"
    data = {"run_id": run_id, "split": args.split, "rows": rows}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    print(f"已存 {out.relative_to(EVAL.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
