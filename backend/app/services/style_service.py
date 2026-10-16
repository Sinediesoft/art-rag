"""畫作卡推測的流程編排（docs/adr/018）：上傳的照片即時推測風格大類、題材、媒材。"""

import time
from functools import lru_cache

from app.analysis.style import guess
from app.rag.embedders import embed_image
from app.rag.preprocess import load_image
from app.services import uploads
from app.services.identity import Account
from app.services.memory_guard import guarded


@lru_cache(maxsize=64)
def _photo(image_id: str) -> tuple[dict, int]:
    """結果固定，快取被擠掉就重算；找不到照片時丟 AppError（例外不會被快取）。
    授權（擁有者）在快取之外：photo_style 每次呼叫都先驗。"""
    img = load_image(uploads.load_unchecked(image_id))
    t0 = time.perf_counter()
    result = guess(embed_image(img))
    return result, round((time.perf_counter() - t0) * 1000)


@guarded("search_image", {"clip"})
def photo_style(image_id: str, viewer: Account) -> dict:
    uploads.authorize(image_id, viewer)
    result, ms = _photo(image_id)
    return {**result, "latency_ms": ms}
