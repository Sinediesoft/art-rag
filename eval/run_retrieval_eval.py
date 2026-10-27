"""文字段落檢索評估（TASK-20261027-001）：在程序內直接量 retrieve 的最終回傳，不經 LLM，
也不經 Jev 篩選、重排、生成閘門；不調參。結果寫 eval/runs/<run_id>-retrieval-layer-metrics.json。

模式（各自分組統計，不混在一起平均）：
- production_retrieve：直接呼叫 app.services.chat_service.retrieve，原樣保存有序 sources
  （含 global-fill 補庫、catalog 目錄段），不替換 retrieve、不改問句、不裁切、不注入 distractors。
  - 畫作指定（specified）：retrieve(問句, artwork_id)
  - 畫作全庫（global）：retrieve(問句, None)
  - 圖紙指定（specified）：retrieve(問句, None, part_id, MetaFilter.of(帳號, "mfg"))
  指定模式的問句含 global_fill_keywords 時，retrieve 會補同領域全庫：記 global_fill_triggered
  與跨文件回傳，不宣稱只搜自己的段落；沒有觸發的題另列 owner_only cohort。
- collection_global_diagnostic_postfilter（只有圖紙）：產品沒有「不指定圖紙、圖紙全庫」的
  retrieve 入口（不帶 part_id 一律走畫作庫），所以在評估層對真的 store.mfg 量：
  embed_text → search_chunks(k, owners=帳號 scope 看得到的圖紙) → 套 min_chunk_score 與
  relative_chunk_ratio。沒有 owner 優先、沒有「至少留 1 段」、沒有 catalog；
  不是 retrieve，也不是產品的全庫流程。

題目與 gold：畫作用 eval/qa.jsonl 的 artwork_id 題（圖紙題、staging 文件、只對自動色彩段落的題
列出排除原因），圖紙用 eval/drawing_retrieval/qa.jsonl。gold 由 retrieval_runtime.resolve_gold_cases
從文件 ID＋topics 推導全部 chunk_id，再依帳號看得到的圖紙 apply_visibility（看不到＝合法空答案）。
帳號照 eval/drawing_retrieval/gold.json 的 accounts（預設 manager、sales）。
指標見 retrieval_metrics：Recall@1/2/5、MRR、實際回傳 Precision、多正解 nDCG@5、
空答案誤回傳率、整份回傳的外洩數，每個數字附分子分母；逐題保存問句、gold、
有序回傳 ID＋分數，可以重算。

執行環境：一開始 retrieval_runtime.bootstrap(strict=True) 關掉 env_file（不讀 .env），
設定只從環境變數與 shared/*.yaml 取得。正式成績要 EMBED_MODE=real、索引 manifest 是 real，
還要有 retrieval_runtime.py build-index 寫的 real 索引收據（預設取 eval/runs 最新的
*-retrieval-index.json），而且收據和目前載入的索引一致；任一不符就不評估，只寫失敗的 JSON。
EMBED_MODE=mock 只給測試：要加 --allow-mock，輸出只能寫到 repo 外的暫存目錄。

用法：
  python eval/run_retrieval_eval.py [--domains art,mfg] [--accounts manager,sales]
      [--receipt PATH] [--run-id ID] [--out-dir eval/runs]
      [--qa-art eval/qa.jsonl] [--qa-mfg eval/drawing_retrieval/qa.jsonl]
      [--gold eval/drawing_retrieval/gold.json] [--allow-mock]
結束碼：0＝完整且通過全部檢查；1＝評估失敗或不能當正式成績（仍寫 JSON）；
2＝參數不對、mock 輸出位置不對、設定已先讀過或 run 檔已存在（不寫檔）。
"""

import argparse
import functools
import json
import re
import sys
import traceback
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import retrieval_metrics as rm  # noqa: E402
import retrieval_runtime as rr  # noqa: E402

print = functools.partial(print, flush=True)  # noqa: A001 — 背景執行時也能即時看到進度

