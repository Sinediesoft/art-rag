"""汙染測試找到的 4 個外洩（eval/run_impersonation_test.py）：安全與稽核紀錄只給主管；
角色不能做的修改，預覽不回傳任何從資料庫推出來的數量。"""

import pytest
from conftest import as_account

from app.services import change_service


@pytest.fixture(autouse=True)
def back_to_manager(client):
    yield
    as_account(client, "manager")


def test_preview_does_not_leak_stock_to_roles_that_cannot_change_it(client):
    """「改成 120」→ 摘要「+102 件」就能推回目前庫存；角色不能做時一個數量都不回。"""
    as_account(client, "guest")
    d = client.post(
        "/api/v1/changes/preview", json={"question": "把一廠成品倉法蘭庫存改成 120"}
    ).json()
    assert d["next"] == "rejected" and d["pending_id"] is None
    assert d["params"] == {} and d["param_labels"] == {} and "件" not in d["summary"]
    assert any(c["ok"] is False for c in d["checks"])


def test_approvals_only_show_own_requests(client, monkeypatch):
    rows = [
        {"ap_no": "AP-1", "requester_id": "planner", "status": "待核准"},
        {"ap_no": "AP-2", "requester_id": "wh1", "status": "已核准"},
    ]
    monkeypatch.setattr(
        change_service,
        "list_approvals",
        lambda account: {
            "can_approve": account.can("approve"),
            "pending": rows[:1],
            "mine": [r for r in rows if r["requester_id"] == account.id],
            "recent": rows,
        },
    )
    out = change_service.list_approvals_for(change_service_account("wh1"))
    assert [a["ap_no"] for a in out["recent"]] == ["AP-2"] and out["pending"] == []
    out = change_service.list_approvals_for(change_service_account("manager"))
    assert len(out["recent"]) == 2 and len(out["pending"]) == 1


def change_service_account(account_id: str):
    from app.services.identity import get_account

    return get_account(account_id)


def test_audit_is_manager_only(client):
    as_account(client, "wh2")  # 二廠倉管改一廠庫存 → 範圍外，記一筆「拒絕」
    client.post("/api/v1/changes/preview", json={"question": "把一廠成品倉法蘭庫存改成 120"})
    as_account(client, "guest")
    client.post("/api/v1/changes/preview", json={"question": "把一廠成品倉法蘭庫存改成 120"})
    denied = client.get("/api/v1/audit")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    as_account(client, "manager")
    actors = {i["actor_id"] for i in client.get("/api/v1/audit").json()["items"]}
    assert {"guest", "wh2"} <= actors


def test_security_logs_are_manager_only(client, monkeypatch):
    from types import SimpleNamespace

    from app.api import routes

    base = {"created_at": "2026-10-08T00:00:00+00:00", "request_id": "req_x", "stage": 2,
            "rule": "越權嘗試", "judge": "地端規則"}  # fmt: skip
    rows = [
        {**base, "no": "SEC-0002", "account_id": "planner", "account_label": "生管",
         "text": "question_sha256:9b2df38d"},
        {**base, "no": "SEC-0001", "account_id": "guest", "account_label": "訪客",
         "text": "question_sha256:e3b0c442"},
    ]  # fmt: skip
    fake = SimpleNamespace(recent_security=lambda limit: rows, count_security=lambda since: {})
    monkeypatch.setattr(routes, "get_logs_repo", lambda: fake)
    as_account(client, "guest")
    denied = client.get("/api/v1/security/logs")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
    as_account(client, "manager")
    assert len(client.get("/api/v1/security/logs").json()["items"]) == 2
