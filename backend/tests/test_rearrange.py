"""檢索段落篩選（MIRA 的 Rearrange）：輸出解析、保底、退回原段落、開關優先順序。

模型輸出用假的 provider 代替，不需要索引與模型，
所以 `pytest --noconftest tests/test_rearrange.py` 也能跑。
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import RearrangeSpec
from app.rag import rearrange
from app.rag.providers import ProviderUnavailable

SOURCES = [
    {"ref": i + 1, "chunk_id": f"c{i}", "title": "谿山行旅圖", "topic": t, "text": f"段落{i}"}
    for i, t in enumerate(["落款與發現", "技法", "基本資料", "地位", "構圖"])
]


def fake_provider(output: str = "", error: Exception | None = None, delay: float = 0):
    class Fake:
        max_tokens = temperature = None

        async def stream(self, messages):
            await asyncio.sleep(delay)
            if error:
                raise error
            yield output

    return lambda strategy: Fake()


def run(question: str = "簽名在哪？"):
    return asyncio.run(rearrange.rearrange(question, SOURCES))


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1,3", [0, 2]), ("「2」", [1]), ("3、1", [0, 2]), ("無", []), ("1，4。", [0, 3])],
)
def test_parse_accepts_fixed_format(text, expected):
    assert rearrange.parse_choice(text, 5) == expected


@pytest.mark.parametrize("text", ["第 1 段有幫助", "7", "1,9", "", "1-3"])
def test_parse_rejects_anything_else(text):
    assert rearrange.parse_choice(text, 5) is None


def test_keeps_picked_and_renumbers_refs(monkeypatch):
    monkeypatch.setattr(rearrange, "get_provider", fake_provider("1,3"))
    kept, info = run()
    assert [s["chunk_id"] for s in kept] == ["c0", "c2"]
    assert [s["ref"] for s in kept] == [1, 2]  # 回答裡的 [n] 要對得上
    assert info["candidates"] == 5 and info["kept"] == 2 and info["fallback"] is None


def test_none_helpful_still_keeps_one(monkeypatch):
    monkeypatch.setattr(rearrange, "get_provider", fake_provider("無"))
    kept, info = run()
    assert [s["chunk_id"] for s in kept] == ["c0"] and info["kept"] == 1


@pytest.mark.parametrize(
    "provider",
    [
        fake_provider("這幾段都跟簽名有關，第一段最相關。"),
        fake_provider(error=ProviderUnavailable("主推論伺服器無回應（模擬斷線）")),
    ],
)
def test_falls_back_to_all_sources(monkeypatch, provider):
    """看不懂或連不上：原封不動用原本的段落，不擋回答。"""
    monkeypatch.setattr(rearrange, "get_provider", provider)
    kept, info = run()
    assert kept == SOURCES and info["fallback"] and info["kept"] == 5


def test_timeout_falls_back(monkeypatch):
    cfg = SimpleNamespace(rearrange=RearrangeSpec(timeout_s=0.05))
    monkeypatch.setattr(rearrange, "get_models_config", lambda: cfg)
    monkeypatch.setattr(rearrange, "get_provider", fake_provider("1", delay=1))
    kept, info = run()
    assert kept == SOURCES and "逾時" in info["fallback"]


def test_switch_priority(monkeypatch):
    """請求 ＞ .env 的 REARRANGE ＞ models.yaml（預設關）。"""
    env = SimpleNamespace(rearrange="")
    monkeypatch.setattr(rearrange, "get_settings", lambda: env)
    assert rearrange.enabled() is False  # models.yaml 預設關
    env.rearrange = "true"
    assert rearrange.enabled() is True
    assert rearrange.enabled(requested=False) is False
