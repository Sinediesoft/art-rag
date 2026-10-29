"""檢索層衍生指標的證據測試（TASK-20261029-002 AC-3～AC-9）：只讀已保存的 run、answers、衍生 JSON
與 README，不呼叫模型、不 import app、不讀 .env。

1. 獨立重算：本檔自己從原 run 的逐題 raw_sources、可見 gold、cohort 與 answers 關鍵字重算全部衍生
   數字（不 import eval/retrieval_derived_metrics，也不信任衍生 JSON 的摘要），逐項對照
   eval/runs/retrieval-derived/20261027T184738-52cd-derived.json 與 README 衍生表。
2. 反例：竄改摘要／分子分母／衍生 gold／meta 原文／來源 hash、README 數字，驗證器都要抓到；
   缺原文、有效原文衝突、順序矛盾、缺欄位、answers 不一致時，CLI 的 build_derived 明確失敗，
   並列出受影響題。
3. CLI：拒絕覆寫來源或已存在輸出、不放 eval/runs 根目錄、來源 hash 不符就停；
   只用 stdlib 與兩個純函式庫。

可單檔跑：pytest --noconftest tests/test_retrieval_derived_evidence.py
"""

import ast
import copy
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import derive_retrieval_metrics as cli  # noqa: E402

RUN_REL = "eval/runs/20261027T184738-52cd-retrieval-layer-metrics.json"
ANSWERS_REL = "eval/drawing_retrieval/answers.jsonl"
DERIVED_REL = "eval/runs/retrieval-derived/20261027T184738-52cd-derived.json"
RUN_PATH, ANSWERS_PATH, DERIVED_PATH = (ROOT / p for p in (RUN_REL, ANSWERS_REL, DERIVED_REL))
README = ROOT / "README.md"
RUN_SHA = "834344262d75a9d64c566183b6ebf8c58bc5052e3954e0672e95575a7a620707"
ANSWERS_SHA = "70c249dac1f483f5df43d18aeab602bc38ec60ec22fc2dad126db864fe37bcfa"
EXEC_SHA = "e126b5da8eb9b3e58eaf0b3c484fdbc4997346f6"
RUN_ID = "20261027T184738-52cd"

OLD_END = "<!-- retrieval-layer-table:end -->"
NEW_START, NEW_END = (
    "<!-- retrieval-derived-table:start -->",
    "<!-- retrieval-derived-table:end -->",
)
GROUPS = (
    "art/production_retrieve/specified",
    "art/production_retrieve/global",
    "mfg/production_retrieve/specified/manager",
    "mfg/collection_global_diagnostic_postfilter/global/manager",
    "mfg/production_retrieve/specified/sales",
    "mfg/collection_global_diagnostic_postfilter/global/sales",
)
N_IDS = ("N-T1", "N-T2", "N-T3", "N-T4", "N-T5", "N-T6")
D_IDS = ("D-T1", "D-T2", "D-T3", "D-T4", "D-T5", "D-T6")
CEILINGS = (0.2484126984, 0.2, 0.2193548387, 0.2193548387, 0.24, 0.24)
DENOMS = (21, 21, 31, 31, 10, 10)
META_OCCURRENCES = {
    "mfg-001#meta": 4,
    "mfg-002#meta": 6,
    "mfg-003#meta": 6,
    "mfg-004#meta": 12,
    "mfg-005#meta": 10,
    "mfg-006#meta": 5,
}
KS = (1, 2, 5)
SKIP_NAMES = ("recall@1", "recall@2", "recall@5", "mrr")
META_NAMES = (*SKIP_NAMES, "precision")
HEX64 = re.compile(r"[0-9a-f]{64}")
COLUMNS = (
    "id",
    "orig",
    "from_run",
    "precision",
    "ceiling",
    "skip_meta",
    "meta_answerable",
    "judged",
    "denominators",
    "reason",
    "path",
    "status",
)


# ---------------------------------------------------------------- 讀檔


