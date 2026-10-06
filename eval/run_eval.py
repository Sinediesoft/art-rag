"""自動化評估（make eval）：呼叫執行中的後端 API，輸出 CSV 與摘要 JSON 到 eval/runs/。

- 以圖搜圖：photos/known 中「已收錄」畫作的 Top-1／Top-5 命中率
- 拒答：photos/unknown 與「尚未收錄」畫作的照片，應回覆查無
- 問答：qa.jsonl 每題 × 各策略；answer_ok 以關鍵字比對、citation_ok 檢查引用是否指向正確畫作與段落
  （兩者都是人工評分前的自動化代理指標，正式成績依企劃書由兩人獨立評分）
- 每題有題目類型 type（common 常識／kb_only 知識庫獨有／no_answer 無答案／distractor 干擾，
  未標註記為 untyped），結果依類型分組；每筆也記錄送出本機的資料量（egress）
- 干擾段落題（docs/adr/019）：題目的 distractors 會注入檢索結果（後端要 EVAL_INJECTION=true），
  forbidden 是干擾段落裡的錯誤事實。另計三項：干擾段落有沒有通過篩選進 prompt（kept）、
  回答有沒有引用它（cited）、回答有沒有出現錯誤事實（misled）；被帶偏或引用了就不算答對

雲端對照組（api_nokb＝A1 無檢索、api_kb＝A2 有檢索）需要後端 ALLOW_CLOUD=true 與 API_KEY。

用法：python eval/run_eval.py [--base http://localhost:8000]
      [--strategies hybrid,hybrid_norag,hybrid_plain,hybrid_rearrange,api_nokb,api_kb,mock]
      [--split dev|test|all]
      hybrid_plain／hybrid_rearrange：檢索段落篩選（MIRA 的 Rearrange）關／開的對照
"""

import argparse
import csv
import json
import re
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent

STRATEGY_BODY = {
    "hybrid": {"strategy": "hybrid"},
    "hybrid_norag": {"strategy": "hybrid", "use_retrieval": False},
    # 檢索段落篩選（MIRA 的 Rearrange）開關對照：明確指定，不受伺服器預設影響（make eval-rearrange）
    "hybrid_plain": {"strategy": "hybrid", "rearrange": False},
    "hybrid_rearrange": {"strategy": "hybrid", "rearrange": True},
    # 問答附圖／不附圖對照（Issue #2、docs/adr/024，make eval-send-image）：
    # 明確指定，不受伺服器預設影響
    "hybrid_img": {"strategy": "hybrid", "send_image": True},
    "hybrid_noimg": {"strategy": "hybrid", "send_image": False},
    "api_nokb": {"strategy": "api_nokb"},
    "api_kb": {"strategy": "api_kb"},
    "lora": {"strategy": "lora"},
    "mock": {"strategy": "mock"},
}


def p95(values: list[float]) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def eval_images(client: httpx.Client, kb_ids: set[str]) -> dict:
    rows = []
    for photo in sorted((EVAL / "photos").rglob("*.jpg")):
        truth = photo.stem.split("__")[0]
        in_kb = truth in kb_ids
        with photo.open("rb") as f:
            image_id = client.post(
                "/api/v1/images", files={"file": (photo.name, f, "image/jpeg")}
            ).json()["image_id"]
        r = client.post("/api/v1/search/image", json={"image_id": image_id}).json()
        ids = [x["artwork"]["id"] for x in r["results"]]
        rows.append(
            {
                "photo": photo.relative_to(EVAL).as_posix(),
                "truth": truth if in_kb else "(未收錄)",
                "matched": r["matched"],
                "predicted": r["best_artwork_id"],
                "top1": in_kb and r["best_artwork_id"] == truth,
                "top5": in_kb and truth in ids[:5],
                "rejected": not in_kb and not r["matched"],
                "clip": r["results"][0]["score"] if r["results"] else None,
                "inliers": r["results"][0]["inliers"] if r["results"] else None,
            }
        )
    known = [r for r in rows if r["truth"] != "(未收錄)"]
    unknown = [r for r in rows if r["truth"] == "(未收錄)"]
    return {
        "rows": rows,
        "image_top1": sum(r["top1"] for r in known) / len(known) if known else None,
        "image_top5": sum(r["top5"] for r in known) / len(known) if known else None,
        "reject_rate": sum(r["rejected"] for r in unknown) / len(unknown) if unknown else None,
        "n_known": len(known),
        "n_unknown": len(unknown),
    }


