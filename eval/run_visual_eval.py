"""看圖細節題（Issue #2、docs/adr/024）。

答案在畫面上看得出來、但段落裡沒寫的題目，附圖／不附圖各問一次。

題目在 eval/qa_visual.jsonl（看不到後端與 prompt 的代理只看畫作圖片與段落出的題，人工篩過）。
量「不附圖」實際損失多少；自動計分只是代理指標，每題的回答都會印出來，請人工核對。
排除過的題目：答案是「沒有／有」的（和拒答的「知識庫中沒有」分不開）、段落或色彩分析段落其實寫了答案的。

用法（後端要在執行）：python eval/run_visual_eval.py [--base http://localhost:8000]
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
REFUSAL = "沒有這方面"


def ask(client: httpx.Client, q: dict, send_image: bool) -> dict:
    body = {"question": q["question"], "artwork_id": q["artwork_id"], "send_image": send_image}
    text, topics, done = [], [], {}
    with client.stream("POST", "/api/v1/chat", json=body, timeout=300) as r:
        event = None
        for line in r.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                d = json.loads(line[5:])
                if event == "token":
                    text.append(d["text"])
                elif event == "sources":
                    topics = [s["topic"] for s in d["sources"]]
                elif event == "done":
                    done = d
    ans = "".join(text).replace("\n", " ")
    refused = REFUSAL in ans
    ok = not refused and all(any(k in ans for k in g) for g in q["keywords"])
    return {
        "answer": ans,
        "ok": ok,
        "refused": refused,
        # 沒拒答、也沒答對＝自己編了一個答案（例如沒附圖還說出河在哪一邊）
        "wrong_claim": not ok and not refused,
        "image_sent": done.get("image_sent"),
        "topics": topics,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    qs = [json.loads(x) for x in (EVAL / "qa_visual.jsonl").read_text("utf-8").splitlines() if x]
    client = httpx.Client(base_url=args.base, timeout=60)
    client.get("/api/v1/auth/accounts").raise_for_status()
    rows = []
    for q in qs:
        print(f"【{q['id']}】{q['question']}")
        for img in (True, False):
            r = ask(client, q, img) | {"id": q["id"], "send_image": img}
            rows.append(r)
            mark = "✓" if r["ok"] else ("拒答" if r["refused"] else "✗ 答錯")
            print(f"  {'附圖' if img else '不附圖'} {mark}：{r['answer'][:100]}")
    summary = {}
    for img in (True, False):
        rs = [r for r in rows if r["send_image"] == img]
        summary["img" if img else "noimg"] = {
            "n": len(rs),
            "correct": sum(r["ok"] for r in rs),
            "refused": sum(r["refused"] for r in rs),
            "wrong_claim": sum(r["wrong_claim"] for r in rs),
        }
    print(json.dumps(summary, ensure_ascii=False))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    # 不放 eval/runs/ 第一層：/eval/runs 會把它當問答評估讀
    out = EVAL / "runs" / "experiments" / f"{run_id}-visual.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"run_id": run_id, "summary": summary, "rows": rows}, ensure_ascii=False, indent=1
        ),
        encoding="utf-8",
    )
    print(f"→ {out.relative_to(EVAL.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
