"""圖片前處理：建索引與查詢共用同一份（共用層 §三）。"""

import io
from pathlib import Path

from PIL import Image, ImageOps

from app.core.config import get_models_config


def load_image(src: bytes | Path) -> Image.Image:
    """EXIF 轉正 → 轉 RGB → 長邊縮到設定值（預設 1024 px）。"""
    img = Image.open(io.BytesIO(src) if isinstance(src, bytes) else src)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    long_edge = int(get_models_config().retrieval["preprocess_long_edge"])
    if max(img.size) > long_edge:
        img.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    return img


def to_jpeg_bytes(img: Image.Image, quality: int = 85) -> bytes:
    """重新編碼成 JPEG，不帶任何 EXIF（含 GPS）。"""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()
