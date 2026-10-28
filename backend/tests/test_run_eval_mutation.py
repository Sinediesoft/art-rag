"""評測的 HTTP、CLI 與輸出契約；固定回應只隔離 I/O，判分與統計仍跑產品程式。

由測試所在 repo 動態載入 run_eval.py，讓 TMP 變異量測可替換目標。
不載入 app、設定、真模型或索引；所有 HTTP 使用 MockTransport，輸出限 pytest TMP。
"""

import copy
import csv
import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "eval" / "run_eval.py"
REAL_CLIENT = httpx.Client
MISSING = object()


def load_evaluator(path):
    spec = importlib.util.spec_from_file_location("run_eval_mutation_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_eval = load_evaluator(TARGET)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("評測契約測試不得建立網路連線")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def question(qid="q", **fields):
    return {
        "id": qid,
        "question": qid,
        "artwork_id": "art-1",
        "split": "test",
        "topics": ["規格"],
        "keywords": [["9 N·m"]],
        **fields,
    }


def source(ref=1, owner="art-1", topic="規格", **fields):
    key = "part_id" if owner.startswith("mfg-") else "artwork_id"
    return {"ref": ref, key: owner, "topic": topic, **fields}


def sse(event, data):
    # SSE 允許冒號後沒有空格，避免只測到容易掩蓋 slicing 錯誤的格式。
    return f"event:{event}\ndata:{json.dumps(data, ensure_ascii=False)}\n\n"


def reply(answer="9 N·m [1]", *, sources=None, done=MISSING, **fields):
    return {
        "answer": answer,
        "sources": [source()] if sources is None else sources,
        "done": {} if done is MISSING else done,
        **fields,
    }


class EvalAPI:
    """可控 HTTP 邊界；不依 keywords 產生答案、不替換判分或統計函式。"""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self.requests = []
        self.bodies = []
        self.clock = 100.0
        self.health = 200
        self.uploads = []

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        assert request.url.host == "eval.invalid"
        path = request.url.path
        if path == "/api/v1/health":
            if self.health == "connection_error":
                raise httpx.ConnectError("isolated unavailable backend", request=request)
            return httpx.Response(self.health, json={})
        if path == "/api/v1/auth/switch":
            assert request.method == "POST"
            assert json.loads(request.content) == {"account_id": "manager"}
            return httpx.Response(200, json={})
        if path == "/api/v1/chat":
            assert request.method == "POST"
            body = json.loads(request.content)
            self.bodies.append(body)
            value = self.replies.get(body["question"], reply())
            value = value(body) if callable(value) else value
            self.clock += value.get("seconds", 2.375)
            if value.get("status", 200) != 200:
                return httpx.Response(value["status"], json=value.get("json", {}))
            payload = ":keepalive\n\n" + sse(
                "sources",
                {
                    "sources": value["sources"],
                    **{k: value[k] for k in ("rearrange", "conflict_check") if k in value},
                },
            )
            for token in value.get("tokens", [value["answer"]]):
                payload += sse("token", {"text": token})
            if value["done"] is not None:
                payload += sse("done", value["done"])
            if value.get("error"):
                payload += sse("error", value["error"])
            payload += sse("heartbeat", {"alive": True})
            return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
        if path == "/api/v1/images":
            assert request.method == "POST"
            assert b'name="file";' in request.content
            assert b"Content-Type: image/jpeg" in request.content
            name = request.content.split(b'filename="')[1].split(b'"')[0].decode()
            self.uploads.append(name)
            return httpx.Response(200, json={"image_id": name})
        if path == "/api/v1/search/image":
            assert request.method == "POST"
            name = json.loads(request.content)["image_id"]
            known = name.startswith("art-1__")
            return httpx.Response(
                200,
                json={
                    "matched": known,
                    "best_artwork_id": "art-1" if known else None,
                    "results": [{"artwork": {"id": "art-1"}, "score": 0.8, "inliers": 4}]
                    if known
                    else [],
                },
            )
        assert request.method == "GET"
        routes = {
            "/api/v1/auth/accounts": {},
            "/api/v1/admin/diagnostics": {"manifest": {"kb_version": "kb-fixed-17"}},
            "/api/v1/status": {"eval_controls": True},
            "/api/v1/artworks": {"items": [{"id": "art-1"}, {"id": "art-2"}]},
            "/api/v1/parts": {"items": [{"id": "mfg-001"}]},
            "/api/v1/artworks/art-1": {
                "descriptions": [{"lang": "zh"}, {"lang": "en"}, {"lang": "zh"}]
            },
            "/api/v1/artworks/art-2": {"descriptions": [{"lang": "ja"}]},
        }
        assert path in routes, f"未預期的 HTTP 請求：{request.method} {path}"
        return httpx.Response(200, json=routes[path])


def install_api(monkeypatch, module, api):
    clients = []

    def client(**kwargs):
        obj = REAL_CLIENT(transport=httpx.MockTransport(api.handle), **kwargs)
        clients.append(obj)
        return obj

    monkeypatch.setattr(module.httpx, "Client", client)
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: api.clock))
    return clients


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", "utf-8")


