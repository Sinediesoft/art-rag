"""錯誤訊息不洩漏內部資訊（2026-10-12 錯誤訊息測試找到的）：例外內容、內網位址、上游回應只寫 log，
回給使用者的只有錯誤碼、固定訊息與 request_id。"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rag import providers
from app.scheduling import timefold_client


@pytest.mark.parametrize("token", ["W10.W10.x", "W10.e30.x", "e30.W10.x", "InN0ciI.e30.x"])
def test_malformed_token_is_401_not_500(client, token):
    """合法 base64 的 JSON 陣列或字串當 header／payload：之前 .get 丟 AttributeError 變成 500。"""
    fresh = TestClient(app)
    for r in (
        fresh.get("/api/v1/artworks", headers={"Authorization": f"Bearer {token}"}),
        TestClient(app, cookies={"artrag_token": token}).get("/api/v1/artworks"),
    ):
        assert r.status_code == 401, r.text
        assert r.json()["error"]["code"] == "TOKEN_INVALID"
        assert "Error" not in r.json()["error"]["message"]


def test_unreadable_upload_is_415_without_exception_text(client):
    for raw in (b"not an image", b""):
        r = client.post("/api/v1/images", files={"file": ("a.jpg", raw, "image/jpeg")})
        assert r.status_code == 415 and r.json()["error"]["code"] == "IMAGE_TYPE_NOT_ALLOWED"
        assert "BytesIO" not in r.text and "Unidentified" not in r.text


def test_unhandled_error_hides_exception_details(client, monkeypatch):
    from app.api import routes

    def boom():
        raise RuntimeError("postgresql://artrag:secret@10.0.0.5/artrag 連線失敗")

    monkeypatch.setattr(routes.memory_guard.guard, "status", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        c.cookies = client.cookies
        r = c.get("/api/v1/memory")
    body = r.json()["error"]
    assert r.status_code == 500 and body["code"] == "INTERNAL_ERROR"
    assert "postgresql" not in r.text and "RuntimeError" not in r.text and body["request_id"]


def test_unknown_api_and_wrong_method_use_the_error_format(client):
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    r = client.delete("/api/v1/artworks")
    assert r.status_code == 405 and r.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_provider_errors_do_not_carry_internal_urls():
    """這些訊息會變成 done.fallback_reason、篩選與矛盾檢查的 fallback 送到前端。"""
    p = providers.OpenAICompatProvider(
        strategy="hybrid", model="m", base_url="http://127.0.0.1:9/v1", api_key="x"
    )

    async def run():
        async for _ in p.stream([{"role": "user", "content": "hi"}]):
            pass

    with pytest.raises(providers.ProviderUnavailable) as e:
        asyncio.run(run())
    assert "127.0.0.1" not in str(e.value) and "連不上推論伺服器" in str(e.value)
    with pytest.raises(providers.ProviderUnavailable) as e:
        providers._local("hybrid", "http://8.8.8.8/v1", "m", "x")
    assert "8.8.8.8" not in str(e.value)


def test_scheduler_errors_do_not_carry_internal_urls(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "scheduler_base_url", "http://127.0.0.1:9")
    with pytest.raises(timefold_client.SchedulerUnavailable) as e:
        asyncio.run(timefold_client._call("GET", "/health"))
    assert "127.0.0.1" not in str(e.value)
