"""檢索層衍生指標的純函式（TASK-20261029-002）：不呼叫模型、不讀寫檔案、不 import app。

只在「已保存的有限回傳」上重算，原指標（retrieval_metrics）與原 G、原 score 一律不動；
去重、集合 Recall、MRR、Precision、macro 平均都重用 retrieval_metrics 的函式。
三種衍生口徑各自獨立計算、不互相疊加：

1. skip_meta：先對 retrieved 做 stable 去重，再移除 chunk_id 以精確 `#meta` 結尾的段落並重新排名
   （其他來源如 #color、catalog 保留），原 G 不變，Recall@1／2／5 與 MRR。這是既有回傳的衍生重排，
   不代表真實重新檢索，也不補查新段落。
2. meta_answerable：草稿 1 的 keywords 是 AND 組（外層）、每組 OR 候選（內層）；每組至少一個候選
   完整逐字（區分大小寫、不正規化、不做語意推論）出現在同 owner `#meta` 原文才合格。合格才令
   G' ＝ G ∪ {owner#meta}（只加一次，不改排名），另算 Recall@1／2／5、MRR、Precision。
   只有原 G 非空、可見、且有草稿 1 答案的圖紙題適用；空 G、看不到、畫作都不適用（指標記 None）。
   空或非法 keywords 不得因 all([]) 自動合格；缺原文是錯誤（MetaTextMissing），不當未命中。
3. precision_ceiling：逐題 min(|G|, |R|) / |R|（原可見 G、原去重 R；不先 skip_meta、不用 G'）；
   G 非空 R 空記 0.0（留分母）、G 空記 None（排除）。

彙整按題 macro-average，附 mean／numerator／denominator；集合 Recall 不是 hit rate。
"""

import hashlib
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retrieval_metrics as rm  # noqa: E402

RULE_VERSION = "retrieval-derived-rules/1"
META_SUFFIX = "#meta"
TEXT_KS = rm.TEXT_KS
SKIP_META_METRICS = ("recall@1", "recall@2", "recall@5", "mrr")
META_ANSWERABLE_METRICS = (*SKIP_META_METRICS, "precision")


class MetaTextMissing(ValueError):
    """必要的 #meta 原文缺漏（None、空字串或非字串）；不能當成「關鍵字沒命中」。"""


def is_meta_id(chunk_id: object) -> bool:
    """精確 `#meta` 尾綴（#metadata、#meta2、#META 都不是）。"""
    return isinstance(chunk_id, str) and chunk_id.endswith(META_SUFFIX)


def meta_chunk_id(owner_id: str) -> str:
    return f"{owner_id}{META_SUFFIX}"


def _metric_names(ks: Sequence[int], *, precision: bool) -> list[str]:
    return [f"recall@{k}" for k in ks] + ["mrr"] + (["precision"] if precision else [])


def _ranked_metrics(ranked: Sequence[str], gold: Sequence[str], ks: Sequence[int]) -> dict:
    """已去重的排名對 gold 的 Recall@K 與 MRR（沿用 retrieval_metrics，G 空回 None）。"""
    out = {f"recall@{k}": rm.recall_at_k(ranked, gold, k) for k in ks}
    out["mrr"] = rm.reciprocal_rank(ranked, gold)
    return out


# ---------------------------------------------------------------- skip_meta


def skip_meta_ranking(returned: Iterable[str]) -> dict:
    """先 stable 去重，再移除所有精確 #meta 段落，其餘順序不變（重新排名＝名次連續）。

    回傳 {"deduped_ids", "ranked_ids", "removed_meta_ids"}。
    """
    deduped = rm.dedupe_ids(returned)
    return {
        "deduped_ids": deduped,
        "ranked_ids": [x for x in deduped if not is_meta_id(x)],
        "removed_meta_ids": [x for x in deduped if is_meta_id(x)],
    }