def read_outputs(runs):
    (summary_path,) = runs.glob("*.json")
    summary = json.loads(summary_path.read_text("utf-8"))
    csv_path = summary_path.with_suffix(".csv")
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        columns = reader.fieldnames
    assert summary["run_id"] == csv_path.stem
    assert datetime.fromisoformat(summary["created_at"]).utcoffset() == UTC.utcoffset(None)
    return rows, summary, columns


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """只替換傳輸、時間與產物目錄；透過 argv 讀取實際暫存 JSONL。"""
    runs = tmp_path / "runs"
    monkeypatch.setattr(run_eval, "RUNS", runs)
    client_groups = []

    def execute(questions, *, api=None, strategies="hybrid", options=(), extra_files=()):
        api = api or EvalAPI()
        client_groups.append(install_api(monkeypatch, run_eval, api))
        paths = [tmp_path / "qa.jsonl"]
        write_jsonl(paths[0], questions)
        for name, records in extra_files:
            path = tmp_path / name
            write_jsonl(path, records)
            paths.append(path)
        argv = [
            "run_eval.py",
            "--base",
            "http://eval.invalid:4312",
            "--skip-images",
            "--strategies",
            strategies,
            "--qa",
            ",".join(map(str, paths)),
            *options,
        ]
        monkeypatch.setattr(sys, "argv", argv)
        code = run_eval.main()
        rows, summary, columns = read_outputs(runs)
        return code, rows, summary, columns, api

    yield execute
    for clients in client_groups:
        for client in clients:
            client.close()


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], None),
        ([None, None], None),
        ([37], 37),
        ([2, 1], 2),
        ([None, 6, 1, 4], 6),
        (list(range(100, -1, -1)), 95),
        (list(range(40)), 37),
    ],
)
def test_p95_empty_nullable_and_rank_boundaries(values, expected):
    original = values.copy()
    assert run_eval.p95(values) == expected
    assert values == original


def test_run_chat_accumulates_tokens_and_preserves_event_metadata():
    sources = [source(3)]
    rearrange = {"ms": 42, "fallback": "TIMEOUT"}
    conflict = {"fallback": False, "conflict": True}
    done = {"model": "fixed", "latency_ms": {"first_token": 4, "total": 8}}
    payload = ":comment\n\n" + sse(
        "sources",
        {
            "sources": sources,
            "rearrange": rearrange,
            "conflict_check": conflict,
        },
    )
    payload += sse("token", {"text": "9 "}) + sse("token", {"text": "N·m\n[3]"})
    payload += sse("done", done) + sse("heartbeat", {"alive": True})

    def handler(request):
        assert request.method == "POST" and request.url.path == "/api/v1/chat"
        assert json.loads(request.content) == {"question": "扭力？", "strategy": "hybrid"}
        return httpx.Response(200, text=payload)

    with REAL_CLIENT(transport=httpx.MockTransport(handler), base_url="http://eval.invalid") as c:
        result = run_eval.run_chat(c, {"question": "扭力？", "strategy": "hybrid"})
    assert result == {
        "answer": "9 N·m\n[3]",
        "sources": sources,
        "rearrange": rearrange,
        "conflict_check": conflict,
        "done": done,
        "error": None,
    }


