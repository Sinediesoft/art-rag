"""檢索層評估的純函式（TASK-20261027-001）：eval/retrieval_metrics.py 的指標與分母，
以及 eval/retrieval_runtime.py 不碰模型與索引檔的部分（env_file 隔離、gold 推導、收據檢查）。

數字都是手算：多正解、短列表、重複 ID、未命中、空結果、expected_empty 與照片 route miss。
"""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import retrieval_metrics as rm  # noqa: E402
import retrieval_runtime as rr  # noqa: E402


def idcg(n: int) -> float:
    return sum(1 / math.log2(r + 1) for r in range(1, n + 1))


# ---------------------------------------------------------------- 去重


def test_dedupe_keeps_first_occurrence_order():
    assert rm.dedupe_ids(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
    assert rm.dedupe_ids([]) == []


@pytest.mark.parametrize("bad", [None, "", 3])
def test_dedupe_rejects_missing_ids(bad):
    with pytest.raises(ValueError):
        rm.dedupe_ids(["a", bad])


def test_k_must_be_positive():
    with pytest.raises(ValueError):
        rm.recall_at_k(["a"], ["a"], 0)
    with pytest.raises(ValueError):
        rm.ndcg_at_k(["a"], ["a"], -1)


# ---------------------------------------------------------------- 單題指標


def test_single_gold_hit_at_rank_two():
    r = rm.score_text_case(["x", "g", "y"], ["g"])
    m = r["metrics"]
    assert (m["recall@1"], m["recall@2"], m["recall@5"]) == (0.0, 1.0, 1.0)
    assert m["mrr"] == 0.5
    assert m["precision"] == pytest.approx(1 / 3)
    assert m["ndcg@5"] is None  # 單一正解不算 nDCG
    assert r["first_relevant_rank"] == 2 and r["relevant_returned"] == 1
    assert r["expected_empty"] is False and r["false_nonempty"] is None


def test_multi_gold_partial_recall_and_ndcg():
    # G={g1,g2,g3}；回傳 x,g1,y,g2：前 5 段命中 rank 2、4
    r = rm.score_text_case(["x", "g1", "y", "g2"], ["g1", "g2", "g3"])
    m = r["metrics"]
    assert m["recall@1"] == 0.0
    assert m["recall@2"] == pytest.approx(1 / 3)
    assert m["recall@5"] == pytest.approx(2 / 3)
    assert m["mrr"] == 0.5
    assert m["precision"] == 0.5  # 2/4，不是 2/5
    dcg = 1 / math.log2(3) + 1 / math.log2(5)
    assert m["ndcg@5"] == pytest.approx(dcg / idcg(3))
    assert m["ndcg@5"] == pytest.approx(0.49819, abs=1e-5)
    assert r["multi_relevant"] is True


def test_duplicate_chunk_ids_count_once():
    r = rm.score_text_case(["g1", "g1", "x", "g1"], ["g1", "g2"])
    m = r["metrics"]
    assert r["returned_ids"] == ["g1", "x"]
    assert (r["raw_returned_count"], r["returned_count"], r["duplicate_count"]) == (4, 2, 2)
    assert (m["recall@1"], m["recall@2"], m["recall@5"]) == (0.5, 0.5, 0.5)
    assert m["precision"] == 0.5  # 1/2，不是 3/4
    assert m["mrr"] == 1.0
    assert m["ndcg@5"] == pytest.approx(1 / idcg(2))


def test_duplicates_do_not_push_later_hits_out_of_top_k():
    m = rm.score_text_case(["a", "a", "g"], ["g"])["metrics"]
    assert m["recall@1"] == 0.0 and m["recall@2"] == 1.0  # 去重後 g 在第 2 名
    assert m["mrr"] == 0.5


def test_duplicate_gold_ids_count_once():
    r = rm.score_text_case(["g"], ["g", "g"])
    assert r["gold_count"] == 1 and r["metrics"]["recall@1"] == 1.0
    assert r["metrics"]["ndcg@5"] is None


def test_miss_scores_zero_not_null():
    m = rm.score_text_case(["x", "y"], ["g"])["metrics"]
    assert (m["recall@1"], m["recall@2"], m["recall@5"], m["mrr"], m["precision"]) == (
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )


def test_empty_return_with_gold_is_zero_precision():
    r = rm.score_text_case([], ["g1", "g2"])
    m = r["metrics"]
    assert m["precision"] == 0.0 and m["mrr"] == 0.0 and m["ndcg@5"] == 0.0
    assert r["first_relevant_rank"] is None and r["nonempty"] is False


def test_short_list_keeps_gold_denominator_and_actual_precision():
    # 只回 1 段：Recall@5 分母仍是 |G|=2；Precision 分母是實際回傳的 1 段，不是 5
    m = rm.score_text_case(["g"], ["g", "h"])["metrics"]
    assert m["recall@5"] == 0.5
    assert m["precision"] == 1.0
    assert m["ndcg@5"] == pytest.approx(1 / idcg(2))


def test_ndcg_ideal_uses_min_k_and_gold():
    assert rm.ndcg_at_k(["g1", "g2"], ["g1", "g2"], 5) == pytest.approx(1.0)
    seven = [f"g{i}" for i in range(7)]
    assert rm.ndcg_at_k(seven[:5], seven, 5) == pytest.approx(1.0)  # IDCG 只取 min(5,7)=5 名
    assert rm.ndcg_at_k(["x", "g1"], ["g1", "g2"], 1) == 0.0
    assert rm.ndcg_at_k(["g1"], [], 5) is None


def test_expected_empty_has_null_ranking_and_counts_false_nonempty():
    r = rm.score_text_case(["x"], [], empty_kind="no_answer")
    assert r["expected_empty"] is True and r["false_nonempty"] is True
    assert all(v is None for v in r["metrics"].values())
    assert r["relevant_returned"] is None
    quiet = rm.score_text_case([], [], empty_kind="hidden")
    assert quiet["false_nonempty"] is False and quiet["metrics"]["precision"] is None


def test_empty_gold_requires_explicit_kind():
    # 缺少標記不能當成「答案為空」
    with pytest.raises(ValueError):
        rm.score_text_case(["x"], [])
    with pytest.raises(ValueError):
        rm.score_text_case(["x"], ["g"], empty_kind="no_answer")
    with pytest.raises(ValueError):
        rm.score_text_case([], [], empty_kind="refused")


# ---------------------------------------------------------------- 彙整


def test_macro_average_and_rate_keep_denominators():
    assert rm.macro_average([1.0, None, 0.5]) == {"mean": 0.75, "numerator": 1.5, "denominator": 2}
    assert rm.macro_average([None]) == {"mean": None, "numerator": 0.0, "denominator": 0}
    assert rm.rate(1, 4) == {"rate": 0.25, "numerator": 1, "denominator": 4}
    assert rm.rate(0, 0) == {"rate": None, "numerator": 0, "denominator": 0}


def test_aggregate_text_cases_separates_answerable_and_expected_empty():
    cases = [
        rm.score_text_case(["g"], ["g"]),  # 全中
        rm.score_text_case(["x", "g1"], ["g1", "g2"]),  # 多正解
        rm.score_text_case(["x"], [], empty_kind="unknown"),
        rm.score_text_case([], [], empty_kind="no_answer"),
        rm.score_text_case(["y"], [], empty_kind="hidden"),
        rm.score_text_case(["z"], [], empty_kind="hidden"),
    ]
    s = rm.aggregate_text_cases(cases)
    assert s["n_cases"] == 6
    a = s["answerable"]
    assert a["n"] == 2 and a["n_multi_relevant"] == 1
    assert a["metrics"]["recall@1"] == {"mean": 0.5, "numerator": 1.0, "denominator": 2}
    assert a["metrics"]["recall@5"] == {"mean": 0.75, "numerator": 1.5, "denominator": 2}
    assert a["metrics"]["mrr"] == {"mean": 0.75, "numerator": 1.5, "denominator": 2}
    assert a["metrics"]["precision"] == {"mean": 0.75, "numerator": 1.5, "denominator": 2}
    nd = a["metrics"]["ndcg@5"]
    assert nd["denominator"] == 1  # 只有多正解那題
    assert nd["mean"] == pytest.approx((1 / math.log2(3)) / idcg(2))
    e = s["expected_empty"]
    assert e["n"] == 4
    assert e["false_nonempty"] == {"rate": 0.75, "numerator": 3, "denominator": 4}
    assert e["by_kind"]["unknown"] == {"rate": 1.0, "numerator": 1, "denominator": 1}
    assert e["by_kind"]["no_answer"] == {"rate": 0.0, "numerator": 0, "denominator": 1}
    assert e["by_kind"]["hidden"] == {"rate": 1.0, "numerator": 2, "denominator": 2}


def test_aggregate_with_no_cases_reports_null_not_zero():
    s = rm.aggregate_text_cases([])
    assert s["answerable"]["metrics"]["recall@1"]["mean"] is None
    assert s["expected_empty"]["false_nonempty"]["rate"] is None
    assert s["expected_empty"]["by_kind"]["hidden"]["denominator"] == 0


# ---------------------------------------------------------------- 外洩


def test_find_leaks_checks_whole_returned_list():
    owner_of = {"mfg-001#00-0": "mfg-001", "mfg-004#01-0": "mfg-004", "mfg-002#03-0": "mfg-002"}
    returned = ["mfg-004#01-0", "mfg-004#01-0", "catalog", "mfg-001#00-0", "mfg-002#03-0"]
    r = rm.find_leaks(returned, owner_of, {"mfg-004"})
    assert r["leaked_ids"] == ["mfg-001#00-0", "mfg-002#03-0"]
    assert r["leaked_owner_ids"] == ["mfg-001", "mfg-002"]
    assert r["leaked_count"] == 2 and r["leak"] is True
    assert r["unowned_ids"] == ["catalog"]
    free = rm.find_leaks(returned, owner_of, None)  # None＝不限（畫作）
    assert free["leak"] is False and free["leaked_ids"] == []


def test_summarize_leaks_counts_chunks_and_cases():
    rows = [
        {"case_id": "a", "leaked_ids": ["c1", "c2"]},
        {"case_id": "b", "leaked_ids": []},
        {"case_id": "c", "leaked_ids": ["c3"]},
    ]
    s = rm.summarize_leaks(rows)
    assert s["leaked_chunks"] == 3
    assert s["leaked_cases"] == {"rate": 2 / 3, "numerator": 2, "denominator": 3}
    assert s["case_ids"] == ["a", "c"]
    assert rm.summarize_leaks([])["leaked_cases"]["rate"] is None


# ---------------------------------------------------------------- 照片候選排名


def test_photo_rank_hit_and_mrr():
    r = rm.score_photo_case(["mfg-002", "mfg-002", "mfg-001", "mfg-003"], "mfg-001")
    m = r["metrics"]
    assert r["rank"] == 2 and r["in_ranking"] is True
    assert (m["recall@1"], m["recall@2"], m["recall@3"], m["recall@5"]) == (0.0, 1.0, 1.0, 1.0)
    assert m["mrr"] == 0.5


def test_photo_route_miss_counts_as_miss():
    r = rm.score_photo_case([], "mfg-001", route_miss=True)
    assert r["in_ranking"] is True and r["route_miss"] is True and r["rank"] is None
    assert all(v == 0.0 for v in r["metrics"].values())
    with pytest.raises(ValueError):
        rm.score_photo_case(["mfg-001"], "mfg-001", route_miss=True)


def test_photo_without_visible_gold_is_outside_ranking():
    r = rm.score_photo_case(["mfg-001"], None)
    assert r["in_ranking"] is False and all(v is None for v in r["metrics"].values())


def test_aggregate_photo_cases_returns_denominator():
    cases = [
        rm.score_photo_case(["mfg-001"], "mfg-001"),
        rm.score_photo_case(["mfg-002", "mfg-003", "mfg-004", "mfg-005", "mfg-001"], "mfg-001"),
        rm.score_photo_case([], "mfg-001", route_miss=True),
        rm.score_photo_case([], "mfg-002"),
        rm.score_photo_case(["mfg-001"], None),  # 未知／看不到：不進排名分母
    ]
    s = rm.aggregate_photo_cases(cases)
    assert s["n_cases"] == 5 and s["n_ranked"] == 4
    assert s["n_route_miss"] == 1 and s["n_empty_candidates"] == 2
    assert s["metrics"]["recall@1"] == {"mean": 0.25, "numerator": 1.0, "denominator": 4}
    assert s["metrics"]["recall@3"]["mean"] == 0.25
    assert s["metrics"]["recall@5"] == {"mean": 0.5, "numerator": 2.0, "denominator": 4}
    assert s["metrics"]["mrr"]["numerator"] == pytest.approx(1.2)
    assert s["metrics"]["mrr"]["mean"] == pytest.approx(0.3)


def test_photo_negative_kind_splits_hidden_and_unknown():
    assert rm.photo_negative_kind("mfg-001", "mfg-001") is None
    assert rm.photo_negative_kind(None, "mfg-001") == "hidden"
    assert rm.photo_negative_kind(None, None) == "unknown"


# ---------------------------------------------------------------- gold 推導（純資料，不讀索引檔）

KB = {
    "doc-1": {
        "id": "doc-1",
        "descriptions": [
            {"topic": "技法"},
            {"topic": "畫面"},
            {"topic": "技法"},
            {"topic": "觀眾留言（未審核）"},
        ],
    },
    "doc-2": {"id": "doc-2", "descriptions": [{"topic": "技法"}]},
    "doc-3": {"id": "doc-3", "descriptions": [{"topic": "技法"}]},  # 索引沒有它
}


def chunk(cid: str, topic: str, owner: str = "doc-1") -> dict:
    return {"chunk_id": cid, "artwork_id": owner, "topic": topic}


CHUNKS = [
    chunk("doc-1#meta", "基本資料"),
    chunk("doc-1#00-0", "技法"),
    chunk("doc-1#00-1", "技法"),  # 長段落切成兩塊
    chunk("doc-1#01-0", "畫面"),
    chunk("doc-1#02-0", "技法"),
    chunk("doc-1#03-0", "觀眾留言（未審核）"),
    chunk("doc-1#color", "色彩分析"),
    chunk("doc-2#00-0", "技法", "doc-2"),
]


def resolve(row: dict, **kw) -> dict:
    base = {"id": "q", "question": "問題？", "artwork_id": "doc-1"}
    return rr.resolve_gold({**base, **row}, domain="art", kb_docs=KB, chunks=CHUNKS, **kw)


def test_gold_takes_every_chunk_of_every_matching_description():
    g = resolve({"topics": ["技法"]})
    assert g["status"] == "answerable"
    # 兩段同 topic 的 descriptions、切塊後的子段都算；doc-2 的同名 topic 不算
    assert g["gold_chunk_ids"] == ["doc-1#00-0", "doc-1#00-1", "doc-1#02-0"]
    assert g["gold_chunk_owner"] == dict.fromkeys(g["gold_chunk_ids"], "doc-1")
    assert g["multi_relevant"] is True and g["unreviewed_gold"] is False
    both = resolve({"topics": ["技法", "畫面"]})
    assert both["gold_chunk_ids"] == ["doc-1#00-0", "doc-1#00-1", "doc-1#01-0", "doc-1#02-0"]


def test_unreviewed_topic_is_flagged_not_dropped():
    g = resolve({"topics": ["觀眾留言（未審核）"]})
    assert g["status"] == "answerable" and g["unreviewed_gold"] is True


@pytest.mark.parametrize("topic", ["色彩分析", "基本資料"])
def test_system_generated_topic_is_excluded_not_no_answer(topic):
    g = resolve({"topics": [topic]})
    assert g["status"] == "excluded" and g["excluded_reason"] == "system_generated_topic"
    assert g["empty_kind"] is None and g["gold_chunk_ids"] == []


def test_partial_mapping_excludes_whole_case():
    g = resolve({"topics": ["技法", "不存在的主題"]})
    assert g["status"] == "excluded" and g["excluded_reason"] == "topic_not_in_descriptions"
    assert g["gold_chunk_ids"] == []  # 不留有對到的子集合
    assert "不存在的主題" in g["excluded_detail"]


def test_explicit_empty_and_unknown_document():
    assert resolve({"topics": [], "expect_refusal": True})["empty_kind"] == "no_answer"
    assert resolve({"topics": [], "type": "no_answer"})["status"] == "expected_empty"
    u = resolve({"artwork_id": "doc-404", "topics": [], "expect_refusal": True})
    assert (u["status"], u["empty_kind"]) == ("expected_empty", "unknown")
    t = resolve({"artwork_id": "doc-404", "topics": [], "type": "unknown"})
    assert (t["status"], t["empty_kind"]) == ("expected_empty", "unknown")
    assert u["gold_chunk_ids"] == [] and u["excluded_reason"] is None
    # 沒指定文件、明確無答案（全庫題）
    g = resolve({"artwork_id": None, "topics": [], "expect_refusal": True})
    assert (g["status"], g["empty_kind"], g["owner_id"]) == ("expected_empty", "no_answer", None)


def test_distractor_rows_keep_normal_gold_and_type():
    g = resolve({"topics": ["畫面"], "type": "distractor", "split": "test"})
    assert g["status"] == "answerable" and g["gold_chunk_ids"] == ["doc-1#01-0"]
    assert (g["qa_type"], g["split"], g["multi_relevant"]) == ("distractor", "test", False)
    assert g["gold_chunk_topic"] == {"doc-1#01-0": "畫面"}


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ({"topics": []}, "missing_topics"),
        ({"topics": ["技法"], "expect_refusal": True}, "conflicting_labels"),
        ({"artwork_id": "doc-404", "topics": ["技法"]}, "owner_not_in_kb"),
        ({"artwork_id": "npm-2", "topics": ["技法"]}, "owner_in_staging"),
        ({"artwork_id": "npm-2", "topics": [], "expect_refusal": True}, "owner_in_staging"),
        ({"artwork_id": "doc-3", "topics": ["技法"]}, "owner_not_in_index"),
        ({"artwork_id": None, "topics": ["技法"]}, "missing_owner"),
        ({"artwork_id": None, "part_id": "mfg-001", "topics": ["用途與組裝"]}, "domain_mismatch"),
        ({"topics": [], "type": "unknown"}, "conflicting_labels"),  # 標不存在，文件卻在 KB
        ({"artwork_id": "ghost", "topics": ["技法"]}, "index_kb_mismatch"),  # 不在 KB 卻有段落
    ],
)
def test_unresolvable_rows_are_excluded_with_reason(row, reason):
    chunks = [*CHUNKS, chunk("ghost#00-0", "技法", "ghost")]
    base = {"id": "q", "question": "問題？", "artwork_id": "doc-1"}
    g = rr.resolve_gold(
        {**base, **row}, domain="art", kb_docs=KB, chunks=chunks, staging_ids={"npm-2"}
    )
    assert g["status"] == "excluded" and g["excluded_reason"] == reason
    assert g["gold_chunk_ids"] == [] and g["empty_kind"] is None
    assert g["excluded_detail"]