SCHEMA_VERSION = "retrieval-layer-metrics/1"
ART_QA = ROOT / "eval" / "qa.jsonl"
MFG_QA = ROOT / "eval" / "drawing_retrieval" / "qa.jsonl"
DRAWING_GOLD = ROOT / "eval" / "drawing_retrieval" / "gold.json"
PRODUCTION = "production_retrieve"
DIAGNOSTIC = "collection_global_diagnostic_postfilter"
SPECIFIED, GLOBAL = "specified", "global"
# 題目屬於另一個領域時改由哪一份題庫評估
QA_BY_DOMAIN = {"art": "eval/qa.jsonl", "mfg": "eval/drawing_retrieval/qa.jsonl"}
MODE_BOUNDARY = {
    PRODUCTION: (
        "在程序內直接呼叫 app.services.chat_service.retrieve（不替換、不改問句），原樣保存有序 "
        "sources：指定文件時 own 優先、過門檻後至少留 1 段，問句含 global_fill_keywords 時補同領域"
        "全庫；不帶文件 ID 時搜全庫，清單／比較題最前面附 catalog。"
    ),
    DIAGNOSTIC: (
        "不是 retrieve，也不是產品的圖紙全庫流程（產品沒有不指定圖紙的 retrieve 入口）："
        "store.mfg＋embed_text＋search_chunks(k, owners=帳號 scope 看得到的圖紙)，再套 "
        "min_chunk_score 與 relative_chunk_ratio；沒有 owner 優先、沒有至少留 1 段、沒有 catalog。"
    ),
}
BOUNDARIES = [
    "只量 retrieve（第 3 段）的最終 sources：不含 /chat 第 1 段的文件授權（看不到或不存在的圖紙"
    "在 /chat 會先降級、不檢索），也不含第 4–7 段的 Jev 篩選、重排、生成閘門與 LLM。",
    "distractor 題用原問句與正常 gold，沒有注入干擾段落，所以沒有量抗注入。",
    "未審核的 descriptions（unreviewed_gold）只代表 topic 相關，不代表內容安全或事實正確；"
    "reviewed_gold_only cohort 不含這些題。",
    "catalog（知識庫目錄）等非索引來源原樣保留、算進 Precision 分母，並以 kind 標記。",
    "不存在文件（unknown）的指定題照題庫寫的 part_id 呼叫 retrieve；不是評估為了切換圖紙庫"
    "而虛構的 ID。",
    "指定已知文件時 retrieve 至少留 1 段，所以 no_answer 題幾乎一定誤回傳；這是檢索層行為，"
    "不是助手的拒答率。",
    "語料很小（畫作 3 幅、圖紙 6 張），Recall／MRR 要搭配 corpus 的段落數解讀。",
]
CASE_FIELDS = (
    "case_id",
    "question",
    "qa_type",
    "split",
    "owner_id",
    "topics",
    "status",
    "explicit_empty",
    "empty_kind",
    "original_empty_kind",
    "gold_chunk_ids",
    "gold_chunk_ids_all",
    "gold_chunk_topic",
    "multi_relevant",
    "unreviewed_gold",
    "hidden_target_count",
    "hidden_owner_ids",
    "owner_visible",
)
_DESC_CHUNK = re.compile(r"#\d{2,}-\d+$")
_RUN_ID = re.compile(r"[\w.-]+")


# ---------------------------------------------------------------- 純函式


def parse_list(text: str) -> list[str]:
    return [x.strip() for x in text.split(",") if x.strip()]


def keyword_hits(question: str, keywords: Iterable[str]) -> list[str]:
    """問句裡出現的 global_fill_keywords（照設定順序）。"""
    return [w for w in keywords if w in question]


def chunk_kind(chunk_id: str, owner_of: Mapping[str, str], catalog_id: str) -> str:
    """回傳來源的種類：description（KB descriptions 段落）、system_generated（#meta、#color）、
    catalog（知識庫目錄，系統依知識庫產生、不在索引裡）、non_index（其他不在索引的來源）。"""
    if chunk_id == catalog_id:
        return "catalog"
    if chunk_id not in owner_of:
        return "non_index"
    return "description" if _DESC_CHUNK.search(chunk_id) else "system_generated"


def normalize_returned(
    sources: Sequence[dict], *, owner_key: str, owner_of: Mapping[str, str], catalog_id: str
) -> list[dict]:
    """retrieve 的 sources → [{rank, chunk_id, owner_id, topic, score, kind}]，順序與長度不變。"""
    return [
        {
            "rank": i,
            "chunk_id": s.get("chunk_id"),
            "owner_id": owner_of.get(s.get("chunk_id"), s.get(owner_key)),
            "topic": s.get("topic"),
            "score": s.get("score"),
            "kind": chunk_kind(s.get("chunk_id"), owner_of, catalog_id),
        }
        for i, s in enumerate(sources, 1)
    ]


def postfilter(scored: Sequence[tuple[dict, float]], min_score: float, ratio: float) -> dict:
    """全庫診斷的門檻：floor＝max(min_score, 最高分×ratio)，和 retrieve 同一組參數，
    但沒有 owner 優先、全部低於門檻時也不保留任何一段。

    回傳：{"kept": [(item, score)], "floor": float, "best": float}
    """
    best = max((s for _, s in scored), default=0.0)
    floor = max(min_score, best * ratio)
    return {"kept": [(i, s) for i, s in scored if s >= floor], "floor": floor, "best": best}