def score_skip_meta(
    returned: Iterable[str], gold: Iterable[str], ks: Sequence[int] = TEXT_KS
) -> dict:
    """derived.skip_meta：原 G 不變；G 空 → applicable False、指標 None；G 非空但排名空 → 全 0。"""
    g = rm.dedupe_ids(gold)
    ranking = skip_meta_ranking(returned)
    ranked = ranking["ranked_ids"]
    names = _metric_names(ks, precision=False)
    if not g:
        return {
            "applicable": False,
            "gold_ids": g,
            "ranked_ids": ranked,
            "removed_meta_ids": ranking["removed_meta_ids"],
            "first_relevant_rank": None,
            "metrics": dict.fromkeys(names),
        }
    return {
        "applicable": True,
        "gold_ids": g,
        "ranked_ids": ranked,
        "removed_meta_ids": ranking["removed_meta_ids"],
        "first_relevant_rank": rm.first_relevant_rank(ranked, g),
        "metrics": _ranked_metrics(ranked, g, ks),
    }


# ---------------------------------------------------------------- precision_ceiling


def precision_ceiling(returned: Iterable[str], gold: Iterable[str]) -> float | None:
    """min(|G|, |R|) / |R|：G、R 都用既有去重定義；G 空回 None；G 非空 R 空回 0.0。"""
    g = rm.dedupe_ids(gold)
    if not g:
        return None
    r = rm.dedupe_ids(returned)
    return min(len(g), len(r)) / len(r) if r else 0.0


def ceiling_detail(returned: Iterable[str], gold: Iterable[str]) -> dict:
    g, r = rm.dedupe_ids(gold), rm.dedupe_ids(returned)
    return {
        "value": precision_ceiling(r, g),
        "gold_count": len(g),
        "returned_count": len(r),
    }


# ---------------------------------------------------------------- meta_answerable


def validate_keywords(keywords: object) -> str | None:
    """草稿 1 的 keywords：非空的 AND 組 list，每組是非空的 OR 候選 list，候選是非空字串。
    合法回 None，否則回原因（字串）。"""
    if not isinstance(keywords, list | tuple) or not keywords:
        return "keywords 必須是非空的 AND 組列表"
    for i, group in enumerate(keywords):
        if not isinstance(group, list | tuple) or not group:
            return f"第 {i} 組必須是非空的 OR 候選列表"
        for cand in group:
            if not isinstance(cand, str) or not cand:
                return f"第 {i} 組的候選必須是非空字串：{cand!r}"
    return None


def match_meta_keywords(keywords: object, meta_text: object) -> dict:
    """每個 AND 組至少一個 OR 候選完整逐字（區分大小寫）出現在 meta 原文才合格。

    回傳：
    {
      "qualified": bool,
      "reason": "ok" | "missing_groups" | "invalid_keywords",
      "detail": str | None,
      "groups": [{"group_index", "candidates": [{"text", "found", "position"}],
                  "satisfied", "hit", "hit_position"}],
      "missing_group_indexes": [int],
    }
    keywords 不合法時不看原文（reason＝invalid_keywords、不合格，不因 all([]) 通過）；
    keywords 合法但原文缺漏（None、空字串、非字串）丟 MetaTextMissing。
    """
    problem = validate_keywords(keywords)
    if problem is not None:
        return {
            "qualified": False,
            "reason": "invalid_keywords",
            "detail": problem,
            "groups": [],
            "missing_group_indexes": [],
        }
    if not isinstance(meta_text, str) or not meta_text:
        raise MetaTextMissing(f"meta 原文缺漏（必須是非空字串）：{type(meta_text).__name__}")
    groups, missing = [], []
    for i, cands in enumerate(keywords):
        rows = []
        for cand in cands:
            pos = meta_text.find(cand)
            rows.append({"text": cand, "found": pos >= 0, "position": pos if pos >= 0 else None})
        first = next((c for c in rows if c["found"]), None)
        groups.append(
            {
                "group_index": i,
                "candidates": rows,
                "satisfied": first is not None,
                "hit": first["text"] if first else None,
                "hit_position": first["position"] if first else None,
            }
        )
        if first is None:
            missing.append(i)
    return {
        "qualified": not missing,
        "reason": "ok" if not missing else "missing_groups",
        "detail": None if not missing else f"缺漏 AND 組 {missing}",
        "groups": groups,
        "missing_group_indexes": missing,
    }


