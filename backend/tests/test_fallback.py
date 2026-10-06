"""備援與資料不出站（共用層 §四）：備援只在本地之間，雲端只當對照組。"""

import asyncio
import json

import pytest

from app.core.config import get_settings
from app.rag import providers
from app.services import chat_service


def collect(gen):
    async def run():
        return [x async for x in gen]

    return asyncio.run(run())


def event(events: list[str], name: str) -> dict | None:
    for e in events:
        if e.startswith(f"event: {name}\n"):
            return json.loads(e.split("data: ", 1)[1])
    return None


@pytest.fixture
def real_llm(monkeypatch):
    """LLM_MODE=real，但每個策略都換成 mock 模型，記錄被呼叫過哪些策略。"""
    monkeypatch.setattr(get_settings(), "llm_mode", "real")
    called: list[str] = []
    real_get = providers.get_provider

    def fake_get(strategy: str):
        called.append(strategy)
        real_get(strategy)  # 保留原本的檢查（ALLOW_CLOUD、內網位址…），通過後才換成 mock
        return providers.MockProvider(strategy=strategy, model=f"mock-{strategy}")

    monkeypatch.setattr(chat_service, "get_provider", fake_get)
    return called


def test_outage_falls_back_to_local_model_not_cloud(real_llm, monkeypatch):
    """主推論伺服器斷線 → 改走本地備援模型；即使雲端已開啟也不會被呼叫。"""
    monkeypatch.setattr(get_settings(), "allow_cloud", True)
    monkeypatch.setattr(get_settings(), "api_key", "test-key")

    def fake_get(strategy: str):
        real_llm.append(strategy)
        if strategy == "hybrid":
            raise providers.ProviderUnavailable("主推論伺服器無回應（模擬斷線）")
        return providers.MockProvider(strategy=strategy, model=f"mock-{strategy}")

    monkeypatch.setattr(chat_service, "get_provider", fake_get)
    events = collect(
        chat_service.chat_stream("作者是誰？", "req_t1", strategy="hybrid", artwork_id="npm-000001")
    )
    done = event(events, "done")
    assert done and done["fallback"] is True
    assert done["strategy_used"] == "hybrid_fallback"
    assert done["egress"] == {"images": 0, "chunks": 0, "bytes": 0, "jev_bytes": 0}
    assert not set(real_llm) & providers.CLOUD_STRATEGIES


def test_all_local_down_suspends_service(real_llm, monkeypatch):
    def down(strategy: str):
        real_llm.append(strategy)
        raise providers.ProviderUnavailable("連不上")

    monkeypatch.setattr(chat_service, "get_provider", down)
    events = collect(
        chat_service.chat_stream("作者是誰？", "req_t2", strategy="hybrid", artwork_id="npm-000001")
    )
    err = event(events, "error")
    assert err and err["code"] == "STRATEGY_UNAVAILABLE"
    assert "服務暫停" in err["message"]
    assert real_llm == ["hybrid", "hybrid_fallback"]
    assert not set(real_llm) & providers.CLOUD_STRATEGIES


def test_cloud_disabled_by_default(real_llm):
    assert get_settings().allow_cloud is False
    events = collect(
        chat_service.chat_stream("作者是誰？", "req_t3", strategy="api_kb", artwork_id="npm-000001")
    )
    err = event(events, "error")
    assert err and "ALLOW_CLOUD=false" in err["message"]


def test_cloud_rejects_user_photos(real_llm, monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_cloud", True)
    monkeypatch.setattr(get_settings(), "api_key", "test-key")
    events = collect(
        chat_service.chat_stream("這是什麼畫？", "req_t4", strategy="api_kb", image_id="img_x")
    )
    assert event(events, "error")["code"] == "CLOUD_UPLOAD_FORBIDDEN"
    assert real_llm == []


def test_cloud_egress_is_recorded(real_llm, monkeypatch, all_chunks):
    """A2（api_kb）送出圖片與段落；A1（api_nokb）不帶任何檢索段落。
    A1 是關檢索對照組：只有評估模式才生成，否則第 6 段直接降級（docs/adr/019）。"""
    monkeypatch.setattr(get_settings(), "allow_cloud", True)
    monkeypatch.setattr(get_settings(), "api_key", "test-key")
    kb = event(
        collect(
            chat_service.chat_stream("技法？", "req_t5", strategy="api_kb", artwork_id="npm-000001")
        ),
        "done",
    )
    assert kb["egress"]["images"] == 1 and kb["egress"]["chunks"] >= 1 and kb["egress"]["bytes"] > 0
    events = collect(
        chat_service.chat_stream(
            "技法？", "req_t6", strategy="api_nokb", artwork_id="npm-000001", eval_mode=True
        )
    )
    assert event(events, "sources")["sources"] == []
    nokb = event(events, "done")
    assert nokb["egress"]["chunks"] == 0 and nokb["use_retrieval"] is False
    assert nokb["degraded"] is False
    # 不是評估模式：A1 被生成閘門擋下，雲端一次都沒呼叫
    real_llm.clear()
    events = collect(
        chat_service.chat_stream("技法？", "req_t7", strategy="api_nokb", artwork_id="npm-000001")
    )
    done = event(events, "done")
    assert done["degraded"] is True and real_llm == []
    assert [s["stage"] for s in done["pipeline"]] == [1, 2, 3, 4, 5, 6]
    assert done["pipeline"][-1]["status"] == "block"


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:11434/v1", True),
        ("http://192.168.1.20:11434/v1", True),
        ("http://100.101.102.103:11434/v1", True),  # Tailscale
        ("http://gpu-box.tailnet-abc.ts.net:11434/v1", True),
        ("http://ollama:11434/v1", True),  # Docker 服務名稱
        ("https://api.openai.com/v1", False),
        ("http://8.8.8.8:11434/v1", False),
    ],
)
def test_local_url_guard(url, local):
    assert providers.is_local_url(url) is local


def test_local_strategy_refuses_public_address(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_mode", "real")
    monkeypatch.setattr(get_settings(), "hybrid_base_url", "https://api.openai.com/v1")
    with pytest.raises(providers.ProviderUnavailable, match="不是本機或內網位址"):
        providers.get_provider("hybrid")


def test_retrieval_stays_within_selected_artwork():
    sources = chat_service.retrieve("用了什麼技法？", "npm-000001")
    assert sources and {s["artwork_id"] for s in sources} == {"npm-000001"}
