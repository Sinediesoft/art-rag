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
關檢索對照組（hybrid_norag、api_nokb）與 mock 要後端 EVAL_CONTROLS=true（docs/adr/030），
否則第 6 段生成閘門直接降級「查無資料」；讀索引 manifest 要展示模式（DEMO_CONTROLS=true）切成主管。

LoRA 不檢索對照組（lora_norag，docs/adr/043，make eval-lora）：done 事件標示改用了備援模型的題目
一律記錯，不算到 LoRA 頭上；lora 開頭的策略只要有一題落到備援或出錯，結束碼就是 3。
--outside-kb 把「問知識庫外畫作」的題目（npm-000002、aic-16568）改成不帶畫作、不附圖送出，
和 no_answer 題一起算亂編率：拒答、答對常識都不算亂編，其他回答算亂編。

用法：python eval/run_eval.py [--base http://localhost:8000]
      [--strategies hybrid,hybrid_norag,hybrid_plain,hybrid_rearrange,api_nokb,api_kb,mock,
                    lora_norag]
      [--split dev|test|all] [--qa qa.jsonl,qa_visual.jsonl] [--outside-kb] [--skip-images]
      圖紙段落題庫：--qa drawing_retrieval/qa.jsonl（make eval EVAL_QA=同左），
      答案在旁附檔 drawing_retrieval/answers.jsonl，讀題時依 id 合併，不改原題庫
      hybrid_plain／hybrid_rearrange：檢索段落篩選（MIRA 的 Rearrange）關／開的對照
"""

import argparse
import copy
import csv
import json
import posixpath
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
RUNS = EVAL / "runs"

# 圖紙段落題庫：原 QA 位元組固定（SHA256 記在 TASK-20261027-001 的收據），答案放旁附檔、讀題時合併
DRAWING_QA = "drawing_retrieval/qa.jsonl"
DRAWING_ANSWERS = "answers.jsonl"
ANSWER_FIELDS = ("keywords", "review_status", "review_note", "no_answer_reason")
SIDECAR_FIELDS = {"id", "expect_refusal", *ANSWER_FIELDS}
REVIEW_STATUSES = ("unreviewed",)

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
    # 參考資料矛盾檢查開／關對照（docs/adr/028）：明確指定，不受伺服器預設影響
    "hybrid_cc": {"strategy": "hybrid", "conflict_check": True},
    "hybrid_nocc": {"strategy": "hybrid", "conflict_check": False},
    "api_nokb": {"strategy": "api_nokb"},
    "api_kb": {"strategy": "api_kb"},
    "lora": {"strategy": "lora"},
    # LoRA 不檢索（docs/adr/043）：知識只在權重裡，和 hybrid_norag（底模不檢索）比
    "lora_norag": {"strategy": "lora", "use_retrieval": False},
    "mock": {"strategy": "mock"},
}

# 亂編率：這兩類題目沒有知識庫答案（no_answer），或問的畫作不在知識庫（outside_kb）
NO_KB_ANSWER_TYPES = ("no_answer", "outside_kb")
REFUSAL = re.compile(r"沒有(這方面的|相關的?)?資料|查無|無法(確定|回答|判斷)|不(清楚|確定)|不知道")


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
    out = {
        "answer": "",
        "sources": [],
        "rearrange": None,
        "conflict_check": None,
        "done": None,
        "error": None,
    }
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
                    out["conflict_check"] = data.get("conflict_check")
                elif event == "token":
                    out["answer"] += data["text"]
                elif event == "done":
                    out["done"] = data
                elif event == "error":
                    out["error"] = data
    return out


def check_keywords(qid: str, keywords) -> None:
    ok = (
        isinstance(keywords, list)
        and keywords
        and all(
            isinstance(g, list) and g and all(isinstance(k, str) and k.strip() for k in g)
            for g in keywords
        )
    )
    if not ok:
        raise ValueError(f"{qid}：keywords 要是非空的「AND 組 → OR 候選字串」，不可有空組或空字串")


def merge_answers(questions: list[dict], answers: list[dict]) -> list[dict]:
    """把旁附答案依 id 合併進題目（回傳新 list，不改傳入的題目）。

    只合併 ANSWER_FIELDS；題目、part_id、topics、type 與拒答意圖都以原題為準，
    旁附檔寫了不同的值就報錯。缺漏、重複、未知 id、不合法 keywords 一律 ValueError。"""
    by_id: dict[str, dict] = {}
    for a in answers:
        aid = a.get("id")
        if not isinstance(aid, str) or not aid:
            raise ValueError(f"答案紀錄缺 id：{a}")
        if aid in by_id:
            raise ValueError(f"答案 id 重複：{aid}")
        by_id[aid] = a
    qids = [q["id"] for q in questions]
    if len(set(qids)) != len(qids):
        raise ValueError("題庫 id 重複")
    if unknown := sorted(set(by_id) - set(qids)):
        raise ValueError(f"答案檔有未知題號：{', '.join(unknown)}")
    if missing := [i for i in qids if i not in by_id]:
        raise ValueError(f"答案檔缺漏題號：{', '.join(missing)}")
    merged = []
    for q in questions:
        a = by_id[q["id"]]
        if extra := sorted(set(a) - SIDECAR_FIELDS):
            raise ValueError(
                f"{q['id']}：答案檔不可帶欄位 {', '.join(extra)}（只能合併答案與審核註記）"
            )
        refusal = bool(q.get("expect_refusal"))
        if "expect_refusal" in a and bool(a["expect_refusal"]) != refusal:
            raise ValueError(f"{q['id']}：答案檔的 expect_refusal 與原題不同")
        if refusal:
            if a.get("keywords"):
                raise ValueError(f"{q['id']}：拒答題不可帶 keywords")
            if not isinstance(a.get("no_answer_reason"), str) or not a["no_answer_reason"].strip():
                raise ValueError(f"{q['id']}：拒答題要有 no_answer_reason")
        else:
            check_keywords(q["id"], a.get("keywords"))
            if "no_answer_reason" in a:
                raise ValueError(f"{q['id']}：可答題不可帶 no_answer_reason")
        if "review_status" in a:
            if a["review_status"] not in REVIEW_STATUSES:
                raise ValueError(f"{q['id']}：review_status 只接受 {'、'.join(REVIEW_STATUSES)}")
            if not isinstance(a.get("review_note"), str) or not a["review_note"].strip():
                raise ValueError(f"{q['id']}：有 review_status 就要有 review_note 說明")
        merged.append(copy.deepcopy(q) | {k: copy.deepcopy(a[k]) for k in ANSWER_FIELDS if k in a})
    return merged


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def load_questions(names: list[str], base: Path = EVAL) -> list[dict]:
    """依序讀題目檔；只有圖紙段落題庫（drawing_retrieval/qa.jsonl）會合併旁附答案，
    並在記憶體補 split=test（原檔沒有 split、不寫回）。其他題庫照原樣。"""
    drawing = (base / DRAWING_QA).resolve()
    questions = []
    for name in names:
        path = base / name
        rows = read_jsonl(path)
        if posixpath.normpath(name) == DRAWING_QA or path.resolve() == drawing:
            rows = merge_answers(rows, read_jsonl(path.with_name(DRAWING_ANSWERS)))
            rows = [{"split": "test"} | q for q in rows]
        questions.extend(rows)
    return questions


def owner_of(q: dict) -> str | None:
    """題目問的是哪一幅畫或哪一張圖紙（圖紙題用 part_id）；一般常識題沒有。"""
    return q.get("part_id") or q.get("artwork_id")


def score_no_kb(q: dict, res: dict, answer_ok: bool) -> dict:
    """亂編率（docs/adr/043）：refused＝拒答；hallucinated＝亂編。

    no_answer：沒拒答就是亂編。生成閘門的降級訊息「查無資料：…」沒有「沒有」兩字，
    answer_ok 的拒答判定認不出來，所以另外用 REFUSAL 判斷。
    outside_kb：問知識庫外的畫作，拒答或答對常識都不算亂編。其他題型回空 dict。"""
    if q.get("type") not in NO_KB_ANSWER_TYPES:
        return {}
    refused = bool(REFUSAL.search(res["answer"]))
    if q["type"] == "no_answer":
        refused = refused or answer_ok
        return {"refused": refused, "hallucinated": not refused}
    return {"refused": refused, "hallucinated": not (refused or answer_ok)}


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


def judge(q: dict, res: dict) -> tuple[bool, bool, bool, dict]:
    """(answer_ok, citation_ok, fallback, 亂編判定)。

    done 事件標示實際改用了備援模型（fallback）：答案不是這個策略產生的，一律記錯、不判亂編。"""
    if res["error"]:
        return False, False, False, {}
    if (res["done"] or {}).get("fallback"):
        return False, False, True, {}
    answer_ok, citation_ok = score_answer(q, res)
    return answer_ok, citation_ok, False, score_no_kb(q, res, answer_ok)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--strategies", default="hybrid,hybrid_norag")
    ap.add_argument("--split", default="all", choices=["dev", "test", "all"])
    ap.add_argument("--qa", default="qa.jsonl", help="eval/ 下的題目檔，逗號分隔")
    ap.add_argument(
        "--outside-kb",
        action="store_true",
        help="問知識庫外畫作的題目改成不帶畫作送出，算亂編率（預設跳過這些題目）",
    )
    ap.add_argument("--skip-images", action="store_true", help="不跑以圖搜圖評估")
    return ap.parse_args()


def _select_questions(
    all_questions: list[dict], kb_ids: set[str], split: str, outside_kb: bool
) -> list[dict]:
    """依 split、知識庫對象與拒答意圖選題，保留原題順序。"""
    questions = []
    for q in all_questions:
        if split != "all" and q["split"] != split:
            continue
        owner = owner_of(q)
        # 標了拒答的未知圖紙題（mfg-099）要保留，不然會被 kb_ids 篩掉、不進分母
        if owner is None or owner in kb_ids or (q.get("part_id") and q.get("expect_refusal")):
            questions.append(q)
        elif outside_kb and not q.get("part_id"):
            questions.append({**q, "type": "outside_kb"})
    return questions


def _chat_body(q: dict, strat: str) -> dict:
    """組出同一策略的請求；知識庫外畫作不帶對象，關檢索時不注入段落。"""
    body = {"question": q["question"], "allow_fallback": False, **STRATEGY_BODY[strat]}
    # 一般常識題沒有對象；知識庫外的畫作也不帶（帶了會被當成找不到的畫作）
    if owner_of(q) and q.get("type") != "outside_kb":
        body["part_id" if q.get("part_id") else "artwork_id"] = owner_of(q)
    # 關檢索的策略不放段落進 prompt，注入沒有意義
    if q.get("distractors") and body.get("use_retrieval", True):
        body["inject"] = q["distractors"]
    return body


def _evaluate_question(
    client: httpx.Client, q: dict, strat: str, source_lang: dict, kb_version: str
) -> dict:
    """執行並判分一題，回傳 CSV 使用的完整結果列。"""
    t0 = time.time()
    body = _chat_body(q, strat)
    res = run_chat(client, body)
    if (res["error"] or {}).get("code") == "FORBIDDEN" and "inject" in body:
        print(f"  {q['id']}：後端沒開 EVAL_INJECTION=true，干擾段落題無法注入")
    done = res["done"] or {}
    answer_ok, citation_ok, fallback, no_kb = judge(q, res)
    dist = score_distractor(q, res) if not res["error"] and "inject" in body else {}
    egress = done.get("egress") or {}
    ra = res["rearrange"] or {}
    return {
        "question_id": q["id"],
        "split": q["split"],
        "type": q.get("type", "untyped"),
        "artwork_id": owner_of(q),
        "source_lang": source_lang.get(owner_of(q), ""),
        "strategy": strat,
        "model": done.get("model", ""),
        "strategy_used": done.get("strategy_used", ""),
        "fallback": fallback,
        "kb_version": kb_version,
        "prompt_version": done.get("prompt_version", ""),
        # 未審核資料的題目（圖紙外包廠回報）：只評是否忠實轉述，非事實正確性保證
        "review_status": q.get("review_status", ""),
        "answer_ok": answer_ok,
        "citation_ok": citation_ok,
        "n_sources": len(res["sources"]),
        "rearrange_ms": ra.get("ms"),
        "rearrange_fallback": ra.get("fallback") or "",
        # 矛盾檢查（docs/adr/028）：conflict＝判為有矛盾、加了提醒；none＝沒有；空白＝沒檢查
        "conflict_check": ""
        if not res["conflict_check"]
        else "fallback"
        if res["conflict_check"]["fallback"]
        else "conflict"
        if res["conflict_check"]["conflict"]
        else "none",
        "first_token_ms": (done.get("latency_ms") or {}).get("first_token"),
        "total_ms": (done.get("latency_ms") or {}).get("total") or round((time.time() - t0) * 1000),
        "cost_twd": done.get("cost_twd", 0),
        "egress_images": egress.get("images", 0),
        "egress_chunks": egress.get("chunks", 0),
        "egress_bytes": egress.get("bytes", 0),
        "error": (res["error"] or {}).get("code", ""),
        "distractor_kept": dist.get("distractor_kept", ""),
        "distractor_cited": dist.get("distractor_cited", ""),
        "distractor_flagged": dist.get("distractor_flagged", ""),
        "distractor_misled": dist.get("distractor_misled", ""),
        "refused": no_kb.get("refused", ""),
        "hallucinated": no_kb.get("hallucinated", ""),
        "answer": res["answer"].replace("\n", " "),
    }


def _answer_rates_by_type(results: list[dict]) -> dict:
    return {
        t: sum(r["answer_ok"] for r in results if r["type"] == t)
        / sum(1 for r in results if r["type"] == t)
        for t in sorted({r["type"] for r in results})
    }


def _answer_rates_by_split_type(results: list[dict]) -> dict:
    # dev 只拿來挑設定（LoRA 挑 epoch），成績看 test（docs/adr/043）
    return {
        sp: _answer_rates_by_type([r for r in results if r["split"] == sp])
        for sp in sorted({r["split"] for r in results})
    }


def _summarize_distractors(injected: list[dict]) -> dict | None:
    """篩選擋下率越高越好，引用率與被帶偏率越低越好。"""
    if not injected:
        return None
    return {
        "n": len(injected),
        "filtered_out": sum(not r["distractor_kept"] for r in injected) / len(injected),
        "cited": sum(r["distractor_cited"] for r in injected) / len(injected),
        "flagged": sum(r["distractor_flagged"] for r in injected) / len(injected),
        "misled": sum(r["distractor_misled"] for r in injected) / len(injected),
    }


def _summarize_no_kb(no_kb_rows: list[dict]) -> dict | None:
    """出錯、落到備援的題目沒有判定，仍算在亂編率分母、不算拒答。"""
    if not no_kb_rows:
        return None
    return {
        "n": len(no_kb_rows),
        "refused": sum(r["refused"] is True for r in no_kb_rows) / len(no_kb_rows),
        "hallucinated": sum(r["hallucinated"] is True for r in no_kb_rows) / len(no_kb_rows),
    }


def _summarize_strategy(results: list[dict]) -> dict:
    ok = [r for r in results if not r["error"]]
    injected = [r for r in ok if r["distractor_kept"] != ""]
    no_kb_rows = [r for r in results if r["type"] in NO_KB_ANSWER_TYPES]
    return {
        "n": len(results),
        "errors": len(results) - len(ok),
        "fallbacks": sum(r["fallback"] for r in results),
        "models": sorted({r["model"] for r in results if r["model"]}),
        "answer_ok": sum(r["answer_ok"] for r in results) / len(results) if results else None,
        "citation_ok": sum(r["citation_ok"] for r in results) / len(results) if results else None,
        "mean_sources": statistics.mean([r["n_sources"] for r in ok]) if ok else None,
        "rearrange_fallbacks": sum(1 for r in results if r["rearrange_fallback"]),
        "p95_first_token_ms": p95([r["first_token_ms"] for r in ok]),
        "p95_total_ms": p95([r["total_ms"] for r in ok]),
        "median_total_ms": statistics.median([r["total_ms"] for r in ok]) if ok else None,
        "cost_twd": round(sum(r["cost_twd"] for r in results), 4),
        "egress_bytes": sum(r["egress_bytes"] for r in results),
        "by_type": _answer_rates_by_type(results),
        "by_split_type": _answer_rates_by_split_type(results),
        "distractor": _summarize_distractors(injected),
        "no_kb_answer": _summarize_no_kb(no_kb_rows),
    }


def _evaluate_questions(
    client: httpx.Client,
    questions: list[dict],
    strategies: list[str],
    source_lang: dict,
    kb_version: str,
) -> tuple[list[dict], dict, str]:
    rows, by_strategy = [], {}
    prompt_version = ""
    for strat in strategies:
        results = []
        for q in questions:
            row = _evaluate_question(client, q, strat, source_lang, kb_version)
            # 整次評估記畫作的 prompt 版本；圖紙題回的是 drawing_vN，每列另外記
            if row["prompt_version"] and not q.get("part_id"):
                prompt_version = row["prompt_version"]
            rows.append(row)
            results.append(row)
            mark = "✓" if row["answer_ok"] else "✗"
            tag = f"（{q['review_status']}）" if q.get("review_status") else ""
            print(f"  [{strat}] {mark} {q['id']}{tag} {row['answer'][:60]}")
        by_strategy[strat] = _summarize_strategy(results)
    return rows, by_strategy, prompt_version


def _write_results(
    run_id: str,
    rows: list[dict],
    img: dict,
    kb_version: str,
    prompt_version: str,
    split: str,
    by_strategy: dict,
) -> None:
    runs = RUNS
    runs.mkdir(exist_ok=True)
    with (runs / f"{run_id}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["question_id"])
        w.writeheader()
        w.writerows(rows)
    if img["rows"]:
        with (runs / f"{run_id}-images.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(img["rows"][0]))
            w.writeheader()
            w.writerows(img["rows"])
    summary = {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "kb_version": kb_version,
        "prompt_version": prompt_version,
        "summary": {k: v for k, v in img.items() if k != "rows"} | {"split": split},
        "by_strategy": by_strategy,
    }
    (runs / f"{run_id}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _print_results(run_id: str, by_strategy: dict) -> None:
    print(json.dumps(by_strategy, ensure_ascii=False, indent=2))
    if "hybrid" in by_strategy and "hybrid_norag" in by_strategy:
        gain = by_strategy["hybrid"]["answer_ok"] - by_strategy["hybrid_norag"]["answer_ok"]
        print(f"檢索增益：{gain * 100:+.0f} 個百分點（目標 ≥ +15）")
    print(f"結果已寫入 eval/runs/{run_id}.*")


def _exit_code(rows: list[dict]) -> int:
    # LoRA 對照組的每一題都要由 LoRA 回答：落到備援或出錯，這次評估就不能當 LoRA 的成績
    broken = [
        f"{r['strategy']} {r['question_id']}（{'備援' if r['fallback'] else r['error']}）"
        for r in rows
        if r["strategy"].startswith("lora") and (r["fallback"] or r["error"])
    ]
    if broken:
        print("LoRA 有題目不是由 LoRA 回答：" + "、".join(broken))
        return 3
    return 0


def main() -> int:
    args = _parse_args()

    client = httpx.Client(base_url=args.base, timeout=60)
    try:
        client.get("/api/v1/health").raise_for_status()
    except httpx.HTTPError:
        print(f"連不上後端 {args.base}，請先執行 make dev 或 make demo")
        return 1
    # 所有 /api/v1 請求都要 JWT（docs/adr/015）：先取一張訪客憑證（存在 client 的 cookie）；
    # 索引 manifest 在管理診斷（docs/adr/030），要展示模式切成主管
    client.get("/api/v1/auth/accounts").raise_for_status()
    client.post("/api/v1/auth/switch", json={"account_id": "manager"}).raise_for_status()
    manifest = client.get("/api/v1/admin/diagnostics").json()["manifest"]
    if not client.get("/api/v1/status").json()["eval_controls"] and any(
        STRATEGY_BODY.get(s, {}).get("use_retrieval") is False or s in ("api_nokb", "mock")
        for s in args.strategies.split(",")
    ):
        print("提醒：後端沒開 EVAL_CONTROLS，關檢索與 mock 對照組會被生成閘門降級（docs/adr/030）")
    kb_ids = {a["id"] for a in client.get("/api/v1/artworks").json()["items"]}
    # 工廠圖紙題（part_id，docs/adr/020）：圖紙多是機密，切成主管才看得到全部
    try:
        all_questions = load_questions(args.qa.split(","))
    except ValueError as e:
        print(f"題庫讀取失敗：{e}")
        return 1
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

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    print(f"評估 {run_id}：kb_version={manifest['kb_version']}，{len(kb_ids)} 幅畫")

    if args.skip_images:
        img = {"rows": []}
    else:
        img = eval_images(client, kb_ids)
        print(
            f"以圖搜圖 Top-1 {img['image_top1']:.0%}（{img['n_known']} 張），"
            f"未收錄拒答 {img['reject_rate']:.0%}（{img['n_unknown']} 張）"
        )

    questions = _select_questions(all_questions, kb_ids, args.split, args.outside_kb)
    rows, by_strategy, prompt_version = _evaluate_questions(
        client, questions, args.strategies.split(","), source_lang, manifest["kb_version"]
    )
    _write_results(
        run_id, rows, img, manifest["kb_version"], prompt_version, args.split, by_strategy
    )
    _print_results(run_id, by_strategy)
    return _exit_code(rows)


if __name__ == "__main__":
    sys.exit(main())
