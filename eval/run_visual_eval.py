"""看圖細節題（Issue #2、docs/adr/024、026）。

答案在畫面上看得出來、但段落裡沒寫的題目，附圖／不附圖各問一次。

題目在 eval/qa_visual.jsonl（看不到後端與 prompt 的代理只看畫作圖片與段落出的題，人工篩過）。
量「不附圖」實際損失多少；自動計分只是代理指標，每題的回答都會印出來，請人工核對。
排除過的題目：答案是「沒有／有」的（和拒答的「知識庫中沒有」分不開）、段落或色彩分析段落其實寫了答案的。

保留題（eval/qa_visual_holdout.jsonl，`--file`）：調 prompt 時不看，
用來確認沒有只對 qa_visual.jsonl 有效。
每題的 expect：
- image（預設）：答案只在畫面上，應標 [畫面]（可以和段落編號並列，例如 [1][畫面]）
- passage：段落寫了答案，應標那段的編號；只標 [畫面]＝把段落寫的說成自己看到的

用法（後端要在執行）：python eval/run_visual_eval.py [--base http://localhost:8000]
                        [--file qa_visual_holdout.jsonl] [--image-only]
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
REFUSAL = "沒有這方面"


def ask(client: httpx.Client, q: dict, send_image: bool) -> dict:
    body = {"question": q["question"], "artwork_id": q["artwork_id"], "send_image": send_image}
    text, refs, done = [], {}, {}
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
                    refs = {s["ref"]: s["topic"] for s in d["sources"]}
                elif event == "done":
                    done = d
    ans = "".join(text).replace("\n", " ")
    refused = REFUSAL in ans
    ok = not refused and all(any(k in ans for k in g) for g in q["keywords"])
    image_cited = "[畫面]" in ans
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", ans)} - {0}
    row = {
        "answer": ans,
        "ok": ok,
        "refused": refused,
        "image_cited": image_cited,
        # 沒拒答、也沒答對＝自己編了一個答案（例如沒附圖還說出河在哪一邊）
        "wrong_claim": not ok and not refused,
        "image_sent": done.get("image_sent"),
        "prompt_version": done.get("prompt_version"),
        "topics": list(refs.values()),
    }
    if q.get("expect", "image") == "image":
        # 出處（docs/adr/026）：答案只在畫面上，應標 [畫面]；只標段落編號＝出處標錯
        row["mis_cited"] = ok and not image_cited and bool(cited)
    else:
        # 段落寫了答案：要標到寫了答案的那段；只標 [畫面]＝把段落寫的說成看圖知道的
        row["passage_cited"] = ok and any(refs.get(n) in q["topics"] for n in cited)
        row["image_only"] = ok and image_cited and not cited
    return row


def summarize(rs: list[dict], expect: str) -> dict:
    s = {
        "n": len(rs),
        "correct": sum(r["ok"] for r in rs),
        "refused": sum(r["refused"] for r in rs),
        "wrong_claim": sum(r["wrong_claim"] for r in rs),
    }
    if expect == "image":
        s["correct_and_image_cited"] = sum(r["ok"] and r["image_cited"] for r in rs)
        s["mis_cited"] = sum(r["mis_cited"] for r in rs)
    else:
        s["passage_cited"] = sum(r["passage_cited"] for r in rs)
        s["image_only"] = sum(r["image_only"] for r in rs)
    return s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--file", default="qa_visual.jsonl", help="eval/ 底下的題目檔")
    ap.add_argument("--image-only", action="store_true", help="只問附圖（不量不附圖的損失）")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    qs = [json.loads(x) for x in (EVAL / args.file).read_text("utf-8").splitlines() if x]
    client = httpx.Client(base_url=args.base, timeout=60)
    client.get("/api/v1/auth/accounts").raise_for_status()
    rows = []
    for q in qs:
        print(f"【{q['id']}】{q['question']}")
        for img in (True,) if args.image_only else (True, False):
            r = ask(client, q, img) | {
                "id": q["id"],
                "send_image": img,
                "expect": q.get("expect", "image"),
            }
            rows.append(r)
            mark = "✓" if r["ok"] else ("拒答" if r["refused"] else "✗ 答錯")
            print(f"  {'附圖' if img else '不附圖'} {mark}：{r['answer'][:100]}")
    summary = {}
    for img in (True, False):
        for expect in ("image", "passage"):
            rs = [r for r in rows if r["send_image"] == img and r["expect"] == expect]
            if rs:
                key = ("img" if img else "noimg") + ("" if expect == "image" else "_passage")
                summary[key] = summarize(rs, expect)
    print(json.dumps(summary, ensure_ascii=False))
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    # 不放 eval/runs/ 第一層：/eval/runs 會把它當問答評估讀
    out = EVAL / "runs" / "experiments" / f"{run_id}-visual.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    versions = sorted({r["prompt_version"] for r in rows if r["prompt_version"]})
    out.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "file": args.file,
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
