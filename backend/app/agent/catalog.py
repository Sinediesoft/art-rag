"""知識庫目錄題（docs/adr/039）：「知識庫有哪些畫」「總共幾幅」「哪一幅最早完成」。

問答只檢索和問題最像的幾段，本來就答不了「全部有哪些」：實測會列出段落裡順帶提到、
不在知識庫的〈早春圖〉，反而漏掉〈大碗島的星期天下午〉。
- 清單題（list_pattern）：智慧助理第 2 段放行後直接從知識庫列出，不檢索、不呼叫 LLM
- 比較題（compare_pattern，沒指定畫作時）：問答的參考資料最前面多一段系統產生的「知識庫目錄」
  （每幅的作者、年代、媒材、尺寸、收藏），交給模型比較並標出處
樣式在 shared/agent.yaml 的 catalog。
"""

import re

from app.core.config import get_agent_config
from app.repositories.index_store import get_store

CHUNK_ID = "catalog"
TOPIC = "知識庫目錄"


def _cfg() -> dict:
    return get_agent_config().get("catalog") or {}


def _match(key: str, question: str) -> bool:
    pattern = _cfg().get(key)
    return bool(pattern and re.search(pattern, question))


def is_list_question(question: str) -> bool:
    """問知識庫有哪些畫、總共幾幅；帶比較字眼（最早、最大…）的不算，交給問答。"""
    return _match("list_pattern", question) and not _match("compare_pattern", question)


def is_compare_question(question: str) -> bool:
    """跨畫作的比較題（哪幅最早、最大…）。"""
    return _match("compare_pattern", question)


def needs_catalog(question: str) -> bool:
    """沒指定畫作的問答要不要附知識庫目錄：清單題或跨畫作的比較題。"""
    return _match("list_pattern", question) or _match("compare_pattern", question)


def _line(a: dict) -> str:
    parts = [a["artist"]["zh"], a.get("date_text"), a.get("medium"), a.get("dimensions")]
    return (
        f"〈{a['title']['zh']}〉：" + "，".join(p for p in parts if p) + f"，現藏{a['collection']}"
    )


def reply() -> str:
    arts = get_store().artworks
    if not arts:
        return "知識庫目前沒有畫作。"
    lines = "\n".join(f"- {_line(a)}" for a in arts)
    return f"目前知識庫共有 {len(arts)} 幅畫：\n{lines}\n\n想知道哪一幅的細節，直接問畫名就可以。"


def source() -> dict:
    """附在問答參考資料最前面的一段（系統依知識庫產生，和色彩分析段一樣沒有網址）。"""
    arts = get_store().artworks
    text = (
        f"知識庫目前共有 {len(arts)} 幅畫（只有這幾幅）："
        + "；".join(_line(a) for a in arts)
        + "。"
    )
    return {
        "ref": 1,
        "chunk_id": CHUNK_ID,
        "topic": TOPIC,
        "text": text,
        "source_url": None,
        "license": "系統依知識庫產生",
        "score": 1.0,
        "artwork_id": None,
        "artwork_title": TOPIC,
        # 第 4 段處理過程與送 Jev 的段落標題讀 title（一般畫作段落也有）
        "title": TOPIC,
        "source_label": "系統依知識庫的作品資料產生",
        "level": "公開",
    }