@pytest.mark.parametrize("payload", ["", ":ping\n\n", sse("heartbeat", {"alive": True})])
def test_run_chat_without_answer_has_stable_empty_contract(payload):
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload)),
        base_url="http://eval.invalid",
    ) as c:
        assert run_eval.run_chat(c, {"question": "?"}) == {
            "answer": "",
            "sources": [],
            "rearrange": None,
            "conflict_check": None,
            "done": None,
            "error": None,
        }


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (
            403,
            {"error": {"code": "FORBIDDEN", "detail": "blocked"}},
            {"code": "FORBIDDEN", "detail": "blocked"},
        ),
        (502, {}, {"code": "HTTP 502"}),
        (429, {"error": None}, {"code": "HTTP 429"}),
    ],
)
def test_run_chat_http_errors_have_structured_or_status_error(status, body, error):
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)),
        base_url="http://eval.invalid",
    ) as c:
        result = run_eval.run_chat(c, {"question": "?"})
    assert result == {
        "answer": "",
        "sources": [],
        "rearrange": None,
        "conflict_check": None,
        "done": None,
        "error": error,
    }


def test_run_chat_stream_error_keeps_partial_answer_but_judge_rejects_it():
    payload = sse("token", {"text": "9 N·m [1]"})
    payload += sse("error", {"code": "GENERATION_FAILED"})
    with REAL_CLIENT(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=payload)),
        base_url="http://eval.invalid",
    ) as c:
        result = run_eval.run_chat(c, {"question": "?"})
    assert result["answer"] == "9 N·m [1]"
    assert result["error"] == {"code": "GENERATION_FAILED"}
    assert result["done"] is None
    assert run_eval.judge(question(), result) == (False, False, False, {})


@pytest.mark.parametrize(
    ("strategy", "option", "enabled"),
    [
        ("hybrid_plain", "rearrange", False),
        ("hybrid_rearrange", "rearrange", True),
        ("hybrid_img", "send_image", True),
        ("hybrid_noimg", "send_image", False),
        ("hybrid_cc", "conflict_check", True),
        ("hybrid_nocc", "conflict_check", False),
    ],
)
def test_cli_strategy_options_reach_http_and_affect_recorded_results(
    cli, strategy, option, enabled
):
    def backend(body):
        assert body["strategy"] == "hybrid"
        assert body[option] is enabled
        assert body["allow_fallback"] is False
        assert body["artwork_id"] == "art-1"
        value = reply(done={"egress": {"images": int(body.get("send_image", False))}})
        if body.get("rearrange"):
            value["rearrange"] = {"ms": 17}
        if body.get("conflict_check"):
            value["conflict_check"] = {"fallback": False, "conflict": True}
        return value

    _, rows, summary, _, _ = cli([question()], strategies=strategy, api=EvalAPI({"q": backend}))
    (row,) = rows
    assert row["strategy"] == strategy
    assert row["egress_images"] == ("1" if option == "send_image" and enabled else "0")
    assert row["rearrange_ms"] == ("17" if option == "rearrange" and enabled else "")
    assert row["conflict_check"] == ("conflict" if option == "conflict_check" and enabled else "")
    assert summary["by_strategy"][strategy]["answer_ok"] == 1.0


@pytest.mark.parametrize(
    ("strategy", "family", "retrieval"),
    [
        ("hybrid", "hybrid", True),
        ("hybrid_norag", "hybrid", False),
        ("api_nokb", "api_nokb", True),
        ("api_kb", "api_kb", True),
        ("lora", "lora", True),
        ("lora_norag", "lora", False),
        ("mock", "mock", True),
    ],
)
def test_cli_model_family_and_retrieval_control_real_requests(cli, strategy, family, retrieval):
    distractors = [{"text": "固定干擾段落", "topic": "規格"}]

    def backend(body):
        assert body["strategy"] == family
        assert body["allow_fallback"] is False
        assert body["question"] == "q"
        if retrieval:
            assert body["inject"] == distractors
        else:
            assert body["use_retrieval"] is False
            assert "inject" not in body
        return reply(done={"strategy_used": family, "model": "fixed", "fallback": False})

    code, rows, summary, _, _ = cli(
        [question(distractors=distractors)], strategies=strategy, api=EvalAPI({"q": backend})
    )
    assert code == 0
    assert rows[0]["strategy_used"] == family and rows[0]["model"] == "fixed"
    assert rows[0]["fallback"] == "False"
    assert (summary["by_strategy"][strategy]["distractor"] is not None) is retrieval


