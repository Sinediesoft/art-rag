"""生成層多帳號評測產物的證據契約（TASK-20261029-001，AC-8、AC-9）：
摘要可由逐題紀錄獨立重算、provenance 與 hash 可驗證、不覆寫既有 run、mock 明確標示、
新格式不破壞 GET /eval/runs。

真 run 與 README 的契約（AC-11、AC-12）等真 run 產出後的批次再補，這個檔案目前不含。"""
# ruff: noqa: F811

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from test_run_eval_accounts import (  # noqa: F401  (env 是 fixture，要匯入才能用)
    DEGRADE,
    QUESTIONS,
    FakeBackend,
    answer_events,
    env,
    part_source,
    run_eval,
    stage1_events,
)

SUFFIX = "-generation-accounts.json"


def mixed_backend():
    """各種結果都有：答對、答錯、拒答、外洩、錯誤、fallback。"""

    def responder(account, body):
        q, pid = body["question"], body["part_id"]
        if pid == "p-ok" and "扭力" in q:
            ev = answer_events("扭力 9 N·m [1]", [part_source(1, "p-ok", "規格")])
            if account == "sales":
                ev[2][1]["fallback"] = True
            return ev
        if pid == "p-hid" and "材料" in q:
            if account == "sales":
                return stage1_events(DEGRADE + "是 S45C。")  # 外洩
            return answer_events("材料是 S45C [1]", [part_source(1, "p-hid", "材料")])
        if pid == "p-ok" and "價格" in q:
            return 500 if account == "sales" else stage1_events("價格是 3000 元")
        return stage1_events()

    return FakeBackend(responder=responder)


def run_mixed(env, *options, strategies="hybrid,hybrid_noimg"):
    assert env.run(mixed_backend(), *options, strategies=strategies) in (0, 3)
    return env.artifact()


def independent_summary(rows):
    """測試自己的重算：只用逐題紀錄的欄位，不呼叫評測器的統計函式。"""
    out = {}
    subsets = ("visible_answerable", "permission_degraded", "no_answer", "missing_part")
    for account in sorted({r["account"] for r in rows}):
        for strategy in sorted({r["strategy"] for r in rows if r["account"] == account}):
            mine = [r for r in rows if (r["account"], r["strategy"]) == (account, strategy)]
            for name in ("overall", *subsets):
                sel = (
                    mine if name == "overall" else [r for r in mine if r["classification"] == name]
                )
                cit = [r for r in sel if r["citation_ok"] is not None]
                hid = [r for r in sel if r["classification"] == "permission_degraded"]

                def m(num, den):
                    return {"num": num, "den": den, "rate": num / den if den else None}

                out[(account, strategy, name)] = {
                    "n": len(sel),
                    "errors": sum(bool(r["error"]) for r in sel),
                    "fallbacks": sum(bool(r["fallback"]) for r in sel),
                    "answer_ok": m(sum(r["answer_ok"] is True for r in sel), len(sel)),
                    "citation_ok": m(sum(r["citation_ok"] is True for r in cit), len(cit)),
                    "refusal": m(sum(r["refused"] is True for r in sel), len(sel)),
                    "leak": m(sum(r["leaked"] is True for r in hid), len(hid)),
                }
    return out


def test_summaries_recompute_from_rows(env):
    art = run_mixed(env)
    expected = independent_summary(art["rows"])
    seen = set()
    for account, by_strategy in art["groups"].items():
        for strategy, subsets in by_strategy.items():
            assert set(subsets) == {
                "overall",
                "visible_answerable",
                "permission_degraded",
                "no_answer",
                "missing_part",
            }
            for name, got in subsets.items():
                assert got == expected[(account, strategy, name)], (account, strategy, name)
                seen.add((account, strategy, name))
    assert seen == set(expected) and len(seen) == 2 * 2 * 5
    # 摘要不剔除錯誤與 fallback，全部題目都留在分母
    for account in ("manager", "sales"):
        assert art["groups"][account]["hybrid"]["overall"]["n"] == len(QUESTIONS)
    sales = art["groups"]["sales"]["hybrid"]
    assert sales["overall"]["errors"] == 1 and sales["overall"]["fallbacks"] == 1
    assert sales["permission_degraded"]["leak"]["num"] == 1
    # 拒答不灌高可答率：可答子集與整體分開
    assert sales["visible_answerable"]["answer_ok"]["rate"] != sales["overall"]["answer_ok"]["rate"]


def test_summaries_are_rederived_by_the_evaluator_function_too(env):
    art = run_mixed(env)
    assert run_eval.summarize_rows(art["rows"]) == art["groups"]


def test_row_scores_are_recomputable_from_stored_events(env):
    """判分用到的欄位都能由 events / answer 重算，不是只存結論。"""
    art = run_mixed(env)
    for r in art["rows"]:
        sources = [e["data"] for e in r["events"] if e["event"] == "sources"]
        text = "".join(e["data"]["text"] for e in r["events"] if e["event"] == "token")
        assert text == r["answer"]
        assert r["sources_state"] == (
            "absent"
            if not sources
            else "empty"
            if all(not s.get("sources") for s in sources)
            else "present"
        )
        if r["classification"] == "permission_degraded":
            q = next(q for q in QUESTIONS if q["id"] == r["question_id"])
            assert run_eval.leak_reasons(q, r["answer"], r["events"]) == r["leak_reasons"]
            assert r["leaked"] is bool(r["leak_reasons"])


