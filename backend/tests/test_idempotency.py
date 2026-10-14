"""寫入的 Idempotency-Key（TASK-20261013-001，AC-12、AC-17）：重送不重複開工單、異動、核准。

另外驗證沒帶 key 的同時送出也不會寫兩次（確認卡、待核准單拿到寫入鎖之後再確認還在）。
帳號依 docs/adr/036：開工單只有主管；主管超額直接生效，送核准改用倉管超額報廢。
合併 ADR 036 後（TASK-20261014-001）另驗：主管超額寫入中斷後重送，補寫的稽核帶同樣的
超額原因（D3-A）；核准重送收尾時申請人帳號已刪不會 500（B1）。
"""

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest
from conftest import DEFAULT_ACCOUNT, as_account

from app.api import schemas as S
from app.repositories.production_repo import IdempotencyLost, get_production_repo, idem_scope
from app.services import change_service, schedule_service

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
    as_account(client, "manager")
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
    as_account(client, "manager")
    client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    r = client.post("/api/v1/production/work-orders", json={**BODY, "qty": 21}, headers=KEY)
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(work_orders()) == 1
    bad = client.post("/api/v1/production/work-orders", json=BODY, headers={"Idempotency-Key": "x"})
    assert bad.status_code == 422


def test_key_is_scoped_to_the_account(client):
    as_account(client, "manager")
    client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    as_account(client, "guest")  # 另一個帳號用同一個 key：不會拿到別人的結果
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert r.status_code == 403
    assert len(work_orders()) == 1


def test_failed_request_releases_the_key_so_a_fixed_request_can_retry(client):
    as_account(client, "manager")
    bad = client.post(
        "/api/v1/production/work-orders", json={**BODY, "due_on": "2020-01-01"}, headers=KEY
    )
    assert bad.status_code == 422 and work_orders() == []
    ok = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert ok.status_code == 201 and len(work_orders()) == 1


def test_concurrent_identical_requests_create_one_work_order(client):
    as_account(client, "manager")
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
    as_account(client, "manager")
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


def scrap_over_quota(client) -> str:
    """倉管報廢 15 件（超過 10 件）：要送主管核准。回傳確認卡。"""
    p = client.post("/api/v1/changes/preview", json={"question": "一廠成品倉法蘭報廢 15 件"}).json()
    assert p["next"] == "approval", p
    return p["pending_id"]


def changes():
    return [c["change_no"] for c in get_production_repo().changes()]


def test_change_commit_replay_writes_once(client):
    as_account(client, "warehouse")
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
    as_account(client, "warehouse")
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
    as_account(client, "warehouse")
    pending = scrap_over_quota(client)
    k = {"Idempotency-Key": "idem-request-0001"}
    ap1 = client.post(f"/api/v1/changes/{pending}/request-approval", json={}, headers=k).json()
    ap2 = client.post(f"/api/v1/changes/{pending}/request-approval", json={}, headers=k).json()
    assert ap1["ap_no"] == ap2["ap_no"] and len(get_production_repo().approvals()) == 1
    as_account(client, "manager")
    ak = {"Idempotency-Key": "idem-approve-0001"}
    a = client.post(f"/api/v1/approvals/{ap1['ap_no']}/approve", json={}, headers=ak)
    b = client.post(f"/api/v1/approvals/{ap1['ap_no']}/approve", json={}, headers=ak)
    assert a.json() == b.json() and a.json()["status"] == "已核准"
    assert len(changes()) == 1
    # 沒有 key 的第二次核准：單據已不是待核准，不會再寫一次
    c = client.post(f"/api/v1/approvals/{ap1['ap_no']}/approve", json={})
    assert c.status_code == 409 and len(changes()) == 1


def test_two_managers_approving_at_the_same_time_write_once(client):
    as_account(client, "warehouse")
    pending = scrap_over_quota(client)
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
    assert statuses.count(200) == 1 and len(changes()) == 1


# ----------------------------------------------------------------------------- 行程中斷（審查 F1）
class Crash(Exception):
    """模擬行程在某一步當掉：之後的程式碼（包含放掉 key 的清理）都沒有機會執行。"""


