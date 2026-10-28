"""eval/run_eval.py 的 --accounts 圖紙生成層多帳號評測（TASK-20261029-001，AC-1～AC-10）。

兩種層次：
- FakeBackend：可控 HTTP 邊界（httpx.MockTransport），驗證帳號切換、可見性、判分與事件保存；
  不依關鍵字產生答案、不替換判分或統計函式。
- 真 chat 服務：用 TestClient 接上實際 FastAPI app（LLM_MODE=mock），並 spy 讀圖、retrieve、
  模型呼叫，證明看不到的圖紙在第 1 段就結束（不是只相信評測器自己的假設）。
"""

import hashlib
import importlib.util
import json
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "eval" / "run_eval.py"
REAL_CLIENT = httpx.Client

# 閘門／第 1 段的降級訊息（shared/agent.yaml degrade_message）
DEGRADE = "查無資料：在你目前可以查閱的資料中，找不到能回答這個問題的內容。"


def load_evaluator(name="run_eval_accounts_contract"):
    spec = importlib.util.spec_from_file_location(name, TARGET)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_eval = load_evaluator()


def sse(event, data):
    return f"event:{event}\ndata:{json.dumps(data, ensure_ascii=False)}\n\n"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")


def part_source(ref=1, part="p-ok", topic="規格", **fields):
    return {"ref": ref, "part_id": part, "topic": topic, "chunk_id": f"{part}#{ref}", **fields}


STAGE1_PIPELINE = [
    {"stage": 1, "name": "文件層授權", "status": "block", "by": "地端", "detail": "x"}
]
LLM_PIPELINE = [
    {"stage": 1, "name": "文件層授權", "status": "pass", "by": "地端", "detail": "x"},
    {"stage": 7, "name": "生成", "status": "pass", "by": "地端", "detail": "x"},
]


def done_event(rid="r-1", **fields):
    return {
        "request_id": rid,
        "model": "fake-llm",
        "strategy_used": "hybrid",
        "fallback": False,
        "prompt_version": "drawing_v2",
        "degraded": False,
        "tokens": {"input": 10, "output": 5},
        "cost_twd": 0.0,
        "egress": {"images": 0, "chunks": 0, "bytes": 0},
        "latency_ms": {"first_token": 5, "total": 9},
        **fields,
    }


def answer_events(answer, sources, rid="r-1", **sources_fields):
    return [
        (
            "sources",
            {
                "request_id": rid,
                "sources": sources,
                "pipeline": LLM_PIPELINE,
                "post_filter": {"kept": len(sources)},
                **sources_fields,
            },
        ),
        ("token", {"text": answer}),
        ("done", done_event(rid)),
    ]


def stage1_events(text=DEGRADE, rid="r-s1"):
    return [
        (
            "sources",
            {"request_id": rid, "sources": [], "pipeline": STAGE1_PIPELINE, "post_filter": None},
        ),
        ("token", {"text": text}),
        (
            "done",
            done_event(
                rid,
                model="第 1 段（未呼叫 LLM）",
                strategy_used="hybrid",
                degraded=True,
                tokens={"input": 0, "output": 0},
                pipeline=STAGE1_PIPELINE,
            ),
        ),
    ]


class FakeBackend:
    """只實作評測器該碰的端點；碰到其他端點直接失敗。"""

    def __init__(self, visible=None, responder=None):
        self.visible = visible or {"manager": ["p-ok", "p-hid"], "sales": ["p-ok"]}
        self.responder = responder or default_responder
        self.current = "guest"
        self.requests = []
        self.chats = []  # (帳號, body)
        self.switch_status = {}  # 帳號 → 非 200 的狀態
        self.switch_current = {}  # 帳號 → 切換後實際身分（模擬切換回應騙人）
        self.accounts_current = {}  # 帳號 → /auth/accounts 回的實際身分
        self.parts_status = {}  # 帳號 → /parts 的非 200 狀態
        self.llm_mode = "real"
        self.kb_version = "kb-fixed-17"

    def public(self, who):
        return {"id": who, "label": who}

    def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/api/v1/health":
            return httpx.Response(200, json={})
        if path == "/api/v1/auth/accounts":
            who = self.accounts_current.get(self.current, self.current)
            return httpx.Response(
                200,
                json={"current": self.public(who), "accounts": [], "kb_version": self.kb_version},
            )
        if path == "/api/v1/auth/switch":
            target = json.loads(request.content)["account_id"]
            if target in self.switch_status:
                return httpx.Response(
                    self.switch_status[target], json={"error": {"code": "FORBIDDEN"}}
                )
            self.current = target
            shown = self.switch_current.get(target, target)
            return httpx.Response(
                200,
                json={"current": self.public(shown), "accounts": [], "kb_version": self.kb_version},
            )
        if path == "/api/v1/parts":
            if self.current in self.parts_status:
                return httpx.Response(self.parts_status[self.current], json={"error": {}})
            ids = self.visible[self.current]
            return httpx.Response(
                200,
                json={
                    "kb_version": self.kb_version,
                    "items": [{"id": i} for i in ids],
                    "hidden": 1,
                },
            )
        if path == "/api/v1/status":
            return httpx.Response(200, json={"eval_controls": True, "llm_mode": self.llm_mode})
        if path == "/api/v1/chat":
            body = json.loads(request.content)
            self.chats.append((self.current, body))
            value = self.responder(self.current, body)
            if isinstance(value, Exception):
                raise value
            if isinstance(value, int):
                return httpx.Response(value, json={"error": {"code": f"E{value}"}})
            payload = ":keepalive\n\n" + "".join(sse(e, d) for e, d in value)
            return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
        raise AssertionError(f"未預期的 HTTP 請求：{request.method} {path}")


def default_responder(account, body):
    """可答題回正確答案與引用；其他題一律回降級訊息。"""
    pid = body["part_id"]
    if pid == "p-ok" and "扭力" in body["question"]:
        return answer_events("扭力是 9 N·m [1]", [part_source(1, "p-ok", "規格")])
    return stage1_events()


QUESTIONS = [
    {
        "id": "q-ok",
        "part_id": "p-ok",
        "question": "扭力是多少？",
        "topics": ["規格"],
        "keywords": [["9 N·m"]],
    },
    {
        "id": "q-hid",
        "part_id": "p-hid",
        "question": "用什麼材料？",
        "topics": ["材料"],
        "keywords": [["S45C"]],
    },
    {
        "id": "q-na",
        "part_id": "p-ok",
        "question": "價格多少？",
        "topics": [],
        "type": "no_answer",
        "expect_refusal": True,
        "no_answer_reason": "圖紙沒有價格",
    },
    {
        "id": "q-ghost",
        "part_id": "p-ghost",
        "question": "不存在的圖紙？",
        "topics": [],
        "type": "unknown",
        "expect_refusal": True,
        "no_answer_reason": "不存在",
    },
    {
        "id": "q-hid-na",
        "part_id": "p-hid",
        "question": "隱藏圖紙的價格？",
        "topics": [],
        "type": "no_answer",
        "expect_refusal": True,
        "no_answer_reason": "圖紙沒有價格",
    },
]


