"""檢索層評估的純函式（TASK-20261027-001）：不呼叫模型、不讀寫檔案、不 import app。

文字段落（retrieve 最終回傳的 chunk_id 列表）與圖紙照片（identify_drawing 的 candidates）共用。
輸入一律是 ID 列表；同一個 ID 只算第一次出現（stable first occurrence），原始完整回傳由
呼叫端另外保存。

文字段落，正答案 G 非空（answerable）：
- Recall@K（K=1、2、5）＝|去重後前 K 個 ∩ G| / |G|；回傳不足 K 個時分母仍是 |G|
- MRR＝1 / 第一個正確段落的名次（1-based，看整份去重回傳）；未命中 0
- Precision＝|R ∩ G| / |R|，R 是最終實際回傳的去重列表（不是固定 5）；G 非空但 R 空記 0
- nDCG@5 只在 |G|>1（多正解）時計：二元相關、DCG＝Σ rel_r / log2(r+1)、
  IDCG 取 min(5, |G|) 個全中；單一正解記 None
正答案為空（expected_empty：unknown／no_answer／hidden）：上面全部記 None，不併入平均；
另計 false_nonempty（回傳非空）。G 空時一定要說明是哪一種空，缺少標記不能當成答案為空。

彙整按題 macro-average，每個數字附 numerator（逐題值加總）與 denominator（有值的題數）；
分母 0 時 mean／rate 記 None，不輸出 0%。

照片候選：每張照片至多一個看得到的正解，Recall@K（K=1、2、3、5）＝正解是否在前 K 名；
route 不是 mfg（route_miss）或候選空都留在分母、記 0；沒有看得到的正解（未收錄／看不到）
不進排名分母，改由 photo_negative_kind 分成 unknown／hidden 另計。
"""

import math
from collections.abc import Iterable, Mapping, Sequence

TEXT_KS = (1, 2, 5)
PHOTO_KS = (1, 2, 3, 5)
NDCG_K = 5
EMPTY_KINDS = ("unknown", "no_answer", "hidden")


def dedupe_ids(ids: Iterable[str]) -> list[str]:
    """去重並保留第一次出現的順序。ID 必須是非空字串（None／空字串代表上游漏資料，直接報錯）。"""
    out, seen = [], set()
    for x in ids:
        if not isinstance(x, str) or not x:
            raise ValueError(f"ID 必須是非空字串：{x!r}")
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _check_k(k: int) -> None:
    if not isinstance(k, int) or k < 1:
        raise ValueError(f"K 必須是正整數：{k!r}")


def recall_at_k(ranked: Sequence[str], gold: Iterable[str], k: int) -> float | None:
    """|去重後前 k 個 ∩ G| / |G|；G 空回 None。"""
    _check_k(k)
    g = set(dedupe_ids(gold))
    if not g:
        return None
    return len(set(dedupe_ids(ranked)[:k]) & g) / len(g)


def first_relevant_rank(ranked: Sequence[str], gold: Iterable[str]) -> int | None:
    """第一個正確 ID 的 1-based 名次（去重後）；沒有命中回 None。"""
    g = set(dedupe_ids(gold))
    for i, x in enumerate(dedupe_ids(ranked), 1):
        if x in g:
            return i
    return None


def reciprocal_rank(ranked: Sequence[str], gold: Iterable[str]) -> float | None:
    """1 / 第一個正確 ID 的名次；未命中 0.0；G 空回 None。"""
    if not set(dedupe_ids(gold)):
        return None
    r = first_relevant_rank(ranked, gold)
    return 0.0 if r is None else 1 / r


def precision_of_returned(returned: Sequence[str], gold: Iterable[str]) -> float | None:
    """|R ∩ G| / |R|，R＝實際回傳的去重列表；G 非空但 R 空回 0.0；G 空回 None。"""
    g = set(dedupe_ids(gold))
    if not g:
        return None
    r = dedupe_ids(returned)
    return len(set(r) & g) / len(r) if r else 0.0


def ndcg_at_k(ranked: Sequence[str], gold: Iterable[str], k: int = NDCG_K) -> float | None:
    """二元相關 nDCG@k：DCG＝Σ_{r≤k} rel_r / log2(r+1)，IDCG 取 min(k, |G|) 個全中；G 空回 None。"""
    _check_k(k)
    g = set(dedupe_ids(gold))
    if not g:
        return None
    dcg = sum(1 / math.log2(r + 1) for r, x in enumerate(dedupe_ids(ranked)[:k], 1) if x in g)
    ideal = sum(1 / math.log2(r + 1) for r in range(1, min(k, len(g)) + 1))
    return dcg / ideal


def text_metric_names(ks: Sequence[int] = TEXT_KS, ndcg_k: int = NDCG_K) -> list[str]:
    return [f"recall@{k}" for k in ks] + ["mrr", "precision", f"ndcg@{ndcg_k}"]


