"""生成端 provider：全部走 OpenAI 相容介面（Ollama、vLLM、Gemini/GPT/Claude 皆可）。

各策略只差在 base_url、model、api_key；另有 mock 回固定格式答案。
- 本地策略（hybrid、hybrid_fallback、lora、ortho2cad）只准連內網位址，確保資料不出站
- ortho2cad：工廠圖紙 → CadQuery 程式碼（llama-server），生成參數用 models.yaml 的 cad 區塊
- 雲端策略（api_nokb、api_kb）只當對照組：ALLOW_CLOUD=false 時一律停用
"""

import asyncio
import ipaddress
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app.core.config import get_models_config, get_settings

LOCAL_STRATEGIES = {"hybrid", "hybrid_fallback", "lora", "ortho2cad"}
CLOUD_STRATEGIES = {"api_nokb", "api_kb"}


class ProviderUnavailable(Exception):
    """設定缺漏或連不上（可改走本地備援）。retryable=True 代表值得重試一次（逾時、5xx）。"""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Provider:
    strategy: str
    model: str
    base_url: str = ""
    api_key: str = ""
    usage: Usage = field(default_factory=Usage)
    # 不填就用 models.yaml 的 generation；3D 重建要長輸出（cad.max_tokens）
    max_tokens: int | None = None
    temperature: float | None = None
    # OpenAI 的 response_format（例如 {"type": "json_schema", ...}）：照片建檔用來限制輸出格式
    # （docs/adr/013）。伺服器不支援時會被忽略，呼叫端仍要自己解析、驗證
    response_format: dict | None = None
    # Ollama 的 keep_alive（例如 "60m"）：模型閒置多久才卸載（docs/adr/025）。
    # None＝不送，用 Ollama 預設 5 分鐘
    keep_alive: str | None = None

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        raise NotImplementedError
        yield ""


# 展示用：/admin 可模擬主推論伺服器斷線（只影響 hybrid、lora；本地備援模型不受影響）
OUTAGE = {"enabled": False}

_LOCAL_SUFFIXES = (".local", ".internal", ".lan", ".ts.net")


def is_local_url(url: str) -> bool:
    """本機、私有網段、Tailscale（100.64.0.0/10、*.ts.net）或內網主機名稱才算本地。"""
    host = httpx.URL(url).host
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith(_LOCAL_SUFFIXES)
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network("100.64.0.0/10")