class Env:
    """一次測試的暫存環境：gold、KB 零件目錄、題庫與輸出目錄。"""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.runs = tmp_path / "runs"
        self.gold = tmp_path / "gold.json"
        self.gold.write_text(json.dumps({"accounts": ["manager", "sales"]}), "utf-8")
        self.kb_parts = tmp_path / "kb" / "parts"
        self.kb_parts.mkdir(parents=True)
        for pid in ("p-ok", "p-hid"):
            (self.kb_parts / f"{pid}.json").write_text(json.dumps({"id": pid}), "utf-8")
        self.qa = tmp_path / "qa.jsonl"
        write_jsonl(self.qa, QUESTIONS)
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(run_eval, "GOLD_PATH", self.gold)
        monkeypatch.setattr(run_eval, "KB_PARTS_DIR", self.kb_parts)
        monkeypatch.setattr(run_eval, "RUNS", self.runs)
        monkeypatch.setattr(run_eval, "OFFICIAL_RUNS", tmp_path / "official-runs")
        monkeypatch.setattr(
            run_eval,
            "_git_provenance",
            lambda: {"head": "a" * 40, "dirty": False, "dirty_paths": [], "error": None},
        )
        self.clients = []

    def install(self, backend):
        def client(**kwargs):
            obj = REAL_CLIENT(transport=httpx.MockTransport(backend.handle), **kwargs)
            self.clients.append(obj)
            return obj

        self.monkeypatch.setattr(run_eval.httpx, "Client", client)

    def run(self, backend, *options, accounts="manager,sales", strategies="hybrid", qa=True):
        self.install(backend)
        argv = ["run_eval.py", "--base", "http://eval.invalid", "--accounts", accounts]
        if strategies is not None:
            argv += ["--strategies", strategies]
        if qa:
            argv += ["--qa", str(self.qa)]
        self.monkeypatch.setattr(sys, "argv", [*argv, *options])
        code = run_eval.main()
        return code

    def artifact(self, run_id=None):
        if run_id:
            return json.loads((self.runs / f"{run_id}-generation-accounts.json").read_text("utf-8"))
        (path,) = self.runs.glob("*-generation-accounts.json")
        return json.loads(path.read_text("utf-8"))

    def no_artifacts(self):
        return not self.runs.exists() or not list(self.runs.iterdir())

    def rows(self, art=None):
        art = art or self.artifact()
        return {(r["account"], r["strategy"], r["question_id"]): r for r in art["rows"]}

    def close(self):
        for c in self.clients:
            c.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    def no_network(*a, **k):
        pytest.fail("FakeBackend 測試不得建立網路連線")

    monkeypatch.setattr(socket, "create_connection", no_network)
    e = Env(tmp_path, monkeypatch)
    yield e
    e.close()


# ------------------------------------------------------------------ AC-1：帳號名單與切換


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("manager", ["manager"]),
        ("manager,sales", ["manager", "sales"]),
        ("sales, manager", ["sales", "manager"]),
    ],
)
def test_accounts_are_validated_against_gold(raw, expected, tmp_path):
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps({"accounts": ["manager", "sales"]}), "utf-8")
    allowed = run_eval.load_gold_accounts(gold)
    assert allowed == ["manager", "sales"]
    assert run_eval.parse_accounts(raw, allowed) == expected
    for bad in (
        "",
        " ",
        ",",
        "manager,",
        "manager,,sales",
        "manager,manager",
        "root",
        "manager,root",
    ):
        with pytest.raises(ValueError):
            run_eval.parse_accounts(bad, allowed)


def test_invalid_accounts_fail_before_any_request_or_output(env):
    backend = FakeBackend()
    for bad in ("", "manager,manager", "root", "manager,"):
        assert env.run(backend, accounts=bad) == 1
    assert backend.requests == [] and backend.chats == []
    assert env.no_artifacts()


def test_gold_without_accounts_fails(env):
    env.gold.write_text(json.dumps({"accounts": []}), "utf-8")
    backend = FakeBackend()
    assert env.run(backend) == 1
    assert backend.requests == []


@pytest.mark.parametrize("failure", ["http", "response_identity", "cookie_identity"])
def test_account_switch_is_verified(env, failure):
    backend = FakeBackend()
    if failure == "http":
        backend.switch_status["sales"] = 403
    elif failure == "response_identity":
        backend.switch_current["sales"] = "manager"  # 切換回應說還是主管
    else:
        backend.accounts_current["sales"] = "manager"  # 切換回應沒騙人，實際憑證還是主管
    code = env.run(backend)
    assert code != 0
    # 切換失敗的帳號沒有任何題目被送出，也沒有沿用上一帳號冒充
    assert all(acct != "sales" for acct, _ in backend.chats)
    assert env.no_artifacts()


def test_account_identity_is_confirmed_for_every_account_switch(env):
    backend = FakeBackend()
    assert env.run(backend) == 0
    switches = [p for _, p in backend.requests if p == "/api/v1/auth/switch"]
    confirms = [p for _, p in backend.requests if p == "/api/v1/auth/accounts"]
    assert len(switches) >= 2 and len(confirms) >= len(switches)


# ------------------------------------------------------------------ AC-2：33 題與不剔除


def test_all_questions_run_for_each_account_and_strategy(env):
    backend = FakeBackend()
    assert env.run(backend, strategies="hybrid,hybrid_noimg") == 0
    rows = env.artifact()["rows"]
    ids = [q["id"] for q in QUESTIONS]
    for account in ("manager", "sales"):
        for strategy in ("hybrid", "hybrid_noimg"):
            got = [
                r["question_id"]
                for r in rows
                if (r["account"], r["strategy"]) == (account, strategy)
            ]
            assert got == ids
    # 看不到的圖紙也真的送出去問了（不能依可見清單剔除）
    assert ("sales", "p-hid") in {(a, b["part_id"]) for a, b in backend.chats}
    assert len(backend.chats) == 2 * 2 * len(QUESTIONS)


