"""檢索層衍生指標的純函式（TASK-20261029-002 AC-1～AC-4、AC-6）：eval/retrieval_derived_metrics.py。

數字都是手算：多正解、短列表、重複 ID、空回傳、expected_empty、AND／OR 關鍵字、部分命中、
原文缺漏、同 owner 的 #meta 才升格 G'。不呼叫模型、不 import app、不讀 .env。
"""

import copy
import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import retrieval_derived_metrics as rdm  # noqa: E402
import retrieval_metrics as rm  # noqa: E402

KS = (1, 2, 5)


# ---------------------------------------------------------------- skip_meta（AC-1）


def test_is_meta_id_only_exact_suffix():
    assert rdm.is_meta_id("mfg-001#meta") and rdm.is_meta_id("npm-000001#meta")
    for other in ("mfg-001#00-0", "x#metadata", "x#meta2", "x#META", "x#meta-0", "meta", "x#color"):
        assert not rdm.is_meta_id(other), other


def test_skip_meta_dedupes_first_then_removes_meta_and_reranks():
    r = rdm.score_skip_meta(["m#meta", "a", "m#meta", "g", "b"], ["g"])
    assert r["ranked_ids"] == ["a", "g", "b"]
    assert r["removed_meta_ids"] == ["m#meta"]
    assert r["first_relevant_rank"] == 2
    m = r["metrics"]
    assert (m["recall@1"], m["recall@2"], m["recall@5"], m["mrr"]) == (0.0, 1.0, 1.0, 0.5)
    assert set(m) == {"recall@1", "recall@2", "recall@5", "mrr"}  # skip_meta 不含 precision


def test_skip_meta_removes_same_and_cross_document_meta_but_keeps_other_sources():
    returned = ["x#meta", "y#meta", "catalog:kb", "x#color", "g"]
    r = rdm.score_skip_meta(returned, ["g"])
    assert r["removed_meta_ids"] == ["x#meta", "y#meta"]
    assert r["ranked_ids"] == ["catalog:kb", "x#color", "g"]  # catalog、#color 不刪
    assert r["metrics"]["mrr"] == pytest.approx(1 / 3)


def test_skip_meta_lookalike_ids_are_not_removed():
    returned = ["a#metadata", "a#meta2", "a#META", "g"]
    assert rdm.score_skip_meta(returned, ["g"])["ranked_ids"] == returned


def test_skip_meta_multi_gold_changes_recall_at_2_only_through_reranking():
    returned = ["g1", "m#meta", "g2"]
    original = rm.score_text_case(returned, ["g1", "g2"])["metrics"]
    derived = rdm.score_skip_meta(returned, ["g1", "g2"])["metrics"]
    assert original["recall@2"] == 0.5 and derived["recall@2"] == 1.0
    assert original["recall@1"] == derived["recall@1"] == 0.5  # 集合 Recall，不是 hit rate
    assert derived["mrr"] == 1.0


def test_skip_meta_short_list_keeps_gold_denominator():
    m = rdm.score_skip_meta(["g1"], ["g1", "g2", "g3"])["metrics"]
    assert m["recall@5"] == pytest.approx(1 / 3) and m["recall@1"] == pytest.approx(1 / 3)


def test_skip_meta_all_meta_after_dedupe_is_zero_not_none():
    r = rdm.score_skip_meta(["a#meta", "a#meta", "b#meta"], ["g"])
    assert r["ranked_ids"] == [] and r["applicable"] is True
    assert r["metrics"] == {"recall@1": 0.0, "recall@2": 0.0, "recall@5": 0.0, "mrr": 0.0}


def test_skip_meta_empty_return_with_gold_is_zero():
    r = rdm.score_skip_meta([], ["g"])
    assert r["metrics"]["mrr"] == 0.0 and r["metrics"]["recall@5"] == 0.0
    assert r["first_relevant_rank"] is None


@pytest.mark.parametrize("returned", [[], ["a"], ["a#meta", "b"]])
def test_skip_meta_empty_gold_is_null_and_not_applicable(returned):
    r = rdm.score_skip_meta(returned, [])
    assert r["applicable"] is False
    assert set(r["metrics"].values()) == {None}