def crashed(call):
    try:
        r = call()
    except Crash:
        return
    assert r.status_code == 500, r.text  # 例外被轉成 500 回應也算


@pytest.fixture
def hard_crash(monkeypatch):
    """真的當掉不會跑 except 區塊裡的 idem_abort：把它換成什麼都不做，記錄會停在當掉當下的狀態。"""
    monkeypatch.setattr(get_production_repo(), "idem_abort", lambda *a, **k: None)
    return monkeypatch


def boom(*a, **k):
    raise Crash("行程當掉")


def idem_rows():
    c = sqlite3.connect(get_production_repo().path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute("SELECT * FROM idempotency")]
    finally:
        c.close()


def age_keys(seconds=121):
    """把所有 key 的建立時間往前推：模擬「前一個執行者很久沒有回應」。"""
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    c = sqlite3.connect(get_production_repo().path)
    c.execute("UPDATE idempotency SET created_at = ?", (old,))
    c.commit()
    c.close()


def audits(op, action="寫入"):
    return [a for a in get_production_repo().audit(200) if a["op"] == op and a["action"] == action]


def test_work_order_crash_after_commit_before_idempotency_finish_returns_the_same_order(
    client, hard_crash
):
    """開單已提交、冪等回應還沒記錄就中斷；同 key 同參數過了 120 秒重送：還是同一張，不開第二張。"""
    as_account(client, "manager")
    repo = get_production_repo()
    hard_crash.setattr(repo, "idem_finish", boom)
    crashed(lambda: client.post("/api/v1/production/work-orders", json=BODY, headers=KEY))
    first = work_orders()
    assert len(first) == 1
    assert [r["state"] for r in idem_rows()] == ["applied"]  # 寫入與標記同一個交易提交
    assert json.loads(idem_rows()[0]["response"])["ref"] == first[0]
    hard_crash.undo()
    age_keys()
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert r.status_code == 201 and r.json()["wo_no"] == first[0]
    assert r.json()["part_name"] and r.json()["source"] == "系統開立"
    assert r.headers["idempotent-replayed"] == "true"
    assert work_orders() == first and len(audits("wo_create")) == 1  # 沒有第二張、稽核也只一筆
    again = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert again.json() == r.json() and [x["state"] for x in idem_rows()] == ["done"]


def test_work_order_crash_after_commit_before_audit_is_completed_on_retry(client, hard_crash):
    as_account(client, "manager")
    repo = get_production_repo()
    hard_crash.setattr(repo, "add_audit", boom)
    crashed(lambda: client.post("/api/v1/production/work-orders", json=BODY, headers=KEY))
    assert len(work_orders()) == 1 and audits("wo_create") == []
    hard_crash.undo()
    age_keys()
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert r.status_code == 201 and r.headers["idempotent-replayed"] == "true"
    assert work_orders() == [r.json()["wo_no"]] and len(audits("wo_create")) == 1


def test_work_order_crash_before_the_write_runs_again_exactly_once(client, hard_crash):
    """業務寫入還沒提交就中斷：記錄停在 running，逾時後重送當作新的執行，只會開一張。"""
    as_account(client, "manager")
    hard_crash.setattr(schedule_service, "create_work_order", boom)
    crashed(lambda: client.post("/api/v1/production/work-orders", json=BODY, headers=KEY))
    assert work_orders() == [] and [r["state"] for r in idem_rows()] == ["running"]
    hard_crash.undo()
    age_keys()
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert r.status_code == 201 and "idempotent-replayed" not in r.headers
    assert work_orders() == [r.json()["wo_no"]]


def test_error_after_the_write_in_the_same_process_lets_the_retry_finish_at_once(
    client, monkeypatch
):
    """業務寫入提交後收尾出錯（不是當掉）：key 不放掉，也不必等逾時，立刻重送就補做收尾。"""
    as_account(client, "manager")
    real = get_production_repo().add_audit
    calls = []

    def flaky(row, once=False):
        calls.append(1)
        if len(calls) == 1:
            raise Crash("稽核寫入失敗")
        return real(row, once=once)

    monkeypatch.setattr(get_production_repo(), "add_audit", flaky)
    crashed(lambda: client.post("/api/v1/production/work-orders", json=BODY, headers=KEY))
    assert [r["state"] for r in idem_rows()] == ["applied"] and len(work_orders()) == 1
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert r.status_code == 201 and r.headers["idempotent-replayed"] == "true"
    assert work_orders() == [r.json()["wo_no"]] and len(audits("wo_create")) == 1


