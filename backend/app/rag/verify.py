"""以圖搜圖第二階段：局部特徵幾何驗證（ORB + RANSAC homography）。

Chinese-CLIP 的整體向量會把風格相近的畫（例如北宋山水）排得很近，分不出是不是「同一幅」；
同一幅畫的照片與原圖之間會有大量幾何一致的特徵點對應，不同畫作則幾乎沒有。

工廠圖紙也用同一套，但每張圖紙的版面都一樣：「Front/Top/Right View」標題、標題欄（格線、欄位名稱，
連材料「S45C 中碳鋼」這類文字都可能相同）、尺寸數字（都是 xx.0000）。實測不同圖紙會在這些地方
互相對上 300 個以上的點，所以知識庫圖紙只在三視圖的幾何線條區抽特徵點（drawing_mask）。
"""

import threading
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

_EDGE = 768
_lock = threading.Lock()

# 知識庫圖紙版面（800×970，見 app/cad/drawing.py）中要遮掉的區域：視圖標題、右側與下方的
# 尺寸標註帶（視圖幾何最多到 x=685、y=735）、整個標題欄
_DRAWING_MASKED = [
    (120, 60, 280, 100),
    (470, 60, 630, 100),
    (470, 410, 630, 450),
    (690, 0, 800, 800),
    (0, 740, 800, 800),
    (0, 800, 800, 970),
]


# 三視圖各自的方框（POSITIONS、VIEW，見 app/cad/drawing.py）：右視圖、前視圖、上視圖。
# 影像比對（docs/adr/012）只比這三格，照片的紙張邊緣、頁面留白不會被當成差異
_DRAWING_VIEWS = [(50, 100, 350, 400), (400, 100, 700, 400), (400, 450, 700, 750)]


def _scale(img: Image.Image) -> float:
    return min(1.0, _EDGE / max(img.size))