def load_answers(path: Path = ANSWERS_PATH) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture(scope="module")
def run() -> dict:
    return json.loads(RUN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def answers() -> list[dict]:
    return load_answers()


@pytest.fixture(scope="module")
def derived() -> dict:
    assert DERIVED_PATH.exists(), "衍生 JSON 還沒產生：python eval/derive_retrieval_metrics.py"
    return json.loads(DERIVED_PATH.read_text(encoding="utf-8"))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------ 獨立重算（不用 eval 的衍生函式庫）


def dd(ids):
    out = []
    for x in ids:
        assert isinstance(x, str) and x, f"ID 缺漏：{x!r}"
        if x not in out:
            out.append(x)
    return out


def recall(ranked, gold, k):
    return len(set(ranked[:k]) & set(gold)) / len(gold)


def mrr(ranked, gold):
    for i, x in enumerate(ranked, 1):
        if x in gold:
            return 1 / i
    return 0.0


def precision(returned, gold):
    return len(set(returned) & set(gold)) / len(returned) if returned else 0.0


def ceiling(returned, gold):
    return (
        None if not gold else (min(len(gold), len(returned)) / len(returned) if returned else 0.0)
    )


def macro(values):
    vals = [v for v in values if v is not None]
    return {
        "mean": sum(vals) / len(vals) if vals else None,
        "numerator": float(sum(vals)),
        "denominator": len(vals),
    }


def close(a, b, tol=1e-12) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return (
        isinstance(a, int | float)
        and isinstance(b, int | float)
        and math.isclose(a, b, rel_tol=0, abs_tol=tol)
    )


def answers_by_id(entries):
    by_id = {}
    for e in entries:
        assert e["id"] not in by_id, f"answers 題號重複：{e['id']}"
        by_id[e["id"]] = e
    return by_id


def valid_texts(run):
    """chunk_id → {text: [位置]}；只收非空字串。回 (texts, total, omitted)。"""
    texts = defaultdict(lambda: defaultdict(list))
    total = omitted = 0
    for gi, g in enumerate(run["groups"]):
        for ri, r in enumerate(g["rows"]):
            for si, s in enumerate(r["raw_sources"]):
                total += 1
                t = s.get("text")
                if isinstance(t, str) and t:
                    texts[s["chunk_id"]][t].append(
                        {
                            "group_id": g["group_id"],
                            "group_index": gi,
                            "case_id": r["case_id"],
                            "row_index": ri,
                            "raw_sources_index": si,
                        }
                    )
                else:
                    omitted += 1
    return texts, total, omitted


def keyword_judgement(keywords, text):
    """每個 AND 組至少一個候選逐字（區分大小寫）出現在 text；回 (合格, 未滿足組, 各組命中)。"""
    hits, missing = [], []
    for i, group in enumerate(keywords):
        found = next(((c, text.find(c)) for c in group if c in text), None)
        hits.append(found)
        if found is None:
            missing.append(i)
    return not missing, missing, hits


def expected_rows(run, answers):
    """逐題的獨立期望值：{(group_id, case_id): {...}}，以及 meta 原文（衝突時 AssertionError）。"""
    by_ans = answers_by_id(answers)
    texts, total, omitted = valid_texts(run)
    for cid, variants in texts.items():
        assert len(variants) == 1, f"{cid} 有 {len(variants)} 種有效原文"
    meta_text = {cid: next(iter(v)) for cid, v in texts.items() if cid.endswith("#meta")}
    out = {}
    for g in run["groups"]:
        for r in g["rows"]:
            ids = dd(s["chunk_id"] for s in r["raw_sources"])
            gold = dd(r["gold_chunk_ids"])
            ranked = [x for x in ids if not x.endswith("#meta")]
            exp = {
                "ids": ids,
                "gold": gold,
                "ranked": ranked,
                "skip": {
                    **{f"recall@{k}": recall(ranked, gold, k) if gold else None for k in KS},
                    "mrr": mrr(ranked, gold) if gold else None,
                },
                "ceiling": ceiling(ids, gold),
                "orig": {
                    **{f"recall@{k}": recall(ids, gold, k) if gold else None for k in KS},
                    "mrr": mrr(ids, gold) if gold else None,
                    "precision": precision(ids, gold) if gold else None,
                },
            }
            meta = f"{r['owner_id']}#meta"
            if g["domain"] != "mfg":
                exp["meta"] = {"applicable": False, "reason": "domain_without_answers"}
            elif not gold:
                exp["meta"] = {"applicable": False, "reason": "empty_gold"}
            elif not r["owner_visible"]:
                exp["meta"] = {"applicable": False, "reason": "owner_not_visible"}
            else:
                a = by_ans[r["case_id"]]
                if a.get("expect_refusal"):
                    q, reason, missing, hits, final = False, "no_keywords", [], None, gold
                else:
                    q, missing, hits = keyword_judgement(a["keywords"], meta_text[meta])
                    reason = "ok" if q else "missing_groups"
                    final = [*gold, meta] if q and meta not in gold else gold
                exp["meta"] = {
                    "applicable": True,
                    "qualified": q,
                    "reason": reason,
                    "missing": missing,
                    "hits": hits,
                    "final": final,
                    "metrics": {
                        **{f"recall@{k}": recall(ids, final, k) for k in KS},
                        "mrr": mrr(ids, final),
                        "precision": precision(ids, final),
                    },
                }
            out[(g["group_id"], r["case_id"])] = exp
    return out, meta_text, texts, total, omitted


def expected_summary(run, rows):
    """每組、每個 cohort（沿用保存的成員）的獨立彙整。"""
    out = {}
    for g in run["groups"]:
        by_case = {r["case_id"]: rows[(g["group_id"], r["case_id"])] for r in g["rows"]}
        cohorts = {}
        for name, c in run["summary"][g["group_id"]]["cohorts"].items():
            ex = [by_case[i] for i in c["case_ids"]]
            meta = [e["meta"] for e in ex]
            applicable = [m for m in meta if m["applicable"]]
            cohorts[name] = {
                "n_cases": len(ex),
                "n_original_answerable": sum(bool(e["gold"]) for e in ex),
                "orig": {n: macro(e["orig"][n] for e in ex) for n in META_NAMES},
                "ceiling": macro(e["ceiling"] for e in ex),
                "skip": {n: macro(e["skip"][n] for e in ex) for n in SKIP_NAMES},
                "meta": {
                    n: macro(m["metrics"][n] if m["applicable"] else None for m in meta)
                    for n in META_NAMES
                },
                "n_applicable": len(applicable),
                "n_qualified": sum(m["qualified"] for m in applicable),
                "reasons": dict(
                    sorted(Counter(m["reason"] for m in meta if not m["applicable"]).items())
                ),
            }
        out[g["group_id"]] = cohorts
    return out


# ---------------------------------------------------------------- 衍生 JSON 驗證器


def cmp_macro(p, label, got, want):
    if not isinstance(got, dict) or set(got) != {"mean", "numerator", "denominator"}:
        p.append(f"{label}：不是 mean／numerator／denominator 結構")
        return
    if got["denominator"] != want["denominator"]:
        p.append(f"{label}：分母 {got['denominator']} ≠ 重算 {want['denominator']}")
    if not close(got["numerator"], want["numerator"]):
        p.append(f"{label}：分子 {got['numerator']} ≠ 重算 {want['numerator']}")
    if not close(got["mean"], want["mean"]):
        p.append(f"{label}：mean {got['mean']} ≠ 重算 {want['mean']}")


def verify_derived(d, run, answers, run_sha, answers_sha) -> list[str]:  # noqa: C901
    p: list[str] = []
    for key, want in (
        ("schema_version", "retrieval-derived-metrics/1"),
        ("kind", "retrieval-derived-metrics"),
        ("rule_version", "retrieval-derived-rules/1"),
        ("status", "derived_from_run"),
        ("real", False),
        ("is_new_model_run", False),
    ):
        if d.get(key) != want:
            p.append(f"{key}：{d.get(key)!r} ≠ {want!r}")
    src = d.get("derived_from_run") or {}
    for key, want in (
        ("run_id", run["run_id"]),
        ("sha256", run_sha),
        ("exec_sha", run["provenance"]["code"]["head"]),
        ("provenance", run["provenance"]),
        ("embed_mode", run["embed_mode"]),
    ):
        if src.get(key) != want:
            p.append(f"derived_from_run.{key} 與來源 run 不符")
    if not src.get("path", "").startswith("eval/runs/") or "/retrieval-derived/" in src["path"]:
        p.append(f"derived_from_run.path 不是來源 run 路徑：{src.get('path')}")
    ans = d.get("answers") or {}
    if ans.get("sha256") != answers_sha:
        p.append("answers.sha256 與 answers 檔不符")
    if ans.get("case_ids") != [a["id"] for a in answers]:
        p.append("answers.case_ids 與 answers 檔不符")
    prog = (d.get("derivation") or {}).get("program") or {}
    for part in ("cli", "lib"):
        if not HEX64.fullmatch((prog.get(part) or {}).get("sha256", "")):
            p.append(f"derivation.program.{part}.sha256 不是 SHA256")
    deriv = d.get("derivation") or {}
    if deriv.get("rule_version") != d.get("rule_version"):
        p.append("derivation.rule_version 與頂層不一致")
    if "derive_retrieval_metrics.py" not in deriv.get("command", "") or not deriv.get("created_at"):
        p.append("derivation 缺指令或時間")
    if deriv.get("offline") is not True:
        p.append("derivation.offline 必須為 true")

    try:
        rows, meta_text, texts, total, omitted = expected_rows(run, answers)
    except AssertionError as e:
        return [*p, f"來源重算失敗：{e}"]
    chk = d.get("chunk_texts") or {}
    for key, want in (
        ("total_sources", total),
        ("omitted_text_sources", omitted),
        ("valid_text_sources", total - omitted),
        ("chunks_with_text", len(texts)),
        ("conflicts", []),
    ):
        if chk.get(key) != want:
            p.append(f"chunk_texts.{key}：{chk.get(key)!r} ≠ 重算 {want!r}")
    mt = d.get("meta_texts") or {}
    if set(mt) != set(meta_text):
        p.append("meta_texts 的 chunk 集合與來源有效原文不符")
    for cid, text in meta_text.items():
        e = mt.get(cid) or {}
        locs = next(iter(texts[cid].values()))
        if e.get("text") != text or e.get("sha256") != hashlib.sha256(text.encode()).hexdigest():
            p.append(f"meta_texts[{cid}]：原文或 sha256 與來源不符")
        if e.get("valid_source_count") != len(locs) or e.get("sources") != locs:
            p.append(f"meta_texts[{cid}]：來源位置（group／case／raw_sources）與來源 run 不符")

    groups = d.get("groups") or []
    if [g.get("group_id") for g in groups] != [g["group_id"] for g in run["groups"]]:
        return [*p, "groups 的順序或組別與來源 run 不同"]
    for gi, (g, sg) in enumerate(zip(groups, run["groups"], strict=True)):
        gid = g["group_id"]
        if g.get("source_position") != f"groups[{gi}]":
            p.append(f"{gid}：source_position 不符")
        if [r.get("case_id") for r in g["rows"]] != [r["case_id"] for r in sg["rows"]]:
            p.append(f"{gid}：逐題順序或題數與來源不同")
            continue
        for ri, (r, sr) in enumerate(zip(g["rows"], sg["rows"], strict=True)):
            lab = f"{gid}/{r['case_id']}"
            e = rows[(gid, r["case_id"])]
            if r.get("source_position") != {"group_index": gi, "row_index": ri}:
                p.append(f"{lab}：source_position 不符")
            if r["owner_id"] != sr["owner_id"] or r["owner_visible"] != sr["owner_visible"]:
                p.append(f"{lab}：owner／可見性與來源不符")
            if r["original"]["gold_ids"] != e["gold"]:
                p.append(f"{lab}：原 G 與來源可見 gold 不符")
            if [x["chunk_id"] for x in r["original"]["returned"]] != e["ids"]:
                p.append(f"{lab}：有序回傳與來源不符")
            first_score = {}
            for x in sr["returned"]:
                first_score.setdefault(x["chunk_id"], x["score"])
            if [x["score"] for x in r["original"]["returned"]] != [
                first_score[i] for i in e["ids"]
            ]:
                p.append(f"{lab}：原 score 與來源不符")
            if r["original"]["metrics"] != sr["score"]["metrics"]:
                p.append(f"{lab}：原指標沒有原樣保留")
            sk = r["skip_meta"]
            if sk["ranked_ids"] != e["ranked"] or sk["gold_ids"] != e["gold"]:
                p.append(f"{lab}：skip_meta 排名或 G 與重算不符")
            for n in SKIP_NAMES:
                if not close(sk["metrics"][n], e["skip"][n]):
                    p.append(f"{lab}：skip_meta {n} {sk['metrics'][n]} ≠ 重算 {e['skip'][n]}")
            pc = r["precision_ceiling"]
            if not close(pc["value"], e["ceiling"]) or pc["returned_count"] != len(e["ids"]):
                p.append(f"{lab}：precision_ceiling 與重算不符")
            if pc["gold_count"] != len(e["gold"]):
                p.append(f"{lab}：precision_ceiling.gold_count 與重算不符")
            ma, em = r["meta_answerable"], e["meta"]
            if ma["applicable"] != em["applicable"] or ma["reason"] != em["reason"]:
                p.append(
                    f"{lab}：meta_answerable 適用性／原因 {ma['reason']} ≠ 重算 {em['reason']}"
                )
                continue
            if not em["applicable"]:
                if set(ma["metrics"].values()) != {None} or ma["derived_gold_ids"] is not None:
                    p.append(f"{lab}：不適用題的衍生指標必須是 null")
                continue
            if ma["qualified"] != em["qualified"] or ma["derived_gold_ids"] != em["final"]:
                p.append(f"{lab}：meta 判定或 G' 與重算不符")
            if ma["gold_ids"] != e["gold"] or ma["ranked_ids"] != e["ids"]:
                p.append(f"{lab}：meta_answerable 的原 G／排名被改動")
            for n in META_NAMES:
                if not close(ma["metrics"][n], em["metrics"][n]):
                    p.append(
                        f"{lab}：meta_answerable {n} {ma['metrics'][n]} ≠ 重算 {em['metrics'][n]}"
                    )
            km = ma.get("keyword_match")
            if em["reason"] == "no_keywords":
                if km is not None:
                    p.append(f"{lab}：拒答題不應有 keyword_match")
                continue
            if km["missing_group_indexes"] != em["missing"]:
                p.append(f"{lab}：缺漏事實組與重算不符")
            got = [
                None if grp["hit"] is None else (grp["hit"], grp["hit_position"])
                for grp in km["groups"]
            ]
            if got != [None if h is None else tuple(h) for h in em["hits"]]:
                p.append(f"{lab}：命中字串／位置與重算不符")
            ref = ma.get("meta_text_ref") or {}
            meta = f"{r['owner_id']}#meta"
            locs = next(iter(texts[meta].values()))
            if ref.get("chunk_id") != meta or ref.get("sha256") != d["meta_texts"][meta]["sha256"]:
                p.append(f"{lab}：meta_text_ref 與 meta_texts 不符")
            if ref.get("first_source") != locs[0]:
                p.append(f"{lab}：meta_text_ref.first_source 與來源位置不符")

    want_summary = expected_summary(run, rows)
    got_summary = d.get("summary") or {}
    if list(got_summary) != list(want_summary):
        p.append("summary 的組別順序與來源不同")
    for gid, cohorts in want_summary.items():
        got_c = (got_summary.get(gid) or {}).get("cohorts") or {}
        if list(got_c) != list(cohorts):
            p.append(f"{gid}：cohort 集合與來源不同")
            continue
        for name, w in cohorts.items():
            g = got_c[name]
            lab = f"{gid}.{name}"
            if g.get("case_ids") != run["summary"][gid]["cohorts"][name]["case_ids"]:
                p.append(f"{lab}：cohort 成員與來源保存的不同")
            if g.get("n_cases") != w["n_cases"]:
                p.append(f"{lab}：n_cases {g.get('n_cases')} ≠ {w['n_cases']}")
            if g.get("n_original_answerable") != w["n_original_answerable"]:
                p.append(f"{lab}：n_original_answerable 與重算不符")
            cmp_macro(p, f"{lab}.precision_ceiling", g["precision_ceiling"], w["ceiling"])
            for n in META_NAMES:
                cmp_macro(p, f"{lab}.original_metrics.{n}", g["original_metrics"][n], w["orig"][n])
                cmp_macro(
                    p,
                    f"{lab}.meta_answerable.{n}",
                    g["meta_answerable"]["metrics"][n],
                    w["meta"][n],
                )
            for n in SKIP_NAMES:
                cmp_macro(p, f"{lab}.skip_meta.{n}", g["skip_meta"]["metrics"][n], w["skip"][n])
            ma = g["meta_answerable"]
            for key, want in (
                ("n_applicable", w["n_applicable"]),
                ("n_qualified", w["n_qualified"]),
                ("not_applicable_reasons", w["reasons"]),
            ):
                if ma.get(key) != want:
                    p.append(f"{lab}.meta_answerable.{key}：{ma.get(key)!r} ≠ 重算 {want!r}")
            if len(ma.get("qualified_case_ids", [])) != w["n_qualified"]:
                p.append(f"{lab}.meta_answerable.qualified_case_ids 與合格題數不符")
    return p


def check(d, run, answers) -> list[str]:
    return verify_derived(d, run, answers, RUN_SHA, ANSWERS_SHA)


# ---------------------------------------------------------------- 真產物


def test_source_files_are_the_pinned_ones_and_untouched():
    assert sha256_of(RUN_PATH) == RUN_SHA, "既有 run 被改動"
    assert sha256_of(ANSWERS_PATH) == ANSWERS_SHA, "草稿 1 answers 被改動"


def test_saved_derived_json_matches_independent_recomputation(derived, run, answers):
    problems = check(derived, run, answers)
    assert problems == [], "\n".join(problems[:30])


def test_saved_derived_json_records_provenance_and_is_not_a_new_real_run(derived, run):
    src = derived["derived_from_run"]
    assert (src["run_id"], src["path"], src["sha256"]) == (RUN_ID, RUN_REL, RUN_SHA)
    assert src["exec_sha"] == EXEC_SHA == run["provenance"]["code"]["head"]
    assert src["provenance"] == run["provenance"]  # 原 provenance 原樣揭露
    assert derived["answers"]["path"] == ANSWERS_REL and derived["answers"]["sha256"] == ANSWERS_SHA
    assert derived["status"] == "derived_from_run" and derived["real"] is False
    assert derived["is_new_model_run"] is False
    prog = derived["derivation"]["program"]
    assert prog["cli"]["path"] == "eval/derive_retrieval_metrics.py"
    assert prog["lib"]["path"] == "eval/retrieval_derived_metrics.py"
    assert derived["derivation"]["offline"] is True
    assert DERIVED_PATH.parent.name == "retrieval-derived"
    assert DERIVED_PATH.parent.parent == ROOT / "eval" / "runs"  # 子目錄，不在 runs 根目錄


def test_six_groups_ceiling_and_denominators_at_full_precision(derived):
    assert tuple(g["group_id"] for g in derived["groups"]) == GROUPS
    for gid, want, den in zip(GROUPS, CEILINGS, DENOMS, strict=True):
        pc = derived["summary"][gid]["cohorts"]["all"]["precision_ceiling"]
        assert pc["denominator"] == den, gid
        assert abs(pc["mean"] - want) < 5e-11, (gid, pc["mean"])


def test_manager_and_sales_specified_ceiling_equal_original_precision(derived):
    manager = derived["summary"][GROUPS[2]]["cohorts"]["all"]
    sales = derived["summary"][GROUPS[4]]["cohorts"]["all"]
    assert manager["precision_ceiling"]["numerator"] == pytest.approx(6.8)
    assert sales["precision_ceiling"]["numerator"] == pytest.approx(2.4)
    for c in (manager, sales):
        assert c["precision_ceiling"]["mean"] == pytest.approx(
            c["original_metrics"]["precision"]["mean"]
        )


def test_group_rows_cover_every_saved_case_without_dropping_any(derived, run):
    assert sum(len(g["rows"]) for g in derived["groups"]) == 176
    for g, sg in zip(derived["groups"], run["groups"], strict=True):
        assert [r["case_id"] for r in g["rows"]] == [r["case_id"] for r in sg["rows"]]
    assert derived["chunk_texts"]["total_sources"] == 759
    assert derived["chunk_texts"]["omitted_text_sources"] == 329
    assert derived["chunk_texts"]["chunks_with_text"] == 54
    assert derived["chunk_texts"]["conflicts"] == []


def test_meta_texts_cover_six_mfg_metas_from_production_with_exact_counts(derived):
    mt = derived["meta_texts"]
    for cid, n in META_OCCURRENCES.items():
        assert mt[cid]["valid_source_count"] == n, cid
        assert mt[cid]["text"], cid
        assert all(s["group_id"].startswith("mfg/production_retrieve/") for s in mt[cid]["sources"])
        assert {s["group_id"] for s in mt[cid]["sources"]} <= {GROUPS[2], GROUPS[4]}


def test_meta_answerable_full_match_is_only_d_mfg003_02_and_manager_only(derived):
    qualified = {
        gid: derived["summary"][gid]["cohorts"]["all"]["meta_answerable"]["qualified_case_ids"]
        for gid in GROUPS
    }
    assert qualified[GROUPS[2]] == ["d-mfg003-02"] and qualified[GROUPS[3]] == ["d-mfg003-02"]
    assert qualified[GROUPS[4]] == [] and qualified[GROUPS[5]] == []
    assert qualified[GROUPS[0]] == qualified[GROUPS[1]] == []
    for gid in GROUPS[:2]:  # 畫作：不適用、分母 0
        ma = derived["summary"][gid]["cohorts"]["all"]["meta_answerable"]
        assert ma["n_applicable"] == 0 and ma["not_applicable_reasons"] == {
            "domain_without_answers": 22
        }
        assert all(m["denominator"] == 0 and m["mean"] is None for m in ma["metrics"].values())


def test_unqualified_cases_are_not_specially_approved(derived):
    rows = {
        r["case_id"]: r
        for r in next(g for g in derived["groups"] if g["group_id"] == GROUPS[2])["rows"]
    }
    a = rows["d-mfg002-02"]["meta_answerable"]
    assert a["qualified"] is False and a["keyword_match"]["missing_group_indexes"] == [1]
    assert [g["candidates"][0]["text"] for g in a["keyword_match"]["groups"]] == [
        "S45C",
        "HRC 22–28",
    ]
    b = rows["d-mfg004-03"]["meta_answerable"]
    assert b["qualified"] is False and b["keyword_match"]["missing_group_indexes"] == [0, 1]
    assert [g["candidates"][0]["text"] for g in b["keyword_match"]["groups"]] == [
        "10±0.05 mm",
        "平面度 0.1 mm",
    ]
    c = rows["d-mfg003-02"]["meta_answerable"]
    assert c["qualified"] and c["derived_gold_ids"][-1] == "mfg-003#meta" and c["meta_added"]


def test_hidden_unknown_no_answer_never_become_answerable(derived):
    for gid in GROUPS[2:]:
        g = next(x for x in derived["groups"] if x["group_id"] == gid)
        for r in g["rows"]:
            if r["status"] == "expected_empty":
                ma = r["meta_answerable"]
                assert ma["applicable"] is False and ma["reason"] == "empty_gold", (
                    gid,
                    r["case_id"],
                )
                assert ma["derived_gold_ids"] is None and r["skip_meta"]["applicable"] is False
                assert r["precision_ceiling"]["value"] is None


def first_rank_facts(run, gid):
    g = next(x for x in run["groups"] if x["group_id"] == gid)
    first_not_one, top1_meta, recall1_lt1 = [], [], []
    for r in g["rows"]:
        if not r["gold_chunk_ids"]:
            continue
        ids = dd(s["chunk_id"] for s in r["raw_sources"])
        gold = set(r["gold_chunk_ids"])
        first = next((i for i, x in enumerate(ids, 1) if x in gold), None)
        if first != 1:
            first_not_one.append(r["case_id"])
            if ids and ids[0] == f"{r['owner_id']}#meta":
                top1_meta.append(r["case_id"])
        if len(set(ids[:1]) & gold) / len(gold) < 1:
            recall1_lt1.append(r["case_id"])
    return first_not_one, top1_meta, recall1_lt1


def test_background_facts_first_relevant_not_first_versus_recall_at_1(run):
    spec = first_rank_facts(run, GROUPS[2])
    diag = first_rank_facts(run, GROUPS[3])
    assert (len(spec[0]), len(spec[1])) == (17, 16)
    assert (len(diag[0]), len(diag[1])) == (17, 15)
    assert set(spec[1]) - set(diag[1]) == {"d-mfg002-01"}
    assert len(spec[2]) == len(diag[2]) == 19  # Recall@1 < 1
    assert set(spec[2]) - set(spec[0]) == {
        "d-mfg003-m1",
        "d-mfg005-m1",
    }  # 多正解 rank1 但 Recall=.5
    assert set(diag[2]) - set(diag[0]) == {"d-mfg003-m1", "d-mfg005-m1"}


# ---------------------------------------------------------------- 驗證器反例（竄改衍生 JSON）


@pytest.fixture
def mutable(derived):
    return copy.deepcopy(derived)


def group(d, gid):
    return next(g for g in d["groups"] if g["group_id"] == gid)


def row(d, gid, case_id):
    return next(r for r in group(d, gid)["rows"] if r["case_id"] == case_id)


def must_fail(d, run, answers, needle):
    problems = check(d, run, answers)
    assert any(needle in x for x in problems), (needle, problems[:5])


def test_verifier_catches_tampered_summary_mean_numerator_denominator(mutable, run, answers):
    pc = mutable["summary"][GROUPS[2]]["cohorts"]["all"]["precision_ceiling"]
    pc["mean"] = 0.25
    must_fail(mutable, run, answers, "precision_ceiling：mean")
    pc["mean"] = 6.8 / 31
    pc["denominator"] = 30
    must_fail(mutable, run, answers, "precision_ceiling：分母")
    pc["denominator"] = 31
    pc["numerator"] = 7.0
    must_fail(mutable, run, answers, "precision_ceiling：分子")


def test_verifier_catches_tampered_skip_meta_and_meta_answerable_summary(mutable, run, answers):
    c = mutable["summary"][GROUPS[2]]["cohorts"]["all"]
    c["skip_meta"]["metrics"]["recall@1"]["mean"] += 0.01
    must_fail(mutable, run, answers, "skip_meta.recall@1")
    c["meta_answerable"]["metrics"]["mrr"]["denominator"] = 1
    must_fail(mutable, run, answers, "meta_answerable.mrr")
    c["meta_answerable"]["n_qualified"] = 2
    must_fail(mutable, run, answers, "n_qualified")


def test_verifier_catches_hit_rate_instead_of_set_recall(mutable, run, answers):
    """多正解題把集合 Recall 改成 hit rate（任一命中算 1）必須被抓到。"""
    r = row(mutable, GROUPS[2], "d-mfg003-m1")
    assert r["meta_answerable"]["metrics"]["recall@1"] < 1
    r["meta_answerable"]["metrics"]["recall@1"] = 1.0
    must_fail(mutable, run, answers, "d-mfg003-m1：meta_answerable recall@1")


def test_verifier_catches_forged_meta_gold_and_judgement(mutable, run, answers):
    r = row(mutable, GROUPS[2], "d-mfg002-02")
    r["meta_answerable"]["qualified"] = True
    r["meta_answerable"]["derived_gold_ids"].append("mfg-002#meta")
    must_fail(mutable, run, answers, "d-mfg002-02：meta 判定或 G'")


def test_verifier_catches_tampered_original_gold_and_original_metrics(mutable, run, answers):
    r = row(mutable, GROUPS[2], "d-mfg001-01")
    r["original"]["gold_ids"] = [*r["original"]["gold_ids"], "mfg-001#meta"]
    must_fail(mutable, run, answers, "原 G 與來源可見 gold 不符")
    r["original"]["gold_ids"] = r["original"]["gold_ids"][:-1]
    r["original"]["metrics"]["precision"] = 1.0
    must_fail(mutable, run, answers, "原指標沒有原樣保留")


def test_verifier_catches_tampered_meta_text_and_sources(mutable, run, answers):
    mutable["meta_texts"]["mfg-003#meta"]["text"] += "。"
    must_fail(mutable, run, answers, "meta_texts[mfg-003#meta]：原文或 sha256")
    mutable["meta_texts"]["mfg-003#meta"]["text"] = mutable["meta_texts"]["mfg-003#meta"]["text"][
        :-1
    ]
    mutable["meta_texts"]["mfg-004#meta"]["sources"][0]["raw_sources_index"] += 1
    must_fail(mutable, run, answers, "meta_texts[mfg-004#meta]：來源位置")


def test_verifier_catches_tampered_hash_provenance_and_answers(mutable, run, answers):
    mutable["derived_from_run"]["sha256"] = "0" * 64
    must_fail(mutable, run, answers, "derived_from_run.sha256")
    mutable["derived_from_run"]["sha256"] = RUN_SHA
    mutable["derived_from_run"]["provenance"]["code"]["head"] = "f" * 40
    must_fail(mutable, run, answers, "derived_from_run.provenance")
    mutable["answers"]["sha256"] = "1" * 64
    must_fail(mutable, run, answers, "answers.sha256")


def test_verifier_catches_wrong_source_hash_arguments(derived, run, answers):
    problems = verify_derived(derived, run, answers, "2" * 64, ANSWERS_SHA)
    assert any("derived_from_run.sha256" in x for x in problems)
    problems = verify_derived(derived, run, answers, RUN_SHA, "3" * 64)
    assert any("answers.sha256" in x for x in problems)


def test_verifier_catches_status_claiming_new_real_run(mutable, run, answers):
    mutable["status"] = "real_run"
    mutable["real"] = True
    must_fail(mutable, run, answers, "status")
    must_fail(mutable, run, answers, "real")


def test_verifier_catches_dropped_case_and_changed_cohort_members(mutable, run, answers):
    mutable["groups"][2]["rows"].pop()
    must_fail(mutable, run, answers, "逐題順序或題數")
    d = copy.deepcopy(mutable)
    d["groups"][2]["rows"] = [*mutable["groups"][2]["rows"], mutable["groups"][3]["rows"][-1]]
    d["summary"][GROUPS[2]]["cohorts"]["all"]["case_ids"].pop()
    must_fail(d, run, answers, "cohort 成員")


def test_verifier_catches_skip_meta_ranking_tamper(mutable, run, answers):
    r = row(mutable, GROUPS[2], "d-mfg001-01")
    r["skip_meta"]["ranked_ids"] = [x["chunk_id"] for x in r["original"]["returned"]]  # 沒略過 meta
    must_fail(mutable, run, answers, "skip_meta 排名")


def test_verifier_catches_source_with_conflicting_text(derived, run, answers):
    bad = copy.deepcopy(run)
    first = True
    for g in bad["groups"]:
        for r in g["rows"]:
            for s in r["raw_sources"]:
                if s["chunk_id"] == "mfg-002#meta" and s.get("text") and first:
                    s["text"] += "（改）"
                    first = False
    problems = check(derived, bad, answers)
    assert any("來源重算失敗" in x and "mfg-002#meta" in x for x in problems)


# ---------------------------------------------------------------- CLI：缺文、衝突、矛盾都明確失敗


def build(run, answers):
    return cli.build_derived(
        run,
        answers,
        run_path=RUN_REL,
        run_sha256=RUN_SHA,
        answers_path=ANSWERS_REL,
        answers_sha256=ANSWERS_SHA,
        program={
            "cli": {"path": "eval/derive_retrieval_metrics.py", "sha256": "a" * 64},
            "lib": {"path": "eval/retrieval_derived_metrics.py", "sha256": "b" * 64},
        },
        command="python eval/derive_retrieval_metrics.py",
        argv=[],
        created_at="2026-10-29T00:00:00+00:00",
    )


def fail_message(run, answers) -> str:
    with pytest.raises(cli.DerivationError) as e:
        build(run, answers)
    return "\n".join(e.value.problems)


def each_source(run, chunk_id):
    for g in run["groups"]:
        for r in g["rows"]:
            for s in r["raw_sources"]:
                if s["chunk_id"] == chunk_id:
                    yield g, r, s


def test_build_from_real_data_equals_saved_derived_json(derived, run, answers):
    d = build(run, answers)
    assert check(d, run, answers) == []
    for key in ("groups", "summary", "meta_texts", "chunk_texts", "derived_from_run", "answers"):
        assert d[key] == derived[key], key


def test_build_fails_listing_cases_when_required_meta_text_missing(run, answers):
    bad = copy.deepcopy(run)
    for _, _, s in each_source(bad, "mfg-003#meta"):
        s["text"] = None  # 診斷本來就省略，再把 production 的也拿掉
    msg = fail_message(bad, answers)
    assert "mfg-003#meta" in msg and "缺" in msg
    for case_id in ("d-mfg003-02", "d-mfg003-01"):
        assert case_id in msg
    assert "d-mfg001-01" not in msg  # 只列受影響題


def test_build_fails_listing_affected_cases_on_conflicting_valid_texts(run, answers):
    bad = copy.deepcopy(run)
    g, r, s = next(each_source(bad, "mfg-002#meta"))
    s["text"] = s["text"] + "（不同版本）"
    msg = fail_message(bad, answers)
    assert "衝突" in msg and "mfg-002#meta" in msg
    assert "d-mfg002-01" in msg and r["case_id"] in msg and g["group_id"] in msg


def test_omitted_diagnostic_text_is_neither_a_conflict_nor_overwrites(run, answers):
    d = build(run, answers)
    assert d["meta_texts"]["mfg-004#meta"]["valid_source_count"] == 12
    diag_ids = {g["group_id"] for g in run["groups"] if g["mode"].startswith("collection_global")}
    assert all(s["group_id"] not in diag_ids for s in d["meta_texts"]["mfg-004#meta"]["sources"])
    bad = copy.deepcopy(run)
    for _, _, s in each_source(bad, "mfg-004#meta"):
        if s.get("text"):
            s["text"] = ""  # 空字串同樣視為省略，不算有效原文
    assert "mfg-004#meta" in fail_message(bad, answers)


def test_build_fails_on_order_contradiction_between_raw_sources_and_returned(run, answers):
    bad = copy.deepcopy(run)
    r = bad["groups"][2]["rows"][0]
    r["raw_sources"][0], r["raw_sources"][1] = r["raw_sources"][1], r["raw_sources"][0]
    msg = fail_message(bad, answers)
    assert "d-mfg001-01" in msg and "順序" in msg


def test_build_fails_on_non_sequential_rank(run, answers):
    bad = copy.deepcopy(run)
    bad["groups"][2]["rows"][3]["returned"][1]["rank"] = 5
    assert "d-mfg001-04" in fail_message(bad, answers)


@pytest.mark.parametrize(
    "field", ["gold_chunk_ids", "raw_sources", "returned", "score", "owner_id", "owner_visible"]
)
def test_build_fails_listing_case_when_required_field_missing(run, answers, field):
    bad = copy.deepcopy(run)
    del bad["groups"][2]["rows"][5][field]
    msg = fail_message(bad, answers)
    assert "d-mfg002-02" in msg and field in msg


def test_build_fails_when_saved_original_score_is_inconsistent(run, answers):
    bad = copy.deepcopy(run)
    bad["groups"][2]["rows"][0]["score"]["metrics"]["recall@1"] = 1.0
    assert "d-mfg001-01" in fail_message(bad, answers)


def test_build_fails_when_a_case_has_error_instead_of_silently_shrinking_denominator(run, answers):
    bad = copy.deepcopy(run)
    bad["groups"][2]["rows"][0]["error"] = "boom"
    assert "d-mfg001-01" in fail_message(bad, answers)


def test_build_fails_when_cohort_membership_contradicts_rows(run, answers):
    bad = copy.deepcopy(run)
    bad["summary"][GROUPS[2]]["cohorts"]["owner_only"]["case_ids"].append("d-mfg001-01")
    assert "owner_only" in fail_message(bad, answers)


def test_build_fails_when_group_summary_original_metric_is_tampered(run, answers):
    bad = copy.deepcopy(run)
    bad["summary"][GROUPS[2]]["cohorts"]["all"]["answerable"]["metrics"]["precision"][
        "numerator"
    ] = 7
    assert "precision" in fail_message(bad, answers)


def test_build_fails_when_answers_are_missing_extra_duplicate_or_invalid(run, answers):
    missing = [a for a in answers if a["id"] != "d-mfg002-02"]
    assert "d-mfg002-02" in fail_message(run, missing)
    extra = [*answers, {"id": "d-mfg999-01", "keywords": [["x"]]}]
    assert "d-mfg999-01" in fail_message(run, extra)
    dup = [*answers, answers[0]]
    assert answers[0]["id"] in fail_message(run, dup)
    for bad_kw in ([], [[]], [[""]], "abc"):
        bad = [dict(a, keywords=bad_kw) if a["id"] == "d-mfg001-01" else a for a in answers]
        assert "d-mfg001-01" in fail_message(run, bad)


def test_build_does_not_mutate_inputs(run, answers):
    snap = copy.deepcopy((run, answers))
    build(run, answers)
    assert (run, answers) == snap


# ---------------------------------------------------------------- CLI main：覆寫、來源、離線


def test_main_writes_reproducible_json_then_refuses_to_overwrite(tmp_path, derived, run, answers):
    out = tmp_path / "out" / "d.json"
    assert cli.main(["--out", str(out)]) == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert check(saved, run, answers) == []
    for key in ("groups", "summary", "meta_texts", "chunk_texts", "derived_from_run", "answers"):
        assert saved[key] == derived[key], key
    before = out.read_bytes()
    assert cli.main(["--out", str(out)]) == 2
    assert out.read_bytes() == before


def test_main_refuses_output_equal_to_a_source_or_existing_file(derived):  # derived：輸出已存在
    before = (sha256_of(RUN_PATH), sha256_of(ANSWERS_PATH))
    assert cli.main(["--out", str(RUN_PATH)]) == 2
    assert cli.main(["--out", str(ANSWERS_PATH)]) == 2
    assert cli.main(["--out", str(DERIVED_PATH)]) == 2  # 已存在的正式輸出
    assert (sha256_of(RUN_PATH), sha256_of(ANSWERS_PATH)) == before


def test_main_refuses_output_in_runs_root(tmp_path):
    target = ROOT / "eval" / "runs" / "zz-should-not-exist-derived.json"
    assert cli.main(["--out", str(target)]) == 2
    assert not target.exists()


def test_main_rejects_source_with_unpinned_hash_without_writing(tmp_path):
    run_copy = tmp_path / RUN_PATH.name
    run_copy.write_bytes(RUN_PATH.read_bytes() + b"\n")  # 內容多一個換行：hash 不同
    out = tmp_path / "d.json"
    assert cli.main(["--run", str(run_copy), "--out", str(out)]) == 2
    assert not out.exists()
    ans_copy = tmp_path / "answers.jsonl"
    ans_copy.write_bytes(ANSWERS_PATH.read_bytes() + b"\n")
    assert cli.main(["--answers", str(ans_copy), "--out", str(out)]) == 2
    assert not out.exists()


def test_main_failure_in_data_exits_1_without_writing(tmp_path, run, capsys, monkeypatch):
    bad = copy.deepcopy(run)
    for _, _, s in each_source(bad, "mfg-003#meta"):
        s["text"] = None
    run_copy = tmp_path / RUN_PATH.name
    run_copy.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    pin = {"run": sha256_of(run_copy), "answers": ANSWERS_SHA}
    monkeypatch.setitem(cli.PINS, RUN_ID, pin)  # 讓副本通過 hash 釘選，才會走到資料檢查
    out = tmp_path / "d.json"
    assert cli.main(["--run", str(run_copy), "--out", str(out)]) == 1
    assert not out.exists()
    err = capsys.readouterr().err
    assert "mfg-003#meta" in err and "d-mfg003-02" in err


def test_cli_and_library_import_only_stdlib_and_pure_modules():
    allowed = {
        "argparse",
        "hashlib",
        "json",
        "shlex",
        "sys",
        "os",
        "datetime",
        "pathlib",
        "collections",
        "typing",
        "retrieval_metrics",
        "retrieval_derived_metrics",
    }
    for name in ("derive_retrieval_metrics.py", "retrieval_derived_metrics.py"):
        tree = ast.parse((ROOT / "eval" / name).read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        assert mods <= allowed, (name, mods - allowed)


def test_importing_cli_loads_no_app_models_or_env_modules():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import derive_retrieval_metrics; "
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'app','torch','transformers','sentence_transformers','httpx','requests','dotenv',"
        "'pydantic_settings','retrieval_runtime','run_retrieval_eval'}); print(bad)"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT / "eval")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == "[]"


# ---------------------------------------------------------------- README 衍生表


def fmt(x) -> str:
    return "—" if x is None else repr(round(x, 4))


def expected_readme_row(d, gid) -> dict:
    c = d["summary"][gid]["cohorts"]["all"]
    ma = c["meta_answerable"]
    na = ma["n_applicable"] == 0
    return {
        "from_run": f"{d['derived_from_run']['run_id']}／{d['derived_from_run']['sha256'][:12]}",
        "precision": fmt(c["original_metrics"]["precision"]["mean"]),
        "ceiling": fmt(c["precision_ceiling"]["mean"]),
        "skip_meta": "／".join(fmt(c["skip_meta"]["metrics"][n]["mean"]) for n in SKIP_NAMES),
        "meta_answerable": "／".join(fmt(ma["metrics"][n]["mean"]) for n in META_NAMES),
        "judged": f"{ma['n_qualified']}／{ma['n_applicable']}",
        "denominators": "／".join(
            str(x)
            for x in (
                c["precision_ceiling"]["denominator"],
                c["skip_meta"]["metrics"]["recall@1"]["denominator"],
                ma["metrics"]["recall@1"]["denominator"],
            )
        ),
        "na": na,
        "path": f"summary.{gid}.cohorts.all",
        "status": "derived_from_run",
    }


def parse_derived_table(text: str) -> list[dict]:
    if NEW_START not in text or NEW_END not in text:
        raise ValueError("README 沒有衍生指標表的標記")
    body = text.split(NEW_START, 1)[1].split(NEW_END, 1)[0]
    rows = []
    for line in body.splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| ID "):
            continue
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) != len(COLUMNS):
            raise ValueError(f"衍生表欄位數 {len(cells)} ≠ {len(COLUMNS)}：{line[:60]}")
        rows.append(dict(zip(COLUMNS, cells, strict=True)))
    return rows