def test_skip_meta_does_not_mutate_inputs_and_original_gold_stays():
    returned, gold = ["m#meta", "g"], ["g"]
    snap = copy.deepcopy((returned, gold))
    r = rdm.score_skip_meta(returned, gold)
    assert (returned, gold) == snap
    assert r["gold_ids"] == ["g"]


def test_skip_meta_rejects_missing_ids():
    with pytest.raises(ValueError):
        rdm.score_skip_meta(["a", None], ["a"])


# ---------------------------------------------------------------- precision_ceiling（AC-4）


@pytest.mark.parametrize(
    ("returned", "gold", "expected"),
    [
        (["a", "b", "c", "d", "e"], ["a"], 0.2),  # |G|=1，|R|=5
        (["a", "b", "c", "d", "e"], ["a", "b"], 0.4),  # 多正解
        (["a", "b"], ["a", "b", "c"], 1.0),  # |G| > |R|：上限 1，不除固定 5
        (["a"], ["a"], 1.0),
        (["a", "a", "b"], ["a", "b", "c"], 1.0),  # R 去重後 2
        (list("abcdefgh"), ["a"], 1 / 8),  # R 多於 5
        (["m#meta", "g"], ["g"], 0.5),  # 不先 skip_meta：meta 仍在 R
    ],
)
def test_precision_ceiling_values(returned, gold, expected):
    assert rdm.precision_ceiling(returned, gold) == pytest.approx(expected)


def test_precision_ceiling_gold_nonempty_empty_return_is_zero():
    assert rdm.precision_ceiling([], ["g"]) == 0.0


@pytest.mark.parametrize("returned", [[], ["a", "b"]])
def test_precision_ceiling_empty_gold_is_none(returned):
    assert rdm.precision_ceiling(returned, []) is None


def test_precision_ceiling_is_at_least_actual_precision():
    returned, gold = ["x", "g1", "y", "g2", "z"], ["g1", "g2", "g3"]
    actual = rm.precision_of_returned(returned, gold)
    assert actual == pytest.approx(0.4)
    ceiling = rdm.precision_ceiling(returned, gold)
    assert ceiling == pytest.approx(3 / 5)
    assert ceiling >= actual


# ---------------------------------------------------------------- 關鍵字比對（AC-2）


def test_keywords_and_groups_all_required_or_candidates_any_one():
    kw = [["鋸床下料", "下料"], ["委外黑色陽極處理", "黑色陽極處理"]]
    r = rdm.match_meta_keywords(kw, "先下料，再做黑色陽極處理。")
    assert r["qualified"] is True and r["reason"] == "ok" and r["missing_group_indexes"] == []
    g0, g1 = r["groups"]
    assert g0["satisfied"] and g0["hit"] == "下料" and g0["hit_position"] == 1
    assert [c["found"] for c in g0["candidates"]] == [False, True]
    assert [c["position"] for c in g0["candidates"]] == [None, 1]
    assert g1["hit"] == "黑色陽極處理" and g1["hit_position"] == 6


def test_keywords_first_found_candidate_in_order_is_the_hit():
    r = rdm.match_meta_keywords([["bb", "a"]], "xx a bb")
    assert r["groups"][0]["hit"] == "bb" and r["groups"][0]["hit_position"] == 5


def test_keywords_partial_match_is_not_qualified_and_lists_missing_groups():
    kw = [["4 支"], ["9 N·m"], ["每 20 件抽驗 1 件"]]
    r = rdm.match_meta_keywords(kw, "底板用 4 支螺栓，扭力 9 N·m。")
    assert r["qualified"] is False and r["reason"] == "missing_groups"
    assert r["missing_group_indexes"] == [2]
    assert [g["satisfied"] for g in r["groups"]] == [True, True, False]


def test_keywords_single_word_does_not_cover_other_required_fact():
    # S45C 出現不代表「硬度」事實也有：兩個必要事實組各自要有命中
    r = rdm.match_meta_keywords([["S45C"], ["HRC 22–28"]], "材料 S45C，調質。")
    assert r["qualified"] is False and r["missing_group_indexes"] == [1]