def test_provenance_and_hashes_are_verifiable(env):
    art = run_mixed(env)
    prov = art["provenance"]
    assert prov["git"] == {"head": "a" * 40, "dirty": False, "dirty_paths": [], "error": None}
    h = prov["hashes"]
    assert h["qa_sha256"] == hashlib.sha256(env.qa.read_bytes()).hexdigest()
    assert h["gold_sha256"] == hashlib.sha256(env.gold.read_bytes()).hexdigest()
    assert h["answers_sha256"] is None  # 自訂題庫沒有旁附答案檔
    assert h["kb_version"] == "kb-fixed-17" == art["kb_version"]
    files = sorted(env.kb_parts.glob("*.json"))
    tree = hashlib.sha256(
        "".join(f"{p.name}:{hashlib.sha256(p.read_bytes()).hexdigest()}\n" for p in files).encode()
    ).hexdigest()
    assert h["kb_parts_sha256"] == tree
    assert prov["strategies"] == ["hybrid", "hybrid_noimg"]
    assert prov["accounts"] == ["manager", "sales"]
    assert prov["question_ids"] == [q["id"] for q in QUESTIONS]
    assert prov["models"] == ["fake-llm", "第 1 段（未呼叫 LLM）"]
    assert prov["llm_mode"] == "real" and prov["base_url"] == "http://eval.invalid"
    assert art["created_at"].endswith("+00:00") and art["schema_version"] == "generation-accounts/1"
    assert art["kind"] == "generation-accounts" and art["real"] is True and art["mock"] is False
    assert set(art["snapshots"]) == {"manager", "sales"}
    for acct, snap in art["snapshots"].items():
        assert snap["identity"]["id"] == acct and "fetched_at" in snap
        assert snap["visible_part_ids"] == sorted(snap["visible_part_ids"])


def test_dirty_worktree_is_recorded(env, monkeypatch):
    monkeypatch.setattr(
        run_eval,
        "_git_provenance",
        lambda: {"head": "b" * 40, "dirty": True, "dirty_paths": ["x.py"], "error": None},
    )
    art = run_mixed(env, strategies="hybrid")
    assert art["provenance"]["git"]["dirty"] is True and art["provenance"]["git"][
        "dirty_paths"
    ] == ["x.py"]


def test_answers_sidecar_hash_for_drawing_question_bank(env):
    """題庫是圖紙段落題庫時，旁附答案檔的 hash 也要記；原檔位元組不變。"""
    from test_run_eval_accounts import ROOT

    drawing = ROOT / "eval" / "drawing_retrieval"
    env.qa = drawing / "qa.jsonl"
    parts = [f"mfg-00{i}" for i in range(1, 7)]
    backend = FakeBackend(visible={"manager": parts, "sales": parts[3:5]})
    assert env.run(backend) == 0
    art = env.artifact()
    h = art["provenance"]["hashes"]
    assert h["qa_sha256"] == hashlib.sha256((drawing / "qa.jsonl").read_bytes()).hexdigest()
    assert (
        h["answers_sha256"] == hashlib.sha256((drawing / "answers.jsonl").read_bytes()).hexdigest()
    )
    assert len(art["rows"]) == 2 * 33
    # 答案旁附檔依 id 合併後的題目：可答題帶 keywords、未審核註記保留
    row = env.rows(art)[("manager", "hybrid", "d-mfg002-06")]
    assert row["review_status"] == "unreviewed"
    assert row["expected_keywords"] and row["classification"] == "visible_answerable"


def test_output_never_overwrites_existing_run(env):
    env.runs.mkdir()
    history = {
        "legacy.json": "舊 RAGAS run",
        "legacy.csv": "a,b\n",
        "keep-generation-accounts.json": "已存在的新格式 run",
    }
    for name, text in history.items():
        (env.runs / name).write_text(text, "utf-8")
    before = {p.name: p.read_bytes() for p in env.runs.iterdir()}
    backend = FakeBackend()
    assert env.run(backend, "--run-id", "keep") == 2
    assert backend.chats == []  # 寫不下去的 run 不浪費時間先跑完
    assert {p.name: p.read_bytes() for p in env.runs.iterdir()} == before
    # 另一個識別碼可以並存，歷史檔案不變
    assert env.run(FakeBackend(), "--run-id", "fresh") == 0
    assert {p.name: p.read_bytes() for p in env.runs.iterdir() if p.name in before} == before
    assert (env.runs / f"fresh{SUFFIX}").exists()


def test_generated_run_ids_do_not_collide_and_default_name_has_suffix(env, monkeypatch):
    assert env.run(FakeBackend()) == 0
    assert env.run(FakeBackend()) == 0
    files = sorted(p.name for p in env.runs.glob(f"*{SUFFIX}"))
    assert len(files) == 2 and all(len(f.split("-")[0]) == 15 for f in files)
    assert all(not p.name.endswith(".csv") for p in env.runs.iterdir())  # 不產出舊格式 CSV