def run_chat(client: httpx.Client, body: dict) -> dict:
    out = {"answer": "", "sources": [], "rearrange": None, "done": None, "error": None}
    with client.stream("POST", "/api/v1/chat", json=body, timeout=180) as resp:
        if resp.status_code != 200:
            resp.read()
            out["error"] = resp.json().get("error") or {"code": f"HTTP {resp.status_code}"}
            return out
        event = None
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
                if event == "sources":
                    out["sources"] = data["sources"]
                    out["rearrange"] = data.get("rearrange")
                elif event == "token":
                    out["answer"] += data["text"]
                elif event == "done":
                    out["done"] = data
                elif event == "error":
                    out["error"] = data
    return out


def owner_of(q: dict) -> str:
    """題目問的是哪一幅畫或哪一張圖紙（圖紙題用 part_id）。"""
    return q.get("part_id") or q["artwork_id"]


# 回答明白指出參考資料互相矛盾（answer_v2 第 5 條，docs/adr/020）。
# 只在回答同時寫出正確答案時才算數。「所述…不同」「不符」是 2026-10-05 看到圖紙題才補的
# （「[1] 與 [2] 所述最終扭力不同」「與本圖紙資料不符」）；單獨的「不同」不算——
# 「在不同時間由不同人發現」是把兩種說法湊在一起，不是指出矛盾。補完重算了當天的評估
CONFLICT = re.compile(
    r"不一致|互相矛盾|矛盾|有出入|說法不一|說法.{0,4}不同|(所述|記載|說法|資料).{0,6}(不同|不符)|不符"
)


def score_distractor(q: dict, res: dict) -> dict:
    """干擾段落題（其他題目回空 dict）：kept＝通過篩選進了 prompt、cited＝回答引用了它、
    flagged＝回答寫出正確答案、也明白指出參考資料說法不一致、
    misled＝回答出現 forbidden 裡的錯誤事實，而且沒有 flagged。

    系統沒辦法自己判斷兩段誰對，所以「答對並指出另一段說法不同」是要的行為，不算被帶偏。"""
    if not q.get("distractors"):
        return {}
    ans = res["answer"]
    injected = {s["ref"] for s in res["sources"] if s.get("injected")}
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", ans)}
    correct = all(any(k in ans for k in group) for group in q["keywords"])
    flagged = correct and bool(CONFLICT.search(ans))
    return {
        "distractor_kept": bool(injected),
        "distractor_cited": bool(injected & cited),
        "distractor_flagged": flagged,
        "distractor_misled": not flagged and any(k in ans for k in q.get("forbidden", [])),
    }