def resolve_domain(
    domain: str,
    rows: Sequence[dict],
    *,
    kb_docs: Mapping[str, dict],
    chunks: Sequence[dict],
    staging_ids: Iterable[str],
) -> dict:
    """retrieval_runtime.resolve_gold_cases，再加兩條評估規則：
    - 沒有文件 ID 的題排除（missing_owner）：指定模式要有 ID，不帶 ID 呼叫 retrieve 會走畫作全庫
    - 屬於另一個領域的題（domain_mismatch）記 handled_by＝改由哪一份題庫評估

    回傳：{"included": [...], "excluded": [... + "handled_by"], "counts": {...}}（排除照題庫順序）
    """
    res = rr.resolve_gold_cases(
        rows, domain=domain, kb_docs=kb_docs, chunks=chunks, staging_ids=staging_ids
    )
    other = next(d for d in rr.DOMAINS if d != domain)
    order = {r["id"]: i for i, r in enumerate(rows)}
    included, excluded = [], list(res["excluded"])
    for c in res["included"]:
        if c["owner_id"] is not None:
            included.append(c)
            continue
        excluded.append(
            c
            | {
                "status": "excluded",
                "empty_kind": None,
                "excluded_reason": "missing_owner",
                "excluded_detail": "沒有文件 ID：指定模式要有 ID，"
                "不帶 ID 呼叫 retrieve 會走畫作全庫",
            }
        )

    def handled_by(e: dict) -> str | None:
        return QA_BY_DOMAIN[other] if e["excluded_reason"] == "domain_mismatch" else None

    excluded = [
        e | {"handled_by": handled_by(e)}
        for e in sorted(excluded, key=lambda e: order[e["case_id"]])
    ]
    return {
        "included": included,
        "excluded": excluded,
        "counts": {
            "rows": len(rows),
            "answerable": sum(c["status"] == "answerable" for c in included),
            "expected_empty": sum(c["status"] == "expected_empty" for c in included),
            "excluded": len(excluded),
            "excluded_by_reason": dict(
                sorted(Counter(e["excluded_reason"] for e in excluded).items())
            ),
        },
    }


def gold_report(res: dict) -> dict:
    """寫進 run 的 gold 摘要：counts、納入的題號、每一題排除的原因。"""
    keys = ("case_id", "owner_id", "topics", "qa_type")
    reasons = ("excluded_reason", "excluded_detail", "handled_by")
    return {
        "counts": res["counts"],
        "case_ids": [c["case_id"] for c in res["included"]],
        "excluded": [{k: e[k] for k in (*keys, *reasons)} for e in res["excluded"]],
    }


def group_spec(domain: str, mode: str, call_kind: str, account: str | None) -> dict:
    """一組評估（領域／模式／呼叫方式／帳號）。
    group_id 例如 mfg/production_retrieve/specified/sales。"""
    parts = [domain, mode, call_kind] + ([account] if account else [])
    return {
        "group_id": "/".join(parts),
        "domain": domain,
        "mode": mode,
        "call_kind": call_kind,
        "account": account,
        "production": mode == PRODUCTION,
        "boundary": MODE_BOUNDARY[mode],
    }


def _production_specified(spec: dict) -> bool:
    return spec["mode"] == PRODUCTION and spec["call_kind"] == SPECIFIED


def case_row(case: dict, spec: dict, keywords: Sequence[str]) -> dict:
    """逐題資料的骨架：題目與套用可見性後的 gold，加上 global-fill 標記（還沒執行）。

    global_fill_triggered：只有 production 指定模式有值（問句含觸發詞＝retrieve 會補全庫）。
    retrieve_wide：production 是否搜全庫（全庫模式一定是；指定模式＝global_fill_triggered）；
    診斷不是 retrieve，兩者都是 None。
    """
    hits = keyword_hits(case["question"], keywords)
    production = spec["mode"] == PRODUCTION
    specified = spec["call_kind"] == SPECIFIED
    return {
        **{k: case[k] for k in CASE_FIELDS},
        "global_fill_keywords_hit": hits,
        "global_fill_triggered": bool(hits) if production and specified else None,
        "retrieve_wide": (bool(hits) if specified else True) if production else None,
        "call": None,
        "raw_sources": [],
        "diagnostic": None,
        "returned": [],
        "cross_document_ids": [],
        "non_index_ids": [],
        "score": None,
        "leak": None,
        "error": None,
        "cohorts": [],
    }