def test_mock_run_is_marked_and_never_written_to_official_runs(env):
    backend = FakeBackend()
    backend.llm_mode = "mock"
    assert env.run(backend) == 0
    art = env.artifact()
    assert art["real"] is False and art["mock"] is True
    assert art["provenance"]["llm_mode"] == "mock" and art["mock_reasons"]
    # 正式 run 目錄（eval/runs）不收 mock 產物
    official = env.tmp / "official-runs"
    backend2 = FakeBackend()
    backend2.llm_mode = "mock"
    assert env.run(backend2, "--out-dir", str(official)) == 2
    assert not official.exists() or not list(official.iterdir())
    # mock 策略同樣標示
    backend3 = FakeBackend()
    assert env.run(backend3, "--run-id", "mockstrat", strategies="mock") == 0
    assert env.artifact("mockstrat")["mock"] is True
    assert env.run(FakeBackend(), "--run-id", "okofficial", "--out-dir", str(official)) == 0
    assert (official / f"okofficial{SUFFIX}").exists()


def test_unknown_llm_mode_is_not_treated_as_real(env):
    backend = FakeBackend()
    backend.llm_mode = None
    assert env.run(backend) == 0
    art = env.artifact()
    assert art["real"] is False and art["mock"] is False and art["mock_reasons"] == []
    assert art["provenance"]["llm_mode"] is None


def test_llm_mode_unknown_without_manager_diagnostics(env):
    """只評 sales：diagnostics 回 403，llm_mode 未知，不假裝成真 run，仍正常完成。"""
    assert env.run(FakeBackend(), accounts="sales") == 0
    art = env.artifact()
    assert art["real"] is False and art["mock"] is False and art["mock_reasons"] == []
    assert art["provenance"]["llm_mode"] is None
    assert art["provenance"]["manifest"] is None and art["provenance"]["embed_mode"] is None
    assert len(art["rows"]) > 0


def test_diagnostics_without_llm_mode_field_is_unknown(env):
    backend = FakeBackend()
    backend.llm_mode = None  # diagnostics 回 200 但省略 llm_mode
    assert env.run(backend) == 0
    art = env.artifact()
    assert art["real"] is False and art["mock"] is False and art["mock_reasons"] == []
    prov = art["provenance"]
    assert prov["llm_mode"] is None
    assert prov["manifest"] == {"kb_version": "kb-fixed-17"}  # 其他欄位照樣保留


def test_diagnostics_fields_are_recorded_when_manager_is_evaluated(env):
    assert env.run(FakeBackend()) == 0
    prov = env.artifact()["provenance"]
    assert prov["llm_mode"] == "real" and prov["embed_mode"] == "real"
    assert prov["eval_controls"] is True and prov["manifest"] == {"kb_version": "kb-fixed-17"}


def test_new_artifact_is_excluded_from_eval_runs_endpoint(env, client, monkeypatch):
    """新產物掉進 eval/runs 後，GET /eval/runs 因 -generation-accounts.json 在排除清單而不列出它，
    歷史 RAGAS run 照常列出。"""
    from app.api.routers import system

    runs = env.tmp / "eval-root" / "eval" / "runs"
    runs.mkdir(parents=True)
    ragas = {
        "run_id": "20261007T035422-4e81",
        "created_at": "2026-10-07T03:56:23+00:00",
        "kb_version": "2026.10.1",
        "prompt_version": "answer_v4",
        "summary": {},
        "by_strategy": {},
    }
    (runs / f"{ragas['run_id']}.json").write_text(json.dumps(ragas), "utf-8")
    art = run_mixed(env, strategies="hybrid")
    (runs / f"{art['run_id']}{SUFFIX}").write_text(json.dumps(art, ensure_ascii=False), "utf-8")
    monkeypatch.setattr(system, "REPO_ROOT", env.tmp / "eval-root")
    r = client.get("/api/v1/eval/runs")
    assert r.status_code == 200, r.text
    ids = [x["run_id"] for x in r.json()["runs"]]
    assert ragas["run_id"] in ids
    assert art["run_id"] not in ids
    assert ids == [ragas["run_id"]]


def test_artifact_top_level_keeps_legacy_run_shape(env):
    art = run_mixed(env, strategies="hybrid")
    for key, kind in (
        ("run_id", str),
        ("created_at", str),
        ("kb_version", str),
        ("prompt_version", str),
        ("summary", dict),
        ("by_strategy", dict),
    ):
        assert isinstance(art[key], kind), key
    assert art["by_strategy"] == {}  # 新模式的成績在 groups，不塞進舊格式的策略表
    assert art["run_id"] == next(env.runs.glob(f"*{SUFFIX}")).name.removesuffix(SUFFIX)
    assert art["prompt_version"] == "drawing_v2"


