"""生成層多帳號評測產物的證據契約（TASK-20261029-001，AC-8、AC-9）：
摘要可由逐題紀錄獨立重算、provenance 與 hash 可驗證、不覆寫既有 run、mock 明確標示、
新格式不破壞 GET /eval/runs。

真 run 與 README 的契約（AC-11、AC-12）等真 run 產出後的批次再補，這個檔案目前不含。"""
# ruff: noqa: F811

import hashlib
import json

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