def test_cli_complete_csv_row_schema_and_server_metadata(cli):
    q = question("drawing", part_id="mfg-001", type="kb_only", review_status="unreviewed")
    value = reply(
        "9 N·m\n[4]",
        sources=[source(4, "mfg-001")],
        done={
            "model": "drawing-fixed",
            "strategy_used": "hybrid",
            "fallback": False,
            "prompt_version": "drawing_v8",
            "latency_ms": {"first_token": 12, "total": 345},
            "cost_twd": 0.0123457,
            "egress": {"images": 2, "chunks": 3, "bytes": 456},
        },
        rearrange={"ms": 23, "fallback": "TIMEOUT"},
        conflict_check={"fallback": False, "conflict": False},
    )
    code, rows, summary, columns, api = cli([q], api=EvalAPI({"drawing": value}))
    expected = {
        "question_id": "drawing",
        "split": "test",
        "type": "kb_only",
        "artwork_id": "mfg-001",
        "source_lang": "",
        "strategy": "hybrid",
        "model": "drawing-fixed",
        "strategy_used": "hybrid",
        "fallback": "False",
        "kb_version": "kb-fixed-17",
        "prompt_version": "drawing_v8",
        "review_status": "unreviewed",
        "answer_ok": "True",
        "citation_ok": "True",
        "n_sources": "1",
        "rearrange_ms": "23",
        "rearrange_fallback": "TIMEOUT",
        "conflict_check": "none",
        "first_token_ms": "12",
        "total_ms": "345",
        "cost_twd": "0.0123457",
        "egress_images": "2",
        "egress_chunks": "3",
        "egress_bytes": "456",
        "error": "",
        "distractor_kept": "",
        "distractor_cited": "",
        "distractor_flagged": "",
        "distractor_misled": "",
        "refused": "",
        "hallucinated": "",
        "answer": "9 N·m [4]",
    }
    assert code == 0 and rows == [expected]
    assert columns == list(expected)
    assert summary["kb_version"] == "kb-fixed-17" and summary["prompt_version"] == ""
    assert summary["summary"] == {"split": "all"}
    stats = summary["by_strategy"]["hybrid"]
    assert stats["cost_twd"] == 0.0123 and stats["rearrange_fallbacks"] == 1
    assert stats["egress_bytes"] == 456
    assert api.bodies == [
        {"question": "drawing", "allow_fallback": False, "strategy": "hybrid", "part_id": "mfg-001"}
    ]
    assert api.requests[:4] == [
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/auth/accounts"),
        ("POST", "/api/v1/auth/switch"),
        ("GET", "/api/v1/admin/diagnostics"),
    ]
    assert api.requests.count(("POST", "/api/v1/auth/switch")) == 2
    assert ("GET", "/api/v1/artworks/mfg-001") not in api.requests


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        (None, ""),
        ({"fallback": True, "conflict": False}, "fallback"),
        ({"fallback": True, "conflict": True}, "fallback"),
        ({"fallback": False, "conflict": True}, "conflict"),
        ({"fallback": False, "conflict": False}, "none"),
    ],
)
def test_cli_conflict_check_states(cli, check, expected):
    _, rows, _, _, _ = cli([question()], api=EvalAPI({"q": reply(conflict_check=check)}))
    assert rows[0]["conflict_check"] == expected


@pytest.mark.parametrize("done", [None, {}, {"latency_ms": {"total": 0}}])
def test_cli_missing_done_and_zero_latency_use_elapsed_milliseconds_and_default_counters(cli, done):
    _, rows, summary, _, _ = cli([question()], api=EvalAPI({"q": reply(done=done)}))
    row = rows[0]
    assert row["total_ms"] == "2375"  # 固定超過一秒，±1 毫秒倍率不會被 round 掩蓋。
    assert row["first_token_ms"] == ""
    assert (
        row["cost_twd"]
        == row["egress_images"]
        == row["egress_chunks"]
        == row["egress_bytes"]
        == "0"
    )
    assert row["type"] == "untyped" and row["source_lang"] == "en+zh"
    assert row["model"] == row["strategy_used"] == row["prompt_version"] == ""
    assert summary["by_strategy"]["hybrid"]["p95_total_ms"] == 2375