@pytest.mark.parametrize(
    ("candidate", "text"),
    [
        ("S45C", "材料 s45c"),  # 區分大小寫
        ("HRC 22–28", "硬度 HRC 22-28"),  # en dash ≠ hyphen
        ("HRC 22–28", "硬度 HRC22–28"),  # 不正規化空白
        ("10 mm", "厚 10mm"),
        ("10 mm", "厚 10\u3000mm"),  # 全形空白 U+3000 ≠ 半形空白
        ("25 N·m", "25 N・m"),  # 中點變體不同
        ("ＡＢＣ", "ABC"),  # 全形≠半形
        (
            unicodedata.normalize("NFC", "é"),
            unicodedata.normalize("NFD", "é"),
        ),  # 不做 Unicode 正規化
        ("扭力", "扭 力"),
        ("螺栓固定", "用螺栓來固定"),  # 不做語意推論／子序列
    ],
)
def test_keywords_no_normalization_or_semantic_inference(candidate, text):
    r = rdm.match_meta_keywords([[candidate]], text)
    assert r["qualified"] is False


def test_keywords_literal_match_is_substring_anywhere_exact_case():
    assert rdm.match_meta_keywords([["S45C"]], "xS45Cx")["qualified"] is True


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        [[]],
        [[""]],
        [["a"], []],
        [["a", ""]],
        "abc",
        [["a", 1]],
        [[None]],
        ["a"],
        {"a": 1},
    ],
)
def test_invalid_or_empty_keywords_never_qualify(bad):
    r = rdm.match_meta_keywords(bad, "任何原文 a abc")
    assert r["qualified"] is False and r["reason"] == "invalid_keywords"
    assert r["groups"] == [] and r["detail"]


@pytest.mark.parametrize("bad_text", [None, "", 5, b"x"])
def test_valid_keywords_with_missing_meta_text_raise(bad_text):
    with pytest.raises(rdm.MetaTextMissing):
        rdm.match_meta_keywords([["a"]], bad_text)


# ---------------------------------------------------------------- G'（AC-2、AC-3）


def test_derived_gold_adds_same_owner_meta_once_and_keeps_order():
    assert rdm.derived_gold(["a", "b"], "mfg-001") == ["a", "b", "mfg-001#meta"]
    assert rdm.derived_gold(["a", "mfg-001#meta"], "mfg-001") == ["a", "mfg-001#meta"]
    assert rdm.derived_gold(["a", "a"], "mfg-001") == ["a", "mfg-001#meta"]


KW_OK = [["4 支"], ["9 N·m"]]
TEXT_OK = "底板 4 支螺栓，扭力 9 N·m。"


def test_meta_answerable_qualified_adds_meta_to_gold_without_reordering():
    returned = ["o-1#meta", "o-1#00-0", "o-1#01-0"]
    r = rdm.score_meta_answerable(
        returned,
        ["o-1#00-0"],
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=KW_OK,
        meta_text=TEXT_OK,
        ks=KS,
    )
    assert r["applicable"] and r["qualified"] and r["reason"] == "ok"
    assert r["gold_ids"] == ["o-1#00-0"]  # 原 G 不變
    assert r["derived_gold_ids"] == ["o-1#00-0", "o-1#meta"] and r["meta_added"] is True
    assert r["ranked_ids"] == returned  # 排名不變，meta 仍在第 1 名
    m = r["metrics"]
    assert m["recall@1"] == 0.5 and m["recall@2"] == 1.0 and m["recall@5"] == 1.0
    assert m["mrr"] == 1.0 and m["precision"] == pytest.approx(2 / 3)
    assert r["meta_chunk_id"] == "o-1#meta"
    assert r["keyword_match"]["qualified"] is True