def score_answer(q: dict, res: dict) -> tuple[bool, bool]:
    ans = res["answer"]
    if q.get("expect_refusal"):
        ok = "沒有" in ans and not re.search(r"\d{3,}", ans)
        return ok, ok
    answer_ok = all(any(k in ans for k in group) for group in q["keywords"])
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", ans)} - {0}
    by_ref = {s["ref"]: s for s in res["sources"]}
    d = score_distractor(q, res)
    flagged = d.get("distractor_flagged", False)
    citation_ok = (
        bool(cited)
        and all(
            n in by_ref
            and (by_ref[n].get("part_id") or by_ref[n].get("artwork_id")) == owner_of(q)
            and (flagged or not by_ref[n].get("injected"))  # 指出矛盾時引用干擾段落是對的
            for n in cited
        )
        and any(by_ref[n]["topic"] in q["topics"] for n in cited if n in by_ref)
    )
    if d.get("distractor_misled") or (d.get("distractor_cited") and not flagged):
        answer_ok = False  # 被干擾段落帶偏就不算答對
    return answer_ok, citation_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--strategies", default="hybrid,hybrid_norag")
    ap.add_argument("--split", default="all", choices=["dev", "test", "all"])
    args = ap.parse_args()

    client = httpx.Client(base_url=args.base, timeout=60)
    try:
        health = client.get("/api/v1/health").json()
    except httpx.HTTPError:
        print(f"連不上後端 {args.base}，請先執行 make dev 或 make demo")
        return 1
    manifest = health["manifest"]
    # 所有 /api/v1 請求都要 JWT（docs/adr/015）：先取一張訪客憑證（存在 client 的 cookie）
    client.get("/api/v1/auth/accounts").raise_for_status()
    kb_ids = {a["id"] for a in client.get("/api/v1/artworks").json()["items"]}
    # 工廠圖紙題（part_id，docs/adr/020）：圖紙多是機密，切成主管才看得到全部
    all_questions = [json.loads(x) for x in (EVAL / "qa.jsonl").read_text("utf-8").splitlines()]
    if any(q.get("part_id") for q in all_questions):
        client.post("/api/v1/auth/switch", json={"account_id": "manager"}).raise_for_status()
        kb_ids |= {p["id"] for p in client.get("/api/v1/parts").json()["items"]}
    source_lang = {
        i: "+".join(
            sorted({d["lang"] for d in client.get(f"/api/v1/artworks/{i}").json()["descriptions"]})
        )
        for i in kb_ids
        if not i.startswith("mfg-")
    }
    prompt_version = ""

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    print(f"評估 {run_id}：kb_version={manifest['kb_version']}，{len(kb_ids)} 幅畫")

    img = eval_images(client, kb_ids)
    print(
        f"以圖搜圖 Top-1 {img['image_top1']:.0%}（{img['n_known']} 張），"
        f"未收錄拒答 {img['reject_rate']:.0%}（{img['n_unknown']} 張）"
    )

    questions = [
        q
        for q in all_questions
        if owner_of(q) in kb_ids and (args.split == "all" or q["split"] == args.split)
    ]
    rows, by_strategy = [], {}
    for strat in args.strategies.split(","):
        results = []
        for q in questions:
            t0 = time.time()
            body = {
                "question": q["question"],
                ("part_id" if q.get("part_id") else "artwork_id"): owner_of(q),
                "allow_fallback": False,
                **STRATEGY_BODY[strat],
            }
            # 關檢索的策略不放段落進 prompt，注入沒有意義
            if q.get("distractors") and body.get("use_retrieval", True):
                body["inject"] = q["distractors"]
            res = run_chat(client, body)
            if (res["error"] or {}).get("code") == "FORBIDDEN" and "inject" in body:
                print(f"  {q['id']}：後端沒開 EVAL_INJECTION=true，干擾段落題無法注入")
            done = res["done"] or {}
            answer_ok, citation_ok = score_answer(q, res) if not res["error"] else (False, False)
            dist = score_distractor(q, res) if not res["error"] and "inject" in body else {}
            # 整次評估記畫作的 prompt 版本；圖紙題回的是 drawing_vN，每列另外記
            if done.get("prompt_version") and not q.get("part_id"):
                prompt_version = done["prompt_version"]
            egress = done.get("egress") or {}
            ra = res["rearrange"] or {}
            row = {
                "question_id": q["id"],
                "split": q["split"],
                "type": q.get("type", "untyped"),
                "artwork_id": owner_of(q),
                "source_lang": source_lang.get(owner_of(q), ""),
                "strategy": strat,
                "model": done.get("model", ""),
                "kb_version": manifest["kb_version"],
                "prompt_version": done.get("prompt_version", ""),
                "answer_ok": answer_ok,
                "citation_ok": citation_ok,
                "n_sources": len(res["sources"]),
                "rearrange_ms": ra.get("ms"),
                "rearrange_fallback": ra.get("fallback") or "",
                "first_token_ms": (done.get("latency_ms") or {}).get("first_token"),
                "total_ms": (done.get("latency_ms") or {}).get("total")
                or round((time.time() - t0) * 1000),
                "cost_twd": done.get("cost_twd", 0),
                "egress_images": egress.get("images", 0),
                "egress_chunks": egress.get("chunks", 0),
                "egress_bytes": egress.get("bytes", 0),
                "error": (res["error"] or {}).get("code", ""),
                "distractor_kept": dist.get("distractor_kept", ""),
                "distractor_cited": dist.get("distractor_cited", ""),
                "distractor_flagged": dist.get("distractor_flagged", ""),
                "distractor_misled": dist.get("distractor_misled", ""),
                "answer": res["answer"].replace("\n", " "),
            }
            rows.append(row)
            results.append(row)
            mark = "✓" if answer_ok else "✗"
            print(f"  [{strat}] {mark} {q['id']} {row['answer'][:60]}")
        ok = [r for r in results if not r["error"]]
        injected = [r for r in ok if r["distractor_kept"] != ""]
        by_strategy[strat] = {
            "n": len(results),
            "errors": len(results) - len(ok),
            "answer_ok": sum(r["answer_ok"] for r in results) / len(results) if results else None,
            "citation_ok": sum(r["citation_ok"] for r in results) / len(results)
            if results
            else None,
            "mean_sources": statistics.mean([r["n_sources"] for r in ok]) if ok else None,
            "rearrange_fallbacks": sum(1 for r in results if r["rearrange_fallback"]),
            "p95_first_token_ms": p95([r["first_token_ms"] for r in ok]),
            "p95_total_ms": p95([r["total_ms"] for r in ok]),
            "median_total_ms": statistics.median([r["total_ms"] for r in ok]) if ok else None,
            "cost_twd": round(sum(r["cost_twd"] for r in results), 4),
            "egress_bytes": sum(r["egress_bytes"] for r in results),
            "by_type": {
                t: sum(r["answer_ok"] for r in results if r["type"] == t)
                / sum(1 for r in results if r["type"] == t)
                for t in sorted({r["type"] for r in results})
            },
            # 干擾段落題：篩選擋下率（越高越好）、引用率與被帶偏率（越低越好）
            "distractor": {
                "n": len(injected),
                "filtered_out": sum(not r["distractor_kept"] for r in injected) / len(injected),
                "cited": sum(r["distractor_cited"] for r in injected) / len(injected),
                "flagged": sum(r["distractor_flagged"] for r in injected) / len(injected),
                "misled": sum(r["distractor_misled"] for r in injected) / len(injected),
            }
            if injected
            else None,
        }

    runs = EVAL / "runs"
    runs.mkdir(exist_ok=True)
    with (runs / f"{run_id}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["question_id"])
        w.writeheader()
        w.writerows(rows)
    with (runs / f"{run_id}-images.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(img["rows"][0]))
        w.writeheader()
        w.writerows(img["rows"])
    summary = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kb_version": manifest["kb_version"],
        "prompt_version": prompt_version,
        "summary": {k: v for k, v in img.items() if k != "rows"} | {"split": args.split},
        "by_strategy": by_strategy,
    }
    (runs / f"{run_id}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(by_strategy, ensure_ascii=False, indent=2))
    if "hybrid" in by_strategy and "hybrid_norag" in by_strategy:
        gain = by_strategy["hybrid"]["answer_ok"] - by_strategy["hybrid_norag"]["answer_ok"]
        print(f"檢索增益：{gain * 100:+.0f} 個百分點（目標 ≥ +15）")
    print(f"結果已寫入 eval/runs/{run_id}.*")
    return 0


if __name__ == "__main__":
    sys.exit(main())
