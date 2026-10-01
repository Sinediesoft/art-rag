"""領域路由（MMed-RAG 的 domain identification）：照片先判斷是畫作（art）還是工廠圖紙（mfg），
再交給該領域的辨識流程。見 docs/adr/004-domain-router.md。

原型法：每個領域的原型＝知識庫圖片 Chinese-CLIP 向量的平均（直接取索引裡的 image_vecs），
margin＝照片與圖紙原型的相似度 − 與畫作原型的相似度。不用訓練；新增畫作或圖紙、重建索引後自動更新。

|margin| < router.min_margin 視為不確定，一律當圖紙：圖紙屬機密，
走圖紙流程最壞只是不能用雲端對照組；反過來把圖紙當成畫作，就走進了允許雲端對照組的那條路。
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.core.config import get_models_config
from app.repositories.index_store import get_store

Domain = Literal["art", "mfg"]


@dataclass(frozen=True)
class Route:
    domain: Domain
    margin: float  # sim(圖紙原型) − sim(畫作原型)；> 0 偏向圖紙。只有一個領域有資料時為 0
    art_score: float | None  # 照片與畫作原型的相似度；該領域沒有資料時為 None
    mfg_score: float | None
    uncertain: bool

    def summary(self) -> dict:
        def rnd(x: float | None) -> float | None:
            return None if x is None else round(x, 4)

        return {
            "domain": self.domain,
            "margin": round(self.margin, 4),
            "art_score": rnd(self.art_score),
            "mfg_score": rnd(self.mfg_score),
            "min_margin": float(get_models_config().router["min_margin"]),
            "uncertain": self.uncertain,
        }


def _prototype(vecs: np.ndarray) -> np.ndarray | None:
    if len(vecs) == 0:
        return None
    p = vecs.mean(axis=0)
    return p / max(float(np.linalg.norm(p)), 1e-12)


def route(vec: np.ndarray) -> Route:
    """vec：照片的 CLIP 向量（embed_image 的輸出，已 L2 正規化）。"""
    store = get_store()
    art, mfg = _prototype(store.art.image_vecs), _prototype(store.mfg.image_vecs)
    a = float(vec @ art) if art is not None else None
    m = float(vec @ mfg) if mfg is not None else None
    if a is None or m is None:  # 只有一個領域有資料，不必判斷
        return Route("art" if m is None else "mfg", 0.0, a, m, uncertain=False)
    margin = m - a
    if abs(margin) < float(get_models_config().router["min_margin"]):
        return Route("mfg", margin, a, m, uncertain=True)
    return Route("mfg" if margin > 0 else "art", margin, a, m, uncertain=False)
