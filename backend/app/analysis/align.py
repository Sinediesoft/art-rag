"""影像對位與比對（docs/adr/012）：照片拍到參考圖的哪一塊、拉正後的線條哪裡不一樣。
只用 numpy 與 OpenCV。

照片和參考圖（知識庫畫作原圖、工廠圖紙）的對應關係 H 由 verify.match() 的 ORB＋RANSAC 求出。
畫作、圖紙都是平面，單應矩陣 H 能把照片上每一點準確對到參考圖；立體實物不在範圍內。
這個模組不碰索引與資料庫，領域差異（要不要比線條、門檻）由 models.yaml 的 image_compare 傳進來。
"""

import io
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

from app.core.config import CompareSpec
from app.rag import verify

# 照片在參考圖上的面積（相對參考圖）超出這個範圍就不合理：太小是特徵點湊巧對上，太大是退化的 H
_AREA_RANGE = (0.002, 25.0)


@dataclass
class Location:
    inliers: int
    polygon: list[list[float]]  # 照片四角（左上、右上、右下、左下）在參考圖上的位置，0–1，可超出
    coverage: float  # 照片拍到參考圖面積的比例 0–1
    center: list[float]  # 拍到的範圍的中心，0–1
    h: np.ndarray = field(repr=False)  # 照片座標 → 參考圖座標


def polygon_from_h(h: np.ndarray, photo_size, ref_size) -> np.ndarray | None:
    """照片四角投到參考圖（像素座標）；鏡像、跑到相機後面、不是凸四邊形或大小離譜時回 None。"""
    if np.linalg.det(h[:2, :2]) <= 0:  # 鏡像：位置框會左右顛倒
        return None
    w, hh = photo_size
    corners = np.array([[0, 0, 1], [w, 0, 1], [w, hh, 1], [0, hh, 1]], float)
    p = corners @ h.T
    if (p[:, 2] <= 1e-9).any():
        return None
    poly = (p[:, :2] / p[:, 2:]).astype(np.float32)
    if not cv2.isContourConvex(poly):
        return None
    area = cv2.contourArea(poly) / (ref_size[0] * ref_size[1])
    return poly if _AREA_RANGE[0] <= area <= _AREA_RANGE[1] else None


def locate(
    query_feats, ref_feats, photo_size, ref_size, *, min_inliers: int, strict: bool = False
) -> Location | None:
    """照片在參考圖上的位置；對應點不夠或位置不合理就回 None（不硬畫）。

    strict=True（圖紙）另外排除退化的 H（verify._plausible）；畫作不能用那套：
    拍一小塊的近照縮放比例很小，會被當成退化。
    """
    inliers, h = verify.match(query_feats, ref_feats, strict)
    if h is None or inliers < min_inliers:
        return None
    poly = polygon_from_h(h, photo_size, ref_size)
    if poly is None:
        return None
    rw, rh = ref_size
    rect = np.float32([[0, 0], [rw, 0], [rw, rh], [0, rh]])
    area, inter = cv2.intersectConvexConvex(poly, rect)
    if area <= 0 or inter is None:
        return None
    center = inter.reshape(-1, 2).mean(axis=0) / [rw, rh]
    return Location(
        inliers=inliers,
        polygon=np.round(poly / [rw, rh], 4).tolist(),
        coverage=round(min(1.0, float(area) / (rw * rh)), 4),
        center=np.round(center, 4).tolist(),
        h=h,
    )


# ---------------------------------------------------------------- 圖紙：線條差異
@dataclass
class Region:
    bbox: list[float]  # x0, y0, x1, y1（0–1，圖紙座標）
    kind: str  # missing：圖紙有、照片沒有；extra：照片有、圖紙沒有；both：兩種都有
    area_ratio: float  # 這處差異的像素占圖紙線條像素的比例


@dataclass
class InkDiff:
    status: str  # same／changed／global_change
    regions: list[Region]
    changed_ratio: float  # 所有差異像素占圖紙線條像素的比例
    missing: np.ndarray = field(repr=False)
    extra: np.ndarray = field(repr=False)
    compared: np.ndarray = field(repr=False)  # 有比對的範圍（照片涵蓋 ∩ 三視圖區 − 最外圈）


