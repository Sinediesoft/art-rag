"""參考資料「說法不一致」的辨識（docs/adr/020、028）。

題目在 eval/qa_conflict.jsonl（看不到 prompt 與計分的代理只看知識庫段落出題、人工核對），分三類：
- exclusive：只會有一個答案的事（誰最先發現、第一次在哪展出、哪一年購入…）；注入一段寫成另一個值、
  讀起來像「另外也發生過」的段落。兩段不可能都對，回答要明白指出說法不一致
- compatible：注入看起來相近、但和正確答案可以同時成立的段落（另一次展出、另一個版本…）；
  不該說不一致
- natural：不注入；知識庫段落本身就有兩件相近但不衝突的事（例如原作與另一個版本的日期）

結果（outcome）：
- exclusive：flagged＝寫出正確答案而且明白說不一致（says_inconsistent）；
  merged＝兩種說法都寫進去卻沒說不一致——當成都發生過、或自己想理由圓過去
  （ADR 020「分別由李霖燦與張大千發現」）；misled＝只寫錯的那個；silent＝只寫對的、沒提另一段；
  other＝其他（例如拒答）
- compatible／natural：ok＝答對、沒說不一致；false_alarm＝說了不一致；wrong＝答錯

每題的 split 是 dev（調 prompt 時看）或 holdout（調完才跑一次確認，不拿來調）。
--no-rearrange：關掉段落篩選，注入的段落一定進 prompt（只量回答 prompt；預設照伺服器設定）。
--conflict-check on|off：回答前的矛盾檢查（docs/adr/028）明確開或關；預設照伺服器設定。
用法（後端要在執行；注入要 .env 設 EVAL_INJECTION=true）：
  python eval/run_conflict_eval.py [--base http://localhost:8000] [--split dev|holdout|all]
                                   [--no-rearrange] [--conflict-check on|off]
"""

import argparse
import json
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL))
from run_eval import run_chat  # noqa: E402

# 「說了不一致」只認明確的說法，比 run_eval 的 CONFLICT 嚴：
# - 不認「資料…不同」：「兩段資料位置描述略有不同，但都指向畫面右側下方」是把兩段圓在一起，
#   不是指出矛盾；
#   compatible 的回答本來也會寫「另一次展出在不同城市」
# - 否定的不算：「可視為同一個款的描述，非說法不一致」「不能說是『說法不一致』」
# 這兩條是人工看過第一批回答（20261007T0144–0147）才補的，補完重算了那幾次
ASSERT = re.compile(r"不一致|矛盾|有出入|說法不一|不符")
NEGATED = re.compile(
    r"(非|不是|並非|不能說是|不算|並不|並無|沒有|無)[「『]?(說法)?(不一致|矛盾|有出入|衝突)|不(矛盾|衝突)"
)


def says_inconsistent(ans: str) -> bool:
    return bool(ASSERT.search(ans)) and not NEGATED.search(ans)


def outcome(q: dict, ans: str) -> str:
    correct = all(any(k in ans for k in g) for g in q["keywords"])
    said = says_inconsistent(ans)
    if q["conflict"] != "exclusive":
        if said:
            return "false_alarm"
        return "ok" if correct else "wrong"
    wrong = any(f in ans for f in q["forbidden"])
    if correct and said:
        return "flagged"
    if correct and wrong:
        return "merged"
    if correct:
        return "silent"
    if wrong and not said:
        return "misled"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--file", default="qa_conflict.jsonl", help="eval/ 底下的題目檔")
    ap.add_argument("--split", default="all", choices=["dev", "holdout", "all"])
    ap.add_argument("--no-rearrange", action="store_true", help="關掉段落篩選")
    ap.add_argument("--conflict-check", choices=["on", "off"], help="矛盾檢查；預設照伺服器設定")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    qs = [json.loads(x) for x in (EVAL / args.file).read_text("utf-8").splitlines() if x]
    qs = [q for q in qs if args.split == "all" or q["split"] == args.split]
    client = httpx.Client(base_url=args.base, timeout=60)
    client.get("/api/v1/auth/accounts").raise_for_status()
    rows = []
    for q in qs:
        body = {"question": q["question"], "artwork_id": q["artwork_id"], "strategy": "hybrid"}
        if q["inject"]:
            body["inject"] = q["inject"]
        if args.no_rearrange:
            body["rearrange"] = False
        if args.conflict_check:
            body["conflict_check"] = args.conflict_check == "on"
        res = run_chat(client, body)
        if (res["error"] or {}).get("code") == "FORBIDDEN":
            print("後端沒開 EVAL_INJECTION=true，注入題無法評估")
            return 1
        ans = res["answer"].replace("\n", " ")
        done = res["done"] or {}
        row = {
            "id": q["id"],
            "split": q["split"],
            "conflict": q["conflict"],
            "outcome": outcome(q, ans) if not res["error"] else "error",
            "answer": ans,
            # 注入的段落有沒有通過段落篩選、進到 prompt（沒進去就量不到）
            "injected_kept": any(s.get("injected") for s in res["sources"]),
            "topics": [s["topic"] for s in res["sources"]],
            # 矛盾檢查的判斷（沒開、或段落不到 2 段時是 null）
            "conflict_check": res["conflict_check"],
            "prompt_version": done.get("prompt_version"),
            "error": res["error"],
        }
        rows.append(row)
        print(f"【{q['id']}｜{q['conflict']}】{row['outcome']}：{ans[:110]}")
    summary = {}
    for kind in ("exclusive", "compatible", "natural"):
        for split in ("dev", "holdout"):
            rs = [r for r in rows if r["conflict"] == kind and r["split"] == split]
            if rs:
                counts: dict = {"n": len(rs)}
                for r in rs:
                    counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
                counts["injected_kept"] = sum(r["injected_kept"] for r in rs)
                summary[f"{kind}/{split}"] = counts
    print(json.dumps(summary, ensure_ascii=False))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    # 不放 eval/runs/ 第一層：/eval/runs 會把它當問答評估讀
    out = EVAL / "runs" / "experiments" / f"{run_id}-conflict.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    versions = sorted({r["prompt_version"] for r in rows if r["prompt_version"]})
    out.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "file": args.file,
                "split": args.split,
                "no_rearrange": args.no_rearrange,
                "conflict_check": args.conflict_check,
                "prompt_version": versions[0] if len(versions) == 1 else versions,
                "summary": summary,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"→ {out.relative_to(EVAL.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
