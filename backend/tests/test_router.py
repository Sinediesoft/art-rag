"""領域路由（MMed-RAG 的領域辨識）：原型法的判斷規則。

只用人工向量、不需要索引與模型，所以 `pytest --noconftest tests/test_router.py` 也能跑。
"""

from types import SimpleNamespace

import numpy as np
import pytest

from app.rag import router


def unit(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def store(monkeypatch):
    # 畫作集中在 x 軸附近、圖紙集中在 y 軸附近
    s = SimpleNamespace(
        art=SimpleNamespace(image_vecs=np.stack([unit(1, 0.1, 0), unit(1, -0.1, 0)])),
        mfg=SimpleNamespace(
            image_vecs=np.stack([unit(0.1, 1, 0), unit(-0.1, 1, 0), unit(0, 1, 0.1)])
        ),
    )
    monkeypatch.setattr(router, "get_store", lambda: s)
    return s


def test_routes_to_nearer_prototype(store):
    art = router.route(unit(1, 0.2, 0))
    assert art.domain == "art" and art.margin < 0 and not art.uncertain
    mfg = router.route(unit(0.2, 1, 0))
    assert mfg.domain == "mfg" and mfg.margin > 0 and not mfg.uncertain


def test_uncertain_goes_to_confidential_side(store):
    """兩個原型差不多近：不確定時一律當圖紙，圖紙流程不接受雲端對照組。"""
    r = router.route(unit(1, 1, 0))
    assert r.uncertain and r.domain == "mfg"
    assert abs(r.margin) < r.summary()["min_margin"]


def test_single_domain_needs_no_decision(store):
    store.mfg.image_vecs = np.zeros((0, 3), np.float32)
    r = router.route(unit(0, 1, 0))
    assert r.domain == "art" and r.mfg_score is None and not r.uncertain


def test_summary_is_rounded_and_reads_threshold_from_models_yaml(store):
    s = router.route(unit(0.2, 1, 0)).summary()
    assert set(s) == {"domain", "margin", "art_score", "mfg_score", "min_margin", "uncertain"}
    assert s["min_margin"] == pytest.approx(0.05)
    assert s["margin"] == round(s["margin"], 4)
