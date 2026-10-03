"""本地分流（五段防護第 1 段，docs/adr/014）：判斷使用者想做什麼，資料不出本機。
2026-10-02 起意圖一律由這裡判斷（之前是 Jev 的備援）。

分數 = 關鍵字（shared/agent.yaml，只看名稱以外的字）＋實體加分（句子裡有零件、單號、客戶…）
＋bge-m3 與各意圖範例句的相似度，再做 softmax 得到機率，交給信心閘門。
疑問句（沒有「把、幫我」這類祈使語氣）時降低修改類關鍵字的分數：「哪些訂單延後了？」是查詢不是修改。
"""

import math
import re
import threading
import time
from dataclasses import replace
from functools import lru_cache

import numpy as np

from app.agent.entities import Entity
from app.agent.types import System1Result, normalize
from app.core.config import get_agent_config, get_settings
from app.rag.embedders import embed_text

# 句子裡出現某種實體時，各意圖的加分（實體本身就是很強的線索）
ENTITY_BOOST: dict[str, dict[str, float]] = {
    "part": {"drawing_search": 1.0, "drawing_qa": 1.0, "data_query": 1.0, "reconstruct": 0.5},
    "so": {"data_query": 2.0},
    "wo": {"data_query": 2.0},
    "customer": {"data_query": 1.5},
    "warehouse": {"data_query": 1.0},
    "artwork": {"art_qa": 2.0},
    "artist": {"art_qa": 2.0},
}
# 有附照片時（本機已辨識出是畫作或圖紙）
PHOTO_BOOST: dict[str, dict[str, float]] = {
    "art": {"art_qa": 2.5},
    "drawing": {"drawing_qa": 1.5, "drawing_search": 1.0, "reconstruct": 0.5},
}

_lock = threading.Lock()


@lru_cache
def _compiled() -> dict:
    cfg = get_agent_config()
    lr = cfg["local_router"]

    def rules(items: list) -> list[tuple[re.Pattern, float]]:
        return [(re.compile(p, re.I), float(w)) for p, w in items]

    return {
        "intents": {k: rules(v.get("keywords", [])) for k, v in cfg["intents"].items()},
        "ops": {k: rules(v.get("keywords", [])) for k, v in cfg["modify_ops"].items()},
        "flags": {k: re.compile(v["pattern"], re.I) for k, v in cfg["flags"].items()},
        "question": re.compile(lr["question_cues"]),
        "imperative": re.compile(lr["imperative_cues"]),
        "quantity": re.compile(lr["quantity"]),
    }


_example_vecs: dict[str, np.ndarray] = {}


def _examples() -> dict[str, np.ndarray]:
    """各意圖範例句的 bge-m3 向量（第一次用到時計算，之後快取）。"""
    with _lock:
        if not _example_vecs:
            for k, v in get_agent_config()["intents"].items():
                if v.get("examples"):
                    _example_vecs[k] = embed_text(v["examples"])
        return _example_vecs


def warmup() -> None:
    """啟動時先算好範例句向量，第一次路由不用多等 1～2 秒（mock 模式不需要）。"""
    if get_settings().embed_mode != "mock":
        _examples()


def _score(rules: list[tuple[re.Pattern, float]], text: str) -> float:
    return sum(w for p, w in rules if p.search(text))


def is_question(text: str) -> bool:
    c = _compiled()
    return bool(c["question"].search(text)) and not c["imperative"].search(text)


def classify_op(text: str) -> tuple[str | None, dict[str, float]]:
    """修改操作：關鍵字分數最高的一項；都沒命中就回 None（交給參數抽取再判斷）。"""
    c = _compiled()
    scores = {k: _score(r, text) for k, r in c["ops"].items()}
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] <= 0:
        return None, {}
    total = sum(math.exp(v) for v in scores.values())
    return best[0], {k: math.exp(v) / total for k, v in scores.items()}