def old_table_rows(text: str) -> dict[str, list[str]]:
    body = text.split("<!-- retrieval-layer-table:start -->", 1)[1].split(OLD_END, 1)[0]
    out = {}
    for line in body.splitlines():
        cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
        if len(cells) == 9 and cells[0].startswith("N-T"):
            out[cells[0]] = cells
    return out


REQUIRED_TEXT = (
    "derived_from_run",
    "不是新的 real_run",
    "未重跑模型",
    "逐字",
    "區分大小寫",
    "有限既有回傳",
    "首個正解未排第一",
    "Recall@1 未滿 1",
    "min(|G|,|R|)/|R|",
    "macro",
    "d-mfg003-02",
    "d-mfg002-02",
    "d-mfg004-03",
    DERIVED_REL,
    RUN_SHA,
    ANSWERS_SHA,
)


def readme_problems(text, d, run) -> list[str]:
    p: list[str] = []
    for s in REQUIRED_TEXT:
        if s not in text:
            p.append(f"README 缺少：{s}")
    if OLD_END not in text or NEW_START not in text:
        return [*p, "README 缺少原表或衍生表標記"]
    if text.index(OLD_END) > text.index(NEW_START):
        p.append("衍生表必須在原 retrieval-layer-table:end 之後（舊解析器固定九欄）")
    try:
        rows = parse_derived_table(text)
    except ValueError as e:
        return [*p, str(e)]
    if [r["id"] for r in rows] != list(D_IDS):
        return [*p, f"衍生表 ID 應為 {D_IDS}：{[r['id'] for r in rows]}"]
    old = old_table_rows(text)
    for r, n_id, gid in zip(rows, N_IDS, GROUPS, strict=True):
        want = expected_readme_row(d, gid)
        if r["orig"] != n_id:
            p.append(f"{r['id']}：對應原列應為 {n_id}")
        if f"summary.{gid}.cohorts.all" not in old[n_id][7]:
            p.append(f"{r['id']}：原列 {n_id} 的來源欄位不是 {gid}")
        if r["path"] != want["path"]:
            p.append(f"{r['id']}：JSON 欄位 {r['path']} ≠ {want['path']}")
        old_precision = old[n_id][3].split("／")[-1]
        if r["precision"] != old_precision:
            p.append(f"{r['id']}：原 Precision {r['precision']} ≠ 原表 {n_id} 的 {old_precision}")
        for key in (
            "from_run",
            "precision",
            "ceiling",
            "skip_meta",
            "meta_answerable",
            "judged",
            "denominators",
            "status",
        ):
            if r[key] != want[key]:
                p.append(f"{r['id']}：{key} {r[key]} ≠ JSON {want[key]}")
        if want["na"] and "不適用" not in r["reason"]:
            p.append(f"{r['id']}：meta_answerable 不適用卻沒有寫原因")
        if want["na"] and set(r["meta_answerable"].split("／")) != {"—"}:
            p.append(f"{r['id']}：不適用必須記 —")
    return p


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_derived_table_matches_saved_json_and_original_rows(readme, derived, run):
    problems = readme_problems(readme, derived, run)
    assert problems == [], "\n".join(problems)