def test_strategy_flags_reach_the_chat_body_and_default_is_single_hybrid(env):
    backend = FakeBackend()
    assert env.run(backend, strategies=None) == 0
    assert {b["strategy"] for _, b in backend.chats} == {"hybrid"}
    assert all(b["allow_fallback"] is False and "part_id" in b for _, b in backend.chats)
    backend2 = FakeBackend()
    assert env.run(backend2, "--run-id", "second", strategies="hybrid_norag") == 0
    assert all(b["use_retrieval"] is False for _, b in backend2.chats)


def test_split_and_outside_kb_options_cannot_drop_questions(env):
    backend = FakeBackend()
    assert env.run(backend, "--split", "dev") == 1
    assert env.run(backend, "--outside-kb") == 1
    assert backend.chats == []


def test_question_files_are_not_modified(env):
    before = hashlib.sha256(env.qa.read_bytes()).hexdigest()
    assert env.run(FakeBackend()) == 0
    assert hashlib.sha256(env.qa.read_bytes()).hexdigest() == before


def test_accounts_mode_requires_part_questions(env):
    write_jsonl(env.qa, [{"id": "x", "question": "q", "artwork_id": "art-1", "keywords": [["a"]]}])
    backend = FakeBackend()
    assert env.run(backend) == 1
    assert backend.chats == []


# ------------------------------------------------------------------ AC-3：可見性與預期分類


def test_visibility_comes_from_authenticated_backend(env):
    # 刻意反過來：業務看得到全部、主管只看到一張；評測器不能硬編角色權限
    backend = FakeBackend(visible={"manager": ["p-ok"], "sales": ["p-ok", "p-hid"]})
    assert env.run(backend) == 0
    art = env.artifact()
    rows = env.rows(art)
    assert rows[("sales", "hybrid", "q-hid")]["classification"] == "visible_answerable"
    assert rows[("manager", "hybrid", "q-hid")]["classification"] == "permission_degraded"
    snap = art["snapshots"]
    assert snap["sales"]["visible_part_ids"] == ["p-hid", "p-ok"]
    assert snap["manager"]["visible_part_ids"] == ["p-ok"]
    assert snap["sales"]["identity"]["id"] == "sales"
    assert snap["manager"]["kb_version"] == "kb-fixed-17"


@pytest.mark.parametrize("status", [401, 403, 500])
def test_visibility_failure_is_not_an_empty_list(env, status):
    backend = FakeBackend()
    backend.parts_status["sales"] = status
    assert env.run(backend) != 0
    assert all(acct != "sales" for acct, _ in backend.chats)
    assert env.no_artifacts()


def test_expected_outcome_precedence(env):
    assert env.run(FakeBackend()) == 0
    rows = env.rows()
    m, s = ("manager", "hybrid"), ("sales", "hybrid")
    # 主管：全可見 → 可答、no_answer、不存在
    assert rows[(*m, "q-ok")]["classification"] == "visible_answerable"
    assert rows[(*m, "q-ok")]["expected_outcome"] == "answer"
    assert rows[(*m, "q-na")]["classification"] == "no_answer"
    assert rows[(*m, "q-na")]["expected_outcome"] == "refusal"
    assert rows[(*m, "q-ghost")]["classification"] == "missing_part"
    assert rows[(*m, "q-hid")]["classification"] == "visible_answerable"
    # 業務：已存在但看不到 → 權限降級，優先於 no_answer；不存在仍是 missing_part
    assert rows[(*s, "q-hid")]["classification"] == "permission_degraded"
    assert rows[(*s, "q-hid")]["expected_outcome"] == "stage1_degrade"
    assert rows[(*s, "q-hid-na")]["classification"] == "permission_degraded"
    assert rows[(*s, "q-ghost")]["classification"] == "missing_part"
    reasons = {r["classification_reason"] for r in rows.values()}
    assert all(isinstance(x, str) and x for x in reasons) and len(reasons) >= 4


def test_existing_hidden_part_known_only_from_kb_files_when_single_account(env):
    # 只評業務時，看不到的圖紙靠 KB 檔案確認「存在」，不能被當成不存在
    assert env.run(FakeBackend(), accounts="sales") == 0
    rows = env.rows()
    assert rows[("sales", "hybrid", "q-hid")]["classification"] == "permission_degraded"
    assert rows[("sales", "hybrid", "q-ghost")]["classification"] == "missing_part"


# ------------------------------------------------------------------ AC-4：可答題的答案與引用


def one_visible(env, answer_events_value, **kw):
    backend = FakeBackend(
        responder=lambda a, b: (
            answer_events_value
            if b["part_id"] == "p-ok" and "扭力" in b["question"]
            else stage1_events()
        )
    )
    assert env.run(backend, accounts="manager", **kw) == 0
    return env.rows()[("manager", "hybrid", "q-ok")]


@pytest.mark.parametrize(
    ("answer", "sources", "answer_ok", "citation_ok"),
    [
        ("扭力 9 N·m [1]", [part_source(1, "p-ok", "規格")], True, True),
        ("扭力是 8 N·m [1]", [part_source(1, "p-ok", "規格")], False, True),  # 漏答／答錯
        ("扭力 9 N·m [1]", [part_source(1, "p-other", "規格")], True, False),  # 引用到別張圖紙
        ("扭力 9 N·m [1]", [part_source(1, "p-ok", "材料")], True, False),  # 引用 topic 不對
        ("扭力 9 N·m", [part_source(1, "p-ok", "規格")], True, False),  # 缺引用
        ("扭力 9 N·m [7]", [part_source(1, "p-ok", "規格")], True, False),  # 無效 ref
        (
            "扭力 9 N·m [1][2]",
            [part_source(1, "p-ok", "規格"), part_source(2, "p-other", "規格")],
            True,
            False,
        ),  # 混合引用
        (
            "扭力 9 N·m [1][2]",
            [part_source(1, "p-ok", "規格"), part_source(2, "p-ok", "規格")],
            True,
            True,
        ),
    ],
)
def test_visible_answer_and_citation_contract(env, answer, sources, answer_ok, citation_ok):
    row = one_visible(env, answer_events(answer, sources))
    assert (row["answer_ok"], row["citation_ok"]) == (answer_ok, citation_ok)
    assert row["classification"] == "visible_answerable"


def test_visible_answer_and_citation_and_or_keywords(env):
    qs = [{**QUESTIONS[0], "keywords": [["9 N·m", "9Nm"], ["4 支"]]}]
    write_jsonl(env.qa, qs)

    def reply(answer):
        return FakeBackend(
            visible={"manager": ["p-ok"], "sales": ["p-ok"]},
            responder=lambda a, b: answer_events(answer, [part_source(1, "p-ok", "規格")]),
        )

    assert env.run(reply("9Nm、4 支 [1]"), "--run-id", "and-ok", accounts="manager") == 0
    assert env.rows(env.artifact("and-ok"))[("manager", "hybrid", "q-ok")]["answer_ok"] is True
    assert (
        env.run(reply("9 N·m [1]"), "--run-id", "and-miss", accounts="manager") == 0
    )  # 缺「4 支」
    assert env.rows(env.artifact("and-miss"))[("manager", "hybrid", "q-ok")]["answer_ok"] is False