# 畫名與畫家名換成〈A〉再算關鍵字與相似度：〈有絲柏的麥田〉〈大碗島的星期天下午〉
# 本身就是描述畫面的詞，不換的話「有絲柏的麥田收藏在哪裡？」會被當成用畫面描述找畫（以文搜畫）。
# 圖紙名稱（連接法蘭、軸承座）對判斷意圖有幫助，保留
TITLE_KINDS = {"artwork", "artist"}
TITLE_MASK = "〈A〉"


def _mask_titles(text: str, entities: list[Entity]) -> tuple[str, list[Entity]]:
    out, moved, pos, shift = [], [], 0, 0
    for e in sorted(entities, key=lambda e: e.start):
        out.append(text[pos : e.start])
        start = e.start + shift
        if e.kind in TITLE_KINDS:
            out.append(TITLE_MASK)
            moved.append(replace(e, start=start, end=start + len(TITLE_MASK)))
            shift += len(TITLE_MASK) - (e.end - e.start)
        else:
            out.append(text[e.start : e.end])
            moved.append(replace(e, start=start, end=start + (e.end - e.start)))
        pos = e.end
    out.append(text[pos:])
    return "".join(out), moved


def classify(text: str, entities: list[Entity], photo_kind: str | None = None) -> System1Result:
    t0 = time.perf_counter()
    text, entities = _mask_titles(text, entities)
    cfg = get_agent_config()
    lr = cfg["local_router"]
    c = _compiled()
    keys = list(cfg["intents"])
    # 關鍵字只看名稱以外的字：〈有絲柏的麥田〉裡的「麥田、絲柏」是畫名，不是在描述畫面
    # （名稱本身已經有實體加分，見 ENTITY_BOOST）
    residual = text
    for e in sorted(entities, key=lambda e: e.start, reverse=True):
        residual = residual[: e.start] + " " + residual[e.end :]
    logits = {k: _score(c["intents"][k], residual) for k in keys}
    question = is_question(text)
    write_hit = logits["modify"] > 0
    if question:
        logits["modify"] *= float(lr["question_write_factor"])
    if c["quantity"].search(text):
        logits["modify"] += 0.5 if not question else 0.0
        logits["data_query"] += 0.5
    for e in {(e.kind) for e in entities}:
        for k, w in ENTITY_BOOST.get(e, {}).items():
            logits[k] += w
    for k, w in PHOTO_BOOST.get(photo_kind or "", {}).items():
        logits[k] += w
    if write_hit and not question:
        # 祈使句又有修改動詞：「法蘭庫存改成 120」裡的「庫存」不代表要查詢
        logits = {k: (v + 1.0 if k == "modify" else v * 0.5) for k, v in logits.items()}
    hit_any = any(v > 0 for k, v in logits.items() if k != "out_of_scope")
    if not hit_any:
        logits["out_of_scope"] += float(lr["out_of_scope_base"])

    # 只有名稱（「法蘭」）：看不出想做什麼，不用範例句相似度硬猜，交給信心閘門出澄清按鈕
    bare = bool(entities) and not re.sub(r"[\s，,。？?！!、]", "", residual)

    used_embed = False
    if get_settings().embed_mode != "mock" and text.strip() and not bare:
        q = embed_text([text])[0]
        sims = {k: float(np.max(v @ q)) for k, v in _examples().items()}
        if sims:
            top = max(sims.values())
            for k, s in sims.items():
                logits[k] += float(lr["embed_weight"]) * (s - top)
            used_embed = True

    m = max(logits.values())
    exp = {k: math.exp(v - m) for k, v in logits.items()}
    probs = normalize(exp, keys)
    op, op_probs = classify_op(text)
    flags = {k: bool(p.search(text)) for k, p in c["flags"].items()}
    return System1Result(
        engine="local",
        intent_probs=probs,
        op_probs=op_probs if op else {},
        flags=flags,
        flag_probs={k: float(v) for k, v in flags.items()},
        model="關鍵字＋bge-m3" if used_embed else "關鍵字規則",
        latency_ms=round((time.perf_counter() - t0) * 1000),
        detail={
            "logits": {k: round(v, 2) for k, v in logits.items()},
            "question": question,
            "write_mode": write_hit and not question,
            "bare_entity": bare,
        },
    )