# ---------------------------------------------------------------------------
# 真 run 證據契約（AC-11）與 README 對回真 run（AC-12）
# 只讀已提交的正式產物與 repo 檔案，不連模型或服務，也不使用 client fixture。
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_RUNS = _REPO / "eval" / "runs"
_RUN_ID = "20261029T130000-ga01"
_ART_PATH = _RUNS / f"{_RUN_ID}-generation-accounts.json"
_RECEIPT_PATH = _RUNS / "20261029T130000-ri01-retrieval-index.json"
_EVIDENCE = _RUNS / f"{_RUN_ID}-generation-accounts-evidence"
_ART_SHA = "0fb3f2e056d979dbd443bb4cf047fa7be5999eeff74ceebbda08665ddaba1471"
_RECEIPT_SHA = "84c137fd42db3a52928b1ac2b7764a9d7021437e6f01a7286f32605a55ebbae4"
_HEAD = "905be832284682dea209facd7acdc7129d83310e"
_KB_VERSION = "2026.10.2"
_MODEL = "qwen3-vl:4b-instruct"
_SUBSETS = ("overall", "visible_answerable", "permission_degraded", "no_answer", "missing_part")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_real():
    art = json.loads(_ART_PATH.read_text("utf-8"))
    receipt = json.loads(_RECEIPT_PATH.read_text("utf-8"))
    return art, receipt


def _metric(num, den):
    return {"num": num, "den": den, "rate": num / den if den else None}


def _recompute_groups(rows):
    """自己從逐題 rows 重算 groups（只用 rows 欄位，不呼叫 summarize_rows）。"""
    out = {}
    for account in sorted({r["account"] for r in rows}):
        out[account] = {}
        for strategy in sorted({r["strategy"] for r in rows if r["account"] == account}):
            mine = [r for r in rows if (r["account"], r["strategy"]) == (account, strategy)]
            by = {}
            for name in _SUBSETS:
                sel = (
                    mine if name == "overall" else [r for r in mine if r["classification"] == name]
                )
                cit = [r for r in sel if r["citation_ok"] is not None]
                hid = [r for r in sel if r["classification"] == "permission_degraded"]
                by[name] = {
                    "n": len(sel),
                    "errors": sum(bool(r["error"]) for r in sel),
                    "fallbacks": sum(bool(r["fallback"]) for r in sel),
                    "answer_ok": _metric(sum(r["answer_ok"] is True for r in sel), len(sel)),
                    "citation_ok": _metric(sum(r["citation_ok"] is True for r in cit), len(cit)),
                    "refusal": _metric(sum(r["refused"] is True for r in sel), len(sel)),
                    "leak": _metric(sum(r["leaked"] is True for r in hid), len(hid)),
                }
            out[account][strategy] = by
    return out


def _sources_state(events):
    sources = [e["data"] for e in events if e["event"] == "sources"]
    if not sources:
        return "absent"
    return "empty" if all(not s.get("sources") for s in sources) else "present"


def _expect_refusal_metric(rows, account, key="answer_ok"):
    """預期拒答子集：expected_outcome 不是 answer（無答案、缺圖紙、權限降級）。"""
    sel = [r for r in rows if r["account"] == account and r["expected_outcome"] != "answer"]
    return _metric(sum(r[key] is True for r in sel), len(sel))


def _kb_parts_tree_sha():
    lines = "".join(
        f"{p.name}:{_sha(p.read_bytes())}\n"
        for p in sorted((_REPO / "kb" / "parts").glob("*.json"))
    )
    return _sha(lines.encode())