def test_readme_derived_table_is_outside_old_table_and_old_table_is_unchanged(readme):
    start = readme.index("<!-- retrieval-layer-table:start -->")
    end = readme.index(OLD_END)
    body = readme[start:end]
    assert NEW_START not in body and "D-T1" not in body
    assert readme.index(NEW_START) > end
    old = old_table_rows(readme)
    assert list(old) == list(N_IDS)
    assert old["N-T3"][3] == "0.4194／0.7581／1.0／0.6812／0.2194" and old["N-T3"][4] == "31"
    assert old["N-T5"][3] == "0.55／0.6／1.0／0.72／0.24" and old["N-T5"][4] == "10"


def test_readme_background_facts_use_first_relevant_not_first_wording(readme, run):
    spec = first_rank_facts(run, GROUPS[2])
    diag = first_rank_facts(run, GROUPS[3])
    for label, (first, top1, r1) in (("指定", spec), ("全庫診斷", diag)):
        sentence = (
            f"{label}首個正解未排第一 {len(first)} 題，"
            f"其中 {len(top1)} 題第一名是同 owner 的 `#meta`"
        )
        assert sentence in readme, sentence
        assert f"Recall@1 未滿 1 共 {len(r1)} 題" in readme
    assert "d-mfg002-01" in readme and "d-mfg003-m1" in readme and "d-mfg005-m1" in readme


