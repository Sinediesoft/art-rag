"""畫作色彩分析（docs/adr/010）：主色盤、冷暖、明度／彩度分布、色塊分布圖。

只用 numpy：sRGB→CIELAB 與 CIEDE2000 自己實作（OpenCV 的 Lab 換算對浮點輸入是否做 gamma 校正，
文件寫得不清楚；自己寫才能對已知值測試）。k-means 的亂數種子固定，同一張圖每次算出來都一樣。
參數在 shared/models.yaml 的 color_analysis。
"""

import io

import numpy as np
from PIL import Image

from app.core.config import ColorAnalysisSpec, get_models_config

# sRGB（D65）→ XYZ 的矩陣與 D65 白點
_M = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_M_INV = np.linalg.inv(_M)
_WHITE = np.array([0.95047, 1.0, 1.08883])
_EPS, _KAPPA = 216 / 24389, 24389 / 27


def srgb_to_lab(rgb) -> np.ndarray:
    """(..., 3) 的 sRGB（0–255）→ CIELAB（L* 0–100）。"""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _M.T / _WHITE
    f = np.where(xyz > _EPS, np.cbrt(xyz), (_KAPPA * xyz + 16) / 116)
    return np.stack(
        [116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])],
        axis=-1,
    )


def lab_to_srgb(lab) -> np.ndarray:
    """CIELAB → sRGB uint8；超出 sRGB 色域的截到 0–255。"""
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], axis=-1)
    xyz = np.where(f**3 > _EPS, f**3, (116 * f - 16) / _KAPPA) * _WHITE
    lin = np.clip(xyz @ _M_INV.T, 0, 1)
    c = np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)
    return np.clip(np.round(c * 255), 0, 255).astype(np.uint8)


