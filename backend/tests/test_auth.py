"""七段權限控管第 1 段（docs/adr/015）：API 閘道驗 JWT。沒帶、簽章不符、過期、alg 不對都是 401，
請求碰不到任何模型與資料；簽章不符（竄改、偽造）寫進拒絕並記錄。"""

import base64
import json
import time

import pytest
from conftest import DEFAULT_ACCOUNT, as_account
from fastapi.testclient import TestClient

from app.core import jwt, passwords
from app.core.config import get_settings
from app.main import app
from app.repositories.logs_repo import get_logs_repo
from app.services import identity


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_jwt_roundtrip_and_rejections():
    secret = "s3cret"
    claims = {"iss": "art-rag", "sub": "manager", "exp": time.time() + 60}
    token = jwt.encode(claims, secret)
    assert jwt.decode(token, secret, "art-rag")["sub"] == "manager"
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
    assert token["claims"]["clearance"] == 0 and token["claims"]["roles"] == ["一般員工"]
    assert token["unsigned"].count(".") == 1  # 簽章不回傳到畫面
    assert fresh.get("/api/v1/artworks").status_code == 200


def test_tampered_token_is_refused_and_logged(client):
    me = as_account(client, "sales")
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
    acc = identity.get_account("manager")
    monkeypatch.setattr(identity, "_secret", lambda: "old-key-before-restart")
    old = identity.issue(acc)
    monkeypatch.undo()
    before = len(get_logs_repo().recent_security(200))
    r = client.get("/api/v1/approvals", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_STALE"
    assert len(get_logs_repo().recent_security(200)) == before  # 後端重啟不是攻擊，不記錄


def test_token_for_a_removed_account_is_stale_and_not_logged(client):
    """簽章相符、但帳號已經從 access.yaml 拿掉（docs/adr/036 刪掉的生管）：
    不是竄改，回 TOKEN_STALE 讓前端重新取得，不寫進拒絕並記錄。"""
    now = int(time.time())
    claims = {"iss": identity.ISSUER, "sub": "planner", "iat": now, "exp": now + 600}
    token = jwt.encode(claims, identity._secret())
    before = len(get_logs_repo().recent_security(200))
    r = client.get("/api/v1/approvals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_STALE"
    assert len(get_logs_repo().recent_security(200)) == before


def test_expired_token_is_refused(client):
    acc = identity.get_account("manager")
    old = identity.issue(acc, now=time.time() - 10 * 3600)  # 預設效期 8 小時
    r = client.get("/api/v1/approvals", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"


def test_bearer_header_works_without_cookie():
    fresh = TestClient(app)
    token = identity.issue(identity.get_account("warehouse"))
    r = fresh.get("/api/v1/parts", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["hidden"] == 0


def test_accounts_endpoint_replaces_a_stale_token_and_says_why(monkeypatch):
    fresh = TestClient(app)
    monkeypatch.setattr(identity, "_secret", lambda: "old-key-before-restart")
    old = identity.issue(identity.get_account("manager"))
    monkeypatch.undo()
    r = fresh.get("/api/v1/auth/accounts", headers={"Authorization": f"Bearer {old}"})
    body = r.json()
    assert r.status_code == 200 and body["current"]["id"] == "guest"
    issued = body["token"]["checks"][0]
    assert issued["key"] == "issued" and "舊的簽章金鑰" in issued["detail"]


# ---- 帳號密碼登入（docs/adr/045）----

PASSWORD = "correct-horse-42"


@pytest.fixture
def passwords_set(monkeypatch):
    """主管、倉管設了密碼；業務、藝術家沒設。登入失敗次數每個測試從頭算。"""
    hashed = passwords.hash_password(PASSWORD)
    monkeypatch.setattr(
        get_settings(), "account_password_hashes", {"manager": hashed, "warehouse": hashed}
    )
    identity._failures.clear()
    yield
    identity._failures.clear()


def _login(c: TestClient, account_id: str, password: str = PASSWORD):
    return c.post("/api/v1/auth/login", json={"account_id": account_id, "password": password})


def test_password_hash_roundtrip_and_malformed_hashes_never_match():
    h = passwords.hash_password("s3cret-pass")
    assert h.startswith("scrypt$") and passwords.verify_password("s3cret-pass", h)
    assert not passwords.verify_password("s3cret-pasS", h)
    assert passwords.hash_password("s3cret-pass") != h  # 每次的鹽不一樣
    for bad in ["", "plain-text", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$AA$AA"]:
        assert not passwords.verify_password("s3cret-pass", bad)


def test_login_issues_a_token_for_that_account_without_a_previous_token(passwords_set):
    fresh = TestClient(app)  # 沒有 cookie：登入不用先有憑證
    r = _login(fresh, "manager")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current"]["id"] == "manager" and body["token"]["claims"]["amr"] == ["pwd"]
    assert sorted(body["login_accounts"]) == ["manager", "warehouse"]
    assert identity.COOKIE in fresh.cookies
    # 之後的請求照主管的權限（access.yaml 重查），不是照 JWT 裡寫的
    assert fresh.get("/api/v1/auth/accounts").json()["current"]["id"] == "manager"
    assert fresh.get("/api/v1/approvals").status_code == 200


def test_login_works_with_an_expired_token(passwords_set):
    fresh = TestClient(app)
    old = identity.issue(identity.get_account("guest"), now=time.time() - 10 * 3600)
    r = fresh.post(
        "/api/v1/auth/login",
        json={"account_id": "warehouse", "password": PASSWORD},
        headers={"Authorization": f"Bearer {old}"},
    )
    assert r.status_code == 200 and r.json()["current"]["id"] == "warehouse"


@pytest.mark.parametrize(
    ("account_id", "password"),
    [("manager", "wrong-password"), ("nobody", PASSWORD), ("sales", PASSWORD)],
    ids=["wrong-password", "unknown-account", "no-password-set"],
)
def test_failed_logins_look_the_same_and_are_logged_without_the_password(
    passwords_set, account_id, password
):
    fresh = TestClient(app)
    r = _login(fresh, account_id, password)
    err = r.json()["error"]
    assert r.status_code == 401 and err["code"] == "LOGIN_FAILED"
    assert err["message"] == identity.LOGIN_FAILED_MESSAGE  # 三種情況同一句話
    assert identity.COOKIE not in fresh.cookies  # 沒有簽發任何憑證
    log = get_logs_repo().recent_security(1)[0]
    assert log["stage"] == 1 and log["rule"] == "登入失敗" and account_id in log["text"]
    assert password not in log["text"]


def test_repeated_failures_lock_the_account_for_that_source(passwords_set, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_failures", 3)
    fresh = TestClient(app)
    for _ in range(3):
        assert _login(fresh, "manager", "wrong-password").status_code == 401
    r = _login(fresh, "manager")  # 鎖住時連正確的密碼也不收
    assert r.status_code == 429 and r.json()["error"]["code"] == "LOGIN_LOCKED"
    assert _login(fresh, "warehouse").status_code == 200  # 只鎖那一個帳號
    # 暫停時間過了重新計數（TestClient 的來源主機是 testclient）
    key = ("manager", "testclient")
    count, _ = identity._failures[key]
    identity._failures[key] = (count, time.monotonic() - 1)
    assert _login(fresh, "manager").status_code == 200
    assert key not in identity._failures  # 成功就清掉失敗紀錄


def test_logout_goes_back_to_the_default_account(passwords_set):
    fresh = TestClient(app)
    assert _login(fresh, "manager").status_code == 200
    r = fresh.post("/api/v1/auth/logout")
    assert r.status_code == 200
    body = r.json()
    # access.yaml 的 default_account（一般員工），不是 conftest 讓測試預設用的帳號
    assert body["current"]["id"] == "guest" and body["token"]["claims"]["amr"] == []
    assert fresh.get("/api/v1/auth/accounts").json()["current"]["id"] == "guest"


def test_demo_switch_is_marked_as_demo_in_the_token(client):
    me = client.post("/api/v1/auth/switch", json={"account_id": "sales"}).json()
    assert me["token"]["claims"]["amr"] == ["demo"]
    as_account(client, DEFAULT_ACCOUNT)
