"""七段權限控管第 1 段（docs/adr/015）：API 閘道驗 JWT。沒帶、簽章不符、過期、alg 不對都是 401，
請求碰不到任何模型與資料；簽章不符（竄改、偽造）寫進拒絕並記錄。"""

import base64
import json
import time

from conftest import DEFAULT_ACCOUNT, as_account
from fastapi.testclient import TestClient

from app.core import jwt
from app.main import app
from app.repositories.logs_repo import get_logs_repo
from app.services import identity


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_jwt_roundtrip_and_rejections():
    secret = "s3cret"
    claims = {"iss": "art-rag", "sub": "planner", "exp": time.time() + 60}
    token = jwt.encode(claims, secret)
    assert jwt.decode(token, secret, "art-rag")["sub"] == "planner"
    for bad, code in [
        (token[:-2] + ("AA" if not token.endswith("AA") else "BB"), "TOKEN_INVALID"),
        (jwt.encode(claims, "other"), "TOKEN_STALE"),  # 換了金鑰（後端重啟）
        (jwt.encode({**claims, "exp": time.time() - 1}, secret), "TOKEN_EXPIRED"),
        (jwt.encode({**claims, "iss": "evil"}, secret), "TOKEN_INVALID"),
        (f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}.", "TOKEN_INVALID"),
        # kid 對、但用別的金鑰簽：竄改
        (
            f"{token.split('.')[0]}.{_b64({**claims, 'sub': 'manager'})}.{token.split('.')[2]}",
            "TOKEN_INVALID",
        ),
        ("not-a-jwt", "TOKEN_INVALID"),
    ]:
        try:
            jwt.decode(bad, secret, "art-rag")
        except jwt.TokenError as e:
            assert e.code == code, bad
        else:
            raise AssertionError(f"應該拒絕：{bad}")


def test_requests_without_a_token_are_refused_at_the_gateway(client):
    fresh = TestClient(app)  # 沒有 cookie
    r = fresh.get("/api/v1/artworks")
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    assert r.headers["www-authenticate"].startswith("Bearer")
    r = fresh.post("/api/v1/agent/route", json={"question": "法蘭還剩幾件？"})
    assert r.status_code == 401  # 智慧助理也一樣：沒有憑證，第 2 段以後都不會執行
    assert fresh.get("/api/v1/health").status_code == 200  # 健康檢查不用憑證


def test_accounts_endpoint_issues_a_guest_token_when_missing():
    fresh = TestClient(app)
    r = fresh.get("/api/v1/auth/accounts")
    assert r.status_code == 200 and r.json()["current"]["id"] == "guest"
    assert identity.COOKIE in fresh.cookies
    token = r.json()["token"]
    assert token["claims"]["clearance"] == 0 and token["claims"]["roles"] == ["訪客"]
    assert token["unsigned"].count(".") == 1  # 簽章不回傳到畫面
    assert fresh.get("/api/v1/artworks").status_code == 200


def test_tampered_token_is_refused_and_logged(client):
    me = as_account(client, "sales_a")
    assert me["clearance"] == 1
    unsigned = client.get("/api/v1/auth/accounts").json()["token"]["unsigned"]
    head, body = unsigned.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged = f"{head}.{_b64({**claims, 'clearance': 2, 'roles': ['主管']})}.fake-signature"
    r = client.post(
        "/api/v1/agent/route",
        json={"question": "連接法蘭有哪些公差要求？"},
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_INVALID"
    log = get_logs_repo().recent_security(1)[0]
    assert log["stage"] == 1 and log["rule"] == "憑證無效" and "clearance=2" in log["text"]
    as_account(client, DEFAULT_ACCOUNT)


def test_token_from_a_previous_key_is_stale_and_not_logged(client, monkeypatch):
    acc = identity.get_account("planner")
    monkeypatch.setattr(identity, "_secret", lambda: "old-key-before-restart")
    old = identity.issue(acc)
    monkeypatch.undo()
    before = len(get_logs_repo().recent_security(200))
    r = client.get("/api/v1/approvals", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_STALE"
    assert len(get_logs_repo().recent_security(200)) == before  # 後端重啟不是攻擊，不記錄


def test_expired_token_is_refused(client):
    acc = identity.get_account("planner")
    old = identity.issue(acc, now=time.time() - 10 * 3600)  # 預設效期 8 小時
    r = client.get("/api/v1/approvals", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_bearer_header_works_without_cookie():
    fresh = TestClient(app)
    token = identity.issue(identity.get_account("wh1"))
    r = fresh.get("/api/v1/parts", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["hidden"] == 0


def test_accounts_endpoint_replaces_a_stale_token_and_says_why(monkeypatch):
    fresh = TestClient(app)
    monkeypatch.setattr(identity, "_secret", lambda: "old-key-before-restart")
    old = identity.issue(identity.get_account("planner"))
    monkeypatch.undo()
    r = fresh.get("/api/v1/auth/accounts", headers={"Authorization": f"Bearer {old}"})
    body = r.json()
    assert r.status_code == 200 and body["current"]["id"] == "guest"
    issued = body["token"]["checks"][0]
    assert issued["key"] == "issued" and "舊的簽章金鑰" in issued["detail"]
