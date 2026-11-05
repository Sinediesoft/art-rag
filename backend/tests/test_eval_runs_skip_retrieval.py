"""GET /eval/runs 排除全部 17 種非 RAGAS suffix，保留正常與近似檔名並依檔名倒序。"""

import json

import pytest

from app.api.routers import system

RAGAS = {
    "run_id": "20261007T035422-4e81",
    "created_at": "2026-10-07T03:56:23.797066+00:00",
    "kb_version": "2026.10.1",
    "prompt_version": "answer_v4",
    "summary": {},
    "by_strategy": {},
}
# 與 eval/runs/ 真產物相同的頂層形狀（只留辨識用的欄位）
RETRIEVAL_INDEX = {
    "schema_version": "retrieval-index-receipt/1",
    "kind": "retrieval-index-receipt",
    "run_id": "20261027T184737-3b67",
    "created_at": "2026-10-27T18:47:37.921627+00:00",
    "real": True,
    "manifest": {},
}
RETRIEVAL_LAYER_METRICS = {
    "schema_version": "retrieval-layer-metrics/1",
    "kind": "retrieval-layer-metrics",
    "run_id": "20261027T184738-52cd",
    "created_at": "2026-10-27T18:47:38.481943+00:00",
    "status": "ok",
    "summary": {},
}
# 靜態列出正式格式契約，不從 router 的 tuple 或 AST 擷取，避免與產品突變一起改動。
NON_RAGAS_SUFFIXES = (
    "-drawing-retrieval.json",
    "-retrieval-index.json",
    "-retrieval-layer-metrics.json",
    "-generation-accounts.json",
    "-cad.json",
    "-router.json",
    "-sql.json",
    "-color.json",
    "-demo.json",
    "-route.json",
    "-guard.json",
    "-align.json",
    "-intake.json",
    "-style.json",
    "-style-head.json",
    "-versions.json",
    "-met-photos.json",
)


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    runs = tmp_path / "eval" / "runs"
    runs.mkdir(parents=True)
    monkeypatch.setattr(system, "REPO_ROOT", tmp_path)
    return runs


@pytest.mark.parametrize("suffix", NON_RAGAS_SUFFIXES)
def test_eval_runs_skips_non_ragas_suffixes(client, runs_dir, suffix):
    body = {
        "-retrieval-index.json": RETRIEVAL_INDEX,
        "-retrieval-layer-metrics.json": RETRIEVAL_LAYER_METRICS,
    }.get(suffix, {"run_id": "20261028T120000-other", "kind": "non-ragas"})
    (runs_dir / f"{RAGAS['run_id']}.json").write_text(json.dumps(RAGAS), encoding="utf-8")
    (runs_dir / f"{body['run_id']}{suffix}").write_text(json.dumps(body), encoding="utf-8")
    r = client.get("/api/v1/eval/runs")
    assert r.status_code == 200, r.text
    assert [run["run_id"] for run in r.json()["runs"]] == [RAGAS["run_id"]]


def test_eval_runs_keeps_ragas_in_reverse_filename_order(client, runs_dir):
    # 刻意讓 run_id 與 created_at 不依檔名排序，確認實際契約是檔名倒序。
    older = {**RAGAS, "run_id": "z-old-file", "created_at": "2026-10-28T12:00:00+00:00"}
    middle = {**RAGAS, "run_id": "m-middle-file"}
    newer = {**RAGAS, "run_id": "a-new-file", "created_at": "2026-10-01T12:00:00+00:00"}
    for filename, body in (
        ("20261007T120000-middle.json", middle),
        ("20261028T120000-new.json", newer),
        ("20261001T120000-old.json", older),
    ):
        (runs_dir / filename).write_text(json.dumps(body), encoding="utf-8")

    response = client.get("/api/v1/eval/runs")

    assert response.status_code == 200, response.text
    assert response.json()["runs"] == [newer, middle, older]


@pytest.mark.parametrize("suffix", NON_RAGAS_SUFFIXES)
def test_eval_runs_keeps_similar_but_nonmatching_suffixes(client, runs_dir, suffix):
    near_match = {**RAGAS, "run_id": "20261028T120000-near-match"}
    filename = f"{near_match['run_id']}{suffix.removesuffix('.json')}-extra.json"
    (runs_dir / filename).write_text(json.dumps(near_match), encoding="utf-8")
    (runs_dir / f"{RAGAS['run_id']}.json").write_text(json.dumps(RAGAS), encoding="utf-8")

    response = client.get("/api/v1/eval/runs")

    assert response.status_code == 200, response.text
    assert response.json()["runs"] == [near_match, RAGAS]


GENERATION_ACCOUNTS = {
    # 新生成帳號 run 的真形狀：頂層刻意缺 EvalRun 必填欄位也不能讓 /eval/runs 出錯
    "schema_version": "generation-accounts/1",
    "kind": "generation-accounts",
    "run_id": "20261029T120000-gen1",
    "created_at": "2026-10-29T12:00:00+00:00",
    "real": True,
    "rows": [],
}


def test_eval_runs_mixed_ragas_generation_accounts_and_retrieval(client, runs_dir):
    ragas_old = {**RAGAS, "run_id": "20261006T010101-old1"}
    ragas_new = {**RAGAS, "run_id": "20261028T010101-new1"}
    files = {
        f"{ragas_old['run_id']}.json": ragas_old,
        f"{ragas_new['run_id']}.json": ragas_new,
        f"{GENERATION_ACCOUNTS['run_id']}-generation-accounts.json": GENERATION_ACCOUNTS,
        f"{RETRIEVAL_INDEX['run_id']}-retrieval-index.json": RETRIEVAL_INDEX,
        f"{RETRIEVAL_LAYER_METRICS['run_id']}-retrieval-layer-metrics.json": (
            RETRIEVAL_LAYER_METRICS
        ),
    }
    for name, body in files.items():
        (runs_dir / name).write_text(json.dumps(body), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in runs_dir.iterdir()}

    response = client.get("/api/v1/eval/runs")

    assert response.status_code == 200, response.text
    assert response.json()["runs"] == [ragas_new, ragas_old]
    assert {p.name: p.read_bytes() for p in runs_dir.iterdir()} == before  # 不重寫任何歷史檔


def test_eval_runs_only_generation_accounts_file_gives_empty_list(client, runs_dir):
    name = f"{GENERATION_ACCOUNTS['run_id']}-generation-accounts.json"
    (runs_dir / name).write_text(json.dumps(GENERATION_ACCOUNTS), encoding="utf-8")
    response = client.get("/api/v1/eval/runs")
    assert response.status_code == 200, response.text
    assert response.json() == {"runs": []}