def test_meta_answerable_can_lower_scores_and_original_metrics_are_independent():
    returned = ["o-1#00-0", "o-1#01-0"]  # meta 沒被回傳
    gold = ["o-1#00-0"]
    orig = rm.score_text_case(returned, gold)["metrics"]
    r = rdm.score_meta_answerable(
        returned,
        gold,
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=KW_OK,
        meta_text=TEXT_OK,
        ks=KS,
    )
    m = r["metrics"]
    assert orig["recall@1"] == 1.0 and m["recall@1"] == 0.5  # 集合 Recall 分母變 2：不要求上升
    assert orig["precision"] == 0.5 and m["precision"] == 0.5 and m["mrr"] == 1.0
    assert rm.score_text_case(returned, gold)["metrics"] == orig  # 原函式結果不受影響


def test_meta_answerable_not_qualified_keeps_original_gold_and_values():
    returned = ["o-1#meta", "o-1#00-0"]
    gold = ["o-1#00-0"]
    r = rdm.score_meta_answerable(
        returned,
        gold,
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=[["4 支"], ["沒有的事實"]],
        meta_text=TEXT_OK,
        ks=KS,
    )
    assert r["applicable"] and r["qualified"] is False and r["reason"] == "missing_groups"
    assert r["derived_gold_ids"] == gold and r["meta_added"] is False
    orig = rm.score_text_case(returned, gold)["metrics"]
    for k in ("recall@1", "recall@2", "recall@5", "mrr", "precision"):
        assert r["metrics"][k] == orig[k]


def test_meta_answerable_only_adds_same_owner_meta_not_cross_document():
    returned = ["other#meta", "o-1#00-0"]
    r = rdm.score_meta_answerable(
        returned,
        ["o-1#00-0"],
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=KW_OK,
        meta_text=TEXT_OK,
        ks=KS,
    )
    assert r["derived_gold_ids"] == ["o-1#00-0", "o-1#meta"]
    assert r["metrics"]["recall@5"] == 0.5 and r["metrics"]["mrr"] == 0.5


def test_meta_answerable_meta_already_in_gold_is_not_double_counted():
    r = rdm.score_meta_answerable(
        ["o-1#meta"],
        ["o-1#meta"],
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=KW_OK,
        meta_text=TEXT_OK,
        ks=KS,
    )
    assert r["derived_gold_ids"] == ["o-1#meta"] and r["meta_added"] is False
    assert r["metrics"]["recall@1"] == 1.0


def test_meta_answerable_set_recall_not_hit_rate():
    r = rdm.score_meta_answerable(
        ["g1"],
        ["g1", "g2"],
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=KW_OK,
        meta_text=TEXT_OK,
        ks=KS,
    )
    assert r["derived_gold_ids"] == ["g1", "g2", "o-1#meta"]
    assert r["metrics"]["recall@5"] == pytest.approx(1 / 3)  # hit rate 會是 1.0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (dict(gold=[]), "empty_gold"),  # 空答案不因別的帳號保存了 meta 而可答
        (dict(gold=["g"], owner_visible=False), "owner_not_visible"),
        (dict(gold=["g"], answers_applicable=False), "domain_without_answers"),  # 畫作
    ],
)
def test_meta_answerable_not_applicable_cases_have_null_metrics(kwargs, reason):
    kwargs = {"owner_visible": True, "answers_applicable": True} | kwargs
    gold = kwargs.pop("gold")
    r = rdm.score_meta_answerable(
        ["o-1#meta", "g"],
        gold,
        "o-1",
        keywords=KW_OK,
        meta_text=TEXT_OK,
        ks=KS,
        **kwargs,
    )
    assert r["applicable"] is False and r["reason"] == reason and r["qualified"] is None
    assert r["derived_gold_ids"] is None and set(r["metrics"].values()) == {None}


def test_meta_answerable_refusal_entry_without_keywords_is_not_upgraded():
    r = rdm.score_meta_answerable(
        ["o-1#00-0"],
        ["o-1#00-0"],
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=None,
        meta_text=None,
        ks=KS,
    )
    assert r["applicable"] and r["qualified"] is False and r["reason"] == "no_keywords"
    assert r["derived_gold_ids"] == ["o-1#00-0"]