def test_unreviewed_note_is_kept_on_the_row(env):
    qs = [
        {**QUESTIONS[0], "review_status": "unreviewed", "review_note": "外包廠回報，只評忠實轉述"}
    ]
    write_jsonl(env.qa, qs)
    backend = FakeBackend(
        visible={"manager": ["p-ok"], "sales": ["p-ok"]},
        responder=lambda a, b: answer_events("9 N·m [1]", [part_source(1, "p-ok", "規格")]),
    )
    assert env.run(backend, accounts="manager") == 0
    assert env.rows()[("manager", "hybrid", "q-ok")]["review_status"] == "unreviewed"


# ------------------------------------------------------------------ AC-5：看不到的圖紙


def hidden_row(env, events):
    backend = FakeBackend(
        responder=lambda a, b: events if b["part_id"] == "p-hid" else stage1_events()
    )
    assert env.run(backend, accounts="sales") == 0
    return env.rows()[("sales", "hybrid", "q-hid")]


def test_hidden_drawing_requires_stage1_only(env):
    row = hidden_row(env, stage1_events())
    assert row["classification"] == "permission_degraded"
    assert row["stage1_only"] is True and row["stage1_failures"] == []
    assert (row["answer_ok"], row["leaked"], row["refused"]) == (True, False, True)
    assert row["leak_reasons"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        "llm_pipeline",
        "tokens_used",
        "sources_nonempty",
        "no_pipeline",
        "not_degraded",
        "egress_chunks",
    ],
)
def test_hidden_drawing_not_stage1_only_is_wrong(env, mutate):
    ev = stage1_events()
    sources, token, done = ev[0][1], ev[1][1], ev[2][1]
    if mutate == "llm_pipeline":
        sources["pipeline"] = LLM_PIPELINE
        done["pipeline"] = LLM_PIPELINE
    elif mutate == "tokens_used":
        done["tokens"] = {"input": 12, "output": 3}
    elif mutate == "sources_nonempty":
        sources["sources"] = [part_source(1, "p-hid", "材料")]
    elif mutate == "no_pipeline":
        sources.pop("pipeline")
        done.pop("pipeline")
    elif mutate == "not_degraded":
        done["degraded"] = False
    elif mutate == "egress_chunks":
        done["egress"] = {"images": 0, "chunks": 2, "bytes": 10}
    row = hidden_row(env, [("sources", sources), ("token", token), ("done", done)])
    assert row["stage1_only"] is False and row["stage1_failures"]
    assert row["answer_ok"] is False


@pytest.mark.parametrize(
    ("text", "leak_marker"),
    [
        (DEGRADE + "該零件材料是 S45C。", "keyword_hit"),
        ("查無資料。S45C", "keyword_hit"),
        (DEGRADE + " 不過可參考 [1]。", "citation_marker"),
    ],
)
def test_hidden_drawing_content_and_leakage_are_scored(env, text, leak_marker):
    row = hidden_row(env, stage1_events(text))
    assert row["refused"] is True  # 有拒答字樣，但不能因此通過
    assert row["answer_ok"] is False
    assert row["leaked"] is True
    assert any(x.startswith(leak_marker) for x in row["leak_reasons"])


def test_hidden_drawing_answer_without_refusal_is_leak_and_wrong(env):
    row = hidden_row(env, answer_events("材料是 S45C [1]", [part_source(1, "p-hid", "材料")]))
    assert (row["answer_ok"], row["leaked"], row["refused"]) == (False, True, False)
    reasons = set(row["leak_reasons"])
    assert {"sources_returned", "citation_marker", "keyword_hit:S45C"} <= reasons


def test_hidden_drawing_long_refusal_without_evidence_is_wrong_but_not_counted_as_leak(env):
    row = hidden_row(
        env, stage1_events("查無資料。這個問題比較複雜，建議你找設計課同事再確認看看。")
    )
    assert row["answer_ok"] is False and row["leaked"] is False and row["leak_reasons"] == []


def test_hidden_drawing_leak_is_recomputable_from_the_row_alone(env):
    row = hidden_row(env, answer_events("材料是 S45C [1]", [part_source(1, "p-hid", "材料")]))
    recomputed = run_eval.leak_reasons({"keywords": [["S45C"]]}, row["answer"], row["events"])
    assert recomputed == row["leak_reasons"]


# ------------------------------------------------------------------ AC-6：拒答、錯誤、fallback


@pytest.mark.parametrize("qid", ["q-na", "q-ghost"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (DEGRADE, True),  # 舊 score_answer 認不出的降級訊息，新模式按 REFUSAL 認得
        ("圖紙上沒有這方面的資料。", True),
        ("不確定，圖紙沒寫。", True),
        ("價格是 3000 元。", False),  # 捏造
    ],
)
def test_no_answer_and_missing_part_use_refusal(env, qid, text, expected):
    backend = FakeBackend(responder=lambda a, b: stage1_events(text))
    assert env.run(backend, accounts="manager") == 0
    row = env.rows()[("manager", "hybrid", qid)]
    assert (row["refused"], row["answer_ok"]) == (expected, expected)


def test_no_answer_and_missing_reasons_are_kept_separately(env):
    assert env.run(FakeBackend()) == 0
    rows = env.rows()
    na = rows[("manager", "hybrid", "q-na")]
    ghost = rows[("manager", "hybrid", "q-ghost")]
    assert na["classification"] != ghost["classification"]
    assert na["no_answer_reason"] == "圖紙沒有價格" and ghost["no_answer_reason"] == "不存在"
    assert na["classification_reason"] != ghost["classification_reason"]


def test_legacy_refusal_scoring_is_unchanged_for_old_mode():
    q = {"expect_refusal": True}
    assert run_eval.score_answer(q, {"answer": DEGRADE, "sources": []}) == (False, False)
    assert run_eval.score_answer(q, {"answer": "沒有資料", "sources": []}) == (True, True)