def test_cli_mixed_outcomes_have_independent_denominators_and_rounded_cost(cli):
    questions = [
        question("good", type="common"),
        question("wrong", type="common", split="dev", artwork_id="art-2"),
        question("fallback", artwork_id=None, type="no_answer", expect_refusal=True),
        question("error", artwork_id=None, type="no_answer", expect_refusal=True),
        question("outside", artwork_id="art-out", type="common"),
        question("refusal", artwork_id=None, type="no_answer", expect_refusal=True, split="dev"),
    ]
    api = EvalAPI(
        {
            "good": reply(
                sources=[source(), source(2, topic="背景")],
                done={
                    "model": "z",
                    "prompt_version": "answer_v3",
                    "cost_twd": 0.010031,
                    "latency_ms": {"first_token": 40, "total": 100},
                    "egress": {"bytes": 11},
                },
            ),
            "wrong": reply(
                "錯誤 [1]",
                sources=[source(owner="art-2", topic="背景")],
                done={
                    "model": "a",
                    "prompt_version": "answer_v4",
                    "cost_twd": 0.020031,
                    "latency_ms": {"first_token": 20, "total": 300},
                    "egress": {"bytes": 22},
                },
            ),
            "fallback": reply(
                "沒有相關資料。",
                sources=[],
                done={
                    "model": "backup",
                    "fallback": True,
                    "cost_twd": 0.030031,
                    "latency_ms": {"total": 700},
                    "egress": {"bytes": 33},
                },
            ),
            "error": reply(status=503, seconds=8.0),
            "outside": reply(
                "錯誤",
                sources=[],
                done={
                    "model": "a",
                    "prompt_version": "answer_v6",
                    "cost_twd": 0.040031,
                    "latency_ms": {"first_token": 30, "total": 500},
                    "egress": {"bytes": 44},
                },
            ),
            "refusal": reply("沒有相關資料。", sources=[]),
        }
    )
    code, rows, summary, _, api = cli(questions, api=api, options=["--outside-kb"])
    assert code == 0
    assert summary["prompt_version"] == "answer_v6"
    stats = summary["by_strategy"]["hybrid"]
    assert stats == {
        "n": 6,
        "errors": 1,
        "fallbacks": 1,
        "models": ["a", "backup", "z"],
        "answer_ok": pytest.approx(2 / 6),
        "citation_ok": pytest.approx(2 / 6),
        "mean_sources": 0.6,
        "rearrange_fallbacks": 0,
        "p95_first_token_ms": 40,
        "p95_total_ms": 2375,
        "median_total_ms": 500,
        "cost_twd": 0.1001,
        "egress_bytes": 110,
        "by_type": {"common": 0.5, "no_answer": pytest.approx(1 / 3), "outside_kb": 0.0},
        "by_split_type": {
            "dev": {"common": 0.0, "no_answer": 1.0},
            "test": {"common": 1.0, "no_answer": 0.0, "outside_kb": 0.0},
        },
        "distractor": None,
        "no_kb_answer": {"n": 4, "refused": 0.25, "hallucinated": 0.25},
    }
    by_id = {r["question_id"]: r for r in rows}
    assert by_id["error"]["error"] == "HTTP 503" and by_id["error"]["total_ms"] == "8000"
    for qid in ("fallback", "error"):
        assert by_id[qid]["answer_ok"] == by_id[qid]["citation_ok"] == "False"
        assert by_id[qid]["refused"] == by_id[qid]["hallucinated"] == ""
    assert by_id["fallback"]["fallback"] == "True"
    assert by_id["refusal"]["refused"] == "True" and by_id["refusal"]["hallucinated"] == "False"
    assert by_id["outside"]["refused"] == "False" and by_id["outside"]["hallucinated"] == "True"
    assert by_id["good"]["source_lang"] == "en+zh" and by_id["wrong"]["source_lang"] == "ja"
    assert [b for b in api.bodies if b["question"] == "outside"] == [
        {"question": "outside", "strategy": "hybrid", "allow_fallback": False},
    ]


def test_cli_distractors_keep_citation_conflict_and_error_contracts(cli):
    ids = ["filtered", "kept", "cited", "misled", "flagged", "forbidden"]
    questions = [
        question(qid, type="distractor", distractors=[{"text": "99 N·m"}], forbidden=["99 N·m"])
        for qid in ids
    ]
    injected = [source(), source(2, injected=True)]
    api = EvalAPI(
        {
            "filtered": reply(),
            "kept": reply(sources=injected),
            "cited": reply("9 N·m [2]", sources=injected),
            "misled": reply("9 N·m、99 N·m [2]", sources=injected),
            "flagged": reply("9 N·m；99 N·m，資料不一致 [1][2]", sources=injected),
            "forbidden": reply(status=403, json={"error": {"code": "FORBIDDEN"}}),
        }
    )
    _, rows, summary, _, _ = cli(questions, api=api)
    flags = [
        tuple(
            r[k]
            for k in (
                "distractor_kept",
                "distractor_cited",
                "distractor_flagged",
                "distractor_misled",
                "answer_ok",
                "citation_ok",
            )
        )
        for r in rows
    ]
    assert flags == [
        ("False", "False", "False", "False", "True", "True"),
        ("True", "False", "False", "False", "True", "True"),
        ("True", "True", "False", "False", "False", "False"),
        ("True", "True", "False", "True", "False", "False"),
        ("True", "True", "True", "False", "True", "True"),
        ("", "", "", "", "False", "False"),
    ]
    assert rows[-1]["error"] == "FORBIDDEN"
    assert summary["by_strategy"]["hybrid"]["distractor"] == {
        "n": 5,
        "filtered_out": 0.2,
        "cited": 0.6,
        "flagged": 0.2,
        "misled": 0.2,
    }