def derived_gold(gold: Iterable[str], owner_id: str) -> list[str]:
    """G' ＝ G ∪ {owner#meta}：去重、保留 G 的順序、meta 只加一次（已在 G 內就不重複）。"""
    g = rm.dedupe_ids(gold)
    meta = meta_chunk_id(owner_id)
    return g if meta in g else [*g, meta]


def score_meta_answerable(
    returned: Iterable[str],
    gold: Iterable[str],
    owner_id: str,
    *,
    owner_visible: bool,
    answers_applicable: bool,
    keywords: object,
    meta_text: object,
    ks: Sequence[int] = TEXT_KS,
) -> dict:
    """derived.meta_answerable：排名一律是原去重回傳（meta 不移除、不移動）。

    適用條件（依序判斷，不適用時指標全 None、derived_gold_ids 為 None、qualified 為 None）：
    answers_applicable 為 False（該領域沒有草稿 1 答案，如畫作）→ domain_without_answers；
    原 G 空 → empty_gold（不因別的帳號保存了 meta 而可答）；owner 看不到 → owner_not_visible。
    適用時：keywords 為 None（拒答題沒有關鍵字）→ no_keywords 不合格；其餘見 match_meta_keywords。
    不合格維持 G（指標等於原指標在 G 上的值）；合格才加同 owner 的 meta。
    """
    g = rm.dedupe_ids(gold)
    ranked = rm.dedupe_ids(returned)
    names = _metric_names(ks, precision=True)
    meta = meta_chunk_id(owner_id)
    base = {
        "gold_ids": g,
        "ranked_ids": ranked,
        "meta_chunk_id": meta,
        "keyword_match": None,
    }

    def not_applicable(reason: str) -> dict:
        return base | {
            "applicable": False,
            "qualified": None,
            "reason": reason,
            "meta_added": False,
            "derived_gold_ids": None,
            "metrics": dict.fromkeys(names),
        }

    if not answers_applicable:
        return not_applicable("domain_without_answers")
    if not g:
        return not_applicable("empty_gold")
    if not owner_visible:
        return not_applicable("owner_not_visible")

    if keywords is None:
        match = None
        qualified, reason = False, "no_keywords"
    else:
        match = match_meta_keywords(keywords, meta_text)
        qualified, reason = match["qualified"], match["reason"]
    final = derived_gold(g, owner_id) if qualified else g
    metrics = _ranked_metrics(ranked, final, ks)
    metrics["precision"] = rm.precision_of_returned(ranked, final)
    return base | {
        "applicable": True,
        "qualified": qualified,
        "reason": reason,
        "keyword_match": match,
        "meta_added": qualified and meta not in g,
        "derived_gold_ids": final,
        "metrics": metrics,
    }


# ---------------------------------------------------------------- 單題與彙整


def derive_case(
    case_id: str,
    returned: Iterable[str],
    gold: Iterable[str],
    owner_id: str,
    *,
    owner_visible: bool,
    answers_applicable: bool,
    keywords: object,
    meta_text: object,
    ks: Sequence[int] = TEXT_KS,
) -> dict:
    """一題的三種衍生口徑（互相獨立，都從原 R 與原可見 G 出發）。"""
    raw = list(returned)
    g = rm.dedupe_ids(gold)
    return {
        "case_id": case_id,
        "owner_id": owner_id,
        "skip_meta": score_skip_meta(raw, g, ks),
        "meta_answerable": score_meta_answerable(
            raw,
            g,
            owner_id,
            owner_visible=owner_visible,
            answers_applicable=answers_applicable,
            keywords=keywords,
            meta_text=meta_text,
            ks=ks,
        ),
        "precision_ceiling": ceiling_detail(raw, g),
    }