@pytest.mark.parametrize(
    "failure", ["http500", "stream_error", "fallback", "transport", "bad_json"]
)
def test_errors_and_fallbacks_remain_in_denominators(env, failure):
    def responder(account, body):
        if body["part_id"] != "p-ok" or "扭力" not in body["question"]:
            return stage1_events()
        if failure == "http500":
            return 500
        if failure == "transport":
            return httpx.ConnectError("boom")
        if failure == "stream_error":
            return [("token", {"text": "9 N·m [1]"}), ("error", {"code": "LLM_TIMEOUT"})]
        if failure == "fallback":
            ev = answer_events("扭力 9 N·m [1]", [part_source(1, "p-ok", "規格")])
            ev[2][1]["fallback"] = True
            return ev
        return [("sources", {"sources": []}), ("token", {"text": "x"})]

    backend = FakeBackend(responder=responder)
    code = env.run(backend, accounts="manager")
    art = env.artifact()
    row = env.rows(art)[("manager", "hybrid", "q-ok")]
    assert code == 0 and len(art["rows"]) == len(QUESTIONS)
    assert row["answer_ok"] is False and row["citation_ok"] is False and row["refused"] is False
    group = art["groups"]["manager"]["hybrid"]["visible_answerable"]
    assert group["n"] == 3 and group["answer_ok"]["den"] == 3 and group["answer_ok"]["num"] == 1
    if failure in ("http500", "transport", "stream_error"):
        assert row["error"] and group["errors"] == 1
    if failure == "fallback":
        assert row["fallback"] is True and group["fallbacks"] == 1


def test_error_on_no_answer_question_is_not_a_successful_refusal(env):
    def responder(account, body):
        if body["part_id"] == "p-ok" and "價格" in body["question"]:
            return [("token", {"text": DEGRADE}), ("error", {"code": "LLM_TIMEOUT"})]
        return stage1_events()

    assert env.run(FakeBackend(responder=responder), accounts="manager") == 0
    row = env.rows()[("manager", "hybrid", "q-na")]
    assert row["answer_ok"] is False and row["refused"] is False and row["error"] == "LLM_TIMEOUT"


# ------------------------------------------------------------------ AC-7：事件原樣保存


def test_sources_events_are_preserved_losslessly(env):
    first = {
        "request_id": "r-9",
        "sources": [],
        "pipeline": [{"stage": 3, "status": "pass", "未知欄位": {"a": [1, 2]}}],
        "post_filter": {"kept": 0, "dropped": [{"chunk_id": "c9", "why": "x"}]},
        "brand_new_field": {"nested": True},
    }
    second = {
        "request_id": "r-9",
        "sources": [part_source(1, "p-ok", "規格", chunk_id="p-ok#7", 另一個="值")],
        "pipeline": LLM_PIPELINE,
        "post_filter": {"kept": 1},
        "rearrange": {"ms": 3},
    }
    events = [
        ("sources", first),
        ("heartbeat", {"alive": True}),
        ("sources", second),
        ("token", {"text": "扭力 9 N·m "}),
        ("token", {"text": "[1]"}),
        ("done", done_event("r-9", 未知done欄位=1)),
    ]
    backend = FakeBackend(
        responder=lambda a, b: (
            events if b["part_id"] == "p-ok" and "扭力" in b["question"] else stage1_events()
        )
    )
    assert env.run(backend, accounts="manager") == 0
    row = env.rows()[("manager", "hybrid", "q-ok")]
    assert row["events"] == [{"seq": i, "event": e, "data": d} for i, (e, d) in enumerate(events)]
    assert [e["data"] for e in row["events"] if e["event"] == "sources"] == [first, second]
    assert row["sources_state"] == "present" and row["answer"] == "扭力 9 N·m [1]"
    assert row["done"] == events[-1][1] and row["request_id"] == "r-9"
    assert (
        row["account"] == "manager" and row["strategy"] == "hybrid" and row["question_id"] == "q-ok"
    )
    # 序列化後的 key 順序也照原樣
    assert list(row["events"][0]["data"]) == list(first)


def test_absent_and_empty_sources_are_distinguished(env):
    def responder(account, body):
        if "扭力" in body["question"]:
            return [("token", {"text": "x"}), ("done", done_event("r-a"))]  # 沒有 sources 事件
        if "價格" in body["question"] and body["part_id"] == "p-ok":
            return [
                ("sources", {"sources": []}),
                ("token", {"text": DEGRADE}),
                ("done", done_event("r-e")),
            ]
        return stage1_events()

    assert env.run(FakeBackend(responder=responder), accounts="manager") == 0
    rows = env.rows()
    assert rows[("manager", "hybrid", "q-ok")]["sources_state"] == "absent"
    assert rows[("manager", "hybrid", "q-ok")]["events"][0]["event"] == "token"
    assert rows[("manager", "hybrid", "q-na")]["sources_state"] == "empty"


def test_http_error_and_stream_error_are_kept_with_correlation(env):
    def responder(account, body):
        if "扭力" in body["question"]:
            return [
                ("sources", {"sources": []}),
                ("token", {"text": "半句"}),
                ("error", {"code": "X", "request_id": "r-err"}),
            ]
        return stage1_events()

    assert env.run(FakeBackend(responder=responder), accounts="manager") == 0
    row = env.rows()[("manager", "hybrid", "q-ok")]
    assert row["error"] == "X" and row["error_event"] == {"code": "X", "request_id": "r-err"}
    assert row["answer"] == "半句" and row["request_id"] == "r-err"


# ------------------------------------------------------------------ AC-8：摘要


def test_empty_groups_have_null_rates(env):
    # 業務看得到全部 → 權限降級子集 N=0；主管看不到 p-hid 與 p-ok 以外也無影響
    backend = FakeBackend(visible={"manager": ["p-ok", "p-hid"], "sales": ["p-ok", "p-hid"]})
    assert env.run(backend) == 0
    groups = env.artifact()["groups"]["sales"]["hybrid"]
    empty = groups["permission_degraded"]
    assert empty["n"] == 0
    for metric in ("answer_ok", "citation_ok", "refusal", "leak"):
        assert empty[metric] == {"num": 0, "den": 0, "rate": None}
    assert groups["overall"]["leak"] == {"num": 0, "den": 0, "rate": None}
    assert groups["overall"]["n"] == len(QUESTIONS)