def score_row(
    case: dict,
    sources: Sequence[dict],
    *,
    owner_key: str,
    owner_of: Mapping[str, str],
    visible: Iterable[str] | None,
    catalog_id: str,
) -> dict:
    """一題的回傳 → 正規化列表、跨文件段落、非索引來源、指標（score_text_case）
    與外洩（看整份列表）。"""
    returned = normalize_returned(
        sources, owner_key=owner_key, owner_of=owner_of, catalog_id=catalog_id
    )
    ids = [e["chunk_id"] for e in returned]
    empty_kind = case["empty_kind"] if case["status"] == "expected_empty" else None
    owner = case["owner_id"]
    return {
        "returned": returned,
        "cross_document_ids": [
            e["chunk_id"] for e in returned if e["owner_id"] and e["owner_id"] != owner
        ],
        "non_index_ids": [e["chunk_id"] for e in returned if e["kind"] in ("catalog", "non_index")],
        "score": rm.score_text_case(ids, case["gold_chunk_ids"], empty_kind=empty_kind),
        "leak": rm.find_leaks(ids, owner_of, visible),
    }


def cohort_names(row: dict, spec: dict) -> list[str]:
    """這一題算進哪些 cohort；執行錯誤的題不算進任何一個。"""
    if row["error"] is not None:
        return []
    names = ["all"]
    if not row["unreviewed_gold"]:
        names.append("reviewed_gold_only")
    if _production_specified(spec):
        names.append("global_fill_triggered" if row["global_fill_triggered"] else "owner_only")
    return names


def summarize_group(rows: Sequence[dict], spec: dict, visible: Iterable[str] | None) -> dict:
    """一組的摘要（全部可由逐題資料重算）。

    cohorts：all、reviewed_gold_only，production 指定模式另有 owner_only（沒觸發 global-fill）
    與 global_fill_triggered；每個都是 {"case_ids", **retrieval_metrics.aggregate_text_cases}。
    執行錯誤的題只列在 error_case_ids，不算進任何分母。
    """
    ok = [r for r in rows if r["error"] is None]
    names = ["all", "reviewed_gold_only"]
    if _production_specified(spec):
        names += ["owner_only", "global_fill_triggered"]
    cohorts = {}
    for name in names:
        picked = [r for r in ok if name in r["cohorts"]]
        cohorts[name] = {
            "case_ids": [r["case_id"] for r in picked],
            **rm.aggregate_text_cases([r["score"] for r in picked]),
        }
    cross = [r for r in ok if r["cross_document_ids"]]
    non_index = [r for r in ok if r["non_index_ids"]]
    leak_rows = [{"case_id": r["case_id"], "leaked_ids": r["leak"]["leaked_ids"]} for r in ok]
    specified = _production_specified(spec)
    return {
        "n_cases": len(rows),
        "n_scored": len(ok),
        "n_errors": len(rows) - len(ok),
        "error_case_ids": [r["case_id"] for r in rows if r["error"] is not None],
        "cohorts": cohorts,
        "leaks": rm.summarize_leaks(leak_rows),
        "visibility": {
            "visible_owner_ids": None if visible is None else sorted(visible),
            "hidden_target_cases": sum(r["hidden_target_count"] > 0 for r in rows),
            "hidden_target_chunks": sum(r["hidden_target_count"] for r in rows),
            "expected_empty_hidden_cases": sum(r["empty_kind"] == "hidden" for r in rows),
        },
        "cross_document": {
            "cases": rm.rate(len(cross), len(ok)),
            "chunks": sum(len(r["cross_document_ids"]) for r in ok),
            "case_ids": [r["case_id"] for r in cross],
        },
        "non_index_sources": {
            "cases": rm.rate(len(non_index), len(ok)),
            "chunks": sum(len(r["non_index_ids"]) for r in ok),
            "case_ids": [r["case_id"] for r in non_index],
        },
        "global_fill_triggered": (
            rm.rate(sum(bool(r["global_fill_triggered"]) for r in ok), len(ok))
            if specified
            else None
        ),
        # 沒觸發 global-fill 的指定題理應只回自己的段落；有例外就列出來，不默默算進 owner_only
        "owner_only_cross_document_case_ids": (
            [r["case_id"] for r in cross if "owner_only" in r["cohorts"]] if specified else None
        ),
    }


