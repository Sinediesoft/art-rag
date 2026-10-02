"""影像對位與比對的流程編排（docs/adr/012）：上傳的照片 vs 知識庫畫作原圖、工廠圖紙或另一張照片。

純 CPU（OpenCV），不載入模型，所以不經過記憶體管理。結果固定，最近 32 組（照片, 對象）留在行程內，
疊圖 API 共用同一份；被擠掉就重算（結果相同）。
"""

import time
from functools import lru_cache
from urllib.parse import quote

from PIL import Image

from app.analysis import align
from app.core.config import REPO_ROOT, get_models_config
from app.core.errors import AppError
from app.rag import verify
from app.rag.preprocess import load_image
from app.repositories.index_store import get_store
from app.services.search_service import load_upload, part_summary

# target 的格式：artwork:<畫作 id>、part:<圖紙 id>，或 image:<另一張上傳照片>（兩張照片互比，畫作）
TARGET_PATTERN = r"^(artwork|part|image):[A-Za-z0-9_.-]+$"
_KIND = {"art": "artwork", "mfg": "part", "pair": "image"}

ART_NOTES = [
    "依照片和原圖的特徵點對應推算位置；只拍到很小一塊（特寫）時可能對不上",
    "只比照片拍到的範圍；整體的明暗、白平衡先拉齊，照片比原圖模糊時，太細的筆觸不比",
    "紅：形狀不同（加筆、補筆、塗糊、多了東西）；橘：顏色不同（褪色、補色）；紫：兩種都有",
    "標出來的是差異候選：反光、陰影也會被標成顏色不同，請對照原圖確認",
]
MFG_NOTES = [
    "只比三個視圖格子裡的線條；視圖標題、標題欄與格子外的尺寸標註不比",
    "紅：知識庫圖紙有、照片沒有；藍：照片有、知識庫圖紙沒有",
]
PAIR_NOTES = [
    "只比兩張都拍到的範圍；整體的明暗、白平衡先拉齊再比",
    "紅：形狀不同（補筆、塗糊、多了東西）；橘：顏色不同（褪色、補色）；紫：兩種都有",
    "兩張要在同樣光線、差不多距離下拍，反光、陰影不同的地方也會被標出來",
]
GLOBAL_CHANGE_NOTE = (
    "差異遍布整張圖：三視圖依外形尺寸縮放，外形一改整張都會不同，"
    "可能是改了外形尺寸，請直接比對標註尺寸"
)
PAIR_GLOBAL_NOTE = "兩張差異太大、遍布整張：可能光線差太多，或不是同一幅畫的同一個地方"
ART_GLOBAL_NOTE = "和原圖的差異遍布整張：可能光線差太多、大片反光，或照片太模糊"


def _target(target: str) -> tuple[str, str, Image.Image, object, str]:
    """回傳 (領域, id, 參考圖, 參考圖的特徵點, 參考圖網址)；找不到就 404。"""
    kind, _, tid = target.partition(":")
    if kind == "image":
        ref = load_image(load_upload(tid))
        return "pair", tid, ref, verify.features(ref), f"/api/v1/images/{tid}"
    store = get_store()
    if kind == "artwork":
        a = store.get_artwork(tid)
        if not a:
            raise AppError("ARTWORK_NOT_FOUND", f"找不到畫作 {tid}", 404)
        path, url, drawing = REPO_ROOT / a["image"]["path"], f"/api/v1/artworks/{tid}/image", False
    else:
        p = store.get_part(tid)
        if not p:
            raise AppError("PART_NOT_FOUND", f"找不到圖紙 {tid}", 404)
        path, url, drawing = REPO_ROOT / p["drawing"], part_summary(p)["drawing_url"], True
    feats = verify.kb_features(path, path.stat().st_mtime, drawing=drawing)
    return ("mfg" if drawing else "art"), tid, Image.open(path).convert("RGB"), feats, url


@lru_cache(maxsize=32)
def _compute(image_id: str, target: str) -> tuple[dict, bytes]:
    """找不到照片或對不上時丟 AppError（例外不會被快取）。"""
    domain, tid, ref, ref_feats, ref_url = _target(target)
    img = load_image(load_upload(image_id))
    t0 = time.perf_counter()
    cfg = get_models_config()
    spec = getattr(cfg.image_compare, domain)
    # 對應點下限沿用辨識的門檻：畫作（含兩張照片互比）用 retrieval，圖紙用 drawing_retrieval
    min_inliers = int(
        (cfg.drawing_retrieval if domain == "mfg" else cfg.retrieval)["verify_min_inliers"]
    )
    loc = align.locate(
        verify.features(img),
        ref_feats,
        img.size,
        ref.size,
        min_inliers=min_inliers,
        strict=domain == "mfg",
    )
    if loc is None:
        what = {"art": "原圖", "mfg": "知識庫圖紙", "pair": "照片 A"}[domain]
        raise AppError(
            "ALIGN_FAILED", f"照片和{what}對不上：對應的特徵點不夠，或算出來的位置不合理", 422
        )

    diff, notes = None, list({"art": ART_NOTES, "mfg": MFG_NOTES, "pair": PAIR_NOTES}[domain])
    if spec.diff == "ink":
        d = align.diff_ink(img, loc.h, ref, spec, boxes=verify.drawing_views(ref))
        png = align.overlay_png(ref, d)
        if d.status == "global_change":
            notes.insert(0, GLOBAL_CHANGE_NOTE)
    elif spec.diff == "tone":
        d = align.diff_tone(img, loc.h, ref, spec)
        png = align.tone_overlay_png(ref, d)
        if d.status == "global_change":
            notes.insert(0, PAIR_GLOBAL_NOTE if domain == "pair" else ART_GLOBAL_NOTE)
    else:
        d, png = None, align.location_png(ref, loc)
    if d is not None:
        diff = {
            "method": spec.diff,
            "status": d.status,
            "regions": [vars(r) for r in d.regions],
            "changed_ratio": d.changed_ratio,
        }
    result = {
        "target": {"kind": _KIND[domain], "id": tid},
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