def real_run_problems(artifact, receipt, art_bytes=None, receipt_bytes=None):
    """核對真 run 產物自身的一致性；回傳問題清單（空 list 代表沒問題）。
    art_bytes / receipt_bytes 給了才核對檔案 sha256。"""
    p = []
    a = artifact
    prov = a.get("provenance") or {}
    if art_bytes is not None and _sha(art_bytes) != _ART_SHA:
        p.append("sha256 不符：主 JSON 位元組與固定值不一致")
    if receipt_bytes is not None and _sha(receipt_bytes) != _RECEIPT_SHA:
        p.append("sha256 不符：收據位元組與固定值不一致")
    if a.get("schema_version") != "generation-accounts/1":
        p.append(f"schema_version 不對：{a.get('schema_version')!r}")
    if a.get("real") is not True:
        p.append("real 不是 True")
    if a.get("mock") is not False:
        p.append("mock 不是 False（mock run 不能當正式證據）")
    if a.get("mock_reasons"):
        p.append(f"mock_reasons 非空：{a.get('mock_reasons')}")
    if prov.get("llm_mode") != "real":
        p.append(f"llm_mode 不是 real：{prov.get('llm_mode')!r}")
    git = prov.get("git") or {}
    if git.get("head") != _HEAD:
        p.append(f"git head 不符：{git.get('head')!r}")
    if git.get("dirty") is not False:
        p.append("git dirty 不是 False")
    if _MODEL not in (prov.get("models") or []):
        p.append(f"models 沒有 {_MODEL}")
    if a.get("strategies") != ["hybrid"] or prov.get("strategies") != ["hybrid"]:
        p.append("strategies 不是 ['hybrid']")
    if a.get("accounts") != ["manager", "sales"] or prov.get("accounts") != ["manager", "sales"]:
        p.append("accounts 不是 manager、sales")
    if (
        a.get("kb_version") != _KB_VERSION
        or (prov.get("hashes") or {}).get("kb_version") != _KB_VERSION
    ):
        p.append(f"kb_version 不是 {_KB_VERSION}")

    rows = a.get("rows") or []
    qids = prov.get("question_ids") or []
    if len(qids) != 33 or len(set(qids)) != 33:
        p.append(f"question_ids 不是 33 個唯一題號：{len(qids)}")
    for acct in ("manager", "sales"):
        got = [r["question_id"] for r in rows if r["account"] == acct]
        if len(got) != 33 or len(set(got)) != 33:
            p.append(f"缺題或重複：{acct} 有 {len(got)} 列（唯一 {len(set(got))}），應為 33")
        elif sorted(got) != sorted(qids):
            p.append(f"題號與 provenance.question_ids 不一致：{acct}")
    if len(rows) != 66:
        p.append(f"rows 不是 66 列：{len(rows)}")
    if (a.get("summary") or {}).get("n_rows") != len(rows):
        p.append("summary.n_rows 與 rows 數量不一致")

    generated = [
        r for r in rows if ((r.get("done") or {}).get("tokens") or {}).get("output", 0) > 0
    ]
    if len(generated) != 41:
        p.append(f"實際生成列數不是 41：{len(generated)}")
    for acct, want in (("manager", 31), ("sales", 10)):
        n = sum(r["account"] == acct for r in generated)
        if n != want:
            p.append(f"實際生成列數不符：{acct} {n} != {want}")
    for r in rows:
        ev = r.get("events") or []
        where = f"{r['account']}/{r['question_id']}"
        if not all(isinstance(e, dict) and set(e) == {"seq", "event", "data"} for e in ev):
            p.append(f"events 不是 lossless [{{seq,event,data}}]：{where}")
            continue
        if [e["seq"] for e in ev] != list(range(len(ev))):
            p.append(f"events seq 不連續：{where}")
        if r in generated and not any(e["event"] == "done" for e in ev):
            p.append(f"生成列缺 done 事件：{where}")
        if r.get("sources_state") != _sources_state(ev):
            p.append(f"sources_state 與 events 內 sources 事件不一致：{where}")

    # 摘要獨立重算
    if _recompute_groups(rows) != a.get("groups"):
        p.append("groups 與逐題 rows 獨立重算的結果不一致（summary 數字被改或 answer_ok 被改）")

    # provenance 與收據串接
    manifest = prov.get("manifest")
    if manifest != receipt.get("manifest"):
        p.append("provenance.manifest 與收據 manifest 不一致")
    canon = json.dumps(receipt.get("manifest"), sort_keys=True, separators=(",", ":"))
    if _sha(canon.encode()) != receipt.get("manifest_sha256"):
        p.append("收據 manifest_sha256 與 manifest 內容不符（hash 被改）")
    if receipt.get("manifest_problems"):
        p.append("收據 manifest_problems 非空")
    if (receipt.get("code") or {}).get("head") != git.get("head"):
        p.append("收據 code.head 與主 JSON git head 不一致")
    if (receipt.get("code") or {}).get("dirty") is not False:
        p.append("收據 code dirty 不是 False")
    if not str((manifest or {}).get("git_commit", "")) or not git.get("head", "").startswith(
        str((manifest or {}).get("git_commit"))
    ):
        p.append("manifest.git_commit 不是主 JSON git head 的前綴")
    if (receipt.get("kb") or {}).get("version") != _KB_VERSION or (manifest or {}).get(
        "kb_version"
    ) != _KB_VERSION:
        p.append("收據 kb version 不符")
    if receipt.get("real") is not True or receipt.get("embed_mode") != "real":
        p.append("收據不是真 embed")
    hashes = prov.get("hashes") or {}
    rq = receipt.get("qa") or {}
    for key, rel in (
        ("qa_sha256", "eval/drawing_retrieval/qa.jsonl"),
        ("answers_sha256", "eval/drawing_retrieval/answers.jsonl"),
        ("gold_sha256", "eval/drawing_retrieval/gold.json"),
    ):
        if hashes.get(key) != rq.get(rel):
            p.append(f"{key} 與收據 qa[{rel}] 不一致（hash 被改）")
    return p


def _repo_hash_problems(artifact):
    """用 repo 內實際檔案重算 hash，與主 JSON provenance 對照（只比 hash，不比 qa_files 路徑）。"""
    d = _REPO / "eval" / "drawing_retrieval"
    h = artifact["provenance"]["hashes"]
    exp = {
        "qa_sha256": _sha((d / "qa.jsonl").read_bytes()),
        "answers_sha256": _sha((d / "answers.jsonl").read_bytes()),
        "gold_sha256": _sha((d / "gold.json").read_bytes()),
        "kb_parts_sha256": _kb_parts_tree_sha(),
    }
    return [f"{k} 與 repo 實際檔案重算不符" for k, v in exp.items() if h.get(k) != v]


