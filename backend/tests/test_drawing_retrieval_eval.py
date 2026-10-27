"""圖紙照片檢索評估（eval/run_drawing_retrieval_eval.py，TASK-20261027-001 AC-5、AC-6、AC-8）。

1. 逐題與摘要（純函式、固定排名的假輸入，不載入 app）：candidates 的 Recall@1/2/3/5、MRR 與分母，
   route miss／空候選記 0 留在分母，看不到與未收錄的照片不進排名分母、另外拆開統計；
   candidates（幾何驗證後的混合排名）和 got（辨識成功）分開；外洩看整份候選；
   原本的欄位與停止條件不變。
2. CLI（子行程、mock 向量、暫存 DATA_DIR）：先在暫存目錄建 mock 索引與收據，再跑評估：
   JSON 可由逐題資料重算、停止條件成立仍寫 JSON、mock 不能寫進 eval/runs、
   收據和載入的索引不符就不跑、real 模式遇到 mock 索引就不跑，全程沒有打開 env_file。

不使用 conftest 的 fixture（可以 pytest --noconftest 單獨跑）；子行程的環境變數全部明列，不繼承。
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
import retrieval_metrics as rm  # noqa: E402
import retrieval_runtime as rr  # noqa: E402
import run_drawing_retrieval_eval as dre  # noqa: E402

SCRIPT = ROOT / "eval" / "run_drawing_retrieval_eval.py"
REPORTED = "eval/drawing_retrieval/mfg-006__reported-screenshot.png"
VISIBLE = {"mfg-004", "mfg-005", "mfg-006"}
# TASK-20261010-001 的逐題欄位，順序不變；新增的欄位接在後面
LEGACY_PHOTO_KEYS = [
    "name", "variant", "expect", "expect_in_kb", "domain", "margin", "got", "candidates",
    "top_inliers", "top_overlap", "ok", "wrong", "leak", "ms",
]  # fmt: skip
LEGACY_TEXT_KEYS = ["q", "by", "expect", "top", "results", "ok", "leak"]
LEGACY_SUMMARY_KEYS = [
    "photo_top1", "photo_by_variant", "photo_rejected", "photo_wrong", "text_ok", "leaks",
]  # fmt: skip


# ---------------------------------------------------------------- 假輸入


def case(expect_in_kb: str | None, variant: str = "original", name: str | None = None) -> dict:
    """photo_cases 展開後的一張照片（不需要真的圖）。"""
    return {
        "name": name or f"{expect_in_kb}__{variant}",
        "variant": variant,
        "expect": expect_in_kb,
    }


def hit(pid: str, *, verified: bool = False, score: float = 0.7, inliers: int | None = None):
    """identify_drawing 的一個候選。"""
    overlap = 0.9 if verified else None
    return {
        "part": {"id": pid, "name": pid},
        "score": score,
        "inliers": inliers,
        "overlap": overlap,
        "coverage": 1.0 if verified else None,
        "verified": verified,
    }


def identified(*hits: dict) -> dict:
    """identify_drawing 的回傳：第一名通過驗證才算認出（和原程式一樣）。"""
    matched = bool(hits) and hits[0]["verified"]
    return {
        "matched": matched,
        "best_part_id": hits[0]["part"]["id"] if matched else None,
        "results": list(hits),
    }


def row(c: dict, res: dict | None, *, domain: str = "mfg", visible=VISIBLE) -> dict:
    return dre.photo_row(c, set(visible), domain, 0.123456, res, 42)


def text(q: str, expect: str, ids: list[str], visible=VISIBLE) -> dict:
    return dre.text_row({"q": q, "by": "名稱", "expect": expect}, set(visible), ids)


# ---------------------------------------------------------------- 逐題：候選排名


def test_candidate_rank_counts_unverified_candidates_and_is_not_matched():
    """第二名是正解但沒通過驗證：排名指標算命中第 2 名，辨識結果仍是沒認出。"""
    r = row(
        case("mfg-006"),
        identified(hit("mfg-005", inliers=30), hit("mfg-006", inliers=12), hit("mfg-004")),
    )
    assert list(r)[: len(LEGACY_PHOTO_KEYS)] == LEGACY_PHOTO_KEYS
    assert (r["got"], r["ok"], r["wrong"], r["leak"]) == (None, False, False, False)
    assert r["candidates"] == ["mfg-005", "mfg-006", "mfg-004"]
    assert (r["top_inliers"], r["top_overlap"], r["margin"], r["ms"]) == (30, None, 0.1235, 42)
    ranking = r["candidate_ranking"]
    assert ranking["rank"] == 2 and ranking["in_ranking"] is True
    assert ranking["route_miss"] is False and ranking["empty_candidates"] is False
    assert ranking["metrics"] == {
        "recall@1": 0.0, "recall@2": 1.0, "recall@3": 1.0, "recall@5": 1.0, "mrr": 0.5,
    }  # fmt: skip
    assert [d["id"] for d in r["candidate_details"]] == r["candidates"]
    assert [d["verified"] for d in r["candidate_details"]] == [False, False, False]
    assert r["candidate_details"][0]["inliers"] == 30
    assert r["negative_kind"] is None and r["leaked_ids"] == []


def test_verified_first_candidate_is_matched_and_rank_one():
    r = row(case("mfg-006"), identified(hit("mfg-006", verified=True, inliers=91), hit("mfg-005")))
    assert (r["got"], r["ok"]) == ("mfg-006", True)
    assert r["candidate_ranking"]["rank"] == 1
    assert r["candidate_ranking"]["metrics"]["mrr"] == 1.0
    assert r["candidate_details"][0] == {
        "id": "mfg-006", "score": 0.7, "inliers": 91, "overlap": 0.9, "coverage": 1.0,
        "verified": True,
    }  # fmt: skip


def test_route_miss_stays_in_ranking_denominator_as_zero():
    """領域路由判成畫作：沒有跑辨識、候選空，排名記 0，不從分母拿掉。"""
    r = row(case("mfg-004"), None, domain="art")
    assert (r["domain"], r["got"], r["candidates"], r["ok"]) == ("art", None, [], False)
    assert (r["top_inliers"], r["top_overlap"]) == (None, None)
    ranking = r["candidate_ranking"]
    assert ranking["in_ranking"] is True and ranking["route_miss"] is True
    assert ranking["empty_candidates"] is True and ranking["rank"] is None
    assert set(ranking["metrics"].values()) == {0.0}
    assert r["candidate_details"] == []


def test_empty_candidates_on_mfg_route_count_as_miss():
    r = row(case("mfg-005"), identified())
    ranking = r["candidate_ranking"]
    assert ranking["route_miss"] is False and ranking["empty_candidates"] is True
    assert ranking["in_ranking"] is True and set(ranking["metrics"].values()) == {0.0}


def test_correct_candidate_outside_top_five_is_miss():
    hits = [hit(f"mfg-00{i}") for i in (1, 2, 3, 4, 5)]
    r = row(case("mfg-006"), identified(*hits), visible={f"mfg-00{i}" for i in range(1, 7)})
    assert r["candidate_ranking"]["rank"] is None
    assert set(r["candidate_ranking"]["metrics"].values()) == {0.0}


def test_hidden_and_unknown_photos_stay_out_of_ranking():
    hidden = row(case("mfg-001"), identified(hit("mfg-004")), visible={"mfg-004"})
    unknown = row(
        case(None, name="unknown-01.png"), identified(hit("mfg-004")), visible={"mfg-004"}
    )
    for r, kind in ((hidden, "hidden"), (unknown, "unknown")):
        assert r["expect"] is None and r["negative_kind"] == kind
        assert r["ok"] is True  # 沒認出＝拒答正確
        assert r["candidate_ranking"]["in_ranking"] is False
        assert set(r["candidate_ranking"]["metrics"].values()) == {None}
    assert hidden["expect_in_kb"] == "mfg-001" and unknown["expect_in_kb"] is None


def test_leak_checks_every_candidate_position():
    """看不到的圖紙排在第 5 名也是外洩（不只看第一名或 got）。"""
    hits = [hit("mfg-004"), hit("mfg-005"), hit("mfg-004"), hit("mfg-005"), hit("mfg-001")]
    r = row(case("mfg-004"), identified(*hits), visible={"mfg-004", "mfg-005"})
    assert r["leak"] is True and r["leaked_ids"] == ["mfg-001"]
    t = text(
        "固定支架", "mfg-004", ["mfg-004", "mfg-005", "mfg-001"], visible={"mfg-004", "mfg-005"}
    )
    assert list(t)[: len(LEGACY_TEXT_KEYS)] == LEGACY_TEXT_KEYS
    assert t["leak"] is True and t["leaked_ids"] == ["mfg-001"]


def test_text_row_keeps_original_rules():
    visible_ok = text("步進馬達安裝板", "mfg-004", ["mfg-004", "mfg-005"])
    assert (visible_ok["expect"], visible_ok["top"], visible_ok["ok"]) == (
        "mfg-004",
        "mfg-004",
        True,
    )
    # 看不到的答案：不能出現在任何名次（其他看得到的圖紙可以出現）
    hidden = text("L 型固定支架", "mfg-001", ["mfg-004", "mfg-005"])
    assert (hidden["expect"], hidden["ok"]) == (None, True)
    wrong_top = text("步進馬達安裝板", "mfg-005", ["mfg-004", "mfg-005"])
    assert wrong_top["ok"] is False and wrong_top["leaked_ids"] == []


# ---------------------------------------------------------------- 摘要與停止條件


def sample_account() -> tuple[list[dict], list[dict]]:
    photos = [
        # 回報的截圖：第一名通過驗證
        row(case("mfg-006", "reported"), identified(hit("mfg-006", verified=True, inliers=91))),
        # 候選第一名是正解但沒通過驗證：排名命中、辨識沒認出
        row(case("mfg-004"), identified(hit("mfg-004", inliers=20), hit("mfg-005"))),
        # 模糊：正解第 3 名
        row(case("mfg-005", "blur"), identified(hit("mfg-004"), hit("mfg-006"), hit("mfg-005"))),
        # 路由判成畫作
        row(case("mfg-004", "blur"), None, domain="art"),
        # 看不到（mfg-001）、未收錄：都沒認出
        row(case("mfg-001"), identified(hit("mfg-004"))),
        row(case(None, name="unknown-01.png"), identified(hit("mfg-005"))),
        row(case(None, name="unknown-02.png"), None, domain="art"),
    ]
    texts = [
        text("步進馬達安裝板", "mfg-004", ["mfg-004", "mfg-005"]),
        text("L 型固定支架", "mfg-001", ["mfg-004"]),
    ]
    return photos, texts


def test_summary_keeps_legacy_fields_and_adds_candidate_ranking():
    photos, texts = sample_account()
    s = dre.account_summary(photos, texts)
    assert list(s)[: len(LEGACY_SUMMARY_KEYS)] == LEGACY_SUMMARY_KEYS
    assert s["photo_top1"] == "1/4"  # 只有回報的截圖認出
    assert s["photo_by_variant"] == {"blur": "0/2", "original": "0/1", "reported": "1/1"}
    assert s["photo_rejected"] == "3/3"
    assert (s["photo_wrong"], s["text_ok"], s["leaks"]) == (0, "2/2", 0)

    ranking = s["candidate_ranking"]
    assert "幾何驗證" in ranking["source"] and "CLIP 粗篩" in ranking["source"]
    assert (ranking["n_cases"], ranking["n_ranked"]) == (7, 4)
    assert (ranking["n_route_miss"], ranking["n_empty_candidates"]) == (1, 1)
    m = ranking["metrics"]
    # 排名分母 4（看得到正解的照片），route miss 記 0
    assert {k: (v["numerator"], v["denominator"]) for k, v in m.items()} == {
        "recall@1": (2.0, 4), "recall@2": (2.0, 4), "recall@3": (3.0, 4), "recall@5": (3.0, 4),
        "mrr": (pytest.approx(1 + 1 + 1 / 3), 4),
    }  # fmt: skip
    assert m["recall@1"]["mean"] == 0.5 and m["recall@3"]["mean"] == 0.75
    # 辨識成功（got）和候選排名是兩種數字：第一名命中 2 張，認出只有 1 張
    assert s["matched_top1"] == {"rate": 0.25, "numerator": 1, "denominator": 4}

    by_variant = s["candidate_ranking_by_variant"]
    assert sorted(by_variant) == ["blur", "original", "reported"]
    assert by_variant["blur"]["n_ranked"] == 2 and by_variant["blur"]["n_route_miss"] == 1
    assert by_variant["blur"]["metrics"]["recall@3"]["numerator"] == 1.0

    neg = s["photo_negatives"]
    assert neg["hidden"] == {
        "n": 1,
        "rejected": {"rate": 1.0, "numerator": 1, "denominator": 1},
        "false_accept": {"rate": 0.0, "numerator": 0, "denominator": 1},
        "leak_cases": {"rate": 0.0, "numerator": 0, "denominator": 1},
    }
    assert neg["unknown"]["n"] == 2 and neg["unknown"]["rejected"]["numerator"] == 2
    assert s["leak_detail"]["photos"]["leaked_cases"]["denominator"] == 7
    assert s["leak_detail"]["texts"]["leaked_chunks"] == 0


def test_summary_without_hidden_photos_reports_null_not_zero():
    photos = [row(case("mfg-006", "reported"), identified(hit("mfg-006", verified=True)))]
    s = dre.account_summary(photos, [])
    assert s["photo_negatives"]["hidden"]["rejected"] == {
        "rate": None, "numerator": 0, "denominator": 0,
    }  # fmt: skip
    assert s["candidate_ranking"]["metrics"]["recall@1"]["mean"] == 1.0


def test_summary_counts_false_accept_and_leaks():
    photos = [
        row(case("mfg-006", "reported"), identified(hit("mfg-006", verified=True))),
        # 未收錄的圖紙被認成 mfg-004：指錯
        row(case(None, name="unknown-01.png"), identified(hit("mfg-004", verified=True))),
        row(case("mfg-004"), identified(hit("mfg-004"), hit("mfg-001")), visible=VISIBLE),
    ]
    texts = [text("L 型固定支架", "mfg-004", ["mfg-004", "mfg-001"])]
    s = dre.account_summary(photos, texts)
    assert s["photo_wrong"] == 1 and s["photo_rejected"] == "0/1"
    assert s["photo_negatives"]["unknown"]["false_accept"]["numerator"] == 1
    assert s["leaks"] == 2
    assert s["leak_detail"]["photos"]["case_ids"] == ["mfg-004__original"]
    assert s["leak_detail"]["texts"]["case_ids"] == ["L 型固定支架"]


def clean_account() -> tuple[list[dict], list[dict]]:
    photos = [
        row(case("mfg-006", "reported"), identified(hit("mfg-006", verified=True))),
        # 低品質 known 沒認出、正解也沒排進候選：照實記錄，不觸發停止
        row(case("mfg-005", "blur"), identified(hit("mfg-004"))),
        row(case(None, name="unknown-01.png"), identified(hit("mfg-004"))),
    ]
    return photos, [text("步進馬達安裝板", "mfg-004", ["mfg-004"])]


def broken(kind: str) -> tuple[list[dict], list[dict]]:
    photos, texts = clean_account()
    if kind == "photo_wrong":
        photos[2] = row(
            case(None, name="unknown-01.png"), identified(hit("mfg-004", verified=True))
        )
    elif kind == "leak":
        photos[1] = row(case("mfg-005", "blur"), identified(hit("mfg-004"), hit("mfg-001")))
    elif kind == "text_not_ok":
        texts.append(text("T 型槽螺帽", "mfg-005", ["mfg-004", "mfg-005"]))
    elif kind == "reported_not_ok":
        photos[0] = row(case("mfg-006", "reported"), identified(hit("mfg-006")))
    elif kind == "reported_missing":
        photos.pop(0)
    return photos, texts


def test_clean_account_does_not_stop_on_low_quality_misses():
    photos, texts = clean_account()
    result = dre.account_result(photos, texts)
    assert result["stop_reasons"] == []
    assert list(result)[:3] == ["summary", "photos", "texts"]
    assert result["summary"]["photo_top1"] == "1/2"
    assert result["summary"]["candidate_ranking"]["metrics"]["recall@5"]["numerator"] == 1.0


@pytest.mark.parametrize(
    "kind", ["photo_wrong", "leak", "text_not_ok", "reported_not_ok", "reported_missing"]
)
def test_every_stop_rule_is_kept(kind):
    photos, texts = broken(kind)
    assert kind in dre.account_result(photos, texts)["stop_reasons"]
    assert kind in dre.STOP_RULES


def test_hidden_reported_screenshot_must_be_rejected():
    """沒有權限的身分：回報的截圖正解是 null，認出任何圖紙都算錯。"""
    ok = row(case("mfg-006", "reported"), identified(hit("mfg-004")), visible={"mfg-004"})
    assert dre.account_result([ok], [])["stop_reasons"] == []
    bad = row(
        case("mfg-006", "reported"), identified(hit("mfg-004", verified=True)), visible={"mfg-004"}
    )
    assert dre.account_result([bad], [])["stop_reasons"] == ["photo_wrong", "reported_not_ok"]


def test_exit_code_and_report_written_even_when_stopped(tmp_path):
    report = {
        "run_id": "r1",
        "accounts": {"a": {"stop_reasons": ["leak"]}, "b": {"stop_reasons": []}},
    }
    assert dre.finalize(report) == 1
    assert report["stop"]["exit_code"] == 1 and report["stop"]["failed_accounts"] == ["a"]
    out = dre.write_report(report, tmp_path / "out")
    assert out == tmp_path / "out" / "r1-drawing-retrieval.json"
    assert json.loads(out.read_text(encoding="utf-8"))["stop"]["exit_code"] == 1
    assert dre.finalize({"run_id": "r2", "accounts": {"a": {"stop_reasons": []}}}) == 0


def test_receipt_selection(tmp_path):
    assert dre.find_receipt("latest", tmp_path) is None
    for run_id in ("20261027T010000-aaaa", "20261027T020000-bbbb", "20261026T230000-cccc"):
        (tmp_path / f"{run_id}-retrieval-index.json").write_text("{}", encoding="utf-8")
    (tmp_path / "20261028T000000-dddd-drawing-retrieval.json").write_text("{}", encoding="utf-8")
    assert dre.find_receipt("latest", tmp_path).name == "20261027T020000-bbbb-retrieval-index.json"
    assert dre.find_receipt(str(tmp_path / "x.json"), tmp_path) == tmp_path / "x.json"


def receipt(**over) -> dict:
    """retrieval_runtime.assemble_index_receipt 組出的收據（預設 real、和 current 一致）。"""
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
        "qa": {"eval/drawing_retrieval/gold.json": "g"},
        "code": {"head": "abc", "dirty": False},
        "isolation": {"env_file_disabled": True},
        "index_build": {"invoked": True, "exit_code": 0},
        "created_at": "2026-10-27T00:00:00+00:00",
    }
    return rr.assemble_index_receipt(**{**parts, **over}) | {"run_id": "20261027T000000-abcd"}


def saved(tmp_path: Path, r: dict) -> str:
    path = tmp_path / f"{r['run_id']}-retrieval-index.json"
    path.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_check_index_accepts_matching_real_receipt(tmp_path):
    path = saved(tmp_path, receipt())
    info, problems = dre.check_index(receipt(index_build=None), path, real=True)
    assert problems == []
    assert info == {
        "selection": "explicit",
        "path": rr.repo_relative(Path(path)),
        "sha256": rr.sha256_file(Path(path)),
        "run_id": "20261027T000000-abcd",
        "created_at": "2026-10-27T00:00:00+00:00",
        "real": True,
        "index_build": {"invoked": True, "exit_code": 0},
    }


def test_check_index_blocks_mock_receipt_for_real_run(tmp_path):
    mock = receipt(
        settings={"embed_mode": "mock", "index_backend": "file"},
        manifest={"embed_mode": "mock", "chunk_count": 21, "part_chunk_count": 34},
    )
    info, problems = dre.check_index(receipt(index_build=None), saved(tmp_path, mock), real=True)
    assert info["real"] is False
    assert any("不是真模型索引" in p for p in problems)
    assert "目前載入的索引 manifest 和收據不同" in problems
    # mock 執行（real=False）時同一份 mock 收據對 mock 索引是可以的
    _, ok = dre.check_index(mock | {"index_build": None}, saved(tmp_path, mock), real=False)
    assert ok == []


def test_check_index_needs_a_rebuild_receipt_matching_the_loaded_index(tmp_path):
    no_build = receipt(index_build={"invoked": False})
    _, problems = dre.check_index(receipt(index_build=None), saved(tmp_path, no_build), real=True)
    assert problems == ["收據不是這次重建索引產生的"]
    changed = receipt(
        index_build=None,
        collections={
            "art": {"chunk_count": 21, "chunks_sha256": "c1", "chunk_vecs": {"sha256": "v1"}},
            "mfg": {"chunk_count": 34, "chunks_sha256": "OTHER", "chunk_vecs": {"sha256": "v2"}},
        },
    )
    _, problems = dre.check_index(changed, saved(tmp_path, receipt()), real=True)
    assert problems == ["mfg 索引的 chunks_sha256 和收據不同"]


def test_check_index_reports_current_problems_once(tmp_path):
    """目前索引本身的問題也擋（例如設定隔離沒生效）；兩份收據同樣的問題只列一次。"""
    stale = {"version": "2026.10.2", "hash_now": "h2", "hash_manifest": "h"}
    current = receipt(index_build=None, kb=stale, isolation={"env_file_disabled": False})
    _, problems = dre.check_index(current, saved(tmp_path, receipt(kb=stale)), real=True)
    assert problems.count("目前 KB 的 hash 和建索引時不同，請重建索引") == 1
    assert "沒有關閉 env_file（可能讀了 .env）" in problems


def test_check_index_reports_missing_or_unreadable_receipt(tmp_path):
    info, problems = dre.check_index(receipt(), str(tmp_path / "nope.json"), real=True)
    assert info["path"] is None and info["sha256"] is None
    assert len(problems) == 1 and "找不到建索引收據" in problems[0]
    bad = tmp_path / "bad-retrieval-index.json"
    bad.write_text("{not json", encoding="utf-8")
    info, problems = dre.check_index(receipt(), str(bad), real=True)
    assert info["path"] is None and len(problems) == 1 and "讀不了" in problems[0]


def test_ranking_line_shows_hits_denominators_and_mrr():
    photos, texts = sample_account()
    line = dre._ranking_line(dre.account_summary(photos, texts)["candidate_ranking"])
    assert line == "R@1 2/4  R@2 2/4  R@3 3/4  R@5 3/4  MRR 0.583（route miss 1）"
    empty = dre._ranking_line(rm.aggregate_photo_cases([]))
    assert empty == "R@1 0/0  R@2 0/0  R@3 0/0  R@5 0/0  MRR —（route miss 0）"


def test_candidate_ranking_definition_names_its_source():
    d = dre.CANDIDATE_RANKING
    assert d["ks"] == [1, 2, 3, 5]
    assert "幾何驗證" in d["description"] and "CLIP 粗篩" in d["description"]
    assert "route" in d["denominator"]


def test_import_does_not_load_app():
    """import 評估腳本不能先載入 app（bootstrap 之前就讀設定會讀到 .env）。"""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import run_drawing_retrieval_eval; "
        "print(sorted(m for m in sys.modules if m == 'app' or m.startswith('app.')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "eval")],
        env=base_env(ROOT / "data-unused"),
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]"


# ---------------------------------------------------------------- CLI（子行程、mock 向量）


def base_env(data_dir: Path, **over: str) -> dict:
    """子行程的完整環境變數（不繼承目前的環境：開發者 shell 的 DATABASE_URL 等不會帶進去）。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(data_dir)),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "en_US.UTF-8",
        "PYTHONPATH": str(ROOT / "backend"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "EMBED_MODE": "mock",
        "EMBED_DEVICE": "cpu",
        "LLM_MODE": "mock",
        "DATA_DIR": str(data_dir),
        "DATABASE_URL": "",
        "JOBS_DATABASE_URL": "",
        "REDIS_URL": "",
        "MEMORY_GUARD": "false",
        "JEV_ENABLED": "false",
        "ALLOW_CLOUD": "false",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    return env | over


# 建 mock 索引：先 bootstrap（不讀 .env），標準 CadQuery 模型換成假的執行器
# （外形幾何和圖紙檢索無關；沙箱裡也不能再開 sandbox-exec），再寫建索引收據
BUILD_INDEX = """
import json, sys
from pathlib import Path

root, receipt_path, qa = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path[:0] = [str(root / "backend"), str(root / "eval")]
import retrieval_runtime as rr

isolation = rr.bootstrap(strict=True)
from app.cad import sandbox


async def fake_run_cad(code, job_dir, **kw):
    job_dir.mkdir(parents=True, exist_ok=True)
    for name in ("model.stl", "model.step"):
        (job_dir / name).write_text("mock", encoding="utf-8")
    dims = {"width": 10.0, "depth": 20.0, "height": 30.0}
    return sandbox.RunResult(True, None, {"dims": dims, "volume": 6000.0, "faces": 6})


sandbox.run_cad = fake_run_cad
build = rr.run_build_index()
assert build["exit_code"] == 0, build
from app.repositories.index_store import get_store

store = get_store()
store.load()
receipt = rr.collect_index_receipt(store, isolation=isolation, qa_paths=[qa], index_build=build)
receipt["run_id"] = "fixture-mock-index"
receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
"""

# 把 Settings 的 env_file 指到假的 .env（模擬 repo 有 .env），用 audit hook 記錄所有打開
# .env 的動作，再照 __main__ 執行評估腳本。mode=read-first 是對照組：沒 bootstrap 先讀一次設定
WATCH_ENV = """
import json, os, runpy, sys
from pathlib import Path

root, log_path, sentinel, mode, script = sys.argv[1:6]
watched = {str(Path(sentinel).resolve()), str((Path(root) / ".env").resolve())}
opened = []


def hook(event, args):
    if event != "open" or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    p = os.fsdecode(args[0])
    if os.path.basename(p) == ".env" or os.path.abspath(p) in watched:
        opened.append(p)


sys.addaudithook(hook)
sys.path.insert(0, str(Path(root) / "backend"))
from app.core import config

config.Settings.model_config["env_file"] = sentinel
if mode == "read-first":
    config.get_settings()
sys.argv = [script, *sys.argv[6:]]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as e:
    code = e.code
Path(log_path).write_text(json.dumps({"opened": opened, "code": code}), encoding="utf-8")
sys.exit(code)
"""


@pytest.fixture(scope="module")
def mock_index(tmp_path_factory):
    """暫存 DATA_DIR 裡的 mock 索引與它的建索引收據（mock 收據只寫暫存目錄）。"""
    base = tmp_path_factory.mktemp("drawing_eval")
    data, receipt = base / "data", base / "fixture-mock-index-retrieval-index.json"
    script = base / "build_index_mock.py"
    script.write_text(BUILD_INDEX, encoding="utf-8")
    gold = ROOT / "eval" / "drawing_retrieval" / "gold.json"
    proc = subprocess.run(
        [sys.executable, str(script), str(ROOT), str(receipt), str(gold)],
        env=base_env(data),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-4000:]
    return {"data": data, "receipt": receipt, "base": base}


def write_gold(path: Path, texts: list[dict], *, reported: bool = True) -> Path:
    photos = [
        {"glob": "kb/drawings/mfg-00[46].png", "variant": "original"},
        {
            "crop": "kb/drawings/mfg-005.png",
            "box": [460, 140, 660, 340],
            "factor": 1.3,
            "variant": "review_crop",
        },
        {"glob": "eval/drawing_photos/unknown/unknown-01.png", "variant": "from_name"},
        {"generate": "views", "from": ["kb/drawings/mfg-004.png"]},
        {
            "generate": "patches",
            "from": ["kb/drawings/mfg-004.png", "eval/drawing_photos/unknown/unknown-01.png"],
            "factors": [2.0],
        },
    ]
    if reported:
        photos.insert(0, {"path": REPORTED, "expect": "mfg-006", "variant": "reported"})
    gold = {"photos": photos, "texts": texts, "accounts": ["manager", "sales"]}
    path.write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    return path


# write_gold 展開後的照片（name、variant、知識庫裡的答案），順序和 gold 一樣
GOLD_PHOTOS = [
    ("mfg-006__reported-screenshot.png", "reported", "mfg-006"),
    ("mfg-004.png", "original", "mfg-004"),
    ("mfg-006.png", "original", "mfg-006"),
    ("mfg-005__crop[460, 140, 660, 340]_x1.3", "review_crop", "mfg-005"),
    ("unknown-01.png", "from_name", None),
    ("mfg-004__view_right", "view_right", "mfg-004"),
    ("mfg-004__view_front", "view_front", "mfg-004"),
    ("mfg-004__view_top", "view_top", "mfg-004"),
    # 局部照片依完整路徑排序：eval/drawing_photos/… 在 kb/drawings/… 前面
    ("unknown-01__patch_right_x2", "patch_right", None),
    ("unknown-01__patch_front_x2", "patch_front", None),
    ("unknown-01__patch_top_x2", "patch_top", None),
    ("mfg-004__patch_right_x2", "patch_right", "mfg-004"),
    ("mfg-004__patch_front_x2", "patch_front", "mfg-004"),
    ("mfg-004__patch_top_x2", "patch_top", "mfg-004"),
]
PASSING_TEXTS = [
    {"q": "MTP-4004", "expect": "mfg-004", "by": "料號"},
    {"q": "D-24-0618", "expect": "mfg-006", "by": "圖號"},
]


def run_cli(mock_index, *args: str, env_over: dict | None = None, wrapper: list[str] | None = None):
    cmd = [sys.executable, *(wrapper or []), str(SCRIPT), *args]
    return subprocess.run(
        cmd,
        env=base_env(mock_index["data"], **(env_over or {})),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )


def official_runs() -> set[str]:
    return {p.name for p in (ROOT / "eval" / "runs").glob("*")}


def legacy_stop(account: dict) -> bool:
    """TASK-20261010-001 的停止條件，直接從逐題資料重算。"""
    photos, texts = account["photos"], account["texts"]
    reported = [r for r in photos if r["variant"] == "reported"]
    return (
        any(r["wrong"] for r in photos)
        or any(r["leak"] for r in photos + texts)
        or not all(r["ok"] for r in texts)
        or not reported
        or not reported[0]["ok"]
    )


def test_mock_cli_writes_recomputable_report_to_temp_dir(mock_index, tmp_path):
    gold = write_gold(tmp_path / "gold.json", PASSING_TEXTS)
    out_dir, before = tmp_path / "out", official_runs()
    proc = run_cli(
        mock_index,
        "--gold", str(gold), "--out-dir", str(out_dir), "--run-id", "t-mock",
        "--index-receipt", str(mock_index["receipt"]),
    )  # fmt: skip
    assert proc.returncode in (0, 1), proc.stdout[-2000:] + proc.stderr[-4000:]
    assert official_runs() == before  # mock 不會寫進 eval/runs
    assert [p.name for p in out_dir.iterdir()] == ["t-mock-drawing-retrieval.json"]
    report = json.loads((out_dir / "t-mock-drawing-retrieval.json").read_text(encoding="utf-8"))

    assert list(report)[:3] == ["run_id", "kb_version", "accounts"]  # 原有欄位
    assert report["run_id"] == "t-mock" and report["schema_version"] == dre.SCHEMA_VERSION
    assert report["embed_mode"] == "mock" and report["real"] is False
    assert report["isolation"]["env_file_disabled"] is True
    assert report["isolation"]["strict"] is True
    assert report["isolation"]["settings_were_loaded"] is False
    assert report["code"]["head"] and isinstance(report["code"]["dirty"], bool)
    assert report["settings"]["index_backend"] == "file"
    assert report["gold"]["sha256"] == rr.sha256_file(gold)
    assert report["identify_params"]["top_k_search"] == 5
    assert report["candidate_ranking_definition"] == dre.CANDIDATE_RANKING

    idx = report["index_receipt"]
    build = idx["build_receipt"]
    assert build["selection"] == "explicit" and build["run_id"] == "fixture-mock-index"
    assert build["sha256"] == rr.sha256_file(mock_index["receipt"])
    assert build["real"] is False and build["index_build"]["exit_code"] == 0
    assert idx["problems"] == []
    saved = json.loads(mock_index["receipt"].read_text(encoding="utf-8"))
    assert idx["current"]["manifest_sha256"] == saved["manifest_sha256"]
    assert idx["current"]["collections"]["mfg"]["chunk_count"] > 0

    assert list(report["accounts"]) == ["manager", "sales"]
    manager, sales = report["accounts"]["manager"], report["accounts"]["sales"]
    assert set(sales["visible_part_ids"]) <= set(manager["visible_part_ids"])
    failed = []
    for acc_id, acc in report["accounts"].items():
        visible = set(acc["visible_part_ids"])
        photos, s = acc["photos"], acc["summary"]
        assert [(r["name"], r["variant"], r["expect_in_kb"]) for r in photos] == GOLD_PHOTOS
        assert len(acc["texts"]) == 2
        assert list(s)[: len(LEGACY_SUMMARY_KEYS)] == LEGACY_SUMMARY_KEYS
        for r in photos:
            assert list(r)[: len(LEGACY_PHOTO_KEYS)] == LEGACY_PHOTO_KEYS
            assert r["expect"] == (r["expect_in_kb"] if r["expect_in_kb"] in visible else None)
            assert r["candidates"] == [d["id"] for d in r["candidate_details"]]
            assert set(r["candidates"]) <= visible  # identify_drawing 只在看得到的圖紙裡找
            if r["domain"] != "mfg":
                assert r["candidates"] == [] and r["candidate_ranking"]["route_miss"] is True
            if r["got"] is not None:
                assert r["got"] == r["candidates"][0] and r["candidate_details"][0]["verified"]
            again = rm.score_photo_case(
                r["candidates"], r["expect"], route_miss=r["domain"] != "mfg"
            )
            assert r["candidate_ranking"]["metrics"] == again["metrics"]
        # 摘要可以由逐題資料重算
        ranked = [r for r in photos if r["expect"]]
        assert s["photo_top1"] == f"{sum(r['ok'] for r in ranked)}/{len(ranked)}"
        agg = rm.aggregate_photo_cases([r["candidate_ranking"] for r in photos])
        assert {k: s["candidate_ranking"][k] for k in agg} == agg
        assert s["candidate_ranking"]["n_ranked"] == len(ranked)
        assert bool(acc["stop_reasons"]) == legacy_stop(acc)
        if legacy_stop(acc):
            failed.append(acc_id)
    assert report["stop"]["failed_accounts"] == failed
    assert report["stop"]["exit_code"] == proc.returncode == (1 if failed else 0)
    assert "候選排名" in proc.stdout


def test_stop_rule_failure_still_writes_json(mock_index, tmp_path):
    """文字查詢第一名不是答案（字面寫了 mfg-004 的名稱，答案標 mfg-005）：非 0 結束，JSON 照寫。"""
    texts = [{"q": "步進馬達安裝板", "expect": "mfg-005", "by": "名稱"}]
    gold = write_gold(tmp_path / "gold.json", texts)
    out_dir = tmp_path / "out"
    proc = run_cli(
        mock_index,
        "--gold", str(gold), "--accounts", "manager", "--out-dir", str(out_dir),
        "--run-id", "t-stop", "--index-receipt", str(mock_index["receipt"]),
    )  # fmt: skip
    assert proc.returncode == 1, proc.stderr[-4000:]
    report = json.loads((out_dir / "t-stop-drawing-retrieval.json").read_text(encoding="utf-8"))
    manager = report["accounts"]["manager"]
    assert manager["texts"][0]["top"] == "mfg-004" and manager["texts"][0]["ok"] is False
    assert manager["summary"]["text_ok"] == "0/1"
    assert "text_not_ok" in manager["stop_reasons"]
    assert report["stop"] == {
        "exit_code": 1, "failed_accounts": ["manager"], "rules": dre.STOP_RULES,
    }  # fmt: skip


def test_mock_run_cannot_write_official_runs_dir(mock_index, tmp_path):
    gold = write_gold(tmp_path / "gold.json", PASSING_TEXTS)
    before = official_runs()
    proc = run_cli(
        mock_index,
        "--gold", str(gold), "--run-id", "t-official",
        "--index-receipt", str(mock_index["receipt"]),
    )  # fmt: skip
    assert proc.returncode == 2
    assert "eval/runs" in proc.stderr
    assert official_runs() == before


@pytest.mark.parametrize("problem", ["missing", "tampered", "latest"])
def test_index_receipt_must_match_loaded_index(mock_index, tmp_path, problem):
    gold = write_gold(tmp_path / "gold.json", PASSING_TEXTS)
    out_dir = tmp_path / "out"
    if problem == "missing":
        receipt = str(tmp_path / "nope-retrieval-index.json")
    elif problem == "tampered":
        saved = json.loads(mock_index["receipt"].read_text(encoding="utf-8"))
        saved["collections"]["mfg"]["chunks_sha256"] = "0" * 64
        path = tmp_path / "tampered-retrieval-index.json"
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        receipt = str(path)
    else:
        receipt = "latest"  # eval/runs 裡就算有 real 收據，也和這份 mock 索引對不上
    proc = run_cli(
        mock_index,
        "--gold", str(gold), "--out-dir", str(out_dir), "--run-id", "t-receipt",
        "--index-receipt", receipt,
    )  # fmt: skip
    assert proc.returncode == 2
    assert "收據" in proc.stderr
    assert not out_dir.exists()


def test_real_mode_refuses_mock_index(mock_index, tmp_path):
    """EMBED_MODE=real 遇到 mock 建的索引：載入索引就失敗，不載模型、不寫成績。"""
    gold = write_gold(tmp_path / "gold.json", PASSING_TEXTS)
    out_dir = tmp_path / "out"
    proc = run_cli(
        mock_index,
        "--gold", str(gold), "--out-dir", str(out_dir), "--run-id", "t-real",
        "--index-receipt", str(mock_index["receipt"]),
        env_over={"EMBED_MODE": "real"},
    )  # fmt: skip
    assert proc.returncode == 2
    assert "索引" in proc.stderr and "embed_mode" in proc.stderr.lower()
    assert not out_dir.exists()


def test_mock_receipt_cannot_back_a_real_run(mock_index, tmp_path):
    """索引 manifest 被改成 real（冒充真模型索引）：mock 收據對不上、也不是 real，不跑。"""
    data = tmp_path / "data"
    shutil.copytree(mock_index["data"] / "index", data / "index")
    manifest_path = data / "index" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(manifest | {"embed_mode": "real"}), encoding="utf-8")
    gold = write_gold(tmp_path / "gold.json", PASSING_TEXTS)
    out_dir = tmp_path / "out"
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPT), "--gold", str(gold), "--out-dir", str(out_dir),
            "--run-id", "t-fake-real", "--index-receipt", str(mock_index["receipt"]),
        ],
        env=base_env(data, EMBED_MODE="real"),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )  # fmt: skip
    assert proc.returncode == 2
    assert "不是真模型索引" in proc.stderr and "manifest" in proc.stderr
    assert not out_dir.exists()