@pytest.mark.parametrize("bad", [[], [[]], [["a"], []]])
def test_meta_answerable_invalid_keywords_not_all_of_empty_true(bad):
    r = rdm.score_meta_answerable(
        ["o-1#00-0"],
        ["o-1#00-0"],
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=bad,
        meta_text="原文",
        ks=KS,
    )
    assert r["qualified"] is False and r["reason"] == "invalid_keywords"
    assert r["derived_gold_ids"] == ["o-1#00-0"]


def test_meta_answerable_missing_meta_text_is_error_not_false():
    with pytest.raises(rdm.MetaTextMissing):
        rdm.score_meta_answerable(
            ["g"],
            ["g"],
            "o-1",
            owner_visible=True,
            answers_applicable=True,
            keywords=KW_OK,
            meta_text=None,
            ks=KS,
        )


def test_meta_answerable_does_not_mutate_inputs():
    returned, gold, kw = ["o-1#meta", "g"], ["g"], [["4 支"], ["9 N·m"]]
    snap = copy.deepcopy((returned, gold, kw))
    rdm.score_meta_answerable(
        returned,
        gold,
        "o-1",
        owner_visible=True,
        answers_applicable=True,
        keywords=kw,
        meta_text=TEXT_OK,
        ks=KS,
    )
    assert (returned, gold, kw) == snap


# ------------------------------------------------------- derive_case／aggregate_cases（AC-4、AC-6）


def derive(case_id, returned, gold, **kw):
    base = dict(
        owner_visible=True,
        answers_applicable=True,
        keywords=KW_OK,
        meta_text=TEXT_OK,
        ks=KS,
    )
    return rdm.derive_case(case_id, returned, gold, "o-1", **(base | kw))


def test_derive_case_keeps_three_derivations_separate():
    c = derive("c1", ["o-1#meta", "o-1#00-0"], ["o-1#00-0"])
    assert c["case_id"] == "c1" and c["owner_id"] == "o-1"
    # skip_meta：只重排，G 不加 meta
    assert c["skip_meta"]["ranked_ids"] == ["o-1#00-0"] and c["skip_meta"]["metrics"]["mrr"] == 1.0
    # meta_answerable：排名不變、G'＝G∪{meta}
    assert c["meta_answerable"]["ranked_ids"] == ["o-1#meta", "o-1#00-0"]
    assert c["meta_answerable"]["metrics"]["mrr"] == 1.0
    assert c["meta_answerable"]["metrics"]["recall@1"] == 0.5
    # precision_ceiling：原 G、原 R（含 meta）
    assert c["precision_ceiling"]["value"] == pytest.approx(0.5)
    assert (
        c["precision_ceiling"]["gold_count"] == 1 and c["precision_ceiling"]["returned_count"] == 2
    )


def test_aggregate_excludes_null_keeps_zero_and_reports_numerator_denominator():
    cases = [
        derive("a", ["o-1#00-0", "x", "y", "z", "w"], ["o-1#00-0"]),  # ceiling 0.2
        derive("b", [], ["g"]),  # G 非空、R 空：ceiling 0、留在分母
        derive("c", ["x"], []),  # G 空：null、排除
        derive("d", ["g1", "g2"], ["g1", "g2", "g3"]),  # ceiling 1.0
    ]
    agg = rdm.aggregate_cases(cases, KS)
    pc = agg["precision_ceiling"]
    assert pc["denominator"] == 3 and pc["numerator"] == pytest.approx(1.2)
    assert pc["mean"] == pytest.approx(0.4)
    assert agg["n_cases"] == 4 and agg["n_original_answerable"] == 3
    sm = agg["skip_meta"]["metrics"]
    assert sm["recall@1"]["denominator"] == 3  # 空答案不進分母
    assert sm["mrr"]["numerator"] == pytest.approx(1.0 + 0.0 + 1 / 1)  # a:1、b:0、d:1


