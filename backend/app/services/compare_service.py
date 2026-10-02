"""影像對位與比對的流程編排（docs/adr/012）：上傳的照片 vs 知識庫畫作原圖或工廠圖紙。

純 CPU（OpenCV），不載入模型，所以不經過記憶體管理。結果固定，最近 32 組（照片, 對象）留在行程內，
疊圖 API 共用同一份；被擠掉就重算（結果相同）。
"""

import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from app.analysis import align
from app.core.config import REPO_ROOT, get_models_config
from app.core.errors import AppError
from app.rag import verify
from app.rag.preprocess import load_image
from app.repositories.index_store import get_store
from app.services.search_service import load_upload, part_summary

# target 的格式：artwork:<畫作 id> 或 part:<圖紙 id>（兩張上傳照片互比是之後的事）
TARGET_PATTERN = r"^(artwork|part):[A-Za-z0-9_.-]+$"

ART_NOTES = ["依照片和原圖的特徵點對應推算位置；只拍到很小一塊（特寫）時可能對不上"]
MFG_NOTES = [
    "只比三個視圖格子裡的線條；視圖標題、標題欄與格子外的尺寸標註不比",
    "紅：知識庫圖紙有、照片沒有；藍：照片有、知識庫圖紙沒有",
]
GLOBAL_CHANGE_NOTE = (
    "差異遍布整張圖：三視圖依外形尺寸縮放，外形一改整張都會不同，"
    "可能是改了外形尺寸，請直接比對標註尺寸"
)


def _target(target: str) -> tuple[str, str, Path, str]:
    """回傳 (領域, id, 參考圖路徑, 參考圖網址)；找不到就 404。"""
    kind, _, tid = target.partition(":")
    store = get_store()
    if kind == "artwork":
        a = store.get_artwork(tid)
        if not a:
            raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {tid}", 404)
        return "art", tid, REPO_ROOT / a["image"]["path"], f"/api/v1/artworks/{tid}/image"
    p = store.get_part(tid)
    if not p:
        raise AppError("PART_NOT_FOUND", f"找不到圖紙 {tid}", 404)
    return "mfg", tid, REPO_ROOT / p["drawing"], part_summary(p)["drawing_url"]


@lru_cache(maxsize=32)
def _compute(image_id: str, target: str) -> tuple[dict, bytes]:
    """找不到照片或對不上時丟 AppError（例外不會被快取）。"""
    domain, tid, path, ref_url = _target(target)
    img = load_image(load_upload(image_id))
    t0 = time.perf_counter()
    cfg = get_models_config()
    spec = getattr(cfg.image_compare, domain)
    min_inliers = int(
        (cfg.retrieval if domain == "art" else cfg.drawing_retrieval)["verify_min_inliers"]
    )
    ref = Image.open(path).convert("RGB")
    loc = align.locate(
        verify.features(img),
        verify.kb_features(path, path.stat().st_mtime, drawing=domain == "mfg"),
        img.size,
        ref.size,
        min_inliers=min_inliers,
        strict=domain == "mfg",
    )
    if loc is None:
        what = "原圖" if domain == "art" else "知識庫圖紙"
        raise AppError(
            "ALIGN_FAILED", f"照片和{what}對不上：對應的特徵點不夠，或算出來的位置不合理", 422
        )

    diff, notes = None, list(ART_NOTES if domain == "art" else MFG_NOTES)
    if spec.diff == "ink":
        d = align.diff_ink(img, loc.h, ref, spec, boxes=verify.drawing_views(ref))
        diff = {
            "method": "ink",
            "status": d.status,
            "regions": [vars(r) for r in d.regions],
            "changed_ratio": d.changed_ratio,
        }
        png = align.overlay_png(ref, d)
        if d.status == "global_change":
            notes.insert(0, GLOBAL_CHANGE_NOTE)
    else:
        png = align.location_png(ref, loc)
    result = {
        "target": {"kind": "artwork" if domain == "art" else "part", "id": tid},
        "inliers": loc.inliers,
        "location": {"polygon": loc.polygon, "coverage": loc.coverage, "center": loc.center},
        "diff": diff,
        "reference_url": ref_url,
        "overlay_url": f"/api/v1/images/{image_id}/align.png?target={quote(target, safe='')}",
        "notes": notes,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
    }
    return result, png


def align_photo(image_id: str, target: str) -> dict:
    return _compute(image_id, target)[0]


def align_overlay(image_id: str, target: str) -> bytes:
    return _compute(image_id, target)[1]