def resolve_on(chunks: list[dict], topics: list[str]) -> dict:
    row = {"id": "q", "question": "?", "artwork_id": "doc-1", "topics": topics}
    return rr.resolve_gold(row, domain="art", kb_docs=KB, chunks=chunks)


def test_index_that_disagrees_with_kb_is_excluded():
    stale = [c for c in CHUNKS if c["chunk_id"] != "doc-1#01-0"]
    g = resolve_on(stale, ["畫面"])
    assert g["excluded_reason"] == "index_kb_mismatch" and "descriptions[1]" in g["excluded_detail"]
    renamed = [{**c, "topic": "舊主題"} if c["chunk_id"] == "doc-1#01-0" else c for c in CHUNKS]
    g = resolve_on(renamed, ["畫面"])
    assert g["excluded_reason"] == "index_kb_mismatch" and "舊主題" in g["excluded_detail"]
    # 索引在別的位置也有同一個 topic（KB 的 descriptions 順序改過但沒重建索引）
    moved = [*CHUNKS, chunk("doc-1#04-0", "畫面")]
    g = resolve_on(moved, ["畫面"])
    assert g["excluded_reason"] == "index_kb_mismatch" and "doc-1#04-0" in g["excluded_detail"]
    # 另一個文件的同名 chunk 不影響
    other_doc = resolve_on([*CHUNKS, chunk("doc-2#05-0", "畫面", "doc-2")], ["畫面"])
    assert other_doc["status"] == "answerable" and other_doc["gold_chunk_ids"] == ["doc-1#01-0"]