def test_cli_drawing_prompt_version_does_not_replace_artwork_version(cli):
    questions = [question("art"), question("empty"), question("part", part_id="mfg-001")]
    api = EvalAPI(
        {
            "art": reply(done={"prompt_version": "answer_v7"}),
            "empty": reply(),
            "part": reply(sources=[source(owner="mfg-001")], done={"prompt_version": "drawing_v8"}),
        }
    )
    _, rows, summary, _, _ = cli(questions, api=api)
    assert [r["prompt_version"] for r in rows] == ["answer_v7", "", "drawing_v8"]
    assert summary["prompt_version"] == "answer_v7"


@pytest.mark.parametrize(
    ("split", "outside", "expected"),
    [
        ("all", False, ["known", "dev", "common", "part", "unknown-part-refusal"]),
        ("test", False, ["known", "common", "part", "unknown-part-refusal"]),
        ("dev", False, ["dev"]),
        (
            "all",
            True,
            [
                "known",
                "dev",
                "outside",
                "outside-refusal",
                "common",
                "part",
                "unknown-part-refusal",
            ],
        ),
        (
            "test",
            True,
            ["known", "outside", "outside-refusal", "common", "part", "unknown-part-refusal"],
        ),
        ("dev", True, ["dev"]),
    ],
)
def test_cli_multi_file_split_owner_selection_and_outside_body(cli, split, outside, expected):
    first = [
        question("known"),
        question("dev", split="dev"),
        question("outside", artwork_id="art-out"),
        question("outside-refusal", artwork_id="art-out", expect_refusal=True),
    ]
    second = [
        question("common", artwork_id=None),
        question("part", part_id="mfg-001"),
        question("unknown-part", part_id="mfg-099"),
        question("unknown-part-refusal", part_id="mfg-099", expect_refusal=True),
    ]
    before = copy.deepcopy(first + second)
    options = ["--split", split] + (["--outside-kb"] if outside else [])
    _, rows, summary, _, api = cli(first, extra_files=[("second.jsonl", second)], options=options)
    assert [r["question_id"] for r in rows] == expected
    assert first + second == before
    assert summary["summary"] == {"split": split}
    assert summary["by_strategy"]["hybrid"]["n"] == len(expected)
    bodies = {b["question"]: b for b in api.bodies}
    for qid in ("outside", "outside-refusal", "common"):
        if qid in bodies:
            assert "artwork_id" not in bodies[qid] and "part_id" not in bodies[qid]
    for row in rows:
        if row["question_id"] in ("outside", "outside-refusal"):
            assert row["type"] == "outside_kb" and row["artwork_id"] == "art-out"
    if "unknown-part-refusal" in bodies:
        assert bodies["unknown-part-refusal"]["part_id"] == "mfg-099"


def test_cli_empty_selection_writes_header_and_null_metrics(cli):
    code, rows, summary, columns, api = cli([question()], options=["--split", "dev"])
    assert code == 0 and rows == [] and columns == ["question_id"] and api.bodies == []
    assert summary["by_strategy"]["hybrid"] == {
        "n": 0,
        "errors": 0,
        "fallbacks": 0,
        "models": [],
        "answer_ok": None,
        "citation_ok": None,
        "mean_sources": None,
        "rearrange_fallbacks": 0,
        "p95_first_token_ms": None,
        "p95_total_ms": None,
        "median_total_ms": None,
        "cost_twd": 0,
        "egress_bytes": 0,
        "by_type": {},
        "by_split_type": {},
        "distractor": None,
        "no_kb_answer": None,
    }


