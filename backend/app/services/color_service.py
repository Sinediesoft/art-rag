"""色彩分析的流程編排（docs/adr/010）：知識庫畫作讀索引裡算好的結果，上傳的照片即時計算。"""

import time
from functools import lru_cache
from pathlib import Path

from app.analysis.color import analyze
from app.core.errors import AppError
from app.rag.preprocess import load_image
from app.repositories.index_store import get_store
from app.services.search_service import load_upload

ORIGINAL_NOTES = ["依數位圖檔計算，可能與原作現況及展場光線下看到的顏色不同"]
PHOTO_NOTES = [
    "依照片計算，會受光線與白平衡影響；辨識成功時，畫作頁顯示的是原圖的分析",
    "照片裡拍到的畫框、牆壁、螢幕邊框也會一起算進去",
]


def _artwork_or_404(artwork_id: str) -> dict:
    a = get_store().get_artwork(artwork_id)
    if not a:
        raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {artwork_id}", 404)
    return a


def artwork_colors(artwork_id: str) -> dict:
    a = _artwork_or_404(artwork_id)
    # 網址帶知識庫 hash：重建索引後瀏覽器不會拿到快取的舊圖（同 part_summary）
    v = get_store().manifest.get("kb_hash", "")[:8]
    return {
        **a["colors"],
        "source": "original",
        "notes": ORIGINAL_NOTES,
        "map_url": f"/api/v1/artworks/{artwork_id}/colormap.png?v={v}",
        "latency_ms": 0,
    }


def artwork_colormap_path(artwork_id: str) -> Path:
    _artwork_or_404(artwork_id)
    return get_store().dir / "colormaps" / f"{artwork_id}.png"


@lru_cache(maxsize=32)
def _photo(image_id: str) -> tuple[dict, bytes, int]:
    """結果固定，快取被擠掉就重算；找不到照片時 load_upload 丟 AppError（例外不會被快取）。"""
    img = load_image(load_upload(image_id))
    t0 = time.perf_counter()
    result, png = analyze(img)
    return result, png, round((time.perf_counter() - t0) * 1000)


def photo_colors(image_id: str) -> dict:
    result, _, ms = _photo(image_id)
    return {
        **result,
        "source": "photo",
        "notes": PHOTO_NOTES,
        "map_url": f"/api/v1/images/{image_id}/colormap.png",
        "latency_ms": ms,
    }


def photo_colormap(image_id: str) -> bytes:
    return _photo(image_id)[1]