def score_text_case(
    returned: Sequence[str],
    gold: Iterable[str],
    *,
    empty_kind: str | None = None,
    ks: Sequence[int] = TEXT_KS,
    ndcg_k: int = NDCG_K,
) -> dict:
    """一題文字段落的指標。

    returned：retrieve 最終回傳的 chunk_id，照原順序（可含重複、catalog 等非索引來源，
    一律照實算進名次與 Precision 分母）。gold：這個身分看得到的正確 chunk_id。
    empty_kind：gold 為空時必填，說明是哪一種空（unknown／no_answer／hidden）；gold 非空時不可填。

    回傳：
    {
      "returned_ids": [str],          # 去重後、保留首次出現順序
      "returned_count": int,          # |R|（去重後）
      "raw_returned_count": int,      # 原始長度
      "duplicate_count": int,
      "gold_ids": [str],              # 去重後
      "gold_count": int,
      "expected_empty": bool,
      "empty_kind": str | None,
      "multi_relevant": bool,         # |G| > 1，nDCG cohort
      "relevant_returned": int | None,     # |R ∩ G|；expected_empty 為 None
      "first_relevant_rank": int | None,   # 1-based；未命中或 expected_empty 為 None
      "nonempty": bool,
      "false_nonempty": bool | None,       # 只有 expected_empty 有值：回傳非空
      "metrics": {"recall@1", "recall@2", "recall@5", "mrr", "precision", "ndcg@5"}:
                 float | None（expected_empty 全為 None；ndcg 只在 multi_relevant 有值）
    }
    """
    raw = list(returned)
    r = dedupe_ids(raw)
    g = dedupe_ids(gold)
    if g and empty_kind is not None:
        raise ValueError("正答案非空的題目不能標 empty_kind")
    if not g and empty_kind not in EMPTY_KINDS:
        raise ValueError(f"正答案為空時 empty_kind 必須是 {EMPTY_KINDS} 之一：{empty_kind!r}")
    names = text_metric_names(ks, ndcg_k)
    out = {
        "returned_ids": r,
        "returned_count": len(r),
        "raw_returned_count": len(raw),
        "duplicate_count": len(raw) - len(r),
        "gold_ids": g,
        "gold_count": len(g),
        "expected_empty": not g,
        "empty_kind": empty_kind,
        "multi_relevant": len(g) > 1,
        "nonempty": bool(r),
    }
    if not g:
        return out | {
            "relevant_returned": None,
            "first_relevant_rank": None,
            "false_nonempty": bool(r),
            "metrics": dict.fromkeys(names),
        }
    metrics = {f"recall@{k}": recall_at_k(r, g, k) for k in ks}
    metrics["mrr"] = reciprocal_rank(r, g)
    metrics["precision"] = precision_of_returned(r, g)
    metrics[f"ndcg@{ndcg_k}"] = ndcg_at_k(r, g, ndcg_k) if len(g) > 1 else None
    return out | {
        "relevant_returned": len(set(r) & set(g)),
        "first_relevant_rank": first_relevant_rank(r, g),
        "false_nonempty": None,
        "metrics": metrics,
    }


def macro_average(values: Iterable[float | None]) -> dict:
    """按題平均；None 不計入。回 {"mean": float|None, "numerator": float, "denominator": int}。"""
    vals = [v for v in values if v is not None]
    total = float(sum(vals))
    return {
        "mean": total / len(vals) if vals else None,
        "numerator": total,
        "denominator": len(vals),
    }


