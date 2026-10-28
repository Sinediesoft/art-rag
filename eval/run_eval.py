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

圖紙生成層多帳號評測（--accounts，TASK-20261029-001）：
      python eval/run_eval.py --accounts manager,sales [--strategies hybrid] [--run-id ID]
      逐帳號切換身分、由後端 GET /parts 取可見圖紙快照，再把圖紙題庫的每一題都送進
      /chat（不剔除看不到的圖紙）；看不到的圖紙要在第 1 段就降級（沒有來源、沒有 token）。
      產物只有 <out-dir>/<run-id>-generation-accounts.json（不覆寫既有檔案、mock 不進 eval/runs）；
      有外洩列結束碼 3，不能寫或連線／身分／快照失敗結束碼 2，輸入錯誤 1。
"""

import argparse
import contextlib
import copy
import csv
import hashlib
import json
import posixpath
import re
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
RUNS = EVAL / "runs"
OFFICIAL_RUNS = EVAL / "runs"  # 正式成績目錄：mock 產物不可寫進來（和 RUNS 分開，測試會各自換掉）
GOLD_PATH = EVAL / "drawing_retrieval" / "gold.json"
KB_PARTS_DIR = ROOT / "kb" / "parts"

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
    ap.add_argument(
        "--strategies",
        default=None,
        help="預設：舊模式 hybrid,hybrid_norag；--accounts 模式 hybrid",
    )
    ap.add_argument("--split", default="all", choices=["dev", "test", "all"])
    ap.add_argument("--qa", default="qa.jsonl", help="eval/ 下的題目檔，逗號分隔")
    ap.add_argument(
        "--outside-kb",
        action="store_true",
        help="問知識庫外畫作的題目改成不帶畫作送出，算亂編率（預設跳過這些題目）",
    )
    ap.add_argument("--skip-images", action="store_true", help="不跑以圖搜圖評估")
    ap.add_argument(
        "--accounts",
        default=None,
        help="圖紙生成層多帳號評測：逗號分隔的帳號（要在 gold.json 的 accounts 內）；不給＝舊模式",
    )
    ap.add_argument("--out-dir", default=None, help="--accounts 模式的輸出目錄（預設 eval/runs）")
    ap.add_argument("--run-id", default=None, help="--accounts 模式的 run 識別碼（預設依時間產生）")
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
    if args.accounts is not None:
        return _main_accounts(args)
    strategies = args.strategies or "hybrid,hybrid_norag"

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
        for s in strategies.split(",")
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
        client, questions, strategies.split(","), source_lang, manifest["kb_version"]
    )
    _write_results(
        run_id, rows, img, manifest["kb_version"], prompt_version, args.split, by_strategy
    )
    _print_results(run_id, by_strategy)
    return _exit_code(rows)


# ====================================================================================
# 圖紙生成層多帳號評測（--accounts，TASK-20261029-001）
# 可見性一律來自後端 GET /parts（用該帳號的憑證）；評測器不宣告任何角色權限。
# 只碰 health、auth/accounts、auth/switch、parts、status、chat。
# ====================================================================================

ACCOUNTS_SCHEMA = "generation-accounts/1"
ACCOUNTS_SUFFIX = "-generation-accounts.json"
SUBSETS = ("visible_answerable", "permission_degraded", "no_answer", "missing_part")
_RUN_ID_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

CLASSIFICATIONS = {
    "visible_answerable": ("answer", "圖紙在這個帳號的可見清單內，題目有答案"),
    "no_answer": ("refusal", "圖紙在這個帳號的可見清單內，但題目標為拒答（圖紙沒有這個資訊）"),
    "permission_degraded": ("stage1_degrade", "圖紙存在但不在這個帳號的可見清單內，第 1 段應降級"),
    "missing_part": ("refusal", "圖紙不存在於知識庫，應拒答"),
}


class AccountsRunError(Exception):
    """連線、身分或可見性快照失敗：整個 run 不產出任何成績。"""


def _git_provenance() -> dict:
    """執行時的程式版本 {head, dirty, dirty_paths, error}（做法同 retrieval_runtime）。"""
    git = ["git", "-C", str(ROOT), "-c", "core.quotePath=false"]
    try:
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            [*git, "status", "--porcelain=v1"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        return {"head": None, "dirty": None, "dirty_paths": [], "error": str(e)}
    paths = [line[3:] for line in status.splitlines() if line.strip()]
    return {"head": head, "dirty": bool(paths), "dirty_paths": paths, "error": None}


def load_gold_accounts(path: Path | None = None) -> list[str]:
    data = json.loads(Path(path or GOLD_PATH).read_text("utf-8"))
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if (
        not isinstance(accounts, list)
        or not accounts
        or not all(isinstance(a, str) and a for a in accounts)
    ):
        raise ValueError("gold.json 沒有可用的 accounts 名單")
    return accounts


def parse_accounts(raw: str, allowed: list[str]) -> list[str]:
    """逗號分隔的帳號；空字串、空組、重複、不在 gold 名單的一律 ValueError。"""
    names = [x.strip() for x in raw.split(",")]
    if not names or any(not n for n in names):
        raise ValueError("--accounts 不可有空的帳號")
    if len(set(names)) != len(names):
        raise ValueError("--accounts 有重複的帳號")
    if unknown := [n for n in names if n not in allowed]:
        raise ValueError(f"帳號不在 gold.json 的 accounts：{', '.join(unknown)}")
    return names


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_body(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError:
        return None


def _flatten_keywords(keywords) -> list[str]:
    """keywords 是「AND 組 → OR 候選」；外洩證據只看字串有沒有出現，依原順序展平。"""
    out: list[str] = []
    for item in keywords or []:
        out.extend(_flatten_keywords(item) if isinstance(item, list) else [item])
    return [k for k in out if isinstance(k, str) and k]


def _sources_events(events: list[dict]) -> list[dict]:
    return [
        e["data"] for e in events if e.get("event") == "sources" and isinstance(e.get("data"), dict)
    ]


def leak_reasons(q: dict, answer: str, events: list[dict]) -> list[str]:
    """看不到的圖紙回答裡的外洩證據：有來源、有引用標記、出現題目的答案關鍵字。
    拒答字樣不能抵銷證據；順序固定為 sources_returned、citation_marker、keyword_hit。"""
    reasons = []
    if any(s.get("sources") for s in _sources_events(events)):
        reasons.append("sources_returned")
    if re.search(r"\[\d+\]", answer):
        reasons.append("citation_marker")
    seen = set()
    for kw in _flatten_keywords(q.get("keywords")):
        if kw in answer and kw not in seen:
            seen.add(kw)
            reasons.append(f"keyword_hit:{kw}")
    return reasons


def _pure_refusal(answer: str) -> bool:
    """降級訊息這種「只有一句查無資料」：不帶引用、數字與英文字母，長度有限。"""
    text = answer.strip()
    sentences = [x for x in re.split(r"[。！？!?\n]", text) if x.strip()]
    return (
        text.startswith("查無資料")
        and len(sentences) <= 1
        and not re.search(r"\[\d+\]", text)
        and not re.search(r"[0-9A-Za-z]", text)
        and len(text) <= 60
    )


def _stage1_failures(
    events: list[dict], done: dict | None, error: str, fallback: bool
) -> list[str]:
    """第 1 段純降級要同時成立的條件；回傳沒成立的原因（空 list＝純降級）。"""
    failures = []
    if not done:
        failures.append("沒有 done 事件")
    pipelines = [s["pipeline"] for s in _sources_events(events) if "pipeline" in s]
    if isinstance(done, dict) and "pipeline" in done:
        pipelines.append(done["pipeline"])
    if not pipelines:
        failures.append("沒有 pipeline 可證明停在第 1 段")
    for pipe in pipelines:
        items = [p for p in pipe if isinstance(p, dict)] if isinstance(pipe, list) else []
        if not any(p.get("stage") == 1 and p.get("status") == "block" for p in items):
            failures.append("pipeline 沒有第 1 段 block")
        if any(isinstance(p.get("stage"), int) and p["stage"] > 1 for p in items):
            failures.append("pipeline 有第 1 段之後的步驟")
    if any(s.get("sources") for s in _sources_events(events)):
        failures.append("sources 不是空的")
    if isinstance(done, dict):
        if done.get("degraded") is not True:
            failures.append("done.degraded 不是 True")
        tokens = done.get("tokens") if isinstance(done.get("tokens"), dict) else {}
        if tokens.get("input") != 0 or tokens.get("output") != 0:
            failures.append("done.tokens 不是 0")
        egress = done.get("egress") if isinstance(done.get("egress"), dict) else {}
        if egress.get("images") != 0 or egress.get("chunks") != 0:
            failures.append("done.egress 的 images／chunks 不是 0")
    if error:
        failures.append(f"有錯誤 {error}")
    if fallback:
        failures.append("改用了備援模型")
    return list(dict.fromkeys(failures))


def run_chat_raw(client: httpx.Client, body: dict) -> dict:
    """送一題 /chat，事件原樣保存（含 token、heartbeat；key 順序不動）。出錯不丟例外。"""
    out = {
        "events": [],
        "answer": "",
        "sources_state": "absent",
        "done": None,
        "error": "",
        "error_event": None,
        "request_id": "",
    }
    events = out["events"]
    invalid = None
    try:
        with client.stream("POST", "/api/v1/chat", json=body, timeout=180) as resp:
            if resp.status_code != 200:
                resp.read()
                payload = _json_body(resp)
                err = payload.get("error") if isinstance(payload, dict) else None
                code = err.get("code") if isinstance(err, dict) else None
                out["error"] = str(code) if code else f"HTTP {resp.status_code}"
                out["error_event"] = {"http_status": resp.status_code, "error": err}
            else:
                name = None
                for line in resp.iter_lines():
                    if not line:
                        name = None
                    elif line.startswith(":"):
                        continue  # keepalive 註解不算事件
                    elif line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        raw = line[5:].strip()
                        item = {"seq": len(events), "event": name or "message"}
                        try:
                            item["data"] = json.loads(raw)
                        except ValueError:
                            item |= {"data": None, "raw": raw, "invalid_json": True}
                            invalid = invalid or item["seq"]
                        events.append(item)
    except httpx.HTTPError as e:
        out["error"] = "TRANSPORT_ERROR"
        out["error_event"] = {
            "code": "TRANSPORT_ERROR",
            "type": type(e).__name__,
            "message": str(e),
        }

    srcs = []
    for e in events:
        data = e["data"]
        if e["event"] == "token" and isinstance(data, dict) and isinstance(data.get("text"), str):
            out["answer"] += data["text"]
        elif e["event"] == "sources":
            srcs.append(data)
        elif e["event"] == "done":
            out["done"] = data
        elif e["event"] == "error":
            out["error_event"] = data
            code = data.get("code") if isinstance(data, dict) else None
            out["error"] = str(code) if code else "STREAM_ERROR"
    if srcs:
        out["sources_state"] = (
            "empty"
            if all(not (s.get("sources") if isinstance(s, dict) else None) for s in srcs)
            else "present"
        )
    if invalid is not None and not out["error"]:
        out["error"] = "STREAM_PARSE_ERROR"
        out["error_event"] = {"code": "STREAM_PARSE_ERROR", "seq": invalid}
    for cand in (out["error_event"], out["done"], srcs[-1] if srcs else None):
        rid = cand.get("request_id") if isinstance(cand, dict) else None
        if rid:
            out["request_id"] = rid
            break
    return out


def _last_sources_list(events: list[dict]) -> list:
    sources = _sources_events(events)
    last = sources[-1].get("sources") if sources else None
    return last if isinstance(last, list) else []


def classify(q: dict, visible: set[str], existing: set[str]) -> tuple[str, str, str, bool]:
    """(classification, expected_outcome, classification_reason, visible)，互斥且有優先序。"""
    pid = q["part_id"]
    if pid in visible:
        cls = "no_answer" if q.get("expect_refusal") else "visible_answerable"
    elif pid in existing:
        cls = "permission_degraded"
    else:
        cls = "missing_part"
    expected, reason = CLASSIFICATIONS[cls]
    return cls, expected, reason, pid in visible


def _score_row(q: dict, cls: str, chat: dict) -> dict:
    """新模式判分：只用逐題紀錄裡的證據，可由 events／answer 重算。"""
    events, answer, done = chat["events"], chat["answer"], chat["done"]
    error = chat["error"]
    fallback = bool((done or {}).get("fallback"))
    broken = bool(error) or fallback
    refused = None
    score = {
        "answer_ok": False,
        "citation_ok": False if cls == "visible_answerable" else None,
        "stage1_only": None,
        "stage1_failures": [],
        "leaked": False,
        "leak_reasons": [],
    }
    if cls == "permission_degraded":
        failures = _stage1_failures(events, done, error, fallback)
        reasons = leak_reasons(q, answer, events)
        score |= {
            "stage1_only": not failures,
            "stage1_failures": failures,
            "leaked": bool(reasons),
            "leak_reasons": reasons,
            "answer_ok": not failures and _pure_refusal(answer) and not reasons,
        }
    refused = False if broken else bool(REFUSAL.search(answer))
    if not broken and cls in ("no_answer", "missing_part"):
        score["answer_ok"] = refused
    elif not broken and cls == "visible_answerable":
        res = {
            "answer": answer,
            "sources": _last_sources_list(events),
            "done": done,
            "error": None,
        }
        # 來源格式不對：保守算錯，不讓整個 run 中斷
        with contextlib.suppress(KeyError, TypeError, ValueError):
            score["answer_ok"], score["citation_ok"] = score_answer(q, res)
    return score | {"refused": refused}


def _group(rows: list[dict]) -> dict:
    def rate(num: int, den: int) -> dict:
        return {"num": num, "den": den, "rate": num / den if den else None}

    cited = [r for r in rows if r["citation_ok"] is not None]
    hidden = [r for r in rows if r["classification"] == "permission_degraded"]
    return {
        "n": len(rows),
        "errors": sum(bool(r["error"]) for r in rows),
        "fallbacks": sum(bool(r["fallback"]) for r in rows),
        "answer_ok": rate(sum(r["answer_ok"] is True for r in rows), len(rows)),
        "citation_ok": rate(sum(r["citation_ok"] is True for r in cited), len(cited)),
        "refusal": rate(sum(r["refused"] is True for r in rows), len(rows)),
        "leak": rate(sum(r["leaked"] is True for r in hidden), len(hidden)),
    }


def summarize_rows(rows: list[dict]) -> dict:
    """{帳號: {策略: {子集: 群組}}}；只用逐題欄位算，錯誤與備援都留在分母。"""
    out: dict = {}
    for account in sorted({r["account"] for r in rows}):
        mine = [r for r in rows if r["account"] == account]
        out[account] = {}
        for strategy in sorted({r["strategy"] for r in mine}):
            sel = [r for r in mine if r["strategy"] == strategy]
            out[account][strategy] = {"overall": _group(sel)} | {
                name: _group([r for r in sel if r["classification"] == name]) for name in SUBSETS
            }
    return out


def _switch_account(client: httpx.Client, account: str) -> dict:
    """切換並雙重驗證身分（切換回應與 GET /auth/accounts 都要是該帳號）；回傳切換回應。"""
    resp = client.post("/api/v1/auth/switch", json={"account_id": account})
    if resp.status_code != 200:
        raise AccountsRunError(f"{account}：切換身分失敗（HTTP {resp.status_code}）")
    body = _json_body(resp)
    current = body.get("current") if isinstance(body, dict) else None
    if not isinstance(current, dict) or current.get("id") != account:
        raise AccountsRunError(f"{account}：切換回應的身分不是 {account}")
    check = client.get("/api/v1/auth/accounts")
    check_body = _json_body(check) if check.status_code == 200 else None
    shown = check_body.get("current") if isinstance(check_body, dict) else None
    if not isinstance(shown, dict) or shown.get("id") != account:
        raise AccountsRunError(f"{account}：切換後 /auth/accounts 的實際身分不是 {account}")
    return body


def _snapshot_account(client: httpx.Client, account: str) -> dict:
    switched = _switch_account(client, account)
    kb_version = switched.get("kb_version")
    if not isinstance(kb_version, str) or not kb_version:
        raise AccountsRunError(f"{account}：切換回應沒有 kb_version")
    resp = client.get("/api/v1/parts")
    if resp.status_code != 200:
        raise AccountsRunError(f"{account}：取得可見圖紙失敗（HTTP {resp.status_code}）")
    body = _json_body(resp)
    items = body.get("items") if isinstance(body, dict) else None
    hidden = body.get("hidden") if isinstance(body, dict) else None
    if (
        not isinstance(items, list)
        or not all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in items)
        or not isinstance(hidden, int)
        or isinstance(hidden, bool)
    ):
        raise AccountsRunError(f"{account}：/parts 回應格式異常，不能當成空清單")
    return {
        "identity": switched["current"],
        "visible_part_ids": sorted({i["id"] for i in items}),
        "hidden": hidden,
        "kb_version": kb_version,
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def _status_controls(client: httpx.Client) -> bool | None:
    """/status 只剩 eval_controls 當備援；llm_mode 不在這裡（StatusResponse 沒有）。"""
    resp = client.get("/api/v1/status")
    body = _json_body(resp) if resp.status_code == 200 else None
    controls = body.get("eval_controls") if isinstance(body, dict) else None
    return controls if isinstance(controls, bool) else None


def _try_diagnostics(client: httpx.Client) -> dict | None:
    """嘗試讀管理診斷（只有主管讀得到）；非 200、例外、非物件都當未知，不讓評測失敗。"""
    try:
        resp = client.get("/api/v1/admin/diagnostics")
        body = _json_body(resp) if resp.status_code == 200 else None
    except httpx.HTTPError:
        return None
    return body if isinstance(body, dict) else None


def _merge_diagnostics(found: dict, body: dict | None) -> None:
    """每個欄位以第一個讀得到、型別正確的帳號為準。"""
    if body is None:
        return
    for key, kind in (
        ("llm_mode", str),
        ("embed_mode", str),
        ("eval_controls", bool),
        ("manifest", dict),
    ):
        if found.get(key) is None and isinstance(body.get(key), kind):
            found[key] = body[key]


def _load_accounts_questions(args) -> tuple[list[str], list[dict], list[Path]] | None:
    """解析帳號與題庫；任何輸入錯誤印訊息回傳 None（呼叫端回 1）。"""
    if args.split != "all" or args.outside_kb:
        print("--accounts 模式不能用 --split／--outside-kb：所有題目都要跑，不剔除任何一題")
        return None
    try:
        accounts = parse_accounts(args.accounts, load_gold_accounts())
    except (ValueError, OSError) as e:
        print(f"帳號名單錯誤：{e}")
        return None
    qa_arg = DRAWING_QA if args.qa == "qa.jsonl" else args.qa
    names = [n.strip() for n in qa_arg.split(",")]
    try:
        questions = load_questions(names)
        for q in questions:
            if not isinstance(q.get("id"), str) or not isinstance(q.get("question"), str):
                raise ValueError("每題都要有字串的 id 與 question")
            if not isinstance(q.get("part_id"), str) or not q["part_id"]:
                raise ValueError(f"{q.get('id')}：--accounts 模式只收圖紙題（要有 part_id）")
            if not q.get("expect_refusal"):
                check_keywords(q["id"], q.get("keywords"))
                if not isinstance(q.get("topics"), list):
                    raise ValueError(f"{q['id']}：可答題要有 topics")
        if len({q["id"] for q in questions}) != len(questions):
            raise ValueError("題庫 id 重複")
        if not questions:
            raise ValueError("題庫是空的")
    except (ValueError, OSError) as e:
        print(f"題庫讀取失敗：{e}")
        return None
    return accounts, questions, [EVAL / n for n in names]


def _kb_parts_sha256() -> str:
    lines = "".join(
        f"{p.name}:{_sha256(p.read_bytes())}\n" for p in sorted(KB_PARTS_DIR.glob("*.json"))
    )
    return _sha256(lines.encode())


def _qa_hashes(paths: list[Path]) -> tuple[str, str | None]:
    qa = _sha256(b"".join(p.read_bytes() for p in paths))
    drawing = (EVAL / DRAWING_QA).resolve()
    for p in paths:
        if p.resolve() == drawing or posixpath.normpath(p.as_posix()) == DRAWING_QA:
            return qa, _sha256(p.with_name(DRAWING_ANSWERS).read_bytes())
    return qa, None


def _account_row(
    account: str, strat: str, q: dict, body: dict, chat: dict, visible: set[str], existing: set[str]
) -> dict:
    cls, expected, reason, is_visible = classify(q, visible, existing)
    score = _score_row(q, cls, chat)
    done = chat["done"] if isinstance(chat["done"], dict) else {}
    return {
        "account": account,
        "strategy": strat,
        "question_id": q["id"],
        "part_id": q["part_id"],
        "question": q["question"],
        "topics": q.get("topics"),
        "expected_keywords": None if q.get("expect_refusal") else q.get("keywords"),
        "review_status": q.get("review_status", ""),
        "no_answer_reason": q.get("no_answer_reason"),
        "classification": cls,
        "expected_outcome": expected,
        "classification_reason": reason,
        "visible": is_visible,
        "request": body,
        "request_id": chat["request_id"],
        "answer": chat["answer"],
        "events": chat["events"],
        "sources_state": chat["sources_state"],
        "done": chat["done"],
        "error": chat["error"],
        "error_event": chat["error_event"],
        "fallback": bool(done.get("fallback")),
        **score,
    }


def _print_account_summary(groups: dict) -> None:
    def fmt(m: dict) -> str:
        return f"{m['num']}/{m['den']}（{m['rate']:.0%}）" if m["rate"] is not None else "n/a"

    for account, by_strategy in groups.items():
        for strategy, subsets in by_strategy.items():
            g = subsets["overall"]
            print(
                f"[{account}/{strategy}] answer_ok {fmt(g['answer_ok'])}，"
                f"citation_ok {fmt(g['citation_ok'])}，拒答 {fmt(g['refusal'])}，"
                f"外洩 {fmt(g['leak'])}，錯誤 {g['errors']}，備援 {g['fallbacks']}"
            )


def _main_accounts(args: argparse.Namespace) -> int:
    loaded = _load_accounts_questions(args)
    if loaded is None:
        return 1
    accounts, questions, qa_paths = loaded
    strategies = [s.strip() for s in (args.strategies or "hybrid").split(",")]
    if (
        not all(strategies)
        or len(set(strategies)) != len(strategies)
        or any(s not in STRATEGY_BODY for s in strategies)
    ):
        print(f"策略錯誤：{args.strategies}（要是逗號分隔、不重複、已知的策略）")
        return 1
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    if not _RUN_ID_OK.fullmatch(run_id):
        print(f"--run-id 只能用英數、點、底線、連字號：{run_id}")
        return 1
    out_dir = Path(args.out_dir) if args.out_dir else Path(RUNS)

    client = httpx.Client(base_url=args.base, timeout=60)
    try:
        return _run_accounts(
            client, args, accounts, questions, qa_paths, strategies, run_id, out_dir
        )
    except (AccountsRunError, httpx.HTTPError) as e:
        print(f"評測中止（沒有寫任何產物）：{e}")
        return 2
    finally:
        client.close()


def _run_accounts(
    client, args, accounts, questions, qa_paths, strategies, run_id: str, out_dir: Path
) -> int:
    try:
        client.get("/api/v1/health").raise_for_status()
    except httpx.HTTPError:
        print(f"連不上後端 {args.base}，請先執行 make dev 或 make demo")
        return 2
    client.get("/api/v1/auth/accounts").raise_for_status()  # 先取一張訪客憑證（docs/adr/015）

    # 第一輪：逐帳號切換、驗身分、取可見圖紙快照；任何一個失敗就整個 run 作廢
    snapshots = {}
    diag: dict = {}
    for a in accounts:
        snapshots[a] = _snapshot_account(client, a)
        _merge_diagnostics(diag, _try_diagnostics(client))
    if len({s["kb_version"] for s in snapshots.values()}) != 1:
        raise AccountsRunError("各帳號看到的 kb_version 不一致")
    kb_version = snapshots[accounts[0]]["kb_version"]
    llm_mode = diag.get("llm_mode")
    eval_controls = diag.get("eval_controls")
    if eval_controls is None:
        eval_controls = _status_controls(client)
    mock_reasons = []
    if llm_mode == "mock":
        mock_reasons.append("後端 llm_mode=mock")
    mock_reasons += [f"策略 {s} 含 mock" for s in strategies if "mock" in s]
    mock = bool(mock_reasons)
    real = llm_mode == "real" and not mock
    if eval_controls is False and any(
        STRATEGY_BODY[s].get("use_retrieval") is False or s in ("api_nokb", "mock")
        for s in strategies
    ):
        print("提醒：後端沒開 EVAL_CONTROLS，關檢索與 mock 對照組會被生成閘門降級（docs/adr/030）")

    # 開跑前先確定寫得下去：不覆寫既有 run、mock 不進正式 run 目錄
    path = out_dir / f"{run_id}{ACCOUNTS_SUFFIX}"
    if mock and out_dir.resolve() == Path(OFFICIAL_RUNS).resolve():
        print(f"mock 結果不能寫進正式 run 目錄 {out_dir}：{'；'.join(mock_reasons)}")
        return 2
    if path.exists():
        print(f"{path} 已存在，不覆寫；請換 --run-id")
        return 2

    existing = {p.stem for p in KB_PARTS_DIR.glob("*.json")}
    for snap in snapshots.values():
        existing |= set(snap["visible_part_ids"])
    qa_sha, answers_sha = _qa_hashes(qa_paths)
    print(f"評測 {run_id}：{len(accounts)} 帳號 × {len(strategies)} 策略 × {len(questions)} 題")

    # 第二輪：每個帳號重新切換並驗身分，再跑全部策略 × 題目（不剔除看不到的圖紙）
    rows = []
    for account in accounts:
        switched = _switch_account(client, account)
        if switched.get("kb_version") != kb_version:
            raise AccountsRunError(f"{account}：評測途中 kb_version 變了")
        visible = set(snapshots[account]["visible_part_ids"])
        for strat in strategies:
            for q in questions:
                body = _chat_body(q, strat)
                row = _account_row(
                    account, strat, q, body, run_chat_raw(client, body), visible, existing
                )
                rows.append(row)
                mark = "✓" if row["answer_ok"] else "✗"
                print(f"  [{account}/{strat}] {mark} {q['id']} {row['classification']}")

    groups = summarize_rows(rows)
    created_at = datetime.now(UTC).isoformat()
    done_events = [r["done"] for r in rows if isinstance(r["done"], dict)]
    prompt_version = next((d["prompt_version"] for d in done_events if d.get("prompt_version")), "")
    models = sorted({d["model"] for d in done_events if d.get("model")})
    provenance = {
        "git": _git_provenance(),
        "hashes": {
            "qa_sha256": qa_sha,
            "answers_sha256": answers_sha,
            "gold_sha256": _sha256(Path(GOLD_PATH).read_bytes()),
            "kb_version": kb_version,
            "kb_parts_sha256": _kb_parts_sha256(),
        },
        "strategies": strategies,
        "accounts": accounts,
        "question_ids": [q["id"] for q in questions],
        "models": models,
        "llm_mode": llm_mode,
        "embed_mode": diag.get("embed_mode"),
        "eval_controls": eval_controls,
        "manifest": diag.get("manifest"),
        "base_url": args.base,
        "qa_files": [str(p) for p in qa_paths],
        "created_at": created_at,
    }
    artifact = {
        # 頂層保留舊 run 的形狀：GET /eval/runs 把沒排除的 .json 都當 EvalRun 讀
        "run_id": run_id,
        "created_at": created_at,
        "kb_version": kb_version,
        "prompt_version": prompt_version,
        "summary": {
            "mode": "generation-accounts",
            "accounts": accounts,
            "strategies": strategies,
            "n_rows": len(rows),
        },
        "by_strategy": {},
        "schema_version": ACCOUNTS_SCHEMA,
        "kind": "generation-accounts",
        "real": real,
        "mock": mock,
        "mock_reasons": mock_reasons,
        "accounts": accounts,
        "strategies": strategies,
        "snapshots": snapshots,
        "provenance": provenance,
        "groups": groups,
        "rows": rows,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2)
    except FileExistsError:
        print(f"{path} 已存在，不覆寫；請換 --run-id")
        return 2
    _print_account_summary(groups)
    print(f"結果已寫入 {path}")
    leaked = [f"{r['account']} {r['question_id']}" for r in rows if r["leaked"]]
    if leaked:
        print("看不到的圖紙有外洩證據：" + "、".join(leaked))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
