"""畫作卡推測（docs/adr/018，借鑒 ArtSeek 的 LICN 畫作卡）：
知識庫沒有這幅畫時，推測風格大類（含年代）、題材、媒材。

和 ArtSeek 不同，不訓練分類網路：用辨識已經載入的 Chinese-CLIP 做零樣本分類——
每個標籤的「別名 × 提示句」各算一個文字向量再平均（prompt ensembling），照片向量和它比餘弦相似度，
機率＝softmax(temperature × 相似度)。風格細分流派分不清楚（印象派／新印象派／後印象派），
但多半錯在同一大類裡，所以以大類（細分第一名所屬的那一類，機率是該類加總）為主、細分只當參考。
先過「像不像畫作」的把關，圖紙、文件、生活照不推測。
參數與標籤在 shared/models.yaml 的 style_guess。
只是推測、沒有出處，不寫進知識庫、不放進問答的 prompt。
"""

import hashlib
import json
import threading

import numpy as np

from app.core.config import StyleGuessSpec, get_models_config, get_settings
from app.rag.embedders import embed_text_clip

NOTES = [
    "依畫面和一組風格、題材、媒材標籤比對的推測，沒有出處，可能出錯",
    "風格的細分流派（例如印象派、新印象派、後印象派）不容易分清楚，請以大類為準",
    "照片的光線、反光、裁切會影響推測；拍正、拍完整幅比較準",
]
FIELD_LABELS = {"style": "風格", "genre": "題材", "media": "媒材"}
TOP_N = 3  # 每一欄回傳的候選數

_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _label_vec(aliases: list[str], templates: list[str]) -> np.ndarray:
    v = embed_text_clip([t.format(a) for a in aliases for t in templates]).mean(axis=0)
    return v / max(float(np.linalg.norm(v)), 1e-12)


def _matrices(spec: StyleGuessSpec) -> dict:
    """標籤向量只和設定有關，算一次就快取（設定改了 hash 就變，重新算）；CLIP 被卸載也不受影響。
    同時進來的請求等第一個算完，不重算。"""
    key = hashlib.sha1(
        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False).encode()
    ).hexdigest()
    with _lock:
        if key not in _cache:
            _cache.clear()  # 只留目前這份設定的
            _cache[key] = _build(spec)
        return _cache[key]


def _build(spec: StyleGuessSpec) -> dict:
    gate = np.stack([_label_vec([p], ["{}"]) for p in spec.painting_prompts + spec.other_prompts])
    out: dict = {"gate": gate, "tasks": {}}
    if spec.style:
        names, groups, periods = [], [], {}
        for g in spec.style.groups:
            periods[g.name] = g.period
            for name in g.labels:
                names.append(name)
                groups.append(g.name)
        vecs = [
            _label_vec(aliases, spec.style.templates)
            for g in spec.style.groups
            for aliases in g.labels.values()
        ]
        out["tasks"]["style"] = (names, np.stack(vecs), groups, periods)
    for key_ in ("genre", "media"):
        task = getattr(spec, key_)
        if task and task.labels:
            vecs = [_label_vec(aliases, task.templates) for aliases in task.labels.values()]
            out["tasks"][key_] = (list(task.labels), np.stack(vecs), None, None)
    return out


def warmup() -> None:
    """啟動時在背景先算好標籤向量（約 250 句，CPU 約 5 秒），第一次推測不用等；不拖慢啟動。"""
    if get_settings().embed_mode == "mock":
        return
    threading.Thread(
        target=lambda: _matrices(get_models_config().style_guess), name="style-warmup", daemon=True
    ).start()


def _softmax(sims: np.ndarray, temperature: float) -> np.ndarray:
    z = temperature * (sims - sims.max())
    p = np.exp(z)
    return p / p.sum()


def _field(key: str, names, mat, groups, periods, vec, spec: StyleGuessSpec) -> dict:
    p = _softmax(mat @ vec, spec.temperature)
    order = np.argsort(-p)
    candidates = [
        {"name": names[i], "prob": round(float(p[i]), 4)} | ({"group": groups[i]} if groups else {})
        for i in order[:TOP_N]
    ]
    if groups:
        # 風格：大類取細分第一名所屬的那一類，機率是那一類的加總（細分分不清，但多半錯在同一類裡）。
        # 不直接比各大類的加總：「現代藝術」有 6 個流派，長尾的機率加起來會蓋過真正的那一類
        # （秀拉原圖：細分第一名新印象派 0.33，但現代藝術加總 0.53 > 印象派一脈）
        name = groups[order[0]]
        prob = float(sum(pi for g, pi in zip(groups, p, strict=True) if g == name))
        period = periods[name]
    else:
        name, prob, period = names[order[0]], float(p[order[0]]), None
    return {
        "key": key,
        "label": FIELD_LABELS[key],
        "name": name,
        "period": period,
        "prob": round(prob, 4),
        "uncertain": prob < spec.min_confidence,
        "candidates": candidates,
    }


def summarize(fields: dict[str, dict]) -> str:
    style, genre, media = fields.get("style"), fields.get("genre"), fields.get("media")
    parts = []
    if style and not style["uncertain"]:
        fine = style["candidates"][0]
        # 細分名稱已經在大類名稱裡（浮世繪／日本浮世繪）就不附註
        same = fine.get("group") == style["name"] and fine["name"] not in style["name"]
        hint = f"（細分最接近{fine['name']}）" if same else ""
        parts.append(f"{style['period']}的{style['name']}{hint}")
    if genre and not genre["uncertain"]:
        parts.append(genre["name"])
    text = f"推測是{'的'.join(parts)}" if parts else "風格與題材看不太出來"
    if media:
        text += "，媒材看不太出來" if media["uncertain"] else f"，媒材像{media['name']}"
    return text + "。"


def guess(vec: np.ndarray, spec: StyleGuessSpec | None = None) -> dict:
    """vec：照片的 Chinese-CLIP 向量（embed_image 的輸出，已 L2 正規化）。"""
    spec = spec or get_models_config().style_guess
    m = _matrices(spec)
    p = _softmax(m["gate"] @ vec, spec.temperature)
    painting = float(p[: len(spec.painting_prompts)].sum())
    base = {"method": spec.method, "painting_score": round(painting, 4), "notes": NOTES}
    if painting < spec.painting_min:
        return base | {
            "is_painting": False,
            "fields": [],
            "summary": f"這張照片看起來不像畫作（像畫作的程度 {painting:.0%}），不推測風格。",
        }
    fields = {k: _field(k, *t, vec, spec) for k, t in m["tasks"].items()}
    return base | {
        "is_painting": True,
        "fields": list(fields.values()),
        "summary": summarize(fields),
    }