def tamper_cell(text: str, row_id: str, column: str, value: str) -> str:
    """把衍生表某列某欄換成別的值。"""
    lines = text.split("\n")
    start = text[: text.index(NEW_START)].count("\n")
    idx = next(i for i in range(start, len(lines)) if lines[i].startswith(f"| {row_id} |"))
    cells = lines[idx].strip().strip("|").split("|")
    cells[COLUMNS.index(column)] = f" {value} "
    lines[idx] = "|" + "|".join(cells) + "|"
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("row_id", "column", "value", "needle"),
    [
        ("D-T1", "ceiling", "0.2485", "ceiling"),
        ("D-T3", "ceiling", "0.2195", "ceiling"),
        ("D-T3", "precision", "0.2195", "precision"),
        ("D-T3", "skip_meta", "0.9／0.9／1.0／0.9", "skip_meta"),
        ("D-T3", "meta_answerable", "0.4／0.8／1.0／0.7／0.2", "meta_answerable"),
        ("D-T3", "denominators", "30／31／31", "denominators"),
        ("D-T3", "judged", "2／31", "judged"),
        ("D-T4", "from_run", "20261027T184738-52cd／834344262d76", "from_run"),
        ("D-T2", "orig", "N-T3", "對應原列"),
        (
            "D-T5",
            "path",
            "summary.mfg/production_retrieve/specified/manager.cohorts.all",
            "JSON 欄位",
        ),
        ("D-T1", "meta_answerable", "0.5／0.5／0.5／0.5／0.5", "meta_answerable"),
        ("D-T1", "reason", "—", "原因"),
        ("D-T6", "status", "real_run", "status"),
    ],
)
def test_readme_validator_catches_tampered_cells(
    readme, derived, run, row_id, column, value, needle
):
    problems = readme_problems(tamper_cell(readme, row_id, column, value), derived, run)
    assert any(needle in x for x in problems), (needle, problems[:4])


def test_readme_validator_catches_missing_statements_and_misplaced_table(readme, derived, run):
    assert any(
        "缺少：逐字" in x for x in readme_problems(readme.replace("逐字", "照字"), derived, run)
    )
    moved = readme.replace(NEW_START, "<!-- x -->").replace(NEW_END, "<!-- y -->")
    assert readme_problems(moved, derived, run)
    inside = readme.replace(OLD_END, "") + OLD_END  # 衍生表跑進舊表標記內
    assert any("必須在原" in x for x in readme_problems(inside, derived, run))