def test_a_request_still_finishing_is_busy_not_run_again(client, hard_crash):
    as_account(client, "manager")
    hard_crash.setattr(get_production_repo(), "idem_finish", boom)
    crashed(lambda: client.post("/api/v1/production/work-orders", json=BODY, headers=KEY))
    hard_crash.undo()  # 還沒到逾時：可能是另一個行程正在收尾
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)
    assert r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert len(work_orders()) == 1


def test_cancel_work_order_crash_after_commit_replays_without_cancelling_twice(client, hard_crash):
    as_account(client, "manager")
    wo = client.post("/api/v1/production/work-orders", json=BODY).json()["wo_no"]
    k = {"Idempotency-Key": "idem-cancel-0002"}
    hard_crash.setattr(get_production_repo(), "idem_finish", boom)
    crashed(lambda: client.delete(f"/api/v1/production/work-orders/{wo}", headers=k))
    assert get_production_repo().get_work_order(wo)["status"] == "已取消"
    hard_crash.undo()
    age_keys()
    r = client.delete(f"/api/v1/production/work-orders/{wo}", headers=k)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert r.headers["idempotent-replayed"] == "true"
    assert len(audits("wo_cancel")) == 1


def test_change_commit_crash_after_write_replays_the_original_result(client, hard_crash):
    """寫入異動已提交、回應還沒記錄就當掉（確認卡也跟著行程消失）：重送拿到原本的結果，
    不會再寫一筆異動，庫存只動一次。"""
    as_account(client, "warehouse")
    pending = stock_change(client)
    k = {"Idempotency-Key": "idem-commit-0002"}
    hard_crash.setattr(change_service, "read_back", boom)
    crashed(lambda: client.post(f"/api/v1/changes/{pending}/commit", headers=k))
    changes = get_production_repo().changes()
    assert len(changes) == 1 and audits(changes[0]["op"]) == []
    hard_crash.undo()
    change_service._pending.clear()  # 行程重啟：記憶體裡的確認卡沒了
    age_keys()
    r = client.post(f"/api/v1/changes/{pending}/commit", headers=k)
    assert r.status_code == 200 and r.headers["idempotent-replayed"] == "true"
    body = r.json()
    assert body["change_no"] == changes[0]["change_no"] and body["rows"] and body["account"]["id"]
    assert len(get_production_repo().changes()) == 1
    assert len(audits(changes[0]["op"])) == 1
    again = client.post(f"/api/v1/changes/{pending}/commit", headers=k)
    assert again.json() == body


def test_request_approval_crash_after_write_returns_the_same_approval(client, hard_crash):
    as_account(client, "warehouse")
    pending = scrap_over_quota(client)
    k = {"Idempotency-Key": "idem-request-0002"}
    hard_crash.setattr(change_service, "_audit", boom)
    crashed(lambda: client.post(f"/api/v1/changes/{pending}/request-approval", json={}, headers=k))
    assert len(get_production_repo().approvals()) == 1
    hard_crash.undo()
    change_service._pending.clear()
    age_keys()
    r = client.post(f"/api/v1/changes/{pending}/request-approval", json={}, headers=k)
    assert r.status_code == 200 and r.headers["idempotent-replayed"] == "true"
    assert len(get_production_repo().approvals()) == 1
    assert r.json()["ap_no"] == get_production_repo().approvals()[0]["ap_no"]
    assert len(audits("stock_scrap", "送核准")) == 1