def run_group(
    spec: dict,
    cases: Sequence[dict],
    call: Callable[[dict], dict],
    *,
    owner_of: Mapping[str, str],
    visible: Iterable[str] | None,
    keywords: Sequence[str],
    catalog_id: str,
) -> dict:
    """逐題呼叫 call(case)（回傳 {"sources", "call", "diagnostic"?}）並計分。

    cases 要先經過 retrieval_runtime.apply_visibility。單題出錯時記 error（類型與訊息），
    不當 0 分、不進任何分母；呼叫端看 summary.n_errors 決定 run 失敗。
    """
    owner_key = rr.DOMAINS[spec["domain"]]["owner_key"]
    visible = None if visible is None else sorted(visible)
    rows = []
    for case in cases:
        row = case_row(case, spec, keywords)
        try:
            result = call(case)
            row["call"] = result.get("call")
            row["diagnostic"] = result.get("diagnostic")
            row["raw_sources"] = list(result["sources"])
            row |= score_row(
                case,
                row["raw_sources"],
                owner_key=owner_key,
                owner_of=owner_of,
                visible=visible,
                catalog_id=catalog_id,
            )
        except Exception as e:  # 逐題記錄、整份 run 標失敗，不吞成 0 分
            row["error"] = {"type": type(e).__name__, "message": str(e)}
        row["cohorts"] = cohort_names(row, spec)
        rows.append(row)
    return {
        **spec,
        "visible_owner_ids": visible,
        "rows": rows,
        "summary": summarize_group(rows, spec, visible),
    }


def mock_out_dir_problem(out_dir: Path) -> str | None:
    """mock 結果只能寫到 repo 外的暫存目錄；可以寫回 None。"""
    p = Path(out_dir).resolve()
    if rr.is_official_runs_dir(p):
        return "mock 結果不能寫進 eval/runs（正式產物）"
    if p.is_relative_to(ROOT.resolve()):
        return f"mock 結果只能寫到 repo 外的暫存目錄（例如 $TMPDIR），不能寫 {rr.repo_relative(p)}"
    return None


def provenance_problems(now: dict, receipt: dict | None, *, mock: bool) -> list[str]:
    """目前索引與 real 索引收據的檢查；有任何問題就不評估。mock 不要求 real，也可以沒有收據。"""
    problems = [f"目前索引：{p}" for p in rr.verify_index_receipt(now, require_real=not mock)]
    if receipt is None:
        if not mock:
            problems.append(
                "沒有 real 索引收據：先跑 eval/retrieval_runtime.py build-index"
                "（make eval-retrieval-index）"
            )
        return problems
    problems += [
        f"索引收據：{p}"
        for p in rr.verify_index_receipt(
            receipt, require_real=not mock, require_build=not mock, current=now
        )
    ]
    for key in ("retrieval_params_sha256", "models_yaml_sha256"):
        if (receipt.get("config") or {}).get(key) != now["config"][key]:
            problems.append(f"索引收據：{key} 和目前設定不同")
    if receipt.get("models") != now["models"]:
        problems.append("索引收據：模型 name／revision／dim 和目前 shared/models.yaml 不同")
    return list(dict.fromkeys(problems))


def corpus_snapshot(now: dict) -> dict:
    """本次實際載入的語料：各領域文件 ID、段落數（含 #meta／#color，不是 descriptions 數）。"""
    keys = ("owner_key", "item_ids", "item_count", "chunk_count", "chunks_by_owner")
    out = {d: {k: c[k] for k in keys} for d, c in now["collections"].items()}
    return out | {"manifest_counts": now["chunk_counts"]}


# ---------------------------------------------------------------- 執行（要先 bootstrap）


def account_visibility(account, parts: Sequence[dict]) -> dict:
    """帳號的圖紙可見集合：MetaFilter（retrieve 用的 scope）和 identity.visible_part_ids
    （文件層授權，含能不能讀 mfg 領域）交叉核對，不硬編。"""
    from app.agent import guard
    from app.services import chat_service, identity

    scope = guard.MetaFilter.of(account, "mfg")
    by_scope = chat_service.visible_parts(scope) or set()
    by_identity = identity.visible_part_ids(account, parts)
    return {
        "account_id": account.id,
        "label": account.label,
        "role": account.role,
        "can_read_mfg": account.can_read("mfg"),
        "scope": scope.public(),
        "scope_visible_parts": sorted(by_scope),
        "visible_part_ids": sorted(by_identity),
        "hidden_part_ids": sorted({p["id"] for p in parts} - by_identity),
        "consistent": by_scope == by_identity,
    }


def production_call(domain: str, call_kind: str, scope, account: str | None) -> Callable:
    """直接呼叫原本的 chat_service.retrieve（每次都從模組取，不保留舊參照）。"""
    from app.services import chat_service

    def call(case: dict) -> dict:
        q, owner = case["question"], case["owner_id"]
        if domain == "mfg":
            args = {"artwork_id": None, "part_id": owner, "scope": f"MetaFilter.of({account}, mfg)"}
            sources = chat_service.retrieve(q, None, owner, scope)
        elif call_kind == SPECIFIED:
            args = {"artwork_id": owner, "part_id": None, "scope": None}
            sources = chat_service.retrieve(q, owner)
        else:
            args = {"artwork_id": None, "part_id": None, "scope": None}
            sources = chat_service.retrieve(q, None)
        return {
            "sources": sources,
            "call": {"function": "app.services.chat_service.retrieve", **args},
        }

    return call