def test_aggregate_all_empty_gold_gives_null_means():
    agg = rdm.aggregate_cases([derive("x", ["a"], []), derive("y", [], [])], KS)
    assert agg["precision_ceiling"] == {"mean": None, "numerator": 0.0, "denominator": 0}
    assert agg["skip_meta"]["metrics"]["mrr"]["mean"] is None
    assert agg["meta_answerable"]["metrics"]["precision"]["denominator"] == 0
    assert agg["meta_answerable"]["n_applicable"] == 0
    assert agg["meta_answerable"]["not_applicable_reasons"] == {"empty_gold": 2}


def test_aggregate_meta_answerable_counts_qualified_and_art_has_zero_denominator():
    ok = derive("ok", ["o-1#meta", "g"], ["g"])
    bad = derive("bad", ["g"], ["g"], keywords=[["不存在"]])
    agg = rdm.aggregate_cases([ok, bad], KS)["meta_answerable"]
    assert agg["n_applicable"] == 2 and agg["n_qualified"] == 1
    assert agg["qualified_case_ids"] == ["ok"]
    assert agg["metrics"]["recall@1"]["denominator"] == 2
    art = [derive("p", ["g"], ["g"], answers_applicable=False, keywords=None, meta_text=None)]
    art_agg = rdm.aggregate_cases(art, KS)["meta_answerable"]
    assert art_agg["n_applicable"] == 0 and art_agg["n_qualified"] == 0
    assert art_agg["metrics"]["mrr"] == {"mean": None, "numerator": 0.0, "denominator": 0}
    assert art_agg["not_applicable_reasons"] == {"domain_without_answers": 1}


def test_aggregate_does_not_merge_cohorts():
    a = derive("a", ["g"], ["g"])
    b = derive("b", ["o-1#meta", "g"], ["g"])
    only_a = rdm.aggregate_cases([a], KS)
    both = rdm.aggregate_cases([a, b], KS)
    assert only_a["skip_meta"]["metrics"]["mrr"]["mean"] == 1.0
    assert both["skip_meta"]["metrics"]["mrr"]["mean"] == 1.0  # skip_meta 後 b 也排第 1
    assert both["precision_ceiling"]["mean"] == pytest.approx((1.0 + 0.5) / 2)


# ---------------------------------------------------------------- 原文彙集


def entry(chunk_id, text, **loc):
    return {"chunk_id": chunk_id, "text": text, "location": {"case_id": "c", **loc}}


def test_collect_texts_uses_only_valid_nonempty_strings_and_none_never_overrides():
    r = rdm.collect_chunk_texts(
        [
            entry("m#meta", None, i=0),
            entry("m#meta", "原文", i=1),
            entry("m#meta", "", i=2),
            entry("m#meta", None, i=3),
            entry("m#meta", "原文", i=4),
            entry("m#meta", 7, i=5),
            entry("z#00-0", None, i=6),
        ]
    )
    assert r["conflicts"] == []
    assert r["texts"]["m#meta"]["text"] == "原文"
    assert [loc["i"] for loc in r["texts"]["m#meta"]["locations"]] == [1, 4]
    assert "z#00-0" not in r["texts"]
    assert r["omitted"] == 5 and r["valid_sources"] == 2
    assert len(r["texts"]["m#meta"]["sha256"]) == 64


def test_collect_texts_reports_conflicts_with_all_variants_and_locations():
    r = rdm.collect_chunk_texts(
        [entry("m#meta", "甲", i=0), entry("m#meta", "乙", i=1), entry("m#meta", "甲", i=2)]
    )
    assert len(r["conflicts"]) == 1
    c = r["conflicts"][0]
    assert c["chunk_id"] == "m#meta"
    variants = {v["text"]: [loc["i"] for loc in v["locations"]] for v in c["variants"]}
    assert variants == {"甲": [0, 2], "乙": [1]}


def test_collect_texts_whitespace_difference_is_a_conflict():
    r = rdm.collect_chunk_texts([entry("m#meta", "a b", i=0), entry("m#meta", "a  b", i=1)])
    assert len(r["conflicts"]) == 1


@pytest.mark.parametrize("bad", [None, "", 3])
def test_collect_texts_rejects_missing_chunk_id(bad):
    with pytest.raises(ValueError):
        rdm.collect_chunk_texts([entry(bad, "x")])