def test_resolve_gold_cases_splits_and_rejects_duplicate_ids():
    rows = [
        {"id": "a", "question": "?", "artwork_id": "doc-1", "topics": ["畫面"]},
        {"id": "b", "question": "?", "artwork_id": "doc-1", "topics": ["色彩分析"]},
        {"id": "c", "question": "?", "artwork_id": "doc-1", "topics": [], "expect_refusal": True},
    ]
    out = rr.resolve_gold_cases(rows, domain="art", kb_docs=KB, chunks=CHUNKS)
    assert [c["case_id"] for c in out["included"]] == ["a", "c"]
    assert [c["case_id"] for c in out["excluded"]] == ["b"]
    assert out["counts"] == {
        "rows": 3,
        "answerable": 1,
        "expected_empty": 1,
        "excluded": 1,
        "excluded_by_reason": {"system_generated_topic": 1},
    }
    with pytest.raises(ValueError):
        rr.resolve_gold_cases(rows + rows[:1], domain="art", kb_docs=KB, chunks=CHUNKS)


# ---------------------------------------------------------------- 身分可見性


def mfg_case(**over) -> dict:
    base = {
        "case_id": "m",
        "domain": "mfg",
        "owner_id": "mfg-001",
        "status": "answerable",
        "empty_kind": None,
        "gold_chunk_ids": ["mfg-001#00-0", "mfg-001#02-0"],
        "gold_chunk_owner": {"mfg-001#00-0": "mfg-001", "mfg-001#02-0": "mfg-001"},
        "multi_relevant": True,
    }
    return {**base, **over}