class OpenAICompatProvider(Provider):
    def body(self, messages: list[dict]) -> dict:
        gen = get_models_config().generation
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": gen["temperature"] if self.temperature is None else self.temperature,
            "max_tokens": int(self.max_tokens or gen["max_tokens"]),
            "stream_options": {"include_usage": True},
        }
        if self.response_format:
            body["response_format"] = self.response_format
        if self.keep_alive:
            body["keep_alive"] = self.keep_alive
        return body

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        s = get_settings()
        if self.strategy in ("hybrid", "lora") and OUTAGE["enabled"]:
            raise ProviderUnavailable("主推論伺服器無回應（模擬斷線）")
        body = self.body(messages)
        timeout = httpx.Timeout(s.generate_timeout_s, connect=s.connect_timeout_s)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = self.base_url.rstrip("/") + "/chat/completions"
        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream("POST", url, json=body, headers=headers) as resp,
            ):
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise ProviderUnavailable(
                        f"HTTP {resp.status_code}: {detail}", retryable=resp.status_code >= 500
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if chunk.get("usage"):
                        self.usage.input_tokens = chunk["usage"].get("prompt_tokens", 0)
                        self.usage.output_tokens = chunk["usage"].get("completion_tokens", 0)
                    for choice in chunk.get("choices", []):
                        text = (choice.get("delta") or {}).get("content")
                        if text:
                            yield text
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderUnavailable(f"連不上 {self.base_url}：{type(e).__name__}") from e
        except httpx.TimeoutException as e:
            raise ProviderUnavailable(
                f"生成逾時（>{s.generate_timeout_s:.0f} 秒）", retryable=True
            ) from e


class MockProvider(Provider):
    """不呼叫任何模型：從檢索結果抽句子組成答案，格式與真模型相同（每句附 [編號]）。"""

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        user = next(p["text"] for p in messages[-1]["content"] if p["type"] == "text")
        refs = re.findall(r"^\[(\d+)\]（[^）]*）(.+)$", user, flags=re.M)
        if not refs:
            answer = "知識庫中沒有這方面的資料。"
        else:
            sentences = []
            for ref, text in refs[:3]:
                first = re.split(r"(?<=[。！？])", text.strip())[0]
                sentences.append(f"{first.rstrip('。')}。[{ref}]")
            answer = "（mock 模式）" + "".join(sentences)
        self.usage.input_tokens = len(user)
        for ch in answer:
            self.usage.output_tokens += 1
            await asyncio.sleep(0.012)
            yield ch


def cloud_status() -> tuple[bool, str]:
    s = get_settings()
    if not s.allow_cloud:
        return False, "雲端對照組已停用（ALLOW_CLOUD=false）"
    if not s.api_key:
        return False, "未設定雲端 API 金鑰（.env 的 API_KEY）"
    return True, "對照組已開啟，只供評估腳本與策略比較頁使用"


# Ollama 提供的本地策略：送 keep_alive（docs/adr/025）。
# ortho2cad 是 llama-server（router 模式自己管載入）
OLLAMA_STRATEGIES = {"hybrid", "hybrid_fallback", "lora"}


def _local(strategy: str, base_url: str, model: str, api_key: str) -> Provider:
    if not is_local_url(base_url):
        raise ProviderUnavailable(f"{base_url} 不是本機或內網位址，拒絕連線（本地策略不外送資料）")
    keep = get_settings().model_keep_alive.strip()
    return OpenAICompatProvider(
        strategy=strategy,
        model=model,
        base_url=base_url,
        api_key=api_key,
        keep_alive=keep if keep and strategy in OLLAMA_STRATEGIES else None,
    )


def get_provider(strategy: str) -> Provider:
    s = get_settings()
    cfg = get_models_config().strategies
    if s.llm_mode == "mock" or strategy == "mock":
        return MockProvider(strategy=strategy, model="mock-extractive")
    if strategy == "hybrid":
        model = s.hybrid_model or cfg["hybrid"].default_model
        return _local("hybrid", s.hybrid_base_url, model, s.hybrid_api_key)
    if strategy == "hybrid_fallback":
        model = s.hybrid_fallback_model or cfg["hybrid_fallback"].default_model
        base_url = s.hybrid_fallback_base_url or s.hybrid_base_url
        return _local("hybrid_fallback", base_url, model, s.hybrid_fallback_api_key)
    if strategy in CLOUD_STRATEGIES:
        ok, detail = cloud_status()
        if not ok:
            raise ProviderUnavailable(detail)
        return OpenAICompatProvider(
            strategy=strategy,
            model=s.api_model or cfg[strategy].default_model,
            base_url=s.api_base_url,
            api_key=s.api_key,
        )
    if strategy == "ortho2cad":
        model = s.ortho2cad_model or cfg["ortho2cad"].default_model
        return _local("ortho2cad", s.ortho2cad_base_url, model, s.ortho2cad_api_key)
    if strategy == "lora":
        if not s.lora_enabled:
            raise ProviderUnavailable("LoRA 生成端尚未啟用（選做，插槽已保留）")
        model = s.lora_model or cfg["lora"].default_model
        return _local("lora", s.lora_base_url, model, s.lora_api_key)
    raise ProviderUnavailable(f"未知的策略：{strategy}")


def estimate_cost_twd(strategy: str, usage: Usage) -> float:
    spec = get_models_config().strategies.get(strategy)
    if not spec:
        return 0.0
    return round(
        usage.input_tokens / 1000 * spec.cost_per_1k_input_twd
        + usage.output_tokens / 1000 * spec.cost_per_1k_output_twd,
        4,
    )


async def ping_ollama(base_url: str, simulate_outage: bool = True) -> tuple[bool, str]:
    """健康檢查：打 OpenAI 相容的 /models。"""
    if simulate_outage and OUTAGE["enabled"]:
        return False, "模擬斷線中"
    if not is_local_url(base_url):
        return False, "不是本機或內網位址"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(base_url.rstrip("/") + "/models")
            return r.status_code == 200, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, type(e).__name__
