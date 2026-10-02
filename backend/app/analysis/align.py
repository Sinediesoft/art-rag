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