def test_hidden_document_becomes_expected_empty_hidden():
    v = rr.apply_visibility(mfg_case(), {"mfg-004", "mfg-005"})
    assert (v["status"], v["empty_kind"]) == ("expected_empty", "hidden")
    assert v["gold_chunk_ids"] == [] and v["hidden_target_count"] == 2
    assert v["gold_chunk_ids_all"] == ["mfg-001#00-0", "mfg-001#02-0"]
    assert v["hidden_owner_ids"] == ["mfg-001"] and v["owner_visible"] is False
    assert v["multi_relevant"] is False


def test_partially_hidden_gold_keeps_visible_targets():
    c = mfg_case(
        gold_chunk_ids=["mfg-001#00-0", "mfg-004#01-0"],
        gold_chunk_owner={"mfg-001#00-0": "mfg-001", "mfg-004#01-0": "mfg-004"},
        owner_id="mfg-004",
    )
    v = rr.apply_visibility(c, {"mfg-004"})
    assert v["status"] == "answerable" and v["gold_chunk_ids"] == ["mfg-004#01-0"]
    assert v["hidden_target_count"] == 1 and v["multi_relevant"] is False


def test_visibility_keeps_unknown_and_marks_hidden_no_answer():
    na = mfg_case(status="expected_empty", empty_kind="no_answer", gold_chunk_ids=[])
    v = rr.apply_visibility({**na, "gold_chunk_owner": {}}, {"mfg-004"})
    assert (v["empty_kind"], v["original_empty_kind"]) == ("hidden", "no_answer")
    unk = mfg_case(
        owner_id="mfg-999",
        status="expected_empty",
        empty_kind="unknown",
        gold_chunk_ids=[],
        gold_chunk_owner={},
    )
    v = rr.apply_visibility(unk, {"mfg-004"})
    assert (v["empty_kind"], v["original_empty_kind"]) == ("unknown", "unknown")
    unrestricted = rr.apply_visibility(mfg_case(), None)
    assert unrestricted["status"] == "answerable" and unrestricted["hidden_target_count"] == 0
    excluded = mfg_case(status="excluded", gold_chunk_ids=[], gold_chunk_owner={})
    assert rr.apply_visibility(excluded, {"mfg-004"})["status"] == "excluded"