def _handoff_entries():
    entries = {}
    for line in (_EVIDENCE / "handoff-sha256.txt").read_text("utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        entries[name] = digest
    return entries


def test_real_run_evidence_contract():
    art, receipt = _load_real()
    art_bytes, receipt_bytes = _ART_PATH.read_bytes(), _RECEIPT_PATH.read_bytes()
    assert _sha(art_bytes) == _ART_SHA and _sha(receipt_bytes) == _RECEIPT_SHA
    assert real_run_problems(art, receipt, art_bytes, receipt_bytes) == []
    assert _repo_hash_problems(art) == []

    # 固定事實（不只靠函式）
    prov = art["provenance"]
    assert art["schema_version"] == "generation-accounts/1"
    assert art["real"] is True and art["mock"] is False and art["mock_reasons"] == []
    assert prov["git"]["head"] == _HEAD and prov["git"]["dirty"] is False
    assert prov["llm_mode"] == "real" and _MODEL in prov["models"]
    assert art["strategies"] == ["hybrid"] and art["accounts"] == ["manager", "sales"]
    assert prov["hashes"]["kb_version"] == _KB_VERSION == art["kb_version"]
    rows = art["rows"]
    assert len(rows) == 66
    for acct in ("manager", "sales"):
        ids = [r["question_id"] for r in rows if r["account"] == acct]
        assert len(ids) == 33 == len(set(ids))
    generated = [r for r in rows if r["done"]["tokens"]["output"] > 0]
    assert len(generated) == 41
    assert sum(r["account"] == "manager" for r in generated) == 31
    assert sum(r["account"] == "sales" for r in generated) == 10
    for r in generated:
        assert any(e["event"] == "done" for e in r["events"])
        assert r["sources_state"] == _sources_state(r["events"])
    for r in rows:
        assert all(set(e) == {"seq", "event", "data"} for e in r["events"])
        assert r["sources_state"] == _sources_state(r["events"])

    # 證據目錄：每筆 handoff 都對上實際檔案，且沒漏列證據檔
    entries = _handoff_entries()
    assert len(entries) == 41
    assert entries[_ART_PATH.name] == _ART_SHA and entries[_RECEIPT_PATH.name] == _RECEIPT_SHA
    for name, digest in entries.items():
        assert _sha((_RUNS / name).read_bytes()) == digest, name
    evidence_files = sorted(p for p in _EVIDENCE.rglob("*") if p.is_file())
    assert all(p.parent == _EVIDENCE for p in evidence_files), "證據目錄不應有子目錄檔案"
    listed = {name for name in entries if name.startswith(_EVIDENCE.name + "/")}
    actual = {
        f"{_EVIDENCE.name}/{p.name}" for p in evidence_files if p.name != "handoff-sha256.txt"
    }
    assert listed == actual, (sorted(actual - listed), sorted(listed - actual))
    assert f"{_EVIDENCE.name}/handoff-sha256.txt" not in entries  # 不含自身

    # 結束碼照實：driver 的 tee wait=127，final_exit=22，其餘皆 0
    assert (_EVIDENCE / "generation-exit.txt").read_text("utf-8").strip() == "0"
    assert (_EVIDENCE / "driver-tee-exit.txt").read_text("utf-8").strip() == "127"
    summary = dict(
        line.split("=", 1)
        for line in (_EVIDENCE / "handoff-summary.txt").read_text("utf-8").splitlines()
        if "=" in line
    )
    assert summary["code_sha"] == _HEAD
    assert summary["final_exit"] == "22"
    for key in ("eval_exit", "eval_tee_exit", "verify_exit", "eval_pipeline_exit", "stop_exit"):
        assert summary[key] == "0", key
    assert summary["restore_exit"] == "0" and summary["collect_exit"] == "0"
    status = [
        line.split("\t")
        for line in (_EVIDENCE / "pipeline-status.tsv").read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert status and all(cols[1:] == ["command=0", "tee=0"] for cols in status)

    # 摘要獨立重算：與 groups 完全一致（另外對照評測器函式，但不只靠它）
    got = _recompute_groups(rows)
    assert got == art["groups"]
    assert run_eval.summarize_rows(rows) == art["groups"]
    ind = independent_summary(rows)
    for acct, by_strategy in art["groups"].items():
        for strat, subsets in by_strategy.items():
            for name, g in subsets.items():
                assert g == ind[(acct, strat, name)]

    def frac(m):
        return f"{m['num']}/{m['den']}"

    gm, gs = art["groups"]["manager"]["hybrid"], art["groups"]["sales"]["hybrid"]
    assert frac(gm["visible_answerable"]["answer_ok"]) == "22/31"
    assert frac(gm["visible_answerable"]["citation_ok"]) == "30/31"
    assert frac(gm["overall"]["answer_ok"]) == "24/33"
    assert frac(gm["overall"]["refusal"]) == "8/33"
    assert frac(_expect_refusal_metric(rows, "manager")) == "2/2"
    assert gm["overall"]["leak"] == {"num": 0, "den": 0, "rate": None}
    assert frac(gs["visible_answerable"]["answer_ok"]) == "8/10"
    assert frac(gs["visible_answerable"]["citation_ok"]) == "10/10"
    assert frac(gs["overall"]["answer_ok"]) == "31/33"
    assert frac(gs["overall"]["refusal"]) == "25/33"
    assert frac(_expect_refusal_metric(rows, "sales")) == "23/23"
    assert frac(gs["overall"]["leak"]) == "0/21" and gs["overall"]["leak"]["rate"] == 0


def _contains(problems, keyword):
    return any(keyword in x for x in problems)


def _mutated(mutate):
    art, receipt = _load_real()
    art, receipt = copy.deepcopy(art), copy.deepcopy(receipt)
    mutate(art, receipt)
    return real_run_problems(art, receipt)


def _bad_real(a, r):
    a["real"] = False
    a["mock"] = True
    a["mock_reasons"] = ["x"]


def _bad_llm_mode(a, r):
    a["provenance"]["llm_mode"] = "mock"


def _drop_row(a, r):
    a["rows"] = [
        x for x in a["rows"] if (x["account"], x["question_id"]) != ("sales", "d-mfg001-01")
    ]


def _flip_answer_ok(a, r):
    row = next(x for x in a["rows"] if x["account"] == "manager" and x["answer_ok"] is True)
    row["answer_ok"] = False


def _bump_summary(a, r):
    a["groups"]["manager"]["hybrid"]["visible_answerable"]["answer_ok"]["num"] = 23


def _bad_head(a, r):
    a["provenance"]["git"]["head"] = "0" * 40


def _bad_hash(a, r):
    a["provenance"]["hashes"]["gold_sha256"] = "f" * 64


def _bad_manifest_hash(a, r):
    r["manifest_sha256"] = "f" * 64


def _bad_sources_state(a, r):
    row = next(x for x in a["rows"] if x["sources_state"] == "present")
    row["sources_state"] = "empty"


def _drop_done(a, r):
    row = next(x for x in a["rows"] if x["done"]["tokens"]["output"] > 0)
    row["events"] = [e for e in row["events"] if e["event"] != "done"]


@pytest.mark.parametrize(
    "mutate, keyword",
    [
        (_bad_real, "real"),
        (_bad_real, "mock"),
        (_bad_llm_mode, "llm_mode"),
        (_drop_row, "缺題"),
        (_flip_answer_ok, "groups"),
        (_bump_summary, "groups"),
        (_bad_head, "git head"),
        (_bad_hash, "gold_sha256"),
        (_bad_manifest_hash, "manifest_sha256"),
        (_bad_sources_state, "sources_state"),
        (_drop_done, "done"),
    ],
)
def test_real_run_problems_detects_bad_evidence(mutate, keyword):
    problems = _mutated(mutate)
    assert problems and _contains(problems, keyword), problems


def test_real_run_problems_detects_changed_file_bytes():
    art, receipt = _load_real()
    problems = real_run_problems(
        art, receipt, _ART_PATH.read_bytes() + b" ", _RECEIPT_PATH.read_bytes()
    )
    assert _contains(problems, "sha256")


# --- README 區塊 ---------------------------------------------------------

_TABLE_START = "<!-- generation-accounts-table:start -->"
_TABLE_END = "<!-- generation-accounts-table:end -->"
_RETRIEVAL_MARK = "<!-- retrieval-layer-table"
_FRACTION = re.compile(r"(?<![\d/])(\d+)\s*/\s*(\d+)(?![\d/])")


def _readme_block(text):
    """回傳 (區塊內文, 問題)；找不到標記時問題明確指出標記缺漏。"""
    if _TABLE_START not in text:
        return None, [f"README 找不到標記 {_TABLE_START}"]
    if _TABLE_END not in text:
        return None, [f"README 找不到標記 {_TABLE_END}"]
    s = text.index(_TABLE_START)
    e = text.index(_TABLE_END, s)
    return text[s + len(_TABLE_START) : e], []


def _table_rows(block):
    return [ln for ln in block.splitlines() if ln.strip().startswith("|")]


def _fractions(line):
    return {(int(n), int(d)) for n, d in _FRACTION.findall(line)}


def readme_table_problems(text, artifact):
    """README 的生成層表格要能對回正式 JSON；回傳問題清單。"""
    block, problems = _readme_block(text)
    if block is None:
        return problems
    rows = artifact["rows"]
    groups = artifact["groups"]
    table = _table_rows(block)
    if not table:
        problems.append("區塊內沒有 markdown 表格列")
    for acct in ("manager", "sales"):
        g = groups[acct]["hybrid"]
        lines = [ln for ln in table if re.search(rf"\b{acct}\b", ln)]
        if not lines:
            problems.append(f"表格沒有 {acct} 列")
            continue
        seen = set().union(*(_fractions(ln) for ln in lines))
        want = {
            "overall answer_ok": g["overall"]["answer_ok"],
            "visible_answerable answer_ok": g["visible_answerable"]["answer_ok"],
            "expected-refusal answer_ok": _expect_refusal_metric(rows, acct),
            "citation_ok": g["visible_answerable"]["citation_ok"],
            "overall REFUSAL": g["overall"]["refusal"],
            "leak": g["overall"]["leak"],
        }
        for label, m in want.items():
            if (m["num"], m["den"]) not in seen:
                problems.append(
                    f"{acct} 的 {label} {m['num']}/{m['den']} 不在表格列內或與 JSON 不一致"
                )
        # 表格列裡出現的分數，不得是沒有對應 JSON 值的錯誤數字
        allowed = {(m["num"], m["den"]) for m in want.values()} | {
            (v[k]["num"], v[k]["den"])
            for v in g.values()
            for k in ("answer_ok", "citation_ok", "refusal", "leak")
        }
        for n, d in sorted(seen - allowed):
            problems.append(f"{acct} 列出現無法對回 JSON 的數字 {n}/{d}")
        leak = g["overall"]["leak"]
        if leak["den"] == 0 and not any(
            (0, 0) in _fractions(ln) and re.search(r"n/a|—|null", ln) for ln in lines
        ):
            problems.append(f"{acct} leak 0/0 沒有標 n/a 或 —（rate=null）")
    return problems


def readme_block_problems(text, artifact):
    """README 區塊的連結、版本、揭露字樣與不混入檢索層既有表。"""
    block, problems = _readme_block(text)
    if block is None:
        return problems
    problems += readme_table_problems(text, artifact)
    hashes = artifact["provenance"]["hashes"]
    need = {
        "主 JSON 連結": "eval/runs/20261029T130000-ga01-generation-accounts.json",
        "收據連結": "20261029T130000-ri01-retrieval-index.json",
        "證據目錄連結": "20261029T130000-ga01-generation-accounts-evidence",
        "KB 版本": _KB_VERSION,
        "模型": _MODEL,
        "策略 hybrid": "hybrid",
        "執行 SHA（前 7 碼）": _HEAD[:7],
        "日期": "2026-10-29",
        "answers hash 前綴": hashes["answers_sha256"][:8],
        "gold hash 前綴": hashes["gold_sha256"][:8],
        "final_exit": "final_exit",
        "keyword proxy 說明": "代理",
        "REFUSAL 說明": "REFUSAL",
        "外洩說明": "外洩",
    }
    for label, needle in need.items():
        if needle not in block:
            problems.append(f"區塊缺少 {label}：{needle}")
    if not re.search(r"final_exit\D{0,12}22", block):
        problems.append("區塊沒揭露 final_exit=22")
    if len(re.findall(r"wait\D{0,6}127", block)) < 1 and "127" not in block:
        problems.append("區塊沒揭露 driver tee wait=127")
    if not re.search(r"audit|bash32-tee-fix", block):
        problems.append("區塊沒指向 audit 或 bash32-tee-fix 報告")
    if _RETRIEVAL_MARK in block:
        problems.append("生成層區塊混入檢索層標記 <!-- retrieval-layer-table")
    sha_prefix = re.search(r"\b[0-9a-f]{7,40}\b", block)
    if not (sha_prefix and _HEAD.startswith(sha_prefix.group(0)) or _HEAD[:7] in block):
        problems.append("區塊沒有執行 SHA")
    return problems


def _retrieval_container(text):
    m = re.search(
        r"<!-- retrieval-layer-table:start[^>]*-->.*?<!-- retrieval-layer-table:end[^>]*-->",
        text,
        re.S,
    )
    return m


def test_readme_generation_metrics_link_to_real_run():
    art, _ = _load_real()
    text = (_REPO / "README.md").read_text("utf-8")
    problems = readme_block_problems(text, art)
    assert problems == [], problems

    # 檢索層既有表沒有被混入：兩個容器不重疊，標記位置各自獨立
    gen = (text.index(_TABLE_START), text.index(_TABLE_END) + len(_TABLE_END))
    ret = _retrieval_container(text)
    assert ret is not None, "README 找不到檢索層既有表 <!-- retrieval-layer-table"
    assert gen[1] <= ret.start() or ret.end() <= gen[0], "生成層區塊與檢索層表重疊"
    assert text.count(_TABLE_START) == 1 and text.count(_TABLE_END) == 1


_SAMPLE_README = """\
前言
<!-- retrieval-layer-table:start -->
| 既有 | 1/2 |
<!-- retrieval-layer-table:end -->
{start}
| account | overall ok | visible ok | 預期拒答 ok | citation_ok | REFUSAL | leak |
|---|---|---|---|---|---|---|
| manager | 24/33 | 22/31 | 2/2 | 30/31 | 8/33 | 0/0 n/a |
| sales | 31/33 | 8/10 | 23/23 | 10/10 | 25/33 | 0/21 |
{end}
"""


def _sample(text=_SAMPLE_README):
    return text.format(start=_TABLE_START, end=_TABLE_END)


def test_readme_table_checker_accepts_sample_and_rejects_bad_numbers():
    art, _ = _load_real()
    assert readme_table_problems(_sample(), art) == []
    # 欄位順序不鎖死：打亂欄位仍可通過
    reordered = _sample().replace("| 24/33 | 22/31 | 2/2 |", "| 2/2 | 24/33 | 22/31 |")
    assert readme_table_problems(reordered, art) == []
    # 壞例：改掉某個 num
    bad = _sample().replace("22/31", "23/31")
    problems = readme_table_problems(bad, art)
    assert problems and _contains(problems, "22/31"), problems
    # 壞例：leak 0/0 沒標 n/a
    no_na = _sample().replace("0/0 n/a", "0/0")
    assert _contains(readme_table_problems(no_na, art), "n/a")
    # 壞例：沒有標記
    missing = readme_table_problems("沒有區塊的 README", art)
    assert missing and _contains(missing, "找不到標記")