@pytest.mark.parametrize(
    ("strategy", "failure", "code"),
    [
        ("lora", "fallback", 3),
        ("lora_norag", "fallback", 3),
        ("lora", "error", 3),
        ("lora_norag", "error", 3),
        ("hybrid", "fallback", 0),
        ("hybrid", "error", 0),
        ("lora", "success", 0),
    ],
)
def test_cli_lora_exit_code_distinguishes_error_fallback_and_other_strategies(
    cli, strategy, failure, code
):
    value = reply(done={"model": "fixed", "fallback": failure == "fallback"})
    if failure == "error":
        value["error"] = {"code": "STRATEGY_UNAVAILABLE"}
    actual, rows, summary, _, _ = cli([question()], strategies=strategy, api=EvalAPI({"q": value}))
    assert actual == code
    assert rows[0]["fallback"] == str(failure == "fallback")
    assert rows[0]["error"] == ("STRATEGY_UNAVAILABLE" if failure == "error" else "")
    assert summary["by_strategy"][strategy]["answer_ok"] == (1.0 if failure == "success" else 0.0)


@pytest.mark.parametrize("health", [503, "connection_error"])
def test_cli_health_failure_exits_one_before_auth_or_output(monkeypatch, tmp_path, health):
    api = EvalAPI()
    api.health = health
    clients = install_api(monkeypatch, run_eval, api)
    monkeypatch.setattr(run_eval, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--base", "http://eval.invalid"])
    try:
        assert run_eval.main() == 1
    finally:
        for client in clients:
            client.close()
    assert api.requests == [("GET", "/api/v1/health")]
    assert not (tmp_path / "runs").exists()


def test_cli_invalid_question_file_exits_one_without_results(monkeypatch, tmp_path):
    api = EvalAPI()
    clients = install_api(monkeypatch, run_eval, api)
    path = tmp_path / "invalid.jsonl"
    path.write_text("{broken json}\n", "utf-8")
    monkeypatch.setattr(run_eval, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_eval.py", "--base", "http://eval.invalid", "--skip-images", "--qa", str(path)],
    )
    try:
        assert run_eval.main() == 1
    finally:
        for client in clients:
            client.close()
    assert api.bodies == [] and not (tmp_path / "runs").exists()


def test_cli_defaults_read_qa_run_both_strategies_and_write_only_eval_runs(monkeypatch, tmp_path):
    # 來源在執行當下讀取，因此 TMP harness 替換的 mutant 也會用於這份隔離副本。
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    script = eval_dir / "run_eval.py"
    script.write_bytes(TARGET.read_bytes())
    module = load_evaluator(script)
    write_jsonl(eval_dir / "qa.jsonl", [question()])
    for name in ("photos/known/art-1__front.jpg", "photos/unknown/street.jpg", "outside.jpg"):
        path = eval_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"isolated-photo")
    api = EvalAPI()
    clients = install_api(monkeypatch, module, api)
    monkeypatch.setattr(sys, "argv", ["run_eval.py", "--base", "http://eval.invalid"])
    try:
        assert module.main() == 0
    finally:
        for client in clients:
            client.close()
    runs = eval_dir / "runs"
    rows, summary, _ = read_outputs(runs)
    assert sorted(p.suffix for p in runs.iterdir()) == [".csv", ".csv", ".json"]
    assert list(eval_dir.glob("*.csv")) == list(eval_dir.glob("*.json")) == []
    assert [r["strategy"] for r in rows] == ["hybrid", "hybrid_norag"]
    assert set(summary["by_strategy"]) == {"hybrid", "hybrid_norag"}
    assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{4}", summary["run_id"])
    assert summary["summary"] == {
        "image_top1": 1.0,
        "image_top5": 1.0,
        "reject_rate": 1.0,
        "n_known": 1,
        "n_unknown": 1,
        "split": "all",
    }
    assert api.uploads == ["art-1__front.jpg", "street.jpg"]
    (images_path,) = runs.glob("*-images.csv")
    with images_path.open(encoding="utf-8", newline="") as f:
        image_rows = list(csv.DictReader(f))
    assert [r["photo"] for r in image_rows] == [
        "photos/known/art-1__front.jpg",
        "photos/unknown/street.jpg",
    ]