# ---------------------------------------------------------------- 設定與收據（純資料）


def test_settings_snapshot_never_records_secrets():
    s = SimpleNamespace(
        embed_mode="real",
        embed_device="cpu",
        llm_mode="real",
        hf_offline=False,
        memory_guard=False,
        jev_enabled=False,
        allow_cloud=False,
        data_dir=Path("/tmp/data"),
        index_dir=Path("/tmp/data/index"),
        artrag_shared_dir="",
        database_url="postgresql://u:pw-secret@db/x",
        api_key="key-secret",
        jev_api_key="jev-secret",
    )
    snap = rr.settings_snapshot(s)
    assert snap["index_backend"] == "pg" and snap["database_url_set"] is True
    assert snap["embed_mode"] == "real" and snap["data_dir"] == "/tmp/data"
    assert "secret" not in repr(snap)
    s.database_url = ""
    assert rr.settings_snapshot(s)["index_backend"] == "file"


def test_retrieval_params_reads_production_thresholds():
    cfg = SimpleNamespace(
        retrieval={"top_k_chunks": 5, "min_chunk_score": 0.3, "relative_chunk_ratio": 0.6, "x": 1},
        global_fill_keywords=["比較", "背景"],
        chunking={"max_chars": 500},
    )
    p = rr.retrieval_params(cfg)
    assert p == {
        "top_k_chunks": 5,
        "min_chunk_score": 0.3,
        "relative_chunk_ratio": 0.6,
        "global_fill_keywords": ["比較", "背景"],
        "chunking": {"max_chars": 500},
    }
    assert rr.sha256_json(p) == rr.sha256_json(dict(reversed(p.items())))