def test_summarize_rows_groups_numerators_and_denominators():
    def row(cls, **f):
        base = {
            "account": "a",
            "strategy": "s",
            "classification": cls,
            "answer_ok": False,
            "citation_ok": None,
            "refused": False,
            "leaked": False,
            "error": "",
            "fallback": False,
        }
        return base | f

    rows = [
        row("visible_answerable", answer_ok=True, citation_ok=True),
        row("visible_answerable", answer_ok=True, citation_ok=False),
        row("visible_answerable", error="X", citation_ok=False),
        row("permission_degraded", answer_ok=True, refused=True),
        row("permission_degraded", leaked=True, refused=True),
        row("no_answer", answer_ok=True, refused=True),
        row("missing_part", fallback=True),
    ]
    g = run_eval.summarize_rows(rows)["a"]["s"]
    assert g["overall"]["answer_ok"] == {"num": 4, "den": 7, "rate": 4 / 7}
    assert g["overall"]["citation_ok"] == {"num": 1, "den": 3, "rate": 1 / 3}
    assert g["overall"]["refusal"] == {"num": 3, "den": 7, "rate": 3 / 7}
    assert g["overall"]["leak"] == {"num": 1, "den": 2, "rate": 0.5}
    assert g["visible_answerable"]["answer_ok"] == {"num": 2, "den": 3, "rate": 2 / 3}
    assert g["visible_answerable"]["errors"] == 1
    assert g["permission_degraded"]["leak"]["num"] == 1
    assert g["missing_part"]["fallbacks"] == 1 and g["missing_part"]["answer_ok"]["num"] == 0
    assert g["no_answer"]["citation_ok"] == {"num": 0, "den": 0, "rate": None}


# ------------------------------------------------------------------ AC-10：舊模式不變


def fixed_clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 29, 1, 2, 3, 456789, tzinfo=tz)

    monkeypatch.setattr(run_eval, "datetime", FixedDatetime)
    monkeypatch.setattr(
        run_eval, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="abcd1234"))
    )
    ticks = iter(range(10_000))
    monkeypatch.setattr(run_eval, "time", SimpleNamespace(time=lambda: 100.0 + next(ticks) * 0.5))


class LegacyAPI:
    """舊模式的固定 API：畫作、圖片、圖紙、LoRA、拒答。"""

    def __init__(self):
        self.requests = []
        self.bodies = []

    def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/api/v1/chat":
            body = json.loads(request.content)
            self.bodies.append(body)
            q = body["question"]
            if q == "拒答題":
                events = [
                    ("sources", {"sources": []}),
                    ("token", {"text": "知識庫沒有這方面的資料"}),
                    ("done", done_event("r-n")),
                ]
            elif q == "圖紙題":
                events = answer_events("扭力 9 N·m [1]", [part_source(1, "mfg-001", "規格")], "r-d")
            elif q == "LoRA題":
                events = answer_events(
                    "雨點皴 [1]", [{"ref": 1, "artwork_id": "art-1", "topic": "技法"}], "r-l"
                )
                events[2][1]["model"] = "lora-x"
            else:
                events = answer_events(
                    "用了雨點皴 [1]", [{"ref": 1, "artwork_id": "art-1", "topic": "技法"}], "r-p"
                )
            payload = "".join(sse(e, d) for e, d in events)
            return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
        if path == "/api/v1/images":
            return httpx.Response(200, json={"image_id": "img"})
        if path == "/api/v1/search/image":
            return httpx.Response(
                200,
                json={
                    "matched": True,
                    "best_artwork_id": "art-1",
                    "results": [{"artwork": {"id": "art-1"}, "score": 0.9, "inliers": 5}],
                },
            )
        routes = {
            "/api/v1/health": {},
            "/api/v1/auth/accounts": {},
            "/api/v1/auth/switch": {},
            "/api/v1/admin/diagnostics": {"manifest": {"kb_version": "kb-legacy-1"}},
            "/api/v1/status": {"eval_controls": True},
            "/api/v1/artworks": {"items": [{"id": "art-1"}]},
            "/api/v1/parts": {"items": [{"id": "mfg-001"}]},
            "/api/v1/artworks/art-1": {"descriptions": [{"lang": "zh"}]},
        }
        assert path in routes, path
        return httpx.Response(200, json=routes[path])


def run_legacy(tmp_path, monkeypatch):
    runs = tmp_path / "legacy-runs"
    eval_dir = tmp_path / "eval"
    (eval_dir / "photos" / "known").mkdir(parents=True)
    (eval_dir / "photos" / "unknown").mkdir(parents=True)
    (eval_dir / "photos" / "known" / "art-1__a.jpg").write_bytes(b"jpg1")
    (eval_dir / "photos" / "unknown" / "zzz__b.jpg").write_bytes(b"jpg2")
    qa = tmp_path / "legacy-qa.jsonl"
    write_jsonl(
        qa,
        [
            {
                "id": "p1",
                "artwork_id": "art-1",
                "question": "畫題",
                "split": "dev",
                "type": "kb_only",
                "topics": ["技法"],
                "keywords": [["雨點皴"]],
            },
            {
                "id": "l1",
                "artwork_id": "art-1",
                "question": "LoRA題",
                "split": "test",
                "type": "kb_only",
                "topics": ["技法"],
                "keywords": [["雨點皴"]],
            },
            {
                "id": "n1",
                "artwork_id": "art-1",
                "question": "拒答題",
                "split": "test",
                "type": "no_answer",
                "expect_refusal": True,
                "topics": [],
                "keywords": [["沒有"]],
            },
            {
                "id": "d1",
                "part_id": "mfg-001",
                "question": "圖紙題",
                "split": "test",
                "topics": ["規格"],
                "keywords": [["9 N·m"]],
                "review_status": "unreviewed",
            },
            {
                "id": "u1",
                "part_id": "mfg-099",
                "question": "未知圖紙",
                "split": "test",
                "expect_refusal": True,
                "topics": [],
                "keywords": [["沒有"]],
            },
        ],
    )
    api = LegacyAPI()
    monkeypatch.setattr(run_eval, "RUNS", runs)
    monkeypatch.setattr(run_eval, "EVAL", eval_dir)
    fixed_clock(monkeypatch)
    clients = []

    def client(**kwargs):
        obj = REAL_CLIENT(transport=httpx.MockTransport(api.handle), **kwargs)
        clients.append(obj)
        return obj

    monkeypatch.setattr(run_eval.httpx, "Client", client)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_eval.py",
            "--base",
            "http://eval.invalid",
            "--strategies",
            "hybrid,lora_norag",
            "--qa",
            str(qa),
        ],
    )
    code = run_eval.main()
    for c in clients:
        c.close()
    stem = "20261029T010203-abcd"
    return code, api, runs, stem


def test_legacy_mode_output_is_unchanged(tmp_path, monkeypatch):
    code, api, runs, stem = run_legacy(tmp_path, monkeypatch)
    assert code == 0
    assert sorted(p.name for p in runs.iterdir()) == [
        f"{stem}-images.csv",
        f"{stem}.csv",
        f"{stem}.json",
    ]
    csv_text = (runs / f"{stem}.csv").read_text("utf-8")
    header = csv_text.splitlines()[0].split(",")
    assert header == LEGACY_COLUMNS
    summary = json.loads((runs / f"{stem}.json").read_text("utf-8"))
    assert summary == LEGACY_SUMMARY
    assert csv_text == LEGACY_CSV
    assert (runs / f"{stem}-images.csv").read_text("utf-8") == LEGACY_IMAGES_CSV
    # 新模式的欄位與端點不滲入舊輸出與流程
    for leaked_field in ("account", "classification", "events", "leaked", "sources_state"):
        assert leaked_field not in header and leaked_field not in json.dumps(summary)
    assert ("GET", "/api/v1/parts") in api.requests
    assert not list(runs.glob("*generation-accounts*"))
    # 每個請求內容（含策略旗標）照舊
    assert [b.get("use_retrieval") for b in api.bodies] == [None] * 5 + [False] * 5
    assert {b["strategy"] for b in api.bodies} == {"hybrid", "lora"}


