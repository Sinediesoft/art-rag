"""寫入的 Idempotency-Key（TASK-20261013-001，AC-12、AC-17）：重送不重複開工單、異動、核准。

另外驗證沒帶 key 的同時送出也不會寫兩次（確認卡、待核准單拿到寫入鎖之後再確認還在）。
"""

import threading

import pytest
from conftest import DEFAULT_ACCOUNT, as_account

from app.repositories.production_repo import get_production_repo
from app.services import schedule_service

KEY = {"Idempotency-Key": "idem-key-00000001"}
BODY = {"part_id": "mfg-002", "qty": 20, "due_on": "2026-10-30"}


@pytest.fixture(autouse=True)
def clean(mock_env, client):
    schedule_service.reset()  # 連工廠資料庫（Text-to-SQL 的衍生檢視）一起還原
    yield
    schedule_service.reset()
    as_account(client, DEFAULT_ACCOUNT)


def work_orders():
    return [w["wo_no"] for w in get_production_repo().work_orders()]


def test_work_order_replay_returns_the_same_order_once(client):
    as_account(client, "planner")
    a = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    b = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert a.status_code == b.status_code == 201
    assert a.json() == b.json() and "idempotent-replayed" not in a.headers
    assert b.headers["idempotent-replayed"] == "true"
    assert len(work_orders()) == 1
    # 沒帶 key：維持原本行為（每次都開一張）
    client.post("/api/v1/production/work-orders", json=BODY)
    assert len(work_orders()) == 2


def test_key_reused_with_other_parameters_conflicts(client):
    as_account(client, "planner")
    client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    r = client.post("/api/v1/production/work-orders", json={**BODY, "qty": 21}, headers=KEY)
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(work_orders()) == 1
    bad = client.post("/api/v1/production/work-orders", json=BODY, headers={"Idempotency-Key": "x"})
    assert bad.status_code == 422


def test_key_is_scoped_to_the_account(client):
    as_account(client, "planner")
    client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    as_account(client, "guest")  # 另一個帳號用同一個 key：不會拿到別人的結果
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert r.status_code == 403
    assert len(work_orders()) == 1


def test_failed_request_releases_the_key_so_a_fixed_request_can_retry(client):
    as_account(client, "planner")
    bad = client.post(
        "/api/v1/production/work-orders", json={**BODY, "due_on": "2020-01-01"}, headers=KEY
    )
    assert bad.status_code == 422 and work_orders() == []
    ok = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert ok.status_code == 201 and len(work_orders()) == 1


def test_concurrent_identical_requests_create_one_work_order(client):
    as_account(client, "planner")
    results = []

    def go():
        r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
        results.append((r.status_code, r.json().get("wo_no") or r.json()["error"]["code"]))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(work_orders()) == 1
    ok = {r for s, r in results if s == 201}
    assert ok == {work_orders()[0]}
    assert all(s in (201, 409) for s, _ in results)  # 其餘是「還在處理」，不是第二張


def test_cancel_work_order_replay(client):
    as_account(client, "planner")
    wo = client.post("/api/v1/production/work-orders", json=BODY).json()["wo_no"]
    k = {"Idempotency-Key": "idem-cancel-0001"}
    a = client.delete(f"/api/v1/production/work-orders/{wo}", headers=k)
    b = client.delete(
        f"/api/v1/production/work-orders/{wo}", headers=k
    )  # 沒有 key 的話第二次是 404
    assert a.status_code == b.status_code == 200 and b.headers["idempotent-replayed"] == "true"
    assert client.delete(f"/api/v1/production/work-orders/{wo}").status_code == 404


def stock_change(client):
    p = client.post(
        "/api/v1/changes/preview", json={"question": "一廠成品倉法蘭盤點少了 3 件"}
    ).json()
    assert p["next"] == "confirm", p
    return p["pending_id"]


def test_change_commit_replay_writes_once(client):
    as_account(client, "wh1")
    pending = stock_change(client)
    k = {"Idempotency-Key": "idem-commit-0001"}
    a = client.post(f"/api/v1/changes/{pending}/commit", headers=k)
    b = client.post(f"/api/v1/changes/{pending}/commit", headers=k)
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert b.headers["idempotent-replayed"] == "true"
    assert len(get_production_repo().changes()) == 1
    c = client.post(f"/api/v1/changes/{pending}/commit")  # 沒有 key：確認卡已使用
    assert c.status_code == 404 and c.json()["error"]["code"] == "CHANGE_NOT_FOUND"
    assert len(get_production_repo().changes()) == 1


def test_confirm_card_submitted_twice_at_once_writes_once(client):
    as_account(client, "wh1")
    pending = stock_change(client)
    codes = []

    def go():
        r = client.post(f"/api/v1/changes/{pending}/commit")
        codes.append(r.status_code)

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(200) == 1 and len(get_production_repo().changes()) == 1


def test_approve_replay_and_double_approve_write_once(client):
    as_account(client, "planner")
    body = {"part_id": "mfg-002", "qty": 80, "due_on": "2026-10-16", "priority": "急件"}
    pending = client.post(
        "/api/v1/changes/preview", json={"op": "wo_create", "params": body}
    ).json()["pending_id"]
    k = {"Idempotency-Key": "idem-request-0001"}
    ap1 = client.post(f"/api/v1/changes/{pending}/request-approval", json={}, headers=k).json()
    ap2 = client.post(f"/api/v1/changes/{pending}/request-approval", json={}, headers=k).json()
    assert ap1["ap_no"] == ap2["ap_no"] and len(get_production_repo().approvals()) == 1
    as_account(client, "manager")
    ak = {"Idempotency-Key": "idem-approve-0001"}
    a = client.post(f"/api/v1/approvals/{ap1['ap_no']}/approve", json={}, headers=ak)
    b = client.post(f"/api/v1/approvals/{ap1['ap_no']}/approve", json={}, headers=ak)
    assert a.json() == b.json() and a.json()["status"] == "已核准"
    assert len(work_orders()) == 1
    # 沒有 key 的第二次核准：單據已不是待核准，不會再開一張
    c = client.post(f"/api/v1/approvals/{ap1['ap_no']}/approve", json={})
    assert c.status_code == 409 and len(work_orders()) == 1


def test_two_managers_approving_at_the_same_time_write_once(client):
    as_account(client, "planner")
    body = {"part_id": "mfg-002", "qty": 80, "due_on": "2026-10-16", "priority": "急件"}
    pending = client.post(
        "/api/v1/changes/preview", json={"op": "wo_create", "params": body}
    ).json()["pending_id"]
    ap = client.post(f"/api/v1/changes/{pending}/request-approval", json={}).json()
    as_account(client, "manager")
    statuses = []

    def go():
        statuses.append(
            client.post(f"/api/v1/approvals/{ap['ap_no']}/approve", json={}).status_code
        )

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert statuses.count(200) == 1 and len(work_orders()) == 1
