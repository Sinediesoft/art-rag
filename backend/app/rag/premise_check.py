"""問題前提檢查：問題本身有沒有把事實說錯（刁難題的錯誤前提題，docs/adr/037 補）。

「董其昌是怎麼在 1958 年找到范寬簽名的？」：參考資料寫的是李霖燦，
回答模型看到問題和資料對不上，常照 prompt 第 3 條直接回「知識庫中沒有這方面的資料」。
改回答 prompt（answer_v6）讓矛盾題退步，沒採用；
這裡和矛盾檢查（conflict_check.py）一樣，回答前另外只問本地模型一件事，說錯了就在參考資料後面加提醒，
回答時先更正再回答。

- 至少 1 段參考資料、問答不是 mock 時才檢查；只看文字、不送照片；只呼叫本地主推論伺服器
- 逾時、連不上、輸出看不懂一律當成沒檢查（fallback），不擋回答
- 開關：請求的 premise_check ＞ .env 的 PREMISE_CHECK ＞ shared/models.yaml 的 premise_check.enabled
"""

import asyncio
import json
import re
import time

import httpx

from app.core.config import get_models_config, get_settings
from app.rag.prompt import format_context, load_template
from app.rag.providers import ProviderUnavailable, get_provider

# 加在參考資料後面
NOTE = (
    "（系統比對：問題裡「{claim}」和參考資料{where}不符，參考資料寫的是「{correct}」。"
    "回答時先依參考資料更正這一點並標註來源，再回答問題其餘的部分；"
    "不要回答「知識庫中沒有這方面的資料」。）"
)


def enabled(requested: bool | None = None) -> bool:
    if requested is not None:
        return requested
    env = get_settings().premise_check.strip().lower()
    if env in ("true", "1", "on"):
        return True
    if env in ("false", "0", "off"):
        return False
    return get_models_config().premise_check.enabled


# 「說錯的部分」要是問題裡的斷定；帶疑問詞或問號的是在問、不是在斷定（模型會把整句問題當成說錯）
_ASKING = re.compile(r"[？?]|誰|哪|什麼|甚麼|如何|怎麼|多久|多少|幾")


def parse_verdict(text: str, valid: set[int]) -> dict | None:
    """模型輸出 → {"mismatch", "claim", "correct", "ref"}；
    格式不對、說錯了卻沒寫清楚哪裡錯時回傳 None。"""
    m = re.search(r"\{.*\}", text, flags=re.S)
    try:
        verdict = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        verdict = None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("mismatch"), bool):
        return None
    if not verdict["mismatch"]:
        return {"mismatch": False}
    claim, correct, ref = verdict.get("claim"), verdict.get("correct"), verdict.get("ref")
    if not (
        isinstance(claim, str) and claim.strip() and isinstance(correct, str) and correct.strip()
    ):
        return None
    if not isinstance(ref, int) or (ref != 0 and ref not in valid):
        return None
    if _ASKING.search(claim):
        return {"mismatch": False}
    return {
        "mismatch": True,
        "claim": claim.strip()[:60],
        "correct": correct.strip()[:80],
        "ref": ref,
    }


def note(info: dict) -> str:
    where = "（作品資料）" if info["ref"] == 0 else f" [{info['ref']}] "
    return NOTE.format(claim=info["claim"], where=where, correct=info["correct"])


def build_messages(question: str, sources: list[dict], card: str) -> list[dict]:
    tpl = load_template(get_models_config().premise_check.prompt_version)
    user = (
        tpl["user"]
        .replace("{{card}}", card or "（無）")
        .replace("{{question}}", question)
        .replace("{{context}}", format_context(sources))
    )
    messages = [{"role": "system", "content": tpl["system"]}] if tpl["system"] else []
    return [*messages, {"role": "user", "content": [{"type": "text", "text": user}]}]


async def _judge(question: str, sources: list[dict], card: str) -> str:
    cfg = get_models_config().premise_check
    provider = get_provider("hybrid")
    provider.max_tokens, provider.temperature = cfg.max_tokens, 0
    return "".join([p async for p in provider.stream(build_messages(question, sources, card))])


async def check(
    question: str, sources: list[dict], card: str = "", strategy: str = "hybrid"
) -> dict | None:
    """回傳檢查資訊 {mismatch, claim, correct, ref, ms, fallback}；
    沒有段落或 mock 時不檢查，回傳 None。"""
    if not sources or strategy == "mock":
        return None
    cfg = get_models_config().premise_check
    info: dict = {"mismatch": False, "ms": 0, "fallback": None}
    t0 = time.perf_counter()
    try:
        text = await asyncio.wait_for(_judge(question, sources, card), cfg.timeout_s)
        verdict = parse_verdict(text, {s["ref"] for s in sources})
        if verdict is None:
            info["fallback"] = f"模型輸出看不懂：{text.strip()[:40]}"
        else:
            info |= verdict
    except TimeoutError:
        info["fallback"] = f"檢查逾時（>{cfg.timeout_s:.0f} 秒）"
    except (ProviderUnavailable, httpx.HTTPError) as e:
        why = e if isinstance(e, ProviderUnavailable) else type(e).__name__
        info["fallback"] = f"本地模型無法使用：{why}"
    info["ms"] = round((time.perf_counter() - t0) * 1000)
    return info
