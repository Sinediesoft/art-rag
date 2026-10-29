"""針對多帳號 eval 缺口驗證外部可見行為，不替換被測函式。"""

import json
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from test_run_eval_accounts import (
    QUESTIONS,
    FakeBackend,
    done_event,
    part_source,
    run_eval,
)
from test_run_eval_accounts import (
    env as env,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_git_provenance_reports_clean_and_utf8_dirty_paths(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Eval Test")
    _git(repo, "config", "user.email", "eval@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("committed\n", "utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "initial")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(run_eval, "ROOT", repo)

    assert run_eval._git_provenance() == {
        "head": head,
        "dirty": False,
        "dirty_paths": [],
        "error": None,
    }

    tracked.write_text("changed\n", "utf-8")
    unicode_path = repo / "圖紙草稿.txt"
    unicode_path.write_text("new\n", "utf-8")
    state = run_eval._git_provenance()
    assert state == {
        "head": head,
        "dirty": True,
        "dirty_paths": ["tracked.txt", "圖紙草稿.txt"],
        "error": None,
    }


@pytest.mark.parametrize(
    "failure", [OSError("git missing"), subprocess.CalledProcessError(128, ["git"])]
)
def test_git_provenance_turns_git_process_failure_into_complete_state(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(run_eval.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(failure))

    assert run_eval._git_provenance() == {
        "head": None,
        "dirty": None,
        "dirty_paths": [],
        "error": str(failure),
    }


def _raw_client(handler):
    return httpx.Client(base_url="http://eval.invalid", transport=httpx.MockTransport(handler))


def test_raw_sse_framing_keeps_first_invalid_seq_and_stream_error_priority():
    raw = (
        b": keepalive\n\n"
        b'data: {"text":"partial"}\n\n'
        b'event:token\ndata: {"text":"tail"}\n\n'
        b"event:token\ndata: {bad-json}\n\n"
        b'event:sources\ndata: {"request_id":"r-source","sources":[]}\n\n'
        b'event:error\ndata: {"code":"BACKEND_FAILURE","request_id":"r-error"}\n\n'
        b"event:token\ndata: {also-bad}\n\n"
    )
    with _raw_client(lambda request: httpx.Response(200, content=raw)) as client:
        got = run_eval.run_chat_raw(client, {"question": "q"})

    # SSE 的 data-only frame 仍保留於 events，但沒有 token event，故不併入 answer。
    assert got["answer"] == "tail"
    assert got["events"] == [
        {"seq": 0, "event": "message", "data": {"text": "partial"}},
        {"seq": 1, "event": "token", "data": {"text": "tail"}},
        {"seq": 2, "event": "token", "data": None, "raw": "{bad-json}", "invalid_json": True},
        {"seq": 3, "event": "sources", "data": {"request_id": "r-source", "sources": []}},
        {
            "seq": 4,
            "event": "error",
            "data": {"code": "BACKEND_FAILURE", "request_id": "r-error"},
        },
        {"seq": 5, "event": "token", "data": None, "raw": "{also-bad}", "invalid_json": True},
    ]
    assert got["error"] == "BACKEND_FAILURE"
    assert got["error_event"] == {"code": "BACKEND_FAILURE", "request_id": "r-error"}
    assert got["request_id"] == "r-error"


def test_raw_sse_parse_error_records_first_bad_event_and_last_source_request_id():
    raw = (
        b'event:sources\ndata: {"request_id":"r-first","sources":[]}\n\n'
        b"event:token\ndata: {bad}\n\n"
        b'event:sources\ndata: {"request_id":"r-last","sources":[]}\n\n'
        b'event:done\ndata: {"model":"m"}\n\n'
    )
    with _raw_client(lambda request: httpx.Response(200, content=raw)) as client:
        got = run_eval.run_chat_raw(client, {"question": "q"})

    assert got["error"] == "STREAM_PARSE_ERROR"
    assert got["error_event"] == {"code": "STREAM_PARSE_ERROR", "seq": 1}
    assert got["request_id"] == "r-last"
    assert got["sources_state"] == "empty"


@pytest.mark.parametrize(
    ("status", "body", "expected_error", "expected_event"),
    [
        (
            403,
            {"error": {"code": "PERMISSION_DENIED"}},
            "PERMISSION_DENIED",
            {"http_status": 403, "error": {"code": "PERMISSION_DENIED"}},
        ),
        (502, b"upstream unavailable", "HTTP 502", {"http_status": 502, "error": None}),
        (502, {"error": ["malformed"]}, "HTTP 502", {"http_status": 502, "error": ["malformed"]}),
    ],
)
def test_raw_chat_http_errors_preserve_structured_status(
    status, body, expected_error, expected_event
):
    def respond(request):
        return httpx.Response(
            status,
            content=body if isinstance(body, bytes) else None,
            json=None if isinstance(body, bytes) else body,
        )

    with _raw_client(respond) as client:
        got = run_eval.run_chat_raw(client, {"question": "q"})

    assert got["error"] == expected_error
    assert got["error_event"] == expected_event
    assert got["events"] == [] and got["answer"] == ""


class _PartialThenReadError(httpx.SyncByteStream):
    def __iter__(self):
        yield b'event:token\ndata: {"text":"partial before disconnect"}\n\n'
        raise httpx.ReadError(
            "connection dropped", request=httpx.Request("POST", "http://eval.invalid/api/v1/chat")
        )

    def close(self):
        pass


def test_raw_chat_read_error_keeps_partial_answer_and_structured_transport_error():
    with _raw_client(lambda request: httpx.Response(200, stream=_PartialThenReadError())) as client:
        got = run_eval.run_chat_raw(client, {"question": "q"})

    assert got["answer"] == "partial before disconnect"
    assert got["events"] == [
        {"seq": 0, "event": "token", "data": {"text": "partial before disconnect"}}
    ]
    assert got["error"] == "TRANSPORT_ERROR"
    assert got["error_event"] == {
        "code": "TRANSPORT_ERROR",
        "type": "ReadError",
        "message": "connection dropped",
    }


def test_http_failure_stays_in_account_artifact_denominator(env):
    def responder(account, body):
        if body["question"] == "扭力是多少？":
            return 403
        return [("token", {"text": "ok"}), ("done", done_event("r-ok"))]

    assert env.run(FakeBackend(responder=responder), accounts="manager") == 0
    art = env.artifact()
    row = next(row for row in art["rows"] if row["question_id"] == "q-ok")
    group = art["groups"]["manager"]["hybrid"]["overall"]
    assert row["error"] == "E403"
    assert row["error_event"] == {"http_status": 403, "error": {"code": "E403"}}
    assert row["answer_ok"] is False and group["n"] == len(QUESTIONS)
    assert group["errors"] == 1 and group["answer_ok"]["den"] == len(QUESTIONS)


def test_last_sources_event_drives_citation_score_in_saved_row(env):
    wrong = {"ref": 1, "part_id": "p-other", "topic": "規格", "chunk_id": "wrong#1"}
    right = part_source(1, "p-ok", "規格")

    def responder(account, body):
        if body["question"] == "扭力是多少？":
            return [
                ("sources", {"request_id": "r-old", "sources": [wrong]}),
                ("sources", {"request_id": "r-final", "sources": [right]}),
                ("token", {"text": "扭力是 9 N·m [1]"}),
                ("done", done_event("r-final")),
            ]
        return [("token", {"text": "ok"}), ("done", done_event("r-other"))]

    assert env.run(FakeBackend(responder=responder), accounts="manager") == 0
    row = next(row for row in env.artifact()["rows"] if row["question_id"] == "q-ok")
    assert row["request_id"] == "r-final"
    assert row["events"][1]["data"]["sources"] == [right]
    assert row["citation_ok"] is True and row["answer_ok"] is True


@pytest.mark.parametrize(
    ("parts_body", "switch_kb_version"),
    [
        ({"items": "not-a-list", "hidden": 0}, "kb-fixed-17"),
        ({"items": [{"id": 3}], "hidden": 0}, "kb-fixed-17"),
        ({"items": [], "hidden": True}, "kb-fixed-17"),
        ({"items": [], "hidden": 0}, ""),
    ],
)
def test_account_snapshot_rejects_malformed_shape_instead_of_empty_visibility(
    parts_body, switch_kb_version
):
    class SnapshotBackend(FakeBackend):
        def handle(self, request):
            if request.url.path == "/api/v1/parts":
                return httpx.Response(200, json=parts_body)
            if request.url.path == "/api/v1/auth/switch" and switch_kb_version == "":
                return httpx.Response(200, json={"current": {"id": "manager"}, "kb_version": ""})
            return super().handle(request)

    backend = SnapshotBackend()
    with (
        httpx.Client(
            transport=httpx.MockTransport(backend.handle), base_url="http://eval.invalid"
        ) as client,
        pytest.raises(run_eval.AccountsRunError),
    ):
        run_eval._snapshot_account(client, "manager")


def test_status_controls_accepts_only_boolean_on_success():
    answers = [(200, True), (200, False), (200, "true"), (503, True)]
    for status, value in answers:
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda request, status=status, value=value: httpx.Response(
                    status, json={"eval_controls": value}
                )
            ),
            base_url="http://eval.invalid",
        ) as client:
            assert run_eval._status_controls(client) == (
                value if status == 200 and isinstance(value, bool) else None
            )


def test_accounts_artifact_exposes_question_snapshot_and_status_fallback(env):
    local = env

    class MissingDiagnosticsBackend(FakeBackend):
        def handle(self, request):
            if request.url.path == "/api/v1/admin/diagnostics":
                return httpx.Response(403, json={"error": {"code": "FORBIDDEN"}})
            if request.url.path == "/api/v1/status":
                return httpx.Response(200, json={"eval_controls": False})
            return super().handle(request)

    assert (
        local.run(MissingDiagnosticsBackend(), "--run-id", "shape-check", accounts="manager") == 0
    )
    art = local.artifact("shape-check")
    assert art["accounts"] == ["manager"] and art["strategies"] == ["hybrid"]
    assert art["summary"] == {
        "mode": "generation-accounts",
        "accounts": ["manager"],
        "strategies": ["hybrid"],
        "n_rows": len(QUESTIONS),
    }
    assert art["provenance"]["eval_controls"] is False
    assert art["provenance"]["qa_files"] == [str(local.qa)]
    assert art["provenance"]["created_at"] == art["created_at"]
    assert art["snapshots"]["manager"]["visible_part_ids"] == ["p-hid", "p-ok"]
    row = next(row for row in art["rows"] if row["question_id"] == "q-na")
    assert row["question"] == next(q["question"] for q in QUESTIONS if q["id"] == "q-na")
    assert row["topics"] == [] and row["visible"] is True
    assert row["request"]["question"] == row["question"]
    assert row["classification"] == "no_answer"


def test_accounts_artifact_preserves_snapshot_counts_and_stage1_applicability(env):
    backend = FakeBackend()
    assert env.run(backend) == 0
    art = env.artifact()

    assert set(art["snapshots"]) == {"manager", "sales"}
    for account, snapshot in art["snapshots"].items():
        assert snapshot["identity"] == {"id": account, "label": account}
        assert snapshot["visible_part_ids"] == sorted(backend.visible[account])
        assert snapshot["hidden"] == 1
        assert snapshot["kb_version"] == backend.kb_version
        assert datetime.fromisoformat(snapshot["fetched_at"]).utcoffset() is not None
        assert "" not in snapshot

    assert {row["classification"] for row in art["rows"]} == {
        "visible_answerable",
        "permission_degraded",
        "no_answer",
        "missing_part",
    }
    for row in art["rows"]:
        assert not row["error"] and row["done"] is not None
        if row["classification"] == "permission_degraded":
            assert row["stage1_only"] is True
        else:
            assert row["stage1_only"] is None
        assert row["stage1_failures"] == []
        assert "" not in row


@pytest.mark.parametrize("kb_version", ["", 7], ids=["empty", "non-string"])
def test_accounts_reject_invalid_snapshot_version_after_confirmed_identity(env, capsys, kb_version):
    class VersionBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.kb_version = kb_version
            self.identities = []

        def handle(self, request):
            response = super().handle(request)
            if request.url.path in ("/api/v1/auth/switch", "/api/v1/auth/accounts"):
                self.identities.append(response.json()["current"]["id"])
            return response

    backend = VersionBackend()
    assert env.run(backend, accounts="manager") == 2
    assert backend.current == "manager"
    assert backend.identities[-2:] == ["manager", "manager"]
    assert ("GET", "/api/v1/parts") not in backend.requests
    assert backend.chats == [] and env.no_artifacts()
    assert "kb_version" in capsys.readouterr().out


@pytest.mark.parametrize("invalid_member", ["", 7], ids=["empty", "non-string"])
def test_accounts_cli_rejects_invalid_gold_member_before_requests(env, capsys, invalid_member):
    env.gold.write_text(json.dumps({"accounts": ["manager", invalid_member]}), "utf-8")
    backend = FakeBackend()

    assert env.run(backend, accounts="manager") == 1
    assert backend.requests == [] and backend.chats == []
    assert env.no_artifacts()
    assert "帳號名單錯誤" in capsys.readouterr().out


@pytest.mark.parametrize("field", ["id", "question"])
def test_accounts_cli_validates_each_question_string_before_requests(env, capsys, field):
    question = {**QUESTIONS[0], field: 123}
    env.qa.write_text(json.dumps(question, ensure_ascii=False) + "\n", "utf-8")
    backend = FakeBackend()

    assert env.run(backend, accounts="manager") == 1
    assert backend.requests == [] and backend.chats == []
    assert env.no_artifacts()
    assert "題庫讀取失敗" in capsys.readouterr().out


@pytest.mark.parametrize("strategies", ["hybrid,hybrid", "unknown_strategy", "hybrid,"])
def test_accounts_cli_rejects_invalid_strategy_list_before_requests(env, capsys, strategies):
    backend = FakeBackend()

    assert env.run(backend, accounts="manager", strategies=strategies) == 1
    assert backend.requests == [] and backend.chats == []
    assert env.no_artifacts()
    assert "策略錯誤" in capsys.readouterr().out
