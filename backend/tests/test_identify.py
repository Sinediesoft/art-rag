"""以圖搜圖兩階段辨識（docs/adr/002）：CLIP 粗篩的前 verify_top_n 名都要做幾何驗證。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_identify_verifies_beyond_top_k(client, monkeypatch):
    """verify_top_n 比要回傳的筆數（top_k）多時，排在後面的也要驗：知識庫變大後正解常排在
    第 4–20 名（make eval-met-photos），調大 verify_top_n 才救得回來（Q-2026-10-04-06）。"""
    from app.rag.preprocess import load_image
    from app.repositories.index_store import get_store
    from app.services import search_service

    store = get_store()
    art = store.artworks[0]
    orig = store.search_images

    def last_place(query, k):
        # 正解（同一張圖，CLIP 1.0）排到最後一名：模擬 CLIP 沒把它排進前 top_k
        return orig(query, len(store.artworks))[::-1][:k]

    monkeypatch.setattr(store, "search_images", last_place)
    img = load_image(ROOT / art["image"]["path"])
    r = search_service.identify("test", top_k=1, img=img)

    assert len(store.artworks) <= 3  # 正解在 verify_top_n（3）名以內
    assert len(r["results"]) == 1
    assert r["matched"] and r["best_artwork_id"] == art["id"]


GEOMETRY = {"verify_min_det": 0.02, "verify_max_persp": 0.002, "verify_min_spread": 0.01}


def _spread_pts(n: int = 40, w: int = 768, h: int = 600):
    import numpy as np

    rng = np.random.default_rng(0)
    return rng.uniform([0, 0], [w, h], size=(n, 2)).astype(np.float32)


def test_degenerate_accepts_normal_and_far_away_photos():
    """正確配對不能被擋：正面拍（det≈1）、站遠拍畫只占照片一小塊
    （det 100，圖紙的 _plausible 會擋）。"""
    import numpy as np

    from app.rag import verify

    pts, shape = _spread_pts(), (600, 768)
    assert not verify._degenerate(np.eye(3), pts, shape, GEOMETRY)
    far = np.diag([10.0, 10.0, 1.0])
    assert not verify._degenerate(far, pts, shape, GEOMETRY)


def test_degenerate_rejects_frame_patterns():
    """畫框花紋的假對應（docs/adr/002「退化的 homography」）：
    壓成一點、鏡像、強烈透視、擠在一小塊。"""
    import numpy as np

    from app.rag import verify

    pts, shape = _spread_pts(), (600, 768)
    collapsed = np.array([[0.01, 0, 0], [0, 0.01, 0], [0, 0, 1.0]])  # det 0.0001
    mirrored = np.diag([-1.0, 1.0, 1.0])
    perspective = np.array([[1.0, 0, 0], [0, 1.0, 0], [0.01, 0, 1.0]])
    for h in (collapsed, mirrored, perspective):
        assert verify._degenerate(h, pts, shape, GEOMETRY)
    clustered = np.full((40, 2), 300, np.float32) + _spread_pts()[:, :1] * 0.005
    assert verify._degenerate(np.eye(3), clustered, shape, GEOMETRY)