def test_collection_digest_changes_with_vectors():
    coll = SimpleNamespace(
        owner_key="part_id",
        items=[{"id": "mfg-001"}],
        chunks=[{"chunk_id": "mfg-001#meta", "part_id": "mfg-001", "topic": "基本資料"}],
        chunk_vecs=np.ones((1, 4), np.float32),
        image_vecs=np.zeros((1, 2), np.float32),
    )
    d = rr.collection_digest(coll)
    assert d["chunk_ids"] == ["mfg-001#meta"] and d["chunk_count"] == 1
    assert d["item_ids"] == ["mfg-001"] and d["chunk_vecs"]["shape"] == [1, 4]
    coll.chunk_vecs = coll.chunk_vecs * 2
    assert rr.collection_digest(coll)["chunk_vecs"]["sha256"] != d["chunk_vecs"]["sha256"]


def receipt(**over) -> dict:
    coll = {"chunk_count": 21, "chunks_sha256": "c1", "chunk_vecs": {"sha256": "v1"}}
    mfg = {"chunk_count": 34, "chunks_sha256": "c2", "chunk_vecs": {"sha256": "v2"}}
    parts = {
        "manifest": {"embed_mode": "real", "chunk_count": 21, "part_chunk_count": 34},
        "manifest_problems": [],
        "collections": {"art": coll, "mfg": mfg},
        "settings": {"embed_mode": "real", "index_backend": "file"},
        "models": {"text": {"name": "BAAI/bge-m3", "revision": "r", "dim": 1024}},
        "retrieval_params": {"top_k_chunks": 5},
        "models_yaml_sha256": "y",
        "kb": {"version": "2026.10.2", "hash_now": "h", "hash_manifest": "h"},
        "qa": {"eval/qa.jsonl": "q"},
        "code": {"head": "abc", "dirty": False},
        "isolation": {"env_file_disabled": True},
        "index_build": {"invoked": True, "exit_code": 0},
        "created_at": "2026-10-27T00:00:00+00:00",
    }
    return rr.assemble_index_receipt(**{**parts, **over})


def test_real_receipt_passes_and_records_counts():
    r = receipt()
    assert r["schema_version"] == rr.RECEIPT_SCHEMA and r["real"] is True
    assert r["chunk_counts"] == {"art": 21, "mfg": 34, "manifest_art": 21, "manifest_mfg": 34}
    assert r["chunk_counts_match"] is True and r["kb"]["match"] is True
    assert rr.verify_index_receipt(r) == []


@pytest.mark.parametrize(
    "over",
    [
        {"settings": {"embed_mode": "mock", "index_backend": "file"}},
        {"manifest": {"embed_mode": "mock", "chunk_count": 21, "part_chunk_count": 34}},
        {"manifest_problems": ["知識庫版本不一致"]},
        {"manifest": {"embed_mode": "real", "chunk_count": 15, "part_chunk_count": 34}},
        {"kb": {"version": "2026.10.2", "hash_now": "h2", "hash_manifest": "h"}},
        {"isolation": {"env_file_disabled": False}},
        {"index_build": {"invoked": True, "exit_code": 1}},
        {"qa": {"eval/drawing_retrieval/qa.jsonl": None}},
    ],
)
def test_receipt_problems_block_official_results(over):
    assert rr.verify_index_receipt(receipt(**over))


