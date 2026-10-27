"""GET /eval/runs 只回 RAGAS run：分層檢索的索引收據（-retrieval-index）與
檢索層指標（-retrieval-layer-metrics）格式不同，沒排除會讓回應驗證失敗（500）。"""

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


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    runs = tmp_path / "eval" / "runs"
    runs.mkdir(parents=True)
    monkeypatch.setattr(system, "REPO_ROOT", tmp_path)
    return runs


@pytest.mark.parametrize(
    ("suffix", "body"),
    [
        ("-retrieval-index.json", RETRIEVAL_INDEX),
        ("-retrieval-layer-metrics.json", RETRIEVAL_LAYER_METRICS),
    ],
)
def test_eval_runs_skips_retrieval_runs(client, runs_dir, suffix, body):
    (runs_dir / f"{RAGAS['run_id']}.json").write_text(json.dumps(RAGAS), encoding="utf-8")
    (runs_dir / f"{body['run_id']}{suffix}").write_text(json.dumps(body), encoding="utf-8")
    r = client.get("/api/v1/eval/runs")
    assert r.status_code == 200, r.text
    assert [run["run_id"] for run in r.json()["runs"]] == [RAGAS["run_id"]]