LEGACY_COLUMNS = [
    "question_id",
    "split",
    "type",
    "artwork_id",
    "source_lang",
    "strategy",
    "model",
    "strategy_used",
    "fallback",
    "kb_version",
    "prompt_version",
    "review_status",
    "answer_ok",
    "citation_ok",
    "n_sources",
    "rearrange_ms",
    "rearrange_fallback",
    "conflict_check",
    "first_token_ms",
    "total_ms",
    "cost_twd",
    "egress_images",
    "egress_chunks",
    "egress_bytes",
    "error",
    "distractor_kept",
    "distractor_cited",
    "distractor_flagged",
    "distractor_misled",
    "refused",
    "hallucinated",
    "answer",
]
LEGACY_SUMMARY = {
    "run_id": "20261029T010203-abcd",
    "created_at": "2026-10-29T01:02:03.456789+00:00",
    "kb_version": "kb-legacy-1",
    "prompt_version": "drawing_v2",
    "summary": {
        "image_top1": 1.0,
        "image_top5": 1.0,
        "reject_rate": 0.0,
        "n_known": 1,
        "n_unknown": 1,
        "split": "all",
    },
    "by_strategy": {
        "hybrid": {
            "n": 5,
            "errors": 0,
            "fallbacks": 0,
            "models": ["fake-llm", "lora-x"],
            "answer_ok": 0.8,
            "citation_ok": 0.8,
            "mean_sources": 0.8,
            "rearrange_fallbacks": 0,
            "p95_first_token_ms": 5,
            "p95_total_ms": 9,
            "median_total_ms": 9,
            "cost_twd": 0.0,
            "egress_bytes": 0,
            "by_type": {"kb_only": 1.0, "no_answer": 1.0, "untyped": 0.5},
            "by_split_type": {
                "dev": {"kb_only": 1.0},
                "test": {"kb_only": 1.0, "no_answer": 1.0, "untyped": 0.5},
            },
            "distractor": None,
            "no_kb_answer": {"n": 1, "refused": 1.0, "hallucinated": 0.0},
        },
        "lora_norag": {
            "n": 5,
            "errors": 0,
            "fallbacks": 0,
            "models": ["fake-llm", "lora-x"],
            "answer_ok": 0.8,
            "citation_ok": 0.8,
            "mean_sources": 0.8,
            "rearrange_fallbacks": 0,
            "p95_first_token_ms": 5,
            "p95_total_ms": 9,
            "median_total_ms": 9,
            "cost_twd": 0.0,
            "egress_bytes": 0,
            "by_type": {"kb_only": 1.0, "no_answer": 1.0, "untyped": 0.5},
            "by_split_type": {
                "dev": {"kb_only": 1.0},
                "test": {"kb_only": 1.0, "no_answer": 1.0, "untyped": 0.5},
            },
            "distractor": None,
            "no_kb_answer": {"n": 1, "refused": 1.0, "hallucinated": 0.0},
        },
    },
}
LEGACY_CSV = (
    "question_id,split,type,artwork_id,source_lang,strategy,model,strategy_used,"
    "fallback,kb_version,prompt_version,review_status,answer_ok,citation_ok,"
    "n_sources,rearrange_ms,rearrange_fallback,conflict_check,first_token_ms,"
    "total_ms,cost_twd,egress_images,egress_chunks,egress_bytes,error,"
    "distractor_kept,distractor_cited,distractor_flagged,distractor_misled,refused,"
    "hallucinated,answer\n"
    "p1,dev,kb_only,art-1,zh,hybrid,fake-llm,hybrid,False,kb-legacy-1,drawing_v2,,"
    "True,True,1,,,,5,9,0.0,0,0,0,,,,,,,,用了雨點皴 [1]\n"
    "l1,test,kb_only,art-1,zh,hybrid,lora-x,hybrid,False,kb-legacy-1,drawing_v2,,"
    "True,True,1,,,,5,9,0.0,0,0,0,,,,,,,,雨點皴 [1]\n"
    "n1,test,no_answer,art-1,zh,hybrid,fake-llm,hybrid,False,kb-legacy-1,drawing_v2,,"
    "True,True,0,,,,5,9,0.0,0,0,0,,,,,,True,False,知識庫沒有這方面的資料\n"
    "d1,test,untyped,mfg-001,,hybrid,fake-llm,hybrid,False,kb-legacy-1,drawing_v2,"
    "unreviewed,True,True,1,,,,5,9,0.0,0,0,0,,,,,,,,扭力 9 N·m [1]\n"
    "u1,test,untyped,mfg-099,,hybrid,fake-llm,hybrid,False,kb-legacy-1,drawing_v2,,"
    "False,False,1,,,,5,9,0.0,0,0,0,,,,,,,,用了雨點皴 [1]\n"
    "p1,dev,kb_only,art-1,zh,lora_norag,fake-llm,hybrid,False,kb-legacy-1,drawing_v2,"
    ",True,True,1,,,,5,9,0.0,0,0,0,,,,,,,,用了雨點皴 [1]\n"
    "l1,test,kb_only,art-1,zh,lora_norag,lora-x,hybrid,False,kb-legacy-1,drawing_v2,,"
    "True,True,1,,,,5,9,0.0,0,0,0,,,,,,,,雨點皴 [1]\n"
    "n1,test,no_answer,art-1,zh,lora_norag,fake-llm,hybrid,False,kb-legacy-1,"
    "drawing_v2,,True,True,0,,,,5,9,0.0,0,0,0,,,,,,True,False,知識庫沒有這方面的資料\n"
    "d1,test,untyped,mfg-001,,lora_norag,fake-llm,hybrid,False,kb-legacy-1,"
    "drawing_v2,unreviewed,True,True,1,,,,5,9,0.0,0,0,0,,,,,,,,扭力 9 N·m [1]\n"
    "u1,test,untyped,mfg-099,,lora_norag,fake-llm,hybrid,False,kb-legacy-1,"
    "drawing_v2,,False,False,1,,,,5,9,0.0,0,0,0,,,,,,,,用了雨點皴 [1]\n"
)
LEGACY_IMAGES_CSV = (
    "photo,truth,matched,predicted,top1,top5,rejected,clip,inliers\n"
    "photos/known/art-1__a.jpg,art-1,True,art-1,True,True,False,0.9,5\n"
    "photos/unknown/zzz__b.jpg,(未收錄),True,art-1,False,False,False,0.9,5\n"
)


