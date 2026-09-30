"""圖紙送進 Ortho2CAD 之前的前處理：只留三視圖區、轉成白底黑線、縮到訓練時的大小。

Ortho2CAD 訓練時的圖紙是 800×800、max_pixels=50176（→ 224×224），偏離這個分布準確率會掉。
"""

import math

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.cad.drawing import CANVAS


def smart_resize(h: int, w: int, factor: int = 32, min_pixels: int = 784, max_pixels: int = 50176):
    """Qwen2/3-VL 影像處理器的 smart_resize：邊長取 factor 的倍數，面積落在 [min, max] 之間。"""
    hb, wb = max(factor, round(h / factor) * factor), max(factor, round(w / factor) * factor)
    if hb * wb > max_pixels:
        beta = math.sqrt(h * w / max_pixels)
        hb = max(factor, math.floor(h / beta / factor) * factor)
        wb = max(factor, math.floor(w / beta / factor) * factor)
    elif hb * wb < min_pixels:
        beta = math.sqrt(min_pixels / (h * w))
        hb, wb = math.ceil(h * beta / factor) * factor, math.ceil(w * beta / factor) * factor
    return hb, wb


def binarize(img: Image.Image) -> Image.Image:
    """照片 → 白底黑線（自適應二值化去掉光線不均與紙張底色）。"""
    g = np.asarray(ImageOps.autocontrast(img.convert("L"), cutoff=1))
    bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 25, 15)
    return Image.fromarray(cv2.medianBlur(bw, 3)).convert("RGB")


def autocrop_square(img: Image.Image, margin: float = 0.06) -> Image.Image:
    """未收錄的圖紙：裁到有線條的範圍，補白成正方形，縮放到 800×800。"""
    g = np.asarray(img.convert("L"))
    ys, xs = np.where(g < 128)
    if len(xs) > 50:
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        img = img.crop((x0, y0, x1 + 1, y1 + 1))
    side = int(max(img.size) * (1 + 2 * margin))
    canvas = Image.new("RGB", (side, side), "white")
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)


def model_input(img: Image.Image, layout: str, max_pixels: int) -> Image.Image:
    """layout：kb＝知識庫圖紙（裁掉標題欄）、rectified＝已拉正的照片、upload＝未收錄的圖紙。"""
    if layout == "kb":
        img = img.convert("RGB").crop((0, 0, CANVAS, CANVAS))
    elif layout == "rectified":
        img = binarize(img.crop((0, 0, CANVAS, CANVAS)))
    else:
        img = autocrop_square(binarize(img))
    h, w = smart_resize(img.height, img.width, max_pixels=max_pixels)
    return img.resize((w, h), Image.Resampling.BICUBIC)