def _gray(img: Image.Image) -> np.ndarray:
    g = np.asarray(img.convert("L"))
    s = _scale(img)
    h, w = g.shape
    return cv2.resize(g, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else g


def drawing_mask(img: Image.Image) -> np.ndarray:
    s = _scale(img) * img.width / 800  # 版面座標 → 縮圖座標
    h, w = _gray(img).shape
    mask = np.full((h, w), 255, np.uint8)
    for x0, y0, x1, y1 in _DRAWING_MASKED:
        mask[int(y0 * s) : int(y1 * s), int(x0 * s) : int(x1 * s)] = 0
    return mask


@lru_cache(maxsize=1)
def _orb():
    return cv2.ORB_create(2000)


def features(img: Image.Image, mask: np.ndarray | None = None):
    with _lock:
        kp, desc = _orb().detectAndCompute(_gray(img), mask)
    return kp, desc, _scale(img)


@lru_cache(maxsize=64)
def kb_features(path: Path, mtime: float, drawing: bool = False):
    """知識庫圖片的特徵點（依檔案 mtime 快取，新增或替換圖片會自動重算）。"""
    img = Image.open(path)
    return features(img, drawing_mask(img) if drawing else None)


def _plausible(h: np.ndarray, pts: np.ndarray) -> bool:
    """排除退化的 homography：把點壓成一條線（det≈0）、放大上千倍、強烈透視，或對應點擠在一小塊。

    實測不同圖紙之間的假對應全是這幾種（det 0.00 或 1298），同一張圖紙的照片 det 約 0.3–3。
    """
    det = float(np.linalg.det(h[:2, :2]))
    if not 0.15 <= det <= 6 or max(abs(h[2, 0]), abs(h[2, 1])) > 0.003:
        return False
    span = pts.max(axis=0) - pts.min(axis=0)
    return bool(span.min() >= 80)


def match(query, ref, strict: bool = False) -> tuple[int, np.ndarray | None]:
    """回傳 (inlier 數, H)；H 把查詢圖（原始座標）對應到知識庫圖（原始座標）。

    strict=True（工廠圖紙）另外檢查 homography 是否合理，不合理就當作沒對上。
    """
    (kq, dq, sq), (kr, dr, sr) = query, ref
    if dq is None or dr is None or len(kq) < 8 or len(kr) < 8:
        return 0, None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(dq, dr, k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 8:
        return 0, None
    src = np.float32([kq[m.queryIdx].pt for m in good])
    dst = np.float32([kr[m.trainIdx].pt for m in good])
    h, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if mask is None or h is None:
        return 0, None
    full = np.diag([1 / sr, 1 / sr, 1.0]) @ h @ np.diag([sq, sq, 1.0])
    if strict and not _plausible(full, dst[mask.ravel() == 1] / sr):
        return 0, None
    return int(mask.sum()), full


def count_inliers(query, ref, strict: bool = False) -> int:
    return match(query, ref, strict)[0]


def rectify(
    gray: np.ndarray, h: np.ndarray, size: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """用 H 把照片（灰階）拉正到參考圖的座標，回傳 (拉正後的圖, 照片涵蓋的範圍)。

    照片外面填白；涵蓋範圍往內縮 4 px，避開照片邊緣內插出來的灰邊。
    """
    warped = cv2.warpPerspective(gray, h, size, borderValue=255)
    covered = cv2.warpPerspective(np.full(gray.shape, 255, np.uint8), h, size, borderValue=0)
    return warped, cv2.erode(covered, np.ones((9, 9), np.uint8)).astype(bool)


def drawing_region(ref_img: Image.Image) -> np.ndarray:
    """知識庫圖紙（原始大小）上的幾何線條區：drawing_mask 遮掉標題、尺寸標註帶、標題欄。"""
    ww, hh = ref_img.size
    return cv2.resize(
        (drawing_mask(ref_img) > 0).astype(np.uint8), (ww, hh), interpolation=cv2.INTER_NEAREST
    ).astype(bool)


def drawing_views(ref_img: Image.Image) -> list[tuple[int, int, int, int]]:
    """三視圖的方框（x0, y0, x1, y1），換算到這張圖紙的像素座標。"""
    s = ref_img.width / 800
    return [tuple(round(v * s) for v in box) for box in _DRAWING_VIEWS]


def photo_ink(warped: np.ndarray, c: int = 20) -> np.ndarray:
    """拉正後照片的線條：照片受光線影響，用自適應門檻（比周圍 25 px 的平均暗 c 以上）。"""
    return cv2.adaptiveThreshold(
        warped, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, c
    ).astype(bool)


def ref_ink(gray: np.ndarray) -> np.ndarray:
    """知識庫圖紙的線條：乾淨的白底黑線，固定門檻即可。"""
    return gray < 160


def ink_layers(
    img: Image.Image, h: np.ndarray, ref_img: Image.Image
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """照片拉正到知識庫圖紙後，回傳 (照片的線條, 圖紙的線條, 比對範圍)，都是圖紙大小的布林陣列。

    比對範圍＝照片涵蓋的地方 ∩ 三視圖的幾何線條區（drawing_region）。
    """
    ref = np.asarray(ref_img.convert("L"))
    warped, covered = rectify(np.asarray(img.convert("L")), h, ref_img.size)
    valid = covered & drawing_region(ref_img)
    return photo_ink(warped) & valid, ref_ink(ref) & valid, valid


def ink_overlap(img: Image.Image, h: np.ndarray, ref_path: Path) -> float:
    """圖紙第三道驗證：用 H 把照片拉正到知識庫圖紙，比對三視圖區的線條是否重合（F1，0–1）。

    幾何驗證只看特徵點；不同圖紙偶爾會有幾十個點碰巧對上，但線條整體不會重合。
    實測同一張圖紙的照片 ≥ 0.60，不同圖紙 ≤ 0.54（且其中 inlier ≥ 25 者 ≤ 0.48）。
    """
    q_ink, r_ink, _ = ink_layers(img, h, Image.open(ref_path))
    if r_ink.sum() < 200 or q_ink.sum() < 200:
        return 0.0
    k = np.ones((5, 5), np.uint8)
    recall = (r_ink & cv2.dilate(q_ink.astype(np.uint8), k).astype(bool)).sum() / r_ink.sum()
    precision = (q_ink & cv2.dilate(r_ink.astype(np.uint8), k).astype(bool)).sum() / q_ink.sum()
    return float(2 * precision * recall / (precision + recall + 1e-9))