def test_cli_never_opens_env_file(mock_index, tmp_path):
    """子行程：repo 有 .env 時（用假的 .env 模擬），評估全程沒有打開它；對照組證明偵測有效。"""
    sentinel = tmp_path / ".env"
    sentinel.write_text("MODEL_KEEP_ALIVE=from-dotenv-sentinel\nEMBED_MODE=real\n", "utf-8")
    watch = tmp_path / "watch_env.py"
    watch.write_text(WATCH_ENV, encoding="utf-8")
    gold = write_gold(tmp_path / "gold.json", PASSING_TEXTS)
    args = [
        "--gold", str(gold), "--accounts", "sales", "--index-receipt", str(mock_index["receipt"]),
    ]  # fmt: skip

    log, out_dir = tmp_path / "normal.json", tmp_path / "out"
    proc = run_cli(
        mock_index,
        *args, "--out-dir", str(out_dir), "--run-id", "t-env",
        wrapper=[str(watch), str(ROOT), str(log), str(sentinel), "normal"],
    )  # fmt: skip
    seen = json.loads(log.read_text(encoding="utf-8"))
    assert proc.returncode in (0, 1), proc.stderr[-4000:]
    assert seen == {"opened": [], "code": proc.returncode}
    report = json.loads((out_dir / "t-env-drawing-retrieval.json").read_text(encoding="utf-8"))
    assert Path(report["isolation"]["env_file_before"]).resolve() == sentinel.resolve()
    assert report["isolation"]["env_file_disabled"] is True
    assert report["embed_mode"] == "mock"  # 假 .env 的 EMBED_MODE=real 沒有被讀到

    # 對照組：bootstrap 之前就讀過設定 → 有打開假 .env，評估拒絕執行、不寫成績
    log2, out2 = tmp_path / "read-first.json", tmp_path / "out2"
    proc2 = run_cli(
        mock_index,
        *args, "--out-dir", str(out2), "--run-id", "t-env2",
        wrapper=[str(watch), str(ROOT), str(log2), str(sentinel), "read-first"],
    )  # fmt: skip
    seen2 = json.loads(log2.read_text(encoding="utf-8"))
    assert seen2["opened"] and all(Path(p).name == ".env" for p in seen2["opened"])
    assert proc2.returncode == 2 and "bootstrap" in proc2.stderr
    assert not out2.exists()