def diagnostic_call(store, owners: Sequence[str], params: Mapping) -> Callable:
    """圖紙全庫診斷：真的 store.mfg、真的 embed_text、search_chunks 的同領域全庫候選，
    owners＝帳號 scope 看得到的圖紙，再套 postfilter。"""
    from app.rag import embedders

    k = int(params["top_k_chunks"])
    lo, ratio = params["min_chunk_score"], params["relative_chunk_ratio"]
    info = {
        "function": DIAGNOSTIC,
        "collection": "store.mfg",
        "embed": "app.rag.embedders.embed_text",
        "owners": sorted(owners),
        "k": k,
        "min_chunk_score": lo,
        "relative_chunk_ratio": ratio,
        "owner_first": False,
        "keep_one_fallback": False,
    }

    def call(case: dict) -> dict:
        qvec = embedders.embed_text([case["question"]])[0]
        hits = store.mfg.search_chunks(qvec, k, owners=set(owners))
        pf = postfilter([(h.item, h.score) for h in hits], lo, ratio)

        def brief(item: dict, score: float) -> dict:
            return {
                "chunk_id": item["chunk_id"],
                "part_id": item["part_id"],
                "topic": item["topic"],
                "score": round(score, 6),
            }

        return {
            "sources": [brief(i, s) for i, s in pf["kept"]],
            "call": info,
            "diagnostic": {
                "candidates": [brief(h.item, h.score) for h in hits],
                "best": pf["best"],
                "floor": pf["floor"],
            },
        }

    return call


def evaluate(
    store,
    *,
    domains: Sequence[str],
    qa: Mapping[str, Sequence[dict]],
    accounts: Sequence[str],
    params: Mapping,
    catalog_id: str,
) -> dict:
    """推導 gold、照領域／模式／帳號逐組執行。回傳 {"gold", "accounts", "groups", "problems"}。

    畫作：production 指定、production 全庫（畫作公開，不分帳號）。
    圖紙：每個帳號 production 指定、全庫診斷各一組。
    """
    from app.agent import guard
    from app.services import identity

    keywords = list(params["global_fill_keywords"])
    colls = {"art": store.art, "mfg": store.mfg}
    out = {"gold": {}, "accounts": {}, "groups": [], "problems": []}
    resolved, owner_of = {}, {}
    for d in domains:
        resolved[d] = resolve_domain(
            d,
            qa[d],
            kb_docs=rr.load_kb_docs(d),
            chunks=colls[d].chunks,
            staging_ids=rr.load_staging_ids(d),
        )
        out["gold"][d] = gold_report(resolved[d])
        owner_of[d] = rr.chunk_owner_map(colls[d].chunks, rr.DOMAINS[d]["owner_key"])

    def add(spec: dict, cases: list[dict], call: Callable, visible) -> None:
        g = run_group(
            spec,
            cases,
            call,
            owner_of=owner_of[spec["domain"]],
            visible=visible,
            keywords=keywords,
            catalog_id=catalog_id,
        )
        out["groups"].append(g)
        print(f"  {g['group_id']}：{len(g['rows'])} 題，{g['summary']['n_errors']} 題錯誤")

    if "art" in domains:
        cases = [rr.apply_visibility(c, None) for c in resolved["art"]["included"]]
        for kind in (SPECIFIED, GLOBAL):
            add(
                group_spec("art", PRODUCTION, kind, None),
                cases,
                production_call("art", kind, None, None),
                None,
            )
    if "mfg" in domains:
        for acc_id in accounts:
            account = identity.get_account(acc_id)
            vis = account_visibility(account, store.parts)
            out["accounts"][acc_id] = vis
            if not vis["consistent"]:
                out["problems"].append(
                    f"{acc_id}：MetaFilter 看得到 {vis['scope_visible_parts']}，"
                    f"identity.visible_part_ids 是 {vis['visible_part_ids']}，兩者不一致"
                )
            visible = vis["visible_part_ids"]
            cases = [rr.apply_visibility(c, visible) for c in resolved["mfg"]["included"]]
            scope = guard.MetaFilter.of(account, "mfg")
            add(
                group_spec("mfg", PRODUCTION, SPECIFIED, acc_id),
                cases,
                production_call("mfg", SPECIFIED, scope, acc_id),
                visible,
            )
            add(
                group_spec("mfg", DIAGNOSTIC, GLOBAL, acc_id),
                cases,
                diagnostic_call(store, vis["scope_visible_parts"], params),
                visible,
            )
    for g in out["groups"]:
        if g["summary"]["n_errors"]:
            ids = ", ".join(g["summary"]["error_case_ids"])
            out["problems"].append(
                f"{g['group_id']}：{g['summary']['n_errors']} 題執行錯誤（{ids}），不是完整結果"
            )
    return out