# ------------------------------------------------------------------ 真 chat 服務整合

SPIED = (
    "retrieve",
    "get_provider",
    "load_image",
    "load_upload",
    "identify_any",
    "inject_distractors",
)


@pytest.fixture(scope="module")
def real_run(client, tmp_path_factory):
    """用實際 FastAPI app 的 /api/v1/chat 跑 33 題（LLM_MODE=mock），並 spy 讀圖、retrieve、模型。

    calls 依時間順序混放：("chat", 帳號, part_id) 標記一題開始，其後到下一個標記前的
    ("retrieve" 等, 參數) 都是那一題在實際服務裡觸發的。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import chat_service

    mp = pytest.MonkeyPatch()
    calls = []

    def spy(name):
        original = getattr(chat_service, name)

        def wrapper(*a, **k):
            calls.append((name, a, k))
            return original(*a, **k)

        mp.setattr(chat_service, name, wrapper)

    for name in SPIED:
        spy(name)

    class SpyClient(TestClient):
        account = None

        def post(self, url, **kw):
            if url.endswith("/auth/switch"):
                self.account = kw["json"]["account_id"]
            return super().post(url, **kw)

        def stream(self, method, url, **kw):
            if url.endswith("/chat"):
                calls.append(("chat", self.account, kw["json"]["part_id"]))
            return super().stream(method, url, **kw)

    out = tmp_path_factory.mktemp("real-run")
    drawing = ROOT / "eval" / "drawing_retrieval"
    names = ("qa.jsonl", "answers.jsonl", "gold.json")
    before = {n: hashlib.sha256((drawing / n).read_bytes()).hexdigest() for n in names}
    module = load_evaluator("run_eval_accounts_real")
    mp.setattr(module, "RUNS", out)
    mp.setattr(module, "OFFICIAL_RUNS", out / "official")
    mp.setattr(module.httpx, "Client", lambda **kw: SpyClient(app))
    mp.setattr(
        sys, "argv", ["run_eval.py", "--accounts", "manager,sales", "--strategies", "hybrid"]
    )
    try:
        code = module.main()
    finally:
        mp.undo()
    art = json.loads(next(out.glob("*-generation-accounts.json")).read_text("utf-8"))
    after = {n: hashlib.sha256((drawing / n).read_bytes()).hexdigest() for n in names}
    return SimpleNamespace(code=code, art=art, calls=calls, before=before, after=after)


def segments(calls):
    """[(帳號, part_id, [該題觸發的被 spy 呼叫名稱])]"""
    out = []
    for c in calls:
        if c[0] == "chat":
            out.append((c[1], c[2], []))
        else:
            out[-1][2].append(c[0])
    return out


def test_all_33_questions_run_for_each_account(real_run):
    art = real_run.art
    lines = (ROOT / "eval/drawing_retrieval/qa.jsonl").read_text("utf-8").splitlines()
    qa_ids = [json.loads(x)["id"] for x in lines if x.strip()]
    assert len(qa_ids) == 33
    for account in ("manager", "sales"):
        got = [
            r["question_id"]
            for r in art["rows"]
            if r["account"] == account and r["strategy"] == "hybrid"
        ]
        assert got == qa_ids
    assert real_run.before == real_run.after  # 原 QA、答案、gold 不寫回
    assert (
        real_run.before["qa.jsonl"]
        == "37fa712273323059ed5a96dc73531b7a5b72b1af98e35522220ff3f3ace53624"
    )
    assert art["provenance"]["hashes"]["qa_sha256"] == real_run.before["qa.jsonl"]
    assert real_run.code == 0


def test_real_chat_visibility_snapshot_matches_backend(real_run):
    art = real_run.art
    # 不硬編哪個角色看得到什麼：只確認快照與分類一致、且環境裡確實有看不到的圖紙與不存在的圖紙
    for account in ("manager", "sales"):
        visible = set(art["snapshots"][account]["visible_part_ids"])
        for r in art["rows"]:
            if r["account"] == account and r["classification"] != "missing_part":
                assert (r["part_id"] in visible) == (r["classification"] != "permission_degraded")
    assert any(r["classification"] == "permission_degraded" for r in art["rows"])
    missing = {r["part_id"] for r in art["rows"] if r["classification"] == "missing_part"}
    assert missing == {"mfg-099"}


def test_real_chat_hidden_drawings_stop_at_stage1_without_read_retrieve_or_llm(real_run):
    art = real_run.art
    hidden = [r for r in art["rows"] if r["classification"] == "permission_degraded"]
    assert hidden
    for r in hidden:
        assert r["stage1_only"] is True, (r["question_id"], r["stage1_failures"])
        assert (r["answer_ok"], r["leaked"], r["refused"]) == (True, False, True)
        assert r["sources_state"] == "empty"
        assert r["done"]["degraded"] is True and r["done"]["tokens"] == {"input": 0, "output": 0}
    hidden_keys = {(r["account"], r["part_id"]) for r in hidden}
    segs = segments(real_run.calls)
    assert len(segs) == len(art["rows"])
    # spy 有效：可見的題目真的檢索、呼叫模型
    visible_segs = [s for s in segs if (s[0], s[1]) not in hidden_keys and s[1] != "mfg-099"]
    assert any("retrieve" in s[2] for s in visible_segs)
    assert any("get_provider" in s[2] for s in visible_segs)
    # 看不到的圖紙、不存在的圖紙：讀圖、retrieve、模型、辨識一律沒被呼叫
    for account, part, called in segs:
        if (account, part) in hidden_keys or part == "mfg-099":
            assert called == [], (account, part, called)


def test_real_chat_events_are_preserved_with_pipeline_and_post_filter(real_run):
    answered = [
        r
        for r in real_run.art["rows"]
        if r["classification"] == "visible_answerable" and not r["error"]
    ]
    assert answered
    for r in answered:
        events = [e for e in r["events"] if e["event"] == "sources"]
        assert events and "pipeline" in events[0]["data"] and "post_filter" in events[0]["data"]
        assert r["done"] and r["request_id"] == r["done"]["request_id"]
    chunk_ids = [
        s.get("chunk_id")
        for r in answered
        for e in r["events"]
        if e["event"] == "sources"
        for s in e["data"]["sources"]
    ]
    assert chunk_ids and all(chunk_ids)
