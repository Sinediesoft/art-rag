"""檢索段落篩選（MIRA 的 Rearrange，見 docs/adr/008-rearrange.md）。

依 retrieval 規則取出的候選段落，一次交給本地生成端判斷哪幾段對回答問題有幫助，只留挑中的，
至少留 1 段（MIRA 也保底）。留下幾段由模型逐題決定，這就是「動態調整筆數」。

- 只呼叫本地主推論伺服器（hybrid），不論問答用哪個策略都不送雲端；
  問答選 mock 時也用 mock，不呼叫模型
- 只看文字、不送照片；一題只呼叫一次；只有 0～1 段可篩時不呼叫（ms 為 0）
- 逾時、連不上、輸出不是「1,3」或「無」這種固定格式時，一律退回原本的段落，不擋回答
- 開關優先順序：請求的 rearrange ＞ .env 的 REARRANGE ＞ shared/models.yaml 的 rearrange.enabled
"""

import asyncio
import re
import time

import httpx

from app.core.config import get_models_config, get_settings
from app.rag.prompt import load_template
from app.rag.providers import ProviderUnavailable, get_provider

_ANSWER = re.compile(r"^(無|\d+(?:[,，、]\d+)*)$")


def enabled(requested: bool | None = None) -> bool:
    if requested is not None:
        return requested
    env = get_settings().rearrange.strip().lower()
    if env in ("true", "1", "on"):
        return True
    if env in ("false", "0", "off"):
        return False
    return get_models_config().rearrange.enabled


def parse_choice(text: str, n: int) -> list[int] | None:
    """模型輸出 → 留下的段落索引（0 起算）。

    「無」回傳 []；格式不對或編號超出範圍回傳 None（不採信）。"""
    s = re.sub(r"[\s「」『』。.]", "", text)
    if not _ANSWER.match(s):
        return None
    if s == "無":
        return []
    picked = sorted({int(x) - 1 for x in re.split(r"[,，、]", s)})
    return picked if all(0 <= i < n for i in picked) else None


def build_messages(question: str, candidates: list[dict]) -> list[dict]:
    tpl = load_template(get_models_config().rearrange.prompt_version)
    # 與回答用的參考資料同一種寫法；比較題會混入其他畫作的段落，帶標題模型才分得出來
    listing = "\n".join(
        f"[{i + 1}]（〈{c['title']}〉・{c['topic']}）{c['text']}" for i, c in enumerate(candidates)
    )
    user = tpl["user"].replace("{{question}}", question).replace("{{candidates}}", listing)
    return [
        {"role": "system", "content": tpl["system"]},
        {"role": "user", "content": [{"type": "text", "text": user}]},
    ]


async def _judge(question: str, candidates: list[dict], strategy: str) -> str:
    cfg = get_models_config().rearrange
    provider = get_provider("mock" if strategy == "mock" else "hybrid")
    provider.max_tokens, provider.temperature = cfg.max_tokens, 0
    return "".join([p async for p in provider.stream(build_messages(question, candidates))])


async def rearrange(
    question: str, sources: list[dict], strategy: str = "hybrid"
) -> tuple[list[dict], dict]:
    """回傳（留下的段落，篩選資訊）。留下的段落重新編號 ref 1..n，回答裡的 [n] 才對得上。

    strategy 是問答用的策略，只用來判斷是不是 mock。
    篩選失敗（fallback 有值）時原封不動回傳全部段落。"""
    cfg = get_models_config().rearrange
    candidates = sources[: cfg.max_candidates]
    info = {"candidates": len(candidates), "kept": len(sources), "ms": 0, "fallback": None}
    if len(candidates) <= 1:  # 沒有東西可以篩
        return sources, info
    t0 = time.perf_counter()
    kept = sources
    try:
        text = await asyncio.wait_for(_judge(question, candidates, strategy), cfg.timeout_s)
        picked = parse_choice(text, len(candidates))
        if picked is None:
            info["fallback"] = f"模型輸出看不懂：{text.strip()[:40]}"
        else:
            kept = [candidates[i] for i in picked] or candidates[:1]
    except TimeoutError:
        info["fallback"] = f"篩選逾時（>{cfg.timeout_s:.0f} 秒）"
    except (ProviderUnavailable, httpx.HTTPError) as e:
        info["fallback"] = f"本地模型無法使用：{type(e).__name__}: {e}"
    info["ms"] = round((time.perf_counter() - t0) * 1000)
    info["kept"] = len(kept)
    return [{**s, "ref": i + 1} for i, s in enumerate(kept)], info