def load_receipt(arg: str | None, *, mock: bool) -> tuple[Path | None, dict | None, list[str]]:
    """--receipt：路徑或 latest（eval/runs 最新的 *-retrieval-index.json）；real 預設 latest，
    mock 沒給就不用收據。回傳 (路徑, 內容, 讀取問題)。"""
    if arg is None and mock:
        return None, None, []
    if arg in (None, "latest"):
        found = sorted(rr.RUNS_DIR.glob("*-retrieval-index.json"))
        if not found:
            return None, None, []
        path = found[-1]
    else:
        path = Path(arg)
    try:
        return path, json.loads(path.read_text(encoding="utf-8")), []
    except (OSError, ValueError) as e:
        return path, None, [f"讀不到索引收據 {rr.repo_relative(path)}：{e}"]


def new_report(run_id: str, args, argv: Sequence[str], isolation: dict, settings) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "retrieval-layer-metrics",
        "run_id": run_id,
        "created_at": rr.utc_now(),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
        "embed_mode": settings.embed_mode,
        "real": False,
        "official": False,
        "allow_mock": bool(args.allow_mock),
        "argv": list(argv),
        "problems": [],
        "modes": MODE_BOUNDARY,
        "boundaries": BOUNDARIES,
        "domains": parse_list(args.domains),
        "k": None,
        "metric_ks": list(rm.TEXT_KS),
        "ndcg_k": rm.NDCG_K,
        "provenance": {
            "code": rr.git_provenance(),
            "isolation": isolation,
            "settings": rr.settings_snapshot(settings),
            "python": sys.version,
            "inputs": rr.file_hashes([Path(args.qa_art), Path(args.qa_mfg), Path(args.gold)]),
            "retrieval_params": None,
            "index_now": None,
            "index_receipt": None,
        },
        "corpus": {},
        "gold": {},
        "accounts": {},
        "groups": [],
        "summary": {},
    }


def execute(report: dict, args, isolation: dict) -> None:
    """載索引、核對 provenance，通過才評估；結果與問題都寫進 report。"""
    from app.agent import catalog
    from app.core.config import get_models_config
    from app.repositories.index_store import IndexMismatch, get_store

    mock = report["embed_mode"] != "real"
    domains = report["domains"]
    unknown = [d for d in domains if d not in rr.DOMAINS]
    if unknown or not domains:
        report["problems"].append(f"--domains 只能是 art、mfg：{args.domains}")
        return
    paths = {"art": Path(args.qa_art), "mfg": Path(args.qa_mfg)}
    params = rr.retrieval_params(get_models_config())
    report["provenance"]["retrieval_params"] = params
    report["k"] = params["top_k_chunks"]
    store = get_store()
    try:
        store.load()
    except IndexMismatch as e:
        report["problems"].append(f"索引不能用：{e}")
        return
    now = rr.collect_index_receipt(store, isolation=isolation, qa_paths=list(paths.values()))
    report["provenance"]["index_now"] = now
    report["corpus"] = corpus_snapshot(now)
    path, receipt, problems = load_receipt(args.receipt, mock=mock)
    problems += provenance_problems(now, receipt, mock=mock)
    report["provenance"]["index_receipt"] = {
        "path": None if path is None else rr.repo_relative(path),
        "sha256": None if path is None else rr.sha256_file(path),
        "run_id": (receipt or {}).get("run_id"),
        "created_at": (receipt or {}).get("created_at"),
        "real": (receipt or {}).get("real"),
        "index_build": (receipt or {}).get("index_build"),
        "problems": problems,
    }
    report["real"] = not mock and bool(now["real"]) and bool((receipt or {}).get("real"))
    if problems:  # 索引或收據有問題：不評估，不留下任何看起來像成績的數字
        report["problems"] += problems
        return
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    accounts = parse_list(args.accounts) if args.accounts else list(gold["accounts"])
    ev = evaluate(
        store,
        domains=domains,
        qa={d: rr.load_jsonl(paths[d]) for d in domains},
        accounts=accounts,
        params=params,
        catalog_id=catalog.CHUNK_ID,
    )
    for d in domains:
        source = {"qa_path": rr.repo_relative(paths[d]), "qa_sha256": rr.sha256_file(paths[d])}
        ev["gold"][d] = source | ev["gold"][d]
    report["gold"], report["accounts"], report["groups"] = ev["gold"], ev["accounts"], ev["groups"]
    report["summary"] = {g["group_id"]: g["summary"] for g in ev["groups"]}
    report["problems"] += ev["problems"]