def test_script_entrypoint_help_and_invalid_split_are_available_without_backend():
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TMPDIR") if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    help_result = subprocess.run(
        [sys.executable, "-I", str(TARGET), "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert help_result.returncode == 0
    for option in ("--base", "--strategies", "--qa", "--split", "--outside-kb", "--skip-images"):
        assert option in help_result.stdout
    invalid = subprocess.run(
        [sys.executable, "-I", str(TARGET), "--split", "invalid"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert invalid.returncode == 2


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("沒有相關資料。", True),
        ("沒有資料，但 2026 年發生。", False),
        ("只有 99 件，沒有其他資料。", True),
        ("查無資料。", False),
        ("已知資料。", False),
    ],
)
def test_refusal_answer_requires_refusal_and_no_three_digit_claim(answer, expected):
    q = question(expect_refusal=True, type="no_answer")
    result = run_eval.judge(q, {"answer": answer, "sources": [], "done": None, "error": None})
    assert result[:3] == (expected, expected, False)


def test_no_answer_accepted_short_refusal_counts_as_refusal_in_summary():
    q = question(type="no_answer", expect_refusal=True)
    result = run_eval.judge(q, {"answer": "沒有。", "sources": [], "done": {}, "error": None})
    # 簡短拒答通過既有 answer_ok 規則，也必須計入亂編率摘要的 refused。
    assert result[0] is True
    assert result[3] == {"refused": True, "hallucinated": False}


@pytest.mark.parametrize(
    ("answer", "sources", "expected"),
    [
        ("9 N·m [0]", [source(0)], (True, False)),
        ("9 N·m [1][8]", [source()], (True, False)),
        ("9 N·m [1][2]", [source(), source(2, "art-2")], (True, False)),
        ("9 N·m [1][2]", [source(topic="背景"), source(2)], (True, True)),
        ("9 N·m [1]", [source(topic="背景")], (True, False)),
    ],
)
def test_citations_require_all_owners_and_at_least_one_gold_topic(answer, sources, expected):
    assert run_eval.score_answer(question(), {"answer": answer, "sources": sources}) == expected


@pytest.mark.parametrize(
    "conflict", ["不一致", "互相矛盾", "有出入", "說法不一", "所述不同", "不符"]
)
def test_distractor_conflict_requires_correct_answer_and_all_keyword_groups(conflict):
    q = question(
        keywords=[["9 N·m", "九牛頓米"], ["四支"]], distractors=["x"], forbidden=["99 N·m"]
    )
    sources = [source(1, injected=True)]
    correct = {"answer": f"九牛頓米、四支；99 N·m，{conflict} [1]", "sources": sources}
    assert run_eval.score_answer(q, correct) == (True, True)
    assert run_eval.score_distractor(q, correct) == {
        "distractor_kept": True,
        "distractor_cited": True,
        "distractor_flagged": True,
        "distractor_misled": False,
    }
    incomplete = {"answer": f"九牛頓米；99 N·m，{conflict} [1]", "sources": sources}
    assert run_eval.score_answer(q, incomplete) == (False, False)
    assert run_eval.score_distractor(q, incomplete)["distractor_flagged"] is False


def test_distractor_forbidden_fact_is_misleading_without_an_injected_source():
    q = question(distractors=["x"], forbidden=["99 N·m"])
    result = {"answer": "9 N·m、99 N·m [1]", "sources": [source()]}
    assert run_eval.score_answer(q, result) == (False, True)
    assert run_eval.score_distractor(q, result) == {
        "distractor_kept": False,
        "distractor_cited": False,
        "distractor_flagged": False,
        "distractor_misled": True,
    }


@pytest.mark.parametrize("aid", [123, True, ["q"]])
def test_sidecar_non_string_id_is_rejected_as_invalid_input(aid):
    with pytest.raises(ValueError):
        run_eval.merge_answers([question()], [{"id": aid, "keywords": [["9 N·m"]]}])


@pytest.mark.parametrize("reason", [None, "", "沒有來源"])
def test_answerable_sidecar_cannot_carry_no_answer_reason(reason):
    with pytest.raises(ValueError):
        run_eval.merge_answers(
            [question()], [{"id": "q", "keywords": [["9 N·m"]], "no_answer_reason": reason}]
        )


def test_absolute_drawing_qa_path_merges_sidecar_and_preserves_explicit_split(tmp_path):
    base = tmp_path / "eval"
    path = base / "drawing_retrieval" / "qa.jsonl"
    q = question(split="dev", part_id="mfg-001")
    q.pop("keywords")
    write_jsonl(path, [q])
    write_jsonl(path.with_name("answers.jsonl"), [{"id": "q", "keywords": [["9 N·m"]]}])
    assert run_eval.load_questions([str(path.resolve())], base=base) == [
        {**q, "keywords": [["9 N·m"]]},
    ]
