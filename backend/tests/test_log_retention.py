"""log 去敏感化與保存期限（內部人員演練）：問答紀錄是明文，能登入主機的人都讀得到。
用到機密圖紙的回答只存雜湊；問答、分流、SQL 紀錄超過保存期限就刪，安全紀錄與讀取稽核保留。"""

import json
from datetime import UTC, datetime, timedelta

from conftest import as_account

from app.repositories.logs_repo import get_logs_repo


def _last_answer(request_id: str) -> str:
    rows = get_logs_repo()._exec("SELECT answer FROM chat_logs WHERE request_id = ?", (request_id,))
    return rows[0]["answer"]


def _chat(client, part_id: str) -> str:
    r = client.post(
        "/api/v1/chat", json={"question": "材料是什麼？", "part_id": part_id, "strategy": "mock"}
    )
    done = [b for b in r.text.split("\n\n") if b.startswith("event: done")]
    return json.loads(done[0].split("data: ", 1)[1])["request_id"]


def test_answers_about_secret_parts_are_stored_as_fingerprints(client):
    as_account(client, "manager")
    secret = _last_answer(_chat(client, "mfg-001"))  # 機密
    internal = _last_answer(_chat(client, "mfg-004"))  # 內部
    assert secret.startswith("（機密圖紙，回答不存原文；雜湊 ") and "材" not in secret
    assert not internal.startswith("（機密圖紙")


def test_old_content_logs_are_purged_but_audit_logs_are_kept(client):
    repo = get_logs_repo()
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    repo._exec(
        "INSERT INTO route_logs (request_id, created_at, question) VALUES (?, ?, ?)",
        ("req_old_route", old, "舊的問句"),
    )
    repo.add_access_log(
        {
            "created_at": old,
            "request_id": "req_old_read",
            "account_id": "warehouse",
            "account_label": "倉管",
            "part_id": "mfg-001",
            "level": "機密",
            "what": "原尺寸圖紙",
        }
    )
    assert repo.purge_content_logs(30) >= 1
    assert not repo._exec("SELECT 1 FROM route_logs WHERE request_id = 'req_old_route'")
    assert repo._exec("SELECT 1 FROM access_logs WHERE request_id = 'req_old_read'")