def _fmt(stat: dict) -> str:
    return "—" if stat["mean"] is None else f"{stat['mean']:.3f}"


def finish(report: dict, out: Path) -> int:
    """寫 JSON（不覆寫已存在的檔案），印摘要；回傳結束碼。"""
    ok = not report["problems"]
    report["finished_at"] = rr.utc_now()
    report["status"] = "ok" if ok else "failed"
    report["exit_code"] = 0 if ok else 1
    report["official"] = bool(report["real"] and ok)
    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out.open("x", encoding="utf-8") as f:
            f.write(text)
    except FileExistsError:
        print(f"{rr.repo_relative(out)} 已存在：不覆寫歷史 run", file=sys.stderr)
        return 2
    for g in report["groups"]:
        a = g["summary"]["cohorts"]["all"]
        m, fn = a["answerable"]["metrics"], a["expected_empty"]["false_nonempty"]
        print(
            f"  {g['group_id']}：可答 {a['answerable']['n']} 題 R@1 {_fmt(m['recall@1'])} "
            f"R@5 {_fmt(m['recall@5'])} MRR {_fmt(m['mrr'])} P {_fmt(m['precision'])}；"
            f"空答案誤回傳 {fn['numerator']}/{fn['denominator']}；"
            f"外洩 {g['summary']['leaks']['leaked_chunks']} 段"
        )
    for p in report["problems"]:
        print(f"  ✗ {p}", file=sys.stderr)
    print(
        f"結果：{rr.repo_relative(out)}（status={report['status']}，real={report['real']}，"
        f"official={report['official']}）"
    )
    return report["exit_code"]


def run(args, isolation: dict, argv: Sequence[str]) -> int:
    from app.core.config import get_settings

    settings = get_settings()
    out_dir = Path(args.out_dir)
    if settings.embed_mode != "real":
        if not args.allow_mock:
            print(
                f"EMBED_MODE={settings.embed_mode}：正式評估只接受 real；"
                "測試要加 --allow-mock 並輸出到暫存目錄",
                file=sys.stderr,
            )
            return 2
        problem = mock_out_dir_problem(out_dir)
        if problem:
            print(problem, file=sys.stderr)
            return 2
    run_id = args.run_id or rr.new_run_id()
    if not _RUN_ID.fullmatch(run_id):
        print(f"--run-id 只能用英數、底線、點與減號：{run_id!r}", file=sys.stderr)
        return 2
    out = out_dir / f"{run_id}-retrieval-layer-metrics.json"
    if out.exists():
        print(f"{rr.repo_relative(out)} 已存在：不覆寫歷史 run", file=sys.stderr)
        return 2
    report = new_report(run_id, args, argv, isolation, settings)
    print(f"檢索層評估 {run_id}：EMBED_MODE={settings.embed_mode}，domains={report['domains']}")
    try:
        execute(report, args, isolation)
    except Exception as e:  # 非預期的錯誤也寫 JSON，標失敗
        report["problems"].append(f"執行中斷：{type(e).__name__}: {e}")
        report["traceback"] = traceback.format_exc()
    return finish(report, out)


def main(argv: list[str] | None = None, *, strict: bool = True) -> int:
    """strict=False 只給測試程序（設定已先載入）；命令列一律 strict。"""
    ap = argparse.ArgumentParser(description="文字段落檢索評估（不讀 .env、不經 LLM）")
    ap.add_argument("--domains", default="art,mfg", help="逗號分隔：art、mfg")
    ap.add_argument("--accounts", default=None, help="逗號分隔，預設照 gold.json 的 accounts")
    ap.add_argument("--receipt", default=None, help="索引收據路徑或 latest（real 預設 latest）")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--out-dir", default=str(rr.RUNS_DIR))
    ap.add_argument("--qa-art", default=str(ART_QA))
    ap.add_argument("--qa-mfg", default=str(MFG_QA))
    ap.add_argument("--gold", default=str(DRAWING_GOLD), help="圖紙 gold.json（取 accounts）")
    ap.add_argument("--allow-mock", action="store_true", help="測試用：mock 只能輸出到 repo 外")
    args = ap.parse_args(argv)
    try:
        isolation = rr.bootstrap(strict=strict)
    except rr.RuntimeIsolationError as e:
        print(f"設定隔離失敗：{e}", file=sys.stderr)
        return 2
    return run(args, isolation, sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
