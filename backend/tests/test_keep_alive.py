"""Ollama keep_alive（docs/adr/025）。

設了 MODEL_KEEP_ALIVE 才送，而且只送給 Ollama 提供的本地策略。"""

import pytest

from app.core.config import get_settings
from app.rag import providers


@pytest.fixture
def real_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_mode", "real")
    monkeypatch.setattr(get_settings(), "lora_enabled", True)


def body_of(strategy: str) -> dict:
    return providers.get_provider(strategy).body([{"role": "user", "content": "hi"}])


def test_default_sends_no_keep_alive(real_mode, monkeypatch):
    monkeypatch.setattr(get_settings(), "model_keep_alive", "")
    assert "keep_alive" not in body_of("hybrid")


@pytest.mark.parametrize("strategy", ["hybrid", "hybrid_fallback", "lora"])
def test_ollama_strategies_get_keep_alive(real_mode, monkeypatch, strategy):
    monkeypatch.setattr(get_settings(), "model_keep_alive", "60m")
    assert body_of(strategy)["keep_alive"] == "60m"


def test_llama_server_does_not_get_keep_alive(real_mode, monkeypatch):
    """Ortho2CAD 是 llama-server router 模式，自己管載入／卸載。"""
    monkeypatch.setattr(get_settings(), "model_keep_alive", "60m")
    assert "keep_alive" not in body_of("ortho2cad")


def test_cloud_does_not_get_keep_alive(real_mode, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "model_keep_alive", "60m")
    monkeypatch.setattr(s, "allow_cloud", True)
    monkeypatch.setattr(s, "api_key", "test-key")
    assert "keep_alive" not in body_of("api_kb")