def test_approve_crash_after_write_does_not_write_a_second_change(client, hard_crash):
    """核准寫入的異動與「核准單已核准」同一個交易；中斷後重送只補收尾，不再寫一次。"""
    as_account(client, "warehouse")
    pending = scrap_over_quota(client)
    ap = client.post(f"/api/v1/changes/{pending}/request-approval", json={}).json()
    as_account(client, "manager")
    k = {"Idempotency-Key": "idem-approve-0002"}
    hard_crash.setattr(change_service, "read_back", boom)
    crashed(lambda: client.post(f"/api/v1/approvals/{ap['ap_no']}/approve", json={}, headers=k))
    done = changes()
    assert len(done) == 1  # 寫入當下核准單也已經是已核准（不會出現「寫了異動、核准單還待核准」）
    assert get_production_repo().get_approval(ap["ap_no"])["status"] == "已核准"
    assert get_production_repo().get_approval(ap["ap_no"])["change_no"] == done[0]
    hard_crash.undo()
    age_keys()
    r = client.post(f"/api/v1/approvals/{ap['ap_no']}/approve", json={}, headers=k)
    assert r.status_code == 200 and r.headers["idempotent-replayed"] == "true"
    assert r.json()["status"] == "已核准" and r.json()["change_no"] == done[0]
    assert changes() == done and len(audits("stock_scrap", "核准寫入")) == 1


