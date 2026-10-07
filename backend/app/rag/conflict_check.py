"""參考資料矛盾檢查（docs/adr/028）。

回答 prompt 的第 5 條要模型「遇到說法不一致時明白指出」，但模型邊寫回答邊判斷時，
常把只會發生一次的事寫成兩件都發生過（「1958 年與 1962 年分別由李霖燦與張大千發現」）。
這裡在回答之前，另外只問本地模型一件事：這幾段對這個問題有沒有互相矛盾。
有的話在參考資料後面加一句提醒（NOTE），回答時就會明白寫出說法不一致。

- 段落篩選之後還有 2 段以上才檢查；只看文字、不送照片；一題只呼叫一次
- 只呼叫本地主推論伺服器（hybrid），不論問答用哪個策略都不送雲端；問答選 mock 時不檢查
- 逾時、連不上、輸出不是 {"conflict": …, "refs": […]} 時一律當成沒檢查（fallback），不擋回答
- 開關優先順序：請求的 conflict_check ＞ .env 的 CONFLICT_CHECK ＞
  shared/models.yaml 的 conflict_check.enabled
"""

import asyncio
import json
import re
import time

import httpx

from app.core.config import get_models_config, get_settings
from app.rag.prompt import format_context, load_template
from app.rag.providers import ProviderUnavailable, get_provider

# 加在參考資料後面；措辭和回答 prompt 第 5 條一致
NOTE = (
    "（系統比對：參考資料 {refs} 對這個問題的說法不一致。回答時請明白寫出「參考資料的說法不一致」，"
    "分別列出各段的說法並標註來源編號，不要只採信其中一段，也不要寫成兩件事都發生過。）"
)


def enabled(requested: bool | None = None) -> bool:
    if requested is not None:
        return requested
    env = get_settings().conflict_check.strip().lower()
    if env in ("true", "1", "on"):
        return True
    if env in ("false", "0", "off"):
        return False
    return get_models_config().conflict_check.enabled


def parse_verdict(text: str, valid: set[int]) -> list[int] | None:
    """模型輸出 → 互相矛盾的段落編號（sources 的 ref，排序）；沒有矛盾回傳 []。

    格式不對、說有矛盾卻沒列出 2 段以上 valid 裡的編號時回傳 None（不採信）。"""
    m = re.search(r"\{.*\}", text, flags=re.S)
    try:
        verdict = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        verdict = None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("conflict"), bool):
        return None
    if not verdict["conflict"]:
        return []
    refs = verdict.get("refs")
    if not isinstance(refs, list) or not all(isinstance(r, int) for r in refs):
        return None
    picked = sorted(set(refs))
    return picked if len(picked) >= 2 and set(picked) <= valid else None


def note(refs: list[int]) -> str:
    return NOTE.format(refs="、".join(f"[{r}]" for r in refs))


def build_messages(question: str, sources: list[dict]) -> list[dict]:
    tpl = load_template(get_models_config().conflict_check.prompt_version)
    user = (
        tpl["user"]
        .replace("{{question}}", question)
        .replace("{{context}}", format_context(sources))
    )
    messages = [{"role": "system", "content": tpl["system"]}] if tpl["system"] else []
    return [*messages, {"role": "user", "content": [{"type": "text", "text": user}]}]


async def _judge(question: str, sources: list[dict]) -> str:
    cfg = get_models_config().conflict_check
    provider = get_provider("hybrid")
    provider.max_tokens, provider.temperature = cfg.max_tokens, 0
    return "".join([p async for p in provider.stream(build_messages(question, sources))])


async def check(question: str, sources: list[dict], strategy: str = "hybrid") -> dict | None:
    """回傳檢查資訊 {conflict, refs, ms, fallback}；段落不到 2 段或 mock 時不檢查，回傳 None。

    refs 是 sources 的 ref（1 起算），呼叫端照 note(refs) 加提醒。"""
    if len(sources) < 2 or strategy == "mock":
        return None
    cfg = get_models_config().conflict_check
    info: dict = {"conflict": False, "refs": [], "ms": 0, "fallback": None}
    t0 = time.perf_counter()
    try:
        text = await asyncio.wait_for(_judge(question, sources), cfg.timeout_s)
        refs = parse_verdict(text, {s["ref"] for s in sources})
        if refs is None:
            info["fallback"] = f"模型輸出看不懂：{text.strip()[:40]}"
        else:
            info |= {"conflict": bool(refs), "refs": refs}
    except TimeoutError:
        info["fallback"] = f"檢查逾時（>{cfg.timeout_s:.0f} 秒）"
    except (ProviderUnavailable, httpx.HTTPError) as e:
        info["fallback"] = f"本地模型無法使用：{type(e).__name__}: {e}"
    info["ms"] = round((time.perf_counter() - t0) * 1000)
    return info