def rate(numerator: int, denominator: int) -> dict:
    """{"rate": float|None, "numerator": int, "denominator": int}；分母 0 時 rate 為 None。"""
    return {
        "rate": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def aggregate_text_cases(
    cases: Sequence[dict], *, ks: Sequence[int] = TEXT_KS, ndcg_k: int = NDCG_K
) -> dict:
    """彙整 score_text_case 的結果（同一個領域／模式／身分／cohort 一組；呼叫端先分好組，
    不同測法不要混在一起平均）。

    回傳：
    {
      "n_cases": int,
      "answerable": {
        "n": int, "n_multi_relevant": int,
        "metrics": {name: {"mean", "numerator", "denominator"}}   # ndcg 的分母只有多正解題
      },
      "expected_empty": {
        "n": int,
        "false_nonempty": {"rate", "numerator", "denominator"},
        "by_kind": {"unknown"|"no_answer"|"hidden": {"rate", "numerator", "denominator"}}
      }
    }
    """
    answerable = [c for c in cases if not c["expected_empty"]]
    empty = [c for c in cases if c["expected_empty"]]
    return {
        "n_cases": len(cases),
        "answerable": {
            "n": len(answerable),
            "n_multi_relevant": sum(c["multi_relevant"] for c in answerable),
            "metrics": {
                name: macro_average(c["metrics"][name] for c in answerable)
                for name in text_metric_names(ks, ndcg_k)
            },
        },
        "expected_empty": {
            "n": len(empty),
            "false_nonempty": rate(sum(c["false_nonempty"] for c in empty), len(empty)),
            "by_kind": {
                kind: rate(
                    sum(c["false_nonempty"] for c in empty if c["empty_kind"] == kind),
                    sum(c["empty_kind"] == kind for c in empty),
                )
                for kind in EMPTY_KINDS
            },
        },
    }


def find_leaks(
    returned: Sequence[str], owner_of: Mapping[str, str], visible: Iterable[str] | None
) -> dict:
    """整份回傳列表（不是只看前 K 個）裡，屬於這個身分看不到的文件的段落。

    owner_of：chunk_id → 文件 ID（索引的 artwork_id／part_id）。visible：看得到的文件；None＝不限。
    不在 owner_of 的 ID（例如 catalog 這種非索引來源）不屬於任何文件，列在 unowned_ids，不算外洩。

    回傳：{"leaked_ids": [str], "leaked_owner_ids": [str], "leaked_count": int, "leak": bool,
          "unowned_ids": [str]}
    """
    ids = dedupe_ids(returned)
    unowned = [x for x in ids if x not in owner_of]
    if visible is None:
        leaked = []
    else:
        allowed = set(visible)
        leaked = [x for x in ids if x in owner_of and owner_of[x] not in allowed]
    return {
        "leaked_ids": leaked,
        "leaked_owner_ids": sorted({owner_of[x] for x in leaked}),
        "leaked_count": len(leaked),
        "leak": bool(leaked),
        "unowned_ids": unowned,
    }


def summarize_leaks(rows: Sequence[dict]) -> dict:
    """rows：[{"case_id", "leaked_ids"}]（同一組身分／模式的所有題，包含可答與空答案題）。

    回傳：{"leaked_chunks": int, "leaked_cases": {"rate", "numerator", "denominator"},
          "case_ids": [str]}
    """
    hit = [r for r in rows if r["leaked_ids"]]
    return {
        "leaked_chunks": sum(len(r["leaked_ids"]) for r in rows),
        "leaked_cases": rate(len(hit), len(rows)),
        "case_ids": [r["case_id"] for r in hit],
    }


def photo_metric_names(ks: Sequence[int] = PHOTO_KS) -> list[str]:
    return [f"recall@{k}" for k in ks] + ["mrr"]


def score_photo_case(
    candidates: Sequence[str],
    expect: str | None,
    *,
    route_miss: bool = False,
    ks: Sequence[int] = PHOTO_KS,
) -> dict:
    """一張照片的候選排名。candidates：identify_drawing 回傳的圖紙 ID（幾何驗證後的混合排序，
    含沒通過驗證的項目）；expect：這個身分看得到的正解，沒有（未收錄／看不到）為 None。
    route_miss＝領域路由沒判成 mfg，這時候選必須是空的，記 0 分並留在分母。

    回傳：
    {
      "candidate_ids": [str], "expect": str | None,
      "in_ranking": bool,          # expect 非 None 才進排名分母
      "route_miss": bool, "empty_candidates": bool,
      "rank": int | None,          # 1-based
      "metrics": {"recall@1", "recall@2", "recall@3", "recall@5", "mrr"}: float | None
    }
    """
    cand = dedupe_ids(candidates)
    if route_miss and cand:
        raise ValueError("route miss 時不會有候選：先確認呼叫端的候選列表")
    names = photo_metric_names(ks)
    base = {
        "candidate_ids": cand,
        "expect": expect,
        "in_ranking": expect is not None,
        "route_miss": route_miss,
        "empty_candidates": not cand,
    }
    if expect is None:
        return base | {"rank": None, "metrics": dict.fromkeys(names)}
    metrics = {f"recall@{k}": recall_at_k(cand, [expect], k) for k in ks}
    metrics["mrr"] = reciprocal_rank(cand, [expect])
    return base | {"rank": first_relevant_rank(cand, [expect]), "metrics": metrics}


def aggregate_photo_cases(cases: Sequence[dict], *, ks: Sequence[int] = PHOTO_KS) -> dict:
    """彙整 score_photo_case（同一個身分一組）。

    回傳：
    {
      "n_cases": int, "n_ranked": int,           # n_ranked＝排名分母
      "n_route_miss": int, "n_empty_candidates": int,   # 都只數排名分母內的照片
      "metrics": {name: {"mean", "numerator", "denominator"}}
    }
    """
    ranked = [c for c in cases if c["in_ranking"]]
    return {
        "n_cases": len(cases),
        "n_ranked": len(ranked),
        "n_route_miss": sum(c["route_miss"] for c in ranked),
        "n_empty_candidates": sum(c["empty_candidates"] for c in ranked),
        "metrics": {
            name: macro_average(c["metrics"][name] for c in ranked)
            for name in photo_metric_names(ks)
        },
    }


def photo_negative_kind(expect_visible: str | None, expect_in_kb: str | None) -> str | None:
    """沒有看得到的正解時分兩種：知識庫有、這個身分看不到（hidden）；知識庫根本沒有（unknown）。
    有看得到的正解回 None。"""
    if expect_visible is not None:
        return None
    return "unknown" if expect_in_kb is None else "hidden"