def test_mock_receipt_allowed_only_when_not_requiring_real():
    mock = receipt(
        settings={"embed_mode": "mock", "index_backend": "file"},
        manifest={"embed_mode": "mock", "chunk_count": 21, "part_chunk_count": 34},
    )
    assert mock["real"] is False
    assert rr.verify_index_receipt(mock, require_real=False) == []


def test_receipt_must_match_currently_loaded_index():
    r = receipt()
    assert rr.verify_index_receipt(r, current=receipt()) == []
    changed = receipt(
        collections={
            "art": {"chunk_count": 21, "chunks_sha256": "c1", "chunk_vecs": {"sha256": "OTHER"}},
            "mfg": {"chunk_count": 34, "chunks_sha256": "c2", "chunk_vecs": {"sha256": "v2"}},
        }
    )
    assert rr.verify_index_receipt(r, current=changed) == ["art 索引的段落向量和收據不同"]
    other_manifest = receipt(
        manifest={"embed_mode": "real", "chunk_count": 21, "part_chunk_count": 34, "x": 1}
    )
    assert rr.verify_index_receipt(r, current=other_manifest) == [
        "目前載入的索引 manifest 和收據不同"
    ]
    now_mock = receipt(settings={"embed_mode": "mock", "index_backend": "file"})
    assert any("EMBED_MODE" in p for p in rr.verify_index_receipt(r, current=now_mock))


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"code": {"head": None, "dirty": None}}, "HEAD"),
        (
            {
                "collections": {
                    "art": {"chunk_count": 0, "chunks_sha256": "c", "chunk_vecs": {}},
                    "mfg": {"chunk_count": 34, "chunks_sha256": "c2", "chunk_vecs": {}},
                },
                "manifest": {"embed_mode": "real", "chunk_count": 0, "part_chunk_count": 34},
            },
            "art 領域沒有任何段落",
        ),
        ({"index_build": {"invoked": False}}, "不是這次重建索引"),
    ],
)
def test_receipt_requires_head_chunks_and_rebuild(over, needle):
    problems = rr.verify_index_receipt(receipt(**over), require_build=True)
    assert any(needle in p for p in problems)
    assert rr.verify_index_receipt(receipt(), require_build=True) == []


def test_receipt_rejects_other_schema():
    r = receipt() | {"schema_version": "old/0"}
    assert any("old/0" in p for p in rr.verify_index_receipt(r))


def test_mock_artifacts_cannot_land_in_tracked_runs():
    runs = ROOT / "eval" / "runs"
    assert rr.is_official_runs_dir(runs) and rr.is_official_runs_dir(runs / "sub")
    assert not rr.is_official_runs_dir(ROOT / "eval" / "drawing_retrieval")


# ---------------------------------------------------------------- 不讀 .env 的 bootstrap


@pytest.fixture
def settings_sandbox(monkeypatch, tmp_path):
    """把 env_file 指到暫存的假 .env（絕不碰 repo 的 .env），測完清掉 settings 快取。"""
    from app.core import config

    fake = tmp_path / ".env"
    fake.write_text("MODEL_KEEP_ALIVE=from-dotenv-sentinel\n", encoding="utf-8")
    monkeypatch.setitem(config.Settings.model_config, "env_file", fake)
    monkeypatch.delenv("MODEL_KEEP_ALIVE", raising=False)
    config.get_settings.cache_clear()
    yield config, fake
    config.get_settings.cache_clear()
    config.get_models_config.cache_clear()


def test_bootstrap_refuses_after_settings_were_loaded(settings_sandbox):
    config, fake = settings_sandbox
    assert config.get_settings().model_keep_alive == "from-dotenv-sentinel"  # 對照：真的會讀
    with pytest.raises(rr.RuntimeIsolationError):
        rr.bootstrap()
    assert config.Settings.model_config["env_file"] == fake  # 拒絕時不動設定


def test_bootstrap_disables_env_file_and_settings_stop_reading_it(settings_sandbox, monkeypatch):
    config, fake = settings_sandbox
    config.get_settings()
    info = rr.bootstrap(strict=False)
    assert info["env_file_disabled"] is True and info["env_file_before"] == str(fake)
    assert info["settings_were_loaded"] is True
    assert config.Settings.model_config["env_file"] is None
    from pydantic_settings.sources.providers import dotenv as dotenv_source

    opened = []
    monkeypatch.setattr(dotenv_source, "dotenv_values", lambda *a, **k: opened.append(a) or {})
    assert config.get_settings().model_keep_alive == ""
    assert opened == []
    assert rr.env_file_disabled() is True


# ---------------------------------------------------------------- 收據完整 digest 比對（A 批）


def _vec(shape=(21, 4), dtype="float32", sha="v"):
    return {"shape": list(shape), "dtype": dtype, "sha256": sha}