def ciede2000(lab1, lab2) -> np.ndarray:
    """CIEDE2000 色差（Sharma, Wu & Dalal 2005），兩邊可廣播。"""
    lab1, lab2 = np.asarray(lab1, dtype=np.float64), np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    c_bar = (np.hypot(a1, b1) + np.hypot(a2, b2)) / 2
    g = 0.5 * (1 - np.sqrt(c_bar**7 / (c_bar**7 + 25.0**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    zero = c1p * c2p == 0
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(zero, 0, dh)
    d_l, d_c = L2 - L1, c2p - c1p
    d_h = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    l_bar, cp_bar, hsum = (L1 + L2) / 2, (c1p + c2p) / 2, h1p + h2p
    h_bar = np.where(
        np.abs(h1p - h2p) <= 180, hsum / 2, (hsum + np.where(hsum < 360, 360, -360)) / 2
    )
    h_bar = np.where(zero, hsum, h_bar)
    t = (
        1
        - 0.17 * np.cos(np.radians(h_bar - 30))
        + 0.24 * np.cos(np.radians(2 * h_bar))
        + 0.32 * np.cos(np.radians(3 * h_bar + 6))
        - 0.20 * np.cos(np.radians(4 * h_bar - 63))
    )
    s_l = 1 + 0.015 * (l_bar - 50) ** 2 / np.sqrt(20 + (l_bar - 50) ** 2)
    s_c = 1 + 0.045 * cp_bar
    s_h = 1 + 0.015 * cp_bar * t
    r_t = (
        -2
        * np.sqrt(cp_bar**7 / (cp_bar**7 + 25.0**7))
        * np.sin(np.radians(60 * np.exp(-(((h_bar - 275) / 25) ** 2))))
    )
    return np.sqrt(
        (d_l / s_l) ** 2 + (d_c / s_c) ** 2 + (d_h / s_h) ** 2 + r_t * (d_c / s_c) * (d_h / s_h)
    )


# 基本色名與代表色（sRGB）。不用赭石、花青等顏料名：從圖檔的顏色推不出用了哪種顏料
COLOR_NAMES: list[tuple[str, tuple[int, int, int]]] = [
    ("黑", (26, 26, 26)), ("深灰", (77, 77, 77)), ("灰", (128, 128, 128)),
    ("淺灰", (179, 179, 179)), ("白", (245, 245, 245)), ("米白", (237, 228, 211)),
    ("米黃", (217, 196, 154)), ("土黃", (184, 145, 63)), ("褐", (139, 90, 43)),
    ("深褐", (74, 52, 36)), ("暗紅", (139, 26, 26)), ("紅", (212, 42, 42)),
    ("粉紅", (240, 160, 176)), ("橙", (232, 130, 42)), ("黃", (240, 210, 42)),
    ("黃綠", (154, 194, 60)), ("綠", (58, 154, 74)), ("深綠", (31, 77, 43)),
    ("灰綠", (125, 143, 110)), ("青", (42, 168, 176)), ("天藍", (122, 184, 224)),
    ("藍", (42, 92, 200)), ("深藍", (26, 42, 92)), ("紫", (122, 74, 160)),
]  # fmt: skip
_NAME_LAB = srgb_to_lab(np.array([rgb for _, rgb in COLOR_NAMES]))

TONE_PHRASE = {"dark": "以暗調為主", "mid": "以中間調為主", "light": "以亮調為主"}


def color_name(lab) -> str:
    """CIEDE2000 最接近的基本色名。"""
    d = ciede2000(np.asarray(lab, dtype=np.float64)[None], _NAME_LAB)
    return COLOR_NAMES[int(np.argmin(d))][0]


def temperature_masks(lab, spec: ColorAnalysisSpec):
    """回傳 (暖, 冷, 中性) 布林陣列：C* < neutral_chroma 為中性；
    色相落在 warm_hue_deg（可跨 0°）為暖，其餘有彩色為冷。"""
    lab = np.asarray(lab, dtype=np.float64)
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    hue = np.degrees(np.arctan2(lab[..., 2], lab[..., 1])) % 360
    lo, hi = spec.warm_hue_deg
    warm_hue = (hue >= lo) | (hue < hi) if lo > hi else (hue >= lo) & (hue < hi)
    neutral = chroma < spec.neutral_chroma
    return ~neutral & warm_hue, ~neutral & ~warm_hue, neutral


def _tone(lightness: float, spec: ColorAnalysisSpec) -> str:
    lo, hi = spec.lightness_bands
    return "dark" if lightness < lo else "mid" if lightness < hi else "light"


def _nearest(x: np.ndarray, centers: np.ndarray) -> np.ndarray:
    d = (x**2).sum(1)[:, None] - 2 * x @ centers.T + (centers**2).sum(1)[None]
    return d.argmin(1)


def kmeans(x: np.ndarray, k: int, max_iter: int, seed: int) -> np.ndarray:
    """k-means++ 初始化＋固定種子。相異顏色少於 k 時只回傳實際的群數。"""
    rng = np.random.default_rng(seed)
    centers = [x[rng.integers(len(x))]]
    d2 = ((x - centers[0]) ** 2).sum(1)
    for _ in range(1, k):
        if d2.sum() <= 1e-9:
            break
        i = rng.choice(len(x), p=d2 / d2.sum())
        centers.append(x[i])
        d2 = np.minimum(d2, ((x - x[i]) ** 2).sum(1))
    c = np.array(centers)
    for _ in range(max_iter):
        labels = _nearest(x, c)
        new = np.array(
            [x[labels == j].mean(0) if (labels == j).any() else c[j] for j in range(len(c))]
        )
        done = np.abs(new - c).max() < 1e-4
        c = new
        if done:
            break
    return c


def _resize(img: Image.Image, long_edge: int) -> Image.Image:
    im = img.convert("RGB")
    s = long_edge / max(im.size)
    if s < 1:
        size = (max(1, round(im.width * s)), max(1, round(im.height * s)))
        im = im.resize(size, Image.Resampling.LANCZOS)
    return im


def _share(mask: np.ndarray) -> float:
    return round(float(mask.mean()), 4)


def _histogram(v: np.ndarray) -> list[float]:
    counts, _ = np.histogram(np.clip(v, 0, 99.999), bins=10, range=(0, 100))
    return [round(float(x), 4) for x in counts / len(v)]


def summarize(temperature: dict, lightness: dict, chroma: dict, spec: ColorAnalysisSpec) -> str:
    """數字套固定規則產生總結句；畫面與問答段落共用。

    彩度用中位數：5 幅中 4 幅右偏；中位數不受少數鮮豔像素影響。"""
    t = temperature
    if t["neutral"] >= 0.5:
        temp = "以中性色為主"
    elif t["warm"] >= 2 * t["cool"]:
        temp = "整體偏暖"
    elif t["cool"] >= 2 * t["warm"]:
        temp = "整體偏冷"
    else:
        temp = "冷暖並陳"
    lo, hi = spec.chroma_bands
    m = chroma["median"]
    sat = "低彩度" if m < lo else "中等彩度" if m < hi else "高彩度"
    tone = max(("dark", "mid", "light"), key=lambda k: lightness[k])
    return f"{temp}、{sat}、{TONE_PHRASE[tone]}"


def _colormap_png(labels: np.ndarray, order: list[int], rgbs: np.ndarray) -> bytes:
    """每個像素塗成所屬主色的調色盤 PNG（只有主色數種顏色、無損）。"""
    rank = np.zeros(len(rgbs), dtype=np.uint8)
    rank[order] = np.arange(len(order), dtype=np.uint8)
    h, w = labels.shape
    im = Image.frombytes("P", (w, h), rank[labels].astype(np.uint8).tobytes())
    im.putpalette([int(v) for i in order for v in rgbs[i]])
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def analyze(img: Image.Image, spec: ColorAnalysisSpec | None = None) -> tuple[dict, bytes]:
    """回傳 (分析結果, 色塊分布圖 PNG)。結果不含 source／notes／map_url／latency_ms，由呼叫端補上。

    分群用長邊 fit_long_edge 的圖；占比、冷暖、明度、彩度與色塊圖都用長邊 map_long_edge 的圖
    逐像素統計，畫面上看到的面積和數字一致。"""
    spec = spec or get_models_config().color_analysis
    fit = srgb_to_lab(np.asarray(_resize(img, spec.fit_long_edge))).reshape(-1, 3)
    centers = kmeans(fit, spec.n_colors, spec.kmeans_max_iter, spec.seed)
    small = _resize(img, spec.map_long_edge)
    lab = srgb_to_lab(np.asarray(small)).reshape(-1, 3)
    labels = _nearest(lab, centers)
    counts = np.bincount(labels, minlength=len(centers))
    order = [int(i) for i in np.argsort(-counts, kind="stable") if counts[i] > 0]
    rgbs = lab_to_srgb(centers)

    palette = []
    for i in order:
        warm, cool, _ = temperature_masks(centers[i], spec)
        palette.append(
            {
                "hex": "#" + "".join(f"{int(v):02X}" for v in rgbs[i]),
                "rgb": [int(v) for v in rgbs[i]],
                "lab": [round(float(v), 2) for v in centers[i]],
                "share": round(float(counts[i] / len(labels)), 4),
                "name": color_name(centers[i]),
                "temperature": "warm" if warm else "cool" if cool else "neutral",
                "tone": _tone(float(centers[i][0]), spec),
            }
        )

    warm, cool, neutral = temperature_masks(lab, spec)
    lightness_v, chroma_v = lab[:, 0], np.hypot(lab[:, 1], lab[:, 2])
    (l1, l2), (c1, c2) = spec.lightness_bands, spec.chroma_bands
    temperature = {"warm": _share(warm), "cool": _share(cool), "neutral": _share(neutral)}
    lightness = {
        "dark": _share(lightness_v < l1),
        "mid": _share((lightness_v >= l1) & (lightness_v < l2)),
        "light": _share(lightness_v >= l2),
        "mean": round(float(lightness_v.mean()), 1),
        "p5": round(float(np.percentile(lightness_v, 5)), 1),
        "p95": round(float(np.percentile(lightness_v, 95)), 1),
        "histogram": _histogram(lightness_v),
    }
    chroma = {
        "low": _share(chroma_v < c1),
        "mid": _share((chroma_v >= c1) & (chroma_v < c2)),
        "high": _share(chroma_v >= c2),
        "median": round(float(np.median(chroma_v)), 1),
        "histogram": _histogram(chroma_v),
    }
    result = {
        "method": spec.method,
        "palette": palette,
        "temperature": temperature,
        "lightness": lightness,
        "chroma": chroma,
        "summary": summarize(temperature, lightness, chroma, spec),
    }
    return result, _colormap_png(labels.reshape(small.height, small.width), order, rgbs)
