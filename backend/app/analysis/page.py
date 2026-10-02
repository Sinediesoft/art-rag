"""照片建檔的影像前處理（docs/adr/013）：清晰度、找紙張四角拉正、整理成知識庫圖紙的樣子。

只用 numpy＋OpenCV，不碰索引與模型。
"""

import cv2
import numpy as np
from PIL import Image

from app.cad.drawing import CANVAS, TITLE_H

WORK_LONG_EDGE = 1024  # 和上傳照片的長邊相同（retrieval.preprocess_long_edge）
# 知識庫圖紙標題欄的外框（800×970 版面上的 x0, y0, x1, y1），同 drawing.with_title_block 的邊距
TITLE_BOX = (24, CANVAS + 10, CANVAS - 24, CANVAS + TITLE_H - 18)


def _gray(img: Image.Image) -> np.ndarray:
    g = img.convert("L")
    if max(g.size) > WORK_LONG_EDGE:
        g = g.copy()
        g.thumbnail((WORK_LONG_EDGE, WORK_LONG_EDGE))
    return np.asarray(g)


def blur_score(img: Image.Image, k: int = 9) -> float:
    """模糊程度 0（清楚）～1（模糊）：再模糊一次，看相鄰像素的差異還剩多少沒被抹掉
    （Crété-Roffet 等人 2007 的無參考模糊指標；長邊 1024、水平與垂直取較模糊的一邊）。

    已經糊掉的照片再模糊也變化不大，所以分數高；和畫面內容、明暗、對比無關，
    圖紙與畫作可以用同一個門檻。拉普拉斯變異數在畫作上分不開：
    〈谿山行旅圖〉清楚但偏暗的照片只有 125，模糊照最高 294。
    2026-10-03 模擬照：模糊 0.345–0.448；其他拍法與知識庫原圖，畫作 ≤ 0.265、圖紙 ≤ 0.143。
    """
    f = _gray(img).astype(np.float64)
    worst = 0.0
    for axis, size in ((0, (1, k)), (1, (k, 1))):
        d = np.abs(np.diff(f, axis=axis))
        db = np.abs(np.diff(cv2.blur(f, size), axis=axis))
        total = d.sum()
        if total > 0:
            worst = max(worst, float((total - np.maximum(0.0, d - db).sum()) / total))
    return worst


def _order(q: np.ndarray) -> np.ndarray:
    """四個角排成左上、右上、右下、左下。"""
    s, d = q.sum(1), np.diff(q, axis=1).ravel()
    corners = [q[np.argmin(s)], q[np.argmin(d)], q[np.argmax(s)], q[np.argmax(d)]]
    return np.array(corners, np.float32)


def find_page(img: Image.Image, min_area: float = 0.2) -> np.ndarray | None:
    """照片裡最大的紙張四邊形（左上、右上、右下、左下，照片原始座標）；找不到回 None。

    紙張比背景亮：Otsu 二值化 → 閉運算補掉紙上的線條 → 最大外輪廓 → 近似成凸四邊形。
    整張照片就是紙（近拍、掃描）時找到的是畫面四角。紙張要占畫面 min_area 以上。
    """
    a = _gray(img)
    scale = img.width / a.shape[1]
    blur = cv2.GaussianBlur(a, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    c = max(contours, key=cv2.contourArea)
    if cv2.contourArea(c) < min_area * a.shape[0] * a.shape[1]:
        return None
    peri = cv2.arcLength(c, True)
    for eps in (0.01, 0.02, 0.03, 0.05):
        q = cv2.approxPolyDP(c, eps * peri, True)
        if len(q) == 4 and cv2.isContourConvex(q):
            return _order(q.reshape(4, 2).astype(np.float32) * scale)
    return None


def aspect(quad: np.ndarray) -> float:
    """紙張的高／寬（取兩組對邊較長的那條）。"""
    tl, tr, br, bl = quad
    w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    h = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    return float(h / max(w, 1e-6))


def rectify(img: Image.Image, quad: np.ndarray, size: tuple[int, int]) -> Image.Image:
    """依四個角把紙張拉正成 size（寬, 高）。"""
    w, h = size
    dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    m = cv2.getPerspectiveTransform(quad.astype(np.float32), dst)
    out = cv2.warpPerspective(
        np.asarray(img.convert("RGB")), m, (w, h), borderValue=(255, 255, 255)
    )
    return Image.fromarray(out)


def title_block_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """拉正後的圖紙上，標題欄外框的位置（x0, y0, x1, y1）；找不到回 None。

    外框是下方最大的一個閉合長方形：寬度超過紙寬 70%、落在下面 40%、高度是紙高的 8–30%。
    外框貼到圖的左、右、下緣就不算：標題欄被裁掉一塊，模型會抄出半截的字
    （2026-10-03 make eval-intake：一張裁切照的公司讀成「容機械（虛構）」、
    品名讀成「型槽螺帽」，格式都對、規則擋不下）。
    """
    a = np.asarray(img.convert("L"))
    h, w = a.shape
    ink = (a < 160).astype(np.uint8) * 255
    contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    edge = 3
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        clipped = x < edge or x + bw > w - edge or y + bh > h - edge
        fits = bw > 0.7 * w and y > 0.6 * h and 0.08 * h < bh < 0.3 * h and not clipped
        if fits and (best is None or bw * bh > best[2] * best[3]):
            best = (x, y, bw, bh)
    if best is None:
        return None
    x, y, bw, bh = best
    return x, y, x + bw, y + bh


def fit_layout(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image | None:
    """縮放、平移，讓標題欄外框對上知識庫版面的 TITLE_BOX。

    縮放比例不合理（不是同一種版面，或標題欄沒拍完整）回 None。
    """
    x0, y0, x1, y1 = box
    X0, Y0, X1, Y1 = TITLE_BOX
    sx, sy = (X1 - X0) / max(x1 - x0, 1), (Y1 - Y0) / max(y1 - y0, 1)
    if not (0.75 < sx < 1.33 and 0.75 < sy < 1.33 and abs(sx - sy) < 0.12):
        return None
    m = np.float32([[sx, 0, X0 - x0 * sx], [0, sy, Y0 - y0 * sy]])
    out = cv2.warpAffine(
        np.asarray(img.convert("RGB")),
        m,
        (CANVAS, CANVAS + TITLE_H),
        borderValue=(255, 255, 255),
    )
    return Image.fromarray(out)


def clean_drawing(img: Image.Image, paper: float = 215) -> Image.Image:
    """拉正後的照片 → 白底黑線，接近 make drawings 產生的圖紙。

    除以大範圍模糊去掉陰影、反光與暗角；比紙色暗不到約 15%（正規化後 ≥ paper）的當成白紙，
    其餘壓到 160 以下：辨識拉正後比線條時，參考圖用 gray < 160 判斷哪裡有線（verify.ref_ink）。
    """
    g = np.asarray(img.convert("L"), dtype=np.float32)
    bg = cv2.GaussianBlur(g, (0, 0), 21)
    norm = np.clip(g / np.maximum(bg, 1.0) * 255.0, 0, 255)
    out = np.where(norm >= paper, 255.0, norm * (159.0 / paper))
    return Image.fromarray(out.astype(np.uint8)).convert("RGB")