def full_coll(n: int, tag: str) -> dict:
    return {
        "chunk_count": n,
        "chunks_sha256": f"c-{tag}",
        "items_sha256": f"i-{tag}",
        "chunk_vecs": _vec((n, 4), sha=f"cv-{tag}"),
        "image_vecs": _vec((3, 2), sha=f"iv-{tag}"),
    }


def full_receipt(**over) -> dict:
    over = {"collections": {"art": full_coll(21, "a"), "mfg": full_coll(34, "m")}} | over
    return receipt(**over)


def _changed(domain: str, mutate) -> dict:
    colls = {"art": full_coll(21, "a"), "mfg": full_coll(34, "m")}
    mutate(colls[domain])
    return full_receipt(collections=colls)


def test_full_matching_receipt_passes_with_current():
    assert rr.verify_index_receipt(full_receipt(), current=full_receipt()) == []


@pytest.mark.parametrize("domain", ["art", "mfg"])
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c.update(items_sha256="other"), "{d} 索引的 items_sha256 和收據不同"),
        (
            lambda c: c["image_vecs"].update(sha256="other"),
            "{d} 索引的圖片向量 sha256 和收據不同",
        ),
        (
            lambda c: c["image_vecs"].update(shape=[9, 9]),
            "{d} 索引的圖片向量 shape 和收據不同",
        ),
        (
            lambda c: c["image_vecs"].update(dtype="float64"),
            "{d} 索引的圖片向量 dtype 和收據不同",
        ),
        (
            lambda c: c["chunk_vecs"].update(shape=[9, 9]),
            "{d} 索引的段落向量 shape 和收據不同",
        ),
        (
            lambda c: c["chunk_vecs"].update(dtype="float64"),
            "{d} 索引的段落向量 dtype 和收據不同",
        ),
        (
            lambda c: c["chunk_vecs"].update(sha256="other"),
            "{d} 索引的段落向量和收據不同",
        ),
    ],
)
def test_each_collection_description_difference_is_rejected_alone(domain, mutate, message):
    problems = rr.verify_index_receipt(full_receipt(), current=_changed(domain, mutate))
    assert problems == [message.format(d=domain)]


def test_image_only_change_with_same_shape_and_dtype_is_caught():
    # 圖片向量內容不同、其他全部相同：只有 image sha 能抓到
    cur = _changed("art", lambda c: c["image_vecs"].update(sha256="swapped"))
    assert rr.verify_index_receipt(full_receipt(), current=cur) == [
        "art 索引的圖片向量 sha256 和收據不同"
    ]


def test_multiple_mismatches_keep_domain_then_field_order():
    cur = full_receipt(
        collections={
            "art": full_coll(21, "a")
            | {
                "chunks_sha256": "x",
                "items_sha256": "x",
                "chunk_vecs": _vec((21, 4), sha="x"),
                "image_vecs": _vec((9, 9), "float64", "x"),
            },
            "mfg": full_coll(34, "m") | {"items_sha256": "x"},
        }
    )
    assert rr.verify_index_receipt(full_receipt(), current=cur) == [
        "art 索引的 chunks_sha256 和收據不同",
        "art 索引的段落向量和收據不同",
        "art 索引的 items_sha256 和收據不同",
        "art 索引的圖片向量 shape 和收據不同",
        "art 索引的圖片向量 dtype 和收據不同",
        "art 索引的圖片向量 sha256 和收據不同",
        "mfg 索引的 items_sha256 和收據不同",
    ]


def test_absent_or_null_descriptions_equal_each_other_but_not_a_present_one():
    bare = receipt()  # 收據與現況都沒有 items／image 描述
    assert rr.verify_index_receipt(bare, current=receipt()) == []
    nulled = receipt(
        collections={
            "art": {"chunk_count": 21, "chunks_sha256": "c1", "chunk_vecs": {"sha256": "v1"}}
            | {"items_sha256": None, "image_vecs": None},
            "mfg": {"chunk_count": 34, "chunks_sha256": "c2", "chunk_vecs": {"sha256": "v2"}},
        }
    )
    assert rr.verify_index_receipt(bare, current=nulled) == []
    # 收據缺、現況有：不能當成相符
    present = verify = rr.verify_index_receipt(bare, current=full_receipt())
    assert "art 索引的 items_sha256 和收據不同" in present
    assert "art 索引的圖片向量 shape 和收據不同" in verify


def test_non_current_problems_keep_their_order_before_collection_checks():
    r = full_receipt(
        settings={"embed_mode": "mock", "index_backend": "file"},
        manifest_problems=["m"],
        isolation={"env_file_disabled": False},
    )
    cur = _changed("art", lambda c: c.update(items_sha256="x"))
    problems = rr.verify_index_receipt(r, current=cur)
    assert problems[0].startswith("不是真模型索引")
    assert problems[1] == "索引與設定不一致：m"
    assert problems[2] == "沒有關閉 env_file（可能讀了 .env）"
    assert problems[-1] == "art 索引的 items_sha256 和收據不同"
