"""沒說畫名、畫家的畫作問題，判斷問的是哪一幅（docs/adr/038）。

「那個點點畫的島 現在誰收著啊」：文字實體解析（entities.py）只認得畫名與畫家，認不出描述；
bge-m3 的段落相似度各幅畫擠在一起（第一名只贏 0.004～0.03），用門檻分不開。
改成把每幅畫的作品資料與「畫面／構圖」段落當選項，請 Jev Choice 選一幅或「都不是」；
Jev 不能用時改問本地 Qwen3-VL。

- 只在第 2 段放行之後、意圖是畫作問答或找畫、沒有畫名畫家也沒附照片時才呼叫
- 送 Jev 的是代號化後的問句與公開的畫作資料（畫作都是公開的），不送圖片
- 判斷不出來（選「都不是」、信心不夠、輸出看不懂、逾時）就不推測，照原本的全庫檢索作答
"""

import asyncio
import re
import time
from dataclasses import dataclass

import httpx

from app.agent import jev
from app.core.config import get_agent_config
from app.rag.prompt import artwork_card
from app.rag.providers import ProviderUnavailable, get_provider
from app.repositories.index_store import get_store

NONE = "none"


@dataclass
class Guess:
    artwork_id: str | None
    engine: str  # jev／local
    prob: float | None
    ms: int
    egress_bytes: int = 0
    fallback_reason: str | None = None

    def public(self) -> dict:
        return {
            "artwork_id": self.artwork_id,
            "by": "Jev" if self.engine == "jev" else "地端 Qwen3-VL",
            "prob": None if self.prob is None else round(self.prob, 3),
            "ms": self.ms,
            "fallback_reason": self.fallback_reason,
        }


def _options() -> dict[str, str]:
    """每幅畫一個選項：作品資料＋畫面（或構圖）段落；描述多半講畫面上看得到的東西。"""
    store = get_store()
    scene: dict[str, str] = {}
    for c in store.art.chunks:
        if c.get("topic") in ("畫面", "構圖") and c["artwork_id"] not in scene:
            scene[c["artwork_id"]] = c["text"]
    return {a["id"]: f"{artwork_card(a)}；畫面：{scene.get(a['id'], '')}" for a in store.artworks}


async def _by_jev(masked: str, options: dict[str, str], cfg: dict) -> Guess:
    t0 = time.perf_counter()
    criteria = {**options, NONE: cfg["none_label"]}
    reply = await jev.ask(
        {"context": cfg["context"], "question": masked},
        {"which": {"type": "choice", "instructions": cfg["instructions"], "criteria": criteria}},
        timeout_s=float(cfg.get("jev_timeout_s", 3)),
    )
    best, prob, _ = reply.choice("which", list(criteria))
    picked = best if best != NONE and prob >= float(cfg["min_prob"]) else None
    ms = round((time.perf_counter() - t0) * 1000)
    return Guess(picked, "jev", prob, ms, reply.bytes)


async def _by_local(question: str, options: dict[str, str], cfg: dict, reason: str | None) -> Guess:
    t0 = time.perf_counter()
    ids = list(options)
    listing = "\n".join(f"{i + 1}. {options[a]}" for i, a in enumerate(ids))
    prompt = (
        cfg["local_prompt"]
        .replace("{{n}}", str(len(ids)))
        .replace("{{artworks}}", listing)
        .replace("{{question}}", question)
    )
    provider = get_provider("hybrid")
    provider.max_tokens, provider.temperature = 4, 0
    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    picked, why = None, reason
    try:
        text = await asyncio.wait_for(
            _collect(provider, messages), float(cfg.get("local_timeout_s", 8))
        )
        m = re.match(r"\s*(\d+)", text)
        n = int(m.group(1)) if m else -1
        if 1 <= n <= len(ids):
            picked = ids[n - 1]
        elif n != 0:
            why = f"{reason + '；' if reason else ''}地端輸出看不懂：{text.strip()[:20]}"
    except TimeoutError:
        why = f"{reason + '；' if reason else ''}地端判斷逾時"
    except (ProviderUnavailable, httpx.HTTPError) as e:
        why = f"{reason + '；' if reason else ''}地端模型無法使用：{type(e).__name__}"
    return Guess(picked, "local", None, round((time.perf_counter() - t0) * 1000), 0, why)


async def _collect(provider, messages: list[dict]) -> str:
    return "".join([p async for p in provider.stream(messages)])


def asks_about_it(question: str) -> bool:
    """句子是在問那幅畫的內容（有問句詞），不是只在找畫（「找…」「有沒有…的畫」「哪一幅」）。"""
    cfg = get_agent_config().get("art_resolve") or {}
    if re.search(cfg.get("search_pattern", r"$^"), question):
        return False
    return bool(re.search(cfg.get("ask_pattern", r"$^"), question))


async def resolve(question: str, masked: str, use_jev: bool) -> Guess | None:
    """回傳推測結果（artwork_id 為 None＝判斷不出來）；關閉或知識庫沒有畫作時回傳 None。
    question：個資已遮蔽的原文（只給本地模型）；masked：代號化後的問句（只送 Jev）。"""
    cfg = get_agent_config().get("art_resolve") or {}
    if not cfg.get("enabled", False):
        return None
    options = _options()
    if not options:
        return None
    reason = None
    if use_jev:
        try:
            return await _by_jev(masked, options, cfg)
        except jev.JevUnavailable as e:
            reason = str(e)
    return await _by_local(question, options, cfg, reason)