def aggregate_cases(cases: Sequence[Mapping], ks: Sequence[int] = TEXT_KS) -> dict:
    """彙整 derive_case 的結果（同一個 group／cohort 一組；呼叫端先分好組）。None 不進分母、0 保留。

    回傳：
    {
      "n_cases": int, "n_original_answerable": int,   # 原 G 非空的題數
      "precision_ceiling": {mean, numerator, denominator},
      "skip_meta": {"metrics": {recall@K／mrr: {mean, numerator, denominator}}},
      "meta_answerable": {"n_applicable", "n_qualified", "qualified_case_ids",
                          "not_applicable_reasons": {reason: 題數},
                          "metrics": {recall@K／mrr／precision: {...}}},
    }
    """
    skip_names = _metric_names(ks, precision=False)
    meta_names = _metric_names(ks, precision=True)
    meta = [c["meta_answerable"] for c in cases]
    qualified = [c["case_id"] for c in cases if c["meta_answerable"]["qualified"]]
    return {
        "n_cases": len(cases),
        "n_original_answerable": sum(c["precision_ceiling"]["gold_count"] > 0 for c in cases),
        "precision_ceiling": rm.macro_average(c["precision_ceiling"]["value"] for c in cases),
        "skip_meta": {
            "metrics": {
                n: rm.macro_average(c["skip_meta"]["metrics"][n] for c in cases) for n in skip_names
            }
        },
        "meta_answerable": {
            "n_applicable": sum(m["applicable"] for m in meta),
            "n_qualified": len(qualified),
            "qualified_case_ids": qualified,
            "not_applicable_reasons": dict(
                sorted(Counter(m["reason"] for m in meta if not m["applicable"]).items())
            ),
            "metrics": {n: rm.macro_average(m["metrics"][n] for m in meta) for n in meta_names},
        },
    }


# ---------------------------------------------------------------- 原文彙集


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_chunk_texts(entries: Iterable[Mapping]) -> dict:
    """從保存的 raw_sources 彙集每個 chunk 的原文。

    entries：{"chunk_id": str, "text": 任意, "location": dict}（location 由呼叫端決定，例如
    group／case／raw_sources 位置）。只收非空字串的 text；None、空字串、非字串都算「省略」，
    不收、也不會覆蓋有效原文。同一 chunk 的所有有效原文必須逐字相同，不同的列在 conflicts。
    chunk_id 必須是非空字串（缺 ID 是上游漏資料，丟 ValueError）。

    回傳：
    {
      "texts": {chunk_id: {"text", "sha256", "locations": [location]}}  # 只含沒有衝突的 chunk
      "conflicts": [{"chunk_id", "variants": [{"text", "sha256", "locations"}]}],
      "valid_sources": int, "omitted": int,
    }
    """
    variants: dict[str, dict[str, list]] = {}
    valid = omitted = 0
    for e in entries:
        cid = e["chunk_id"]
        if not isinstance(cid, str) or not cid:
            raise ValueError(f"chunk_id 必須是非空字串：{cid!r}")
        text = e["text"]
        if not isinstance(text, str) or not text:
            omitted += 1
            continue
        valid += 1
        variants.setdefault(cid, {}).setdefault(text, []).append(e["location"])
    texts, conflicts = {}, []
    for cid, by_text in variants.items():
        if len(by_text) == 1:
            ((text, locs),) = by_text.items()
            texts[cid] = {"text": text, "sha256": text_sha256(text), "locations": locs}
        else:
            conflicts.append(
                {
                    "chunk_id": cid,
                    "variants": [
                        {"text": t, "sha256": text_sha256(t), "locations": locs}
                        for t, locs in by_text.items()
                    ],
                }
            )
    return {"texts": texts, "conflicts": conflicts, "valid_sources": valid, "omitted": omitted}