def test_a_slow_first_executor_cannot_commit_after_the_key_was_taken_over(client):
    """第一個執行者太慢、key 被接手並完成：它遲到的業務寫入不會提交（標記不是 running 就回滾）。"""
    as_account(client, "manager")
    repo = get_production_repo()
    params = S.WorkOrderCreate(**BODY).model_dump()
    rhash = hashlib.sha256(
        json.dumps(params, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    assert repo.idem_begin("manager", "wo_create", KEY["Idempotency-Key"], rhash)[0] == "new"
    age_keys()  # A 卡住了
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=KEY)  # B 接手並完成
    assert r.status_code == 201 and len(work_orders()) == 1
    row = {
        "wo_no": "WO-2026-99",
        "part_id": "mfg-002",
        "qty": 1,
        "priority": "一般",
        "release_on": "2026-10-01",
        "due_on": "2026-10-30",
        "note": None,
        "created_by": "manager",
    }
    with idem_scope("manager", "wo_create", KEY["Idempotency-Key"]), pytest.raises(IdempotencyLost):
        repo.create_work_order(row)  # A 醒來才要寫
    assert len(work_orders()) == 1 and "WO-2026-99" not in work_orders()


# ------------------------------------- 併入 ADR 036（TASK-20261014-001）：主管超額 × 冪等重送
def test_manager_over_quota_change_crash_after_write_replays_with_the_same_over_quota(
    client, hard_crash
):
    """D3-A：主管超額直接生效的異動帶 Idempotency-Key，寫入已提交、回應與稽核都還沒記錄就當掉；
    重送補寫的稽核 detail.over_quota 是第一次試算的超額原因（和冪等標記一起存），和沒中斷時一致。"""
    as_account(client, "manager")
    ask = {"question": "把一廠成品倉的法蘭庫存改成 100"}
    p = client.post("/api/v1/changes/preview", json=ask).json()
    assert p["next"] == "confirm" and p["reasons"], p  # 主管超額：直接出確認卡
    k = {"Idempotency-Key": "idem-overquota-0001"}
    hard_crash.setattr(change_service, "read_back", boom)
    crashed(lambda: client.post(f"/api/v1/changes/{p['pending_id']}/commit", headers=k))
    done = get_production_repo().changes()
    assert len(done) == 1 and audits(done[0]["op"]) == []  # 已寫入、還沒有稽核
    marker = json.loads(idem_rows()[0]["response"])
    assert idem_rows()[0]["state"] == "applied" and marker["over_quota"] == p["reasons"]
    hard_crash.undo()
    change_service._pending.clear()  # 行程重啟：記憶體裡的確認卡沒了
    age_keys()
    r = client.post(f"/api/v1/changes/{p['pending_id']}/commit", headers=k)
    assert r.status_code == 200 and r.headers["idempotent-replayed"] == "true"
    assert r.json()["change_no"] == done[0]["change_no"]
    assert len(get_production_repo().changes()) == 1
    [row] = audits(done[0]["op"])
    assert row["detail"]["over_quota"] == p["reasons"]
    # 對照組：沒中斷的同一種寫入，稽核的 detail 結構相同
    p2 = client.post("/api/v1/changes/preview", json={"question": "把二廠成品倉的法蘭庫存改成 100"})
    p2 = p2.json()
    assert p2["next"] == "confirm" and p2["reasons"]
    k2 = {"Idempotency-Key": "idem-overquota-0002"}
    ok = client.post(f"/api/v1/changes/{p2['pending_id']}/commit", headers=k2)
    control = next(a for a in audits(done[0]["op"]) if a["ref_no"] == ok.json()["change_no"])
    assert set(control["detail"]) == set(row["detail"]) == {"params", "over_quota"}
    assert control["detail"]["over_quota"] == p2["reasons"]


def test_manager_rush_work_order_crash_after_commit_replays_with_the_same_over_quota(
    client, hard_crash
):
    """D3-A：圖紙頁主管開急件（超過額度直接開立）帶 Idempotency-Key，開單已提交、稽核還沒寫就當掉；
    重送補寫的稽核 detail.over_quota 和沒中斷時開的急件一樣，不重算額度。"""
    as_account(client, "manager")
    rush = {**BODY, "priority": "急件"}
    control = client.post("/api/v1/production/work-orders", json=rush)  # 對照組：沒中斷
    assert control.status_code == 201
    expected = next(a for a in audits("wo_create") if a["ref_no"] == control.json()["wo_no"])
    assert expected["detail"]["over_quota"] == ["急件工單（會插隊影響其他工單交期）"]
    hard_crash.setattr(get_production_repo(), "add_audit", boom)
    crashed(lambda: client.post("/api/v1/production/work-orders", json=rush, headers=KEY))
    marker = json.loads(idem_rows()[0]["response"])
    assert marker["over_quota"] == expected["detail"]["over_quota"]
    assert len(work_orders()) == 2 and len(audits("wo_create")) == 1
    hard_crash.undo()
    age_keys()
    r = client.post("/api/v1/production/work-orders", json=rush, headers=KEY)
    assert r.status_code == 201 and r.headers["idempotent-replayed"] == "true"
    assert len(work_orders()) == 2
    row = next(a for a in audits("wo_create") if a["ref_no"] == r.json()["wo_no"])
    assert row["detail"] == expected["detail"]
    # 一般工單沒有超額：重送補寫的稽核 detail 照樣是空的（標記存的是空清單）
    hard_crash.setattr(get_production_repo(), "add_audit", boom)
    k = {"Idempotency-Key": "idem-normal-wo-0001"}
    crashed(lambda: client.post("/api/v1/production/work-orders", json=BODY, headers=k))
    hard_crash.undo()
    age_keys()
    r = client.post("/api/v1/production/work-orders", json=BODY, headers=k)
    assert r.status_code == 201 and r.headers["idempotent-replayed"] == "true"
    row = next(a for a in audits("wo_create") if a["ref_no"] == r.json()["wo_no"])
    assert row["detail"] is None


def test_approve_replay_after_the_requester_account_was_removed(client, hard_crash, extra_accounts):
    """B1：核准寫入已提交、回應還沒記錄就當掉，之後申請人的帳號從 access.yaml 拿掉（docs/adr/036）；
    重送補收尾不會 500，稽核的申請人顯示代號。"""
    import yaml

    as_account(client, "warehouse_b")
    pending = scrap_over_quota(client)
    ap = client.post(f"/api/v1/changes/{pending}/request-approval", json={}).json()
    as_account(client, "manager")
    k = {"Idempotency-Key": "idem-approve-0003"}
    hard_crash.setattr(change_service, "read_back", boom)
    crashed(lambda: client.post(f"/api/v1/approvals/{ap['ap_no']}/approve", json={}, headers=k))
    assert len(changes()) == 1 and audits("stock_scrap", "核准寫入") == []
    hard_crash.undo()
    path = extra_accounts / "access.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    del cfg["accounts"]["warehouse_b"]
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    age_keys()
    r = client.post(f"/api/v1/approvals/{ap['ap_no']}/approve", json={}, headers=k)
    assert r.status_code == 200 and r.headers["idempotent-replayed"] == "true"
    [row] = audits("stock_scrap", "核准寫入")
    assert row["detail"]["requester"] == "warehouse_b"