def _open(mask: np.ndarray) -> np.ndarray:
    """「多出」用：開運算去掉 1 px 寬的雜訊。照片拉正後，線條邊緣常沿著線多出一長條 1 px 的鋸齒，
    只看面積去不掉。"""
    return cv2.morphologyEx(
        mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)
    ).astype(bool)


def _drop_specks(mask: np.ndarray, min_px: int = 6) -> np.ndarray:
    """「缺少」用：只去掉很小的碎點。缺少的線條取自乾淨的知識庫圖紙，尺寸線只有 1 px 寬，
    用開運算會把整條缺少的細線抹掉。"""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= min_px
    return keep[labels]


def _merge(boxes: list[list[int]], gap: int) -> list[list[int]]:
    """相距 gap 以內的區塊併成一處（同一個改動常是好幾條斷開的線，例如孔的隱藏線）。"""
    boxes = [b[:] for b in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if (
                    a[0] - gap <= b[2]
                    and b[0] - gap <= a[2]
                    and a[1] - gap <= b[3]
                    and b[1] - gap <= a[3]
                ):
                    boxes[i] = [
                        min(a[0], b[0]),
                        min(a[1], b[1]),
                        max(a[2], b[2]),
                        max(a[3], b[3]),
                        a[4] + b[4],
                        a[5] + b[5],
                    ]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return boxes


def _refine(
    ref_g: np.ndarray, warped: np.ndarray, covered: np.ndarray, spec: CompareSpec
) -> np.ndarray | None:
    """一格視圖的 ECC 剛體微調（平移＋小角度旋轉），回傳把照片對到圖紙的 2×3 矩陣；
    沒收斂或超出範圍就回 None（照原本的 H）。

    整張共用一個 H 時，紙張彎曲、鏡頭變形會讓不同視圖各差幾 px。只准平移與旋轉、不准縮放：
    外形尺寸小改時三視圖的比例只差 1–3%，仿射微調會把它修平（實測 2% 被修成「沒有差異」）。
    """
    if covered.sum() < 1000:
        return None
    t = cv2.GaussianBlur(ref_g, (0, 0), 2).astype(np.float32)
    i = cv2.GaussianBlur(warped, (0, 0), 2).astype(np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
    try:
        _, w = cv2.findTransformECC(
            t,
            i,
            np.eye(2, 3, dtype=np.float32),
            cv2.MOTION_EUCLIDEAN,
            criteria,
            covered.astype(np.uint8),
            5,
        )
    except cv2.error:
        return None
    if (
        np.abs(w[:, :2] - np.eye(2)).max() > spec.refine_max_linear
        or np.abs(w[:, 2]).max() > spec.refine_max_shift_px
    ):
        return None
    return w


def diff_ink(
    photo: Image.Image,
    h: np.ndarray,
    ref_img: Image.Image,
    spec: CompareSpec,
    boxes: list[tuple[int, int, int, int]] | None = None,
) -> InkDiff:
    """照片拉正到圖紙後比線條：圖紙有、照片沒有（missing）與照片有、圖紙沒有（extra）。

    - boxes：只比這幾格（圖紙是三視圖的方框，verify.drawing_views），每格各自微調位置；
      沒給就比整張、不微調。
    - 判「缺少」用寬鬆的門檻（faint_ink_c，模糊照片裡變淡的點線也算還在），判「多出」用嚴格的門檻。
    - 差異超過圖紙線條的 max_changed_ratio 時回 global_change、不列區塊：三視圖依外形尺寸縮放
      （app/cad/drawing.py 的 _scale），外形一改整張都會不同，列出幾十處沒有意義。
    """
    ref_g = np.asarray(ref_img.convert("L"))
    hh, ww = ref_g.shape
    warped, covered = verify.rectify(np.asarray(photo.convert("L")), h, (ww, hh))
    m = spec.edge_margin_px
    inner = np.zeros((hh, ww), bool)
    inner[m : hh - m, m : ww - m] = True
    missing, extra, compared = (np.zeros((hh, ww), bool) for _ in range(3))
    k = np.ones((2 * spec.tolerance_px + 1,) * 2, np.uint8)
    ink_total = 0
    for x0, y0, x1, y1 in boxes or [(0, 0, ww, hh)]:
        # 多取一圈，微調時有東西可以移進來；比完只留方框裡面
        p = spec.refine_max_shift_px
        X0, Y0, X1, Y1 = max(x0 - p, 0), max(y0 - p, 0), min(x1 + p, ww), min(y1 + p, hh)
        rg, wg, cv = ref_g[Y0:Y1, X0:X1], warped[Y0:Y1, X0:X1], covered[Y0:Y1, X0:X1]
        # 只有分格時才微調：整張一起調會被格子外的東西（紙張邊緣）拉偏
        a = _refine(rg, wg, cv, spec) if boxes else None
        if a is not None:
            size = (X1 - X0, Y1 - Y0)
            wg = cv2.warpAffine(
                wg, a, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=255
            )
            cv = cv2.warpAffine(
                cv.astype(np.uint8),
                a,
                size,
                flags=cv2.INTER_NEAREST | cv2.WARP_INVERSE_MAP,
                borderValue=0,
            ).astype(bool)
        valid = cv & inner[Y0:Y1, X0:X1]
        r = verify.ref_ink(rg) & valid
        strict = verify.photo_ink(wg) & valid
        faint = verify.photo_ink(wg, spec.faint_ink_c) & valid
        miss = r & ~cv2.dilate(faint.astype(np.uint8), k).astype(bool)
        ext = strict & ~cv2.dilate(r.astype(np.uint8), k).astype(bool)
        box = (slice(y0 - Y0, y1 - Y0), slice(x0 - X0, x1 - X0))
        missing[y0:y1, x0:x1] |= _drop_specks(miss)[box]
        extra[y0:y1, x0:x1] |= _open(ext)[box]
        compared[y0:y1, x0:x1] |= valid[box]
        ink_total += int(r[box].sum())

    ink_total = max(ink_total, 1)
    changed = missing | extra
    changed_ratio = round(float(changed.sum()) / ink_total, 4)
    if changed_ratio > spec.max_changed_ratio:
        return InkDiff("global_change", [], changed_ratio, missing, extra, compared)

    blobs = cv2.dilate(changed.astype(np.uint8), np.ones((spec.merge_px, spec.merge_px), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(blobs)
    boxes = []
    for i in range(1, n):
        comp = labels == i
        x, y, w, hgt = (int(v) for v in stats[i, :4])
        boxes.append([x, y, x + w, y + hgt, int((missing & comp).sum()), int((extra & comp).sum())])
    regions = []
    for x0, y0, x1, y1, mi, ex in sorted(
        _merge(boxes, spec.merge_gap_px), key=lambda b: -(b[4] + b[5])
    ):
        if mi + ex < spec.min_region_px:
            continue
        kind = "missing" if ex < 0.2 * (mi + ex) else "extra" if mi < 0.2 * (mi + ex) else "both"
        regions.append(
            Region(
                bbox=[round(x0 / ww, 4), round(y0 / hh, 4), round(x1 / ww, 4), round(y1 / hh, 4)],
                kind=kind,
                area_ratio=round((mi + ex) / ink_total, 4),
            )
        )
    return InkDiff(
        "changed" if regions else "same", regions, changed_ratio, missing, extra, compared
    )


# ---------------------------------------------------------------- 疊圖
_RED, _BLUE, _AMBER = (220, 38, 38), (37, 99, 235), (217, 119, 6)
_KIND_COLOR = {"missing": _RED, "extra": _BLUE, "both": (147, 51, 234)}


def _png(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb.astype(np.uint8)).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def overlay_png(ref_img: Image.Image, d: InkDiff) -> bytes:
    """圖紙淡化成淺灰，差異塗紅（圖紙有、照片沒有）或藍（照片有、圖紙沒有），每處框起來並編號；
    沒有比對的地方（標題欄、尺寸標註帶、照片沒拍到的範圍）再淡一層。"""
    g = np.asarray(ref_img.convert("L")).astype(np.float32)
    base = 255 - (255 - g) * 0.4
    base = np.where(d.compared, base, 255 - (255 - base) * 0.5)
    rgb = np.repeat(base[..., None], 3, axis=2)
    thick = np.ones((3, 3), np.uint8)
    rgb[cv2.dilate(d.missing.astype(np.uint8), thick).astype(bool)] = _RED
    rgb[cv2.dilate(d.extra.astype(np.uint8), thick).astype(bool)] = _BLUE
    rgb = np.ascontiguousarray(rgb.astype(np.uint8))
    hh, ww = g.shape
    for i, r in enumerate(d.regions, 1):
        x0, y0, x1, y1 = (
            int(round(v)) for v in (r.bbox[0] * ww, r.bbox[1] * hh, r.bbox[2] * ww, r.bbox[3] * hh)
        )
        color = _KIND_COLOR[r.kind]
        cv2.rectangle(rgb, (x0 - 6, y0 - 6), (x1 + 6, y1 + 6), color, 2)
        cv2.putText(
            rgb,
            str(i),
            (x0 - 6, max(y0 - 10, 14)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2,
            cv2.LINE_AA,
        )
    return _png(rgb)


def location_png(ref_img: Image.Image, loc: Location) -> bytes:
    """畫作：原圖上框出照片拍到的範圍，範圍外調暗。"""
    rgb = np.asarray(ref_img.convert("RGB")).astype(np.float32)
    hh, ww = rgb.shape[:2]
    poly = np.round(np.array(loc.polygon) * [ww, hh]).astype(np.int32)
    inside = np.zeros((hh, ww), np.uint8)
    cv2.fillConvexPoly(inside, poly, 1)
    rgb[inside == 0] *= 0.35
    rgb = np.ascontiguousarray(rgb.astype(np.uint8))
    cv2.polylines(rgb, [poly], True, _AMBER, max(2, round(max(ww, hh) / 300)), cv2.LINE_AA)
    return _png(rgb)


# ---------------------------------------------------------------- 照片比照片：形狀與顏色（畫作）
@dataclass
class ToneDiff:
    status: str  # same／changed／global_change
    regions: list[Region]  # kind：shape（形狀不同）／color（顏色不同）／both
    changed_ratio: float  # 差異面積占比對範圍的比例
    shape: np.ndarray = field(repr=False)  # 以下都是參考圖（照片 A）原始大小的布林陣列
    color: np.ndarray = field(repr=False)
    compared: np.ndarray = field(repr=False)


_PAD_DARK = 12  # 照片四邊相連、每個色版都不超過這個值的區塊，是照片自己的黑邊


def _padding(src: np.ndarray) -> np.ndarray:
    """照片自己的黑邊（拍歪拉正後補的黑色、截圖的黑框）→ True：和照片四邊相連、近乎全黑的區塊。
    畫上的墨色很少黑到這個程度，又剛好連到照片邊緣；就算是，也只是那一塊不比，不會報假差異。"""
    dark = (src.max(axis=2) <= _PAD_DARK).astype(np.uint8)
    _, labels = cv2.connectedComponents(dark)
    edge = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    return np.isin(labels, edge[edge > 0])


def _mblur(x: np.ndarray, m: np.ndarray, sigma: float) -> np.ndarray:
    """只用遮罩內的像素做高斯平均（遮罩外的像素完全不參與）。x 可以是 (H, W) 或 (H, W, C)。"""
    w = cv2.GaussianBlur(m, (0, 0), sigma)
    num = cv2.GaussianBlur(x * (m[..., None] if x.ndim == 3 else m), (0, 0), sigma)
    return num / ((w[..., None] if x.ndim == 3 else w) + 1e-6)


def _ssim(a: np.ndarray, b: np.ndarray, m: np.ndarray, sigma: float = 3.0) -> np.ndarray:
    def g(x):
        return _mblur(x, m, sigma)

    ma, mb = g(a), g(b)
    va, vb, cov = g(a * a) - ma * ma, g(b * b) - mb * mb, g(a * b) - ma * mb
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2))


def _local_norm(gray: np.ndarray, m: np.ndarray, sigma: float) -> np.ndarray:
    """局部亮度、對比正規化：去掉光線不均、整體偏暗，只留下筆觸與形狀。只用拍到的像素算，
    沒拍到的地方（補黑）若算進來，在素色牆面這種平坦處會被放大成一圈假的差異。"""
    mean = _mblur(gray, m, sigma)
    s = np.sqrt(np.maximum(_mblur((gray - mean) ** 2, m, sigma), 0)) + 8
    return np.clip((gray - mean) / s * 40 + 128, 0, 255).astype(np.float32)


_TONE_SHIFT_PX = 1  # 比形狀時容許錯開幾 px（工作大小）
# 參考圖要模糊多少才和照片一樣清楚：一個一個試，取和照片最像的（ECC 相關係數最高）
_BLUR_STEPS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)


def _shift(x: np.ndarray, dx: int, dy: int) -> np.ndarray:
    return np.roll(np.roll(x, dy, axis=0), dx, axis=1)


def _match_blur(a_g: np.ndarray, b_g: np.ndarray, covered: np.ndarray) -> np.ndarray:
    """參考圖模糊到和照片一樣清楚。照片拍不出比自己更細的筆觸（手震、失焦、拍照後縮小），
    原圖上那些細節在照片裡糊掉不是畫改了；照片上新加的筆畫在照片裡，不受影響。"""
    m = covered.astype(np.uint8)
    if not m.any():
        return a_g
    best, sigma = cv2.computeECC(a_g, b_g, m), 0.0
    for s in _BLUR_STEPS:
        c = cv2.computeECC(cv2.GaussianBlur(a_g, (0, 0), s), b_g, m)
        if c > best:
            best, sigma = c, s
    return cv2.GaussianBlur(a_g, (0, 0), sigma) if sigma else a_g


# 對位微調由粗到細：先在 1/4 大小只找平移，再到 1/2 找仿射，最後原大小找單應。
# 直接在原大小找單應，差到十幾 px 時不收斂（實測霧多、特徵點擠在一角的近照）
_TONE_REFINE = (
    (0.25, cv2.MOTION_TRANSLATION),
    (0.5, cv2.MOTION_AFFINE),
    (1.0, cv2.MOTION_HOMOGRAPHY),
)
_TONE_REFINE_MAX = 0.06  # 微調最多移動比對範圍四角多少（工作大小長邊的比例），超過就當沒調好


def _refine_tone(
    a_g: np.ndarray, src: np.ndarray, valid: np.ndarray, hs: np.ndarray, size: tuple[int, int]
) -> np.ndarray:
    """照片對到參考圖之後再用 ECC 微調，回傳新的 hs（照片 → 工作大小的參考圖）。

    辨識的 H 只靠特徵點，特徵點少的地方（霧、素色的絹）會差幾 px 到十幾 px，
    SSIM 一錯位整片都像「形狀不同」。對得更準（ECC 相關係數變高）才採用，不然照原本的。
    """
    b_g = cv2.cvtColor(cv2.warpPerspective(src, hs, size), cv2.COLOR_RGB2GRAY).astype(np.float32)
    cov = cv2.erode(cv2.warpPerspective(valid, hs, size), np.ones((9, 9), np.uint8))
    if np.count_nonzero(cov) < 2000:
        return hs

    def level(x, k, interp=cv2.INTER_AREA):
        return cv2.resize(
            x, (max(8, round(size[0] * k)), max(8, round(size[1] * k))), interpolation=interp
        )

    w = np.eye(3)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-6)
    for k, motion in _TONE_REFINE:
        s = np.diag([k, k, 1.0])
        init = s @ w @ np.linalg.inv(s)
        init = init if motion == cv2.MOTION_HOMOGRAPHY else init[:2]
        try:
            _, r = cv2.findTransformECC(
                cv2.GaussianBlur(level(a_g, k), (0, 0), 1.0),
                cv2.GaussianBlur(level(b_g, k), (0, 0), 1.0),
                init.astype(np.float32),
                motion,
                criteria,
                (level(cov, k, cv2.INTER_NEAREST) > 0).astype(np.uint8),
                3,
            )
        except cv2.error:  # 這一層沒收斂：沿用上一層的結果
            continue
        r = r.astype(np.float64)
        w = np.linalg.inv(s) @ (r if r.shape[0] == 3 else np.vstack([r, [0, 0, 1]])) @ s
    ys, xs = np.nonzero(cov)
    box = np.float32(
        [[[xs.min(), ys.min()], [xs.max(), ys.min()], [xs.max(), ys.max()], [xs.min(), ys.max()]]]
    )
    if np.abs(cv2.perspectiveTransform(box, w) - box).max() > _TONE_REFINE_MAX * max(size):
        return hs
    refined = np.linalg.inv(w) @ hs
    b2 = cv2.cvtColor(cv2.warpPerspective(src, refined, size), cv2.COLOR_RGB2GRAY).astype(
        np.float32
    )
    m = (cov > 0).astype(np.uint8)
    t = cv2.GaussianBlur(a_g, (0, 0), 1.0)
    before = cv2.computeECC(t, cv2.GaussianBlur(b_g, (0, 0), 1.0), m)
    after = cv2.computeECC(t, cv2.GaussianBlur(b2, (0, 0), 1.0), m)
    return refined if after > before else hs


def diff_tone(
    photo: Image.Image, h: np.ndarray, ref_img: Image.Image, spec: CompareSpec
) -> ToneDiff:
    """照片（B）對齊到參考圖（ref_img：知識庫原圖，或兩張照片互比時的照片 A）之後比兩件事，
    只比兩張都拍到的範圍：

    - 形狀：灰階做局部亮度正規化後的 SSIM（加筆、補筆、塗糊、多了東西）；
    - 顏色：B 的整體色彩先拉齊參考圖（L*、a*、b* 對齊平均與幅度，吸收光線、白平衡、整張變淡），
      再算 Lab 色差（褪色、補色）。觀眾照片 vs 原圖的光線差比較多，models.yaml 給較高的門檻。

    比之前先排除不是畫本身的差異：照片自己的黑邊、辨識給的位置差幾 px（ECC 微調）、
    照片比原圖模糊（原圖模糊到一樣清楚）、參考圖最外圈（edge_margin_px）。
    在長邊 work_long_edge 的大小比：再細會被照片雜訊、筆觸的細微錯位干擾。
    """
    from app.analysis.color import srgb_to_lab

    W, H = ref_img.size
    s = min(1.0, spec.work_long_edge / max(W, H))
    size = (round(W * s), round(H * s))
    hs = np.diag([s, s, 1.0]) @ h
    # 照片先縮到差不多的大小再投影：warpPerspective 只有線性內插，直接大幅縮小會有疊紋
    f = s * float(np.sqrt(abs(np.linalg.det(h[:2, :2]))))
    src = np.asarray(photo.convert("RGB"))
    valid = np.where(_padding(src), 0, 255).astype(np.uint8)  # 照片上真的拍到東西的地方
    if f < 0.9:
        dsize = (round(src.shape[1] * f), round(src.shape[0] * f))
        src = cv2.resize(src, dsize, interpolation=cv2.INTER_AREA)
        valid = np.where(cv2.resize(valid, dsize, interpolation=cv2.INTER_AREA) > 250, 255, 0)
        valid = valid.astype(np.uint8)
        hs = hs @ np.diag([1 / f, 1 / f, 1.0])
    a_rgb = np.asarray(ref_img.convert("RGB").resize(size, Image.Resampling.LANCZOS))

    def gray(x):
        return cv2.cvtColor(x, cv2.COLOR_RGB2GRAY).astype(np.float32)

    hs = _refine_tone(gray(a_rgb), src, valid, hs, size)
    b_rgb = cv2.warpPerspective(src, hs, size, flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
    raw = cv2.warpPerspective(valid, hs, size, borderValue=0)
    m = (
        cv2.erode(raw, np.ones((3, 3), np.uint8)).astype(np.float32) / 255
    )  # 濾波時的權重：拍到的像素
    covered = cv2.erode(raw, np.ones((9, 9), np.uint8)).astype(bool)  # 比對範圍：再往內縮

    if spec.edge_margin_px:  # 參考圖最外圈不比：整幅掛牆拍時，畫的邊緣拉正後會混到牆面
        k = spec.edge_margin_px
        inner = np.zeros(covered.shape, bool)
        inner[k:-k, k:-k] = True
        covered &= inner

    sigma = max(size) / 25
    a_g, b_g = _match_blur(gray(a_rgb), gray(b_rgb), covered), gray(b_rgb)
    na, nb = _local_norm(a_g, m, sigma), _local_norm(b_g, m, sigma)
    # 容許錯開 1 px：微調後還剩不到 1 px 的誤差，絹的紋理一錯開 SSIM 就掉
    d_shape = np.full(size[::-1], np.inf, np.float32)
    for dx in range(-_TONE_SHIFT_PX, _TONE_SHIFT_PX + 1):
        for dy in range(-_TONE_SHIFT_PX, _TONE_SHIFT_PX + 1):
            ms = m * _shift(m, dx, dy)
            d_shape = np.minimum(d_shape, (1 - _ssim(_shift(nb, dx, dy), na, ms)) / 2)
    shape = (_mblur(d_shape.astype(np.float32), m, 4) > spec.shape_threshold) & covered

    la, lb = (_mblur(srgb_to_lab(x).astype(np.float32), m, 3) for x in (a_rgb, b_rgb))
    if covered.any():
        ma, mb = la[covered].mean(axis=0), lb[covered].mean(axis=0)
        k = (la[covered].std(axis=0) + 1e-6) / (lb[covered].std(axis=0) + 1e-6)
        # a*、b* 的幅度也拉齊：整張一起變淡（展間偏暗、相機飽和度）不是褪色；
        # 夾在 0.5–2 倍，近乎無彩的畫才不會把雜訊放大成色差
        k[1:] = np.clip(k[1:], 0.5, 2.0)
        lb = (lb - mb) * k + ma
    color = (np.linalg.norm(la - lb, axis=2) > spec.color_threshold) & covered

    area = max(int(covered.sum()), 1)
    changed = shape | color
    changed_ratio = round(float(changed.sum()) / area, 4)

    def full(m):
        return cv2.resize(m.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)

    if changed_ratio > spec.max_changed_ratio:
        return ToneDiff("global_change", [], changed_ratio, full(shape), full(color), full(covered))

    blobs = cv2.dilate(changed.astype(np.uint8), np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(blobs)
    found = []
    for i in range(1, n):
        comp = labels == i
        sp, cp = int((shape & comp).sum()), int((color & comp).sum())
        px = int((changed & comp).sum())
        if px < spec.min_region_frac * area:
            continue
        kind = "color" if sp < 0.2 * px else "shape" if cp < 0.2 * px else "both"
        x, y, w, hh = (int(v) for v in stats[i, :4])
        bbox = [
            round(x / size[0], 4),
            round(y / size[1], 4),
            round((x + w) / size[0], 4),
            round((y + hh) / size[1], 4),
        ]
        found.append((px, Region(bbox=bbox, kind=kind, area_ratio=round(px / area, 4))))
    regions = [r for _, r in sorted(found, key=lambda t: -t[0])]
    return ToneDiff(
        "changed" if regions else "same",
        regions,
        changed_ratio,
        full(shape),
        full(color),
        full(covered),
    )


_TONE_COLOR = {"shape": (220, 38, 38), "color": (217, 119, 6), "both": (147, 51, 234)}


def tone_overlay_png(ref_img: Image.Image, d: ToneDiff) -> bytes:
    """照片 A 調暗，形狀不同塗紅、顏色不同塗橘（兩種都有塗紫），每處框起來編號；
    沒比對的地方再暗一層。"""
    rgb = np.asarray(ref_img.convert("RGB")).astype(np.float32) * 0.6
    rgb[~d.compared] *= 0.4
    for mask, color in ((d.color, _TONE_COLOR["color"]), (d.shape, _TONE_COLOR["shape"])):
        rgb[mask] = rgb[mask] * 0.45 + np.array(color) * 0.55
    rgb[d.shape & d.color] = rgb[d.shape & d.color] * 0.45 + np.array(_TONE_COLOR["both"]) * 0.55
    rgb = np.ascontiguousarray(rgb.astype(np.uint8))
    hh, ww = rgb.shape[:2]
    lw = max(2, round(max(ww, hh) / 400))
    for i, r in enumerate(d.regions, 1):
        x0, y0, x1, y1 = (
            int(round(v)) for v in (r.bbox[0] * ww, r.bbox[1] * hh, r.bbox[2] * ww, r.bbox[3] * hh)
        )
        color = _TONE_COLOR[r.kind]
        cv2.rectangle(rgb, (x0, y0), (x1, y1), color, lw)
        cv2.putText(
            rgb,
            str(i),
            (x0 + 3, max(y0 - 6, 16)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6 * lw / 2 + 0.3,
            color,
            lw,
            cv2.LINE_AA,
        )
    return _png(rgb)
