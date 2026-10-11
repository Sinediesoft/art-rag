"""3D 重建的孔位校正（docs/adr/034）：從圖紙俯視圖找圓孔，取代 Ortho2CAD 自己畫的垂直圓孔。

Ortho2CAD 的輸入縮到 224×224，Ø6 的孔只剩約 4 px，孔位、方向與深度常錯（mfg-006 右安裝孔偏
12 mm、注油孔鑽在軸承孔下方）。圖紙本身是 800×800 的清楚線圖，所以：
1. detect_holes（主行程）：OpenCV 在俯視圖找實線圓，再看前視圖同一個 x 有沒有點線（隱藏線）——
   有點線才是孔（凸台、外形圓在前視圖是實線），點線的上下端就是孔的深度範圍
2. strip_vertical_holes（沙箱子行程）：執行前從程式碼移除模型自己畫的垂直圓孔
3. apply_holes（沙箱子行程）：在偵測到的位置切孔，座標依外形比例換算到重建模型上

只用知識庫的標準三視圖（drawing.render_ortho 產生、版面固定）偵測；照片拉正後辨識出的知識庫圖紙
也用它的標準三視圖，不用照片。照片建檔與未收錄的圖紙版面不固定，不校正（cad_service.hole_plan_for）。
座標一律以外形中心為原點（mm）：x 向右、y 向俯視圖上方、z 向上。
"""

import ast
import math

import cv2
import numpy as np
from PIL import Image

from app.cad.drawing import CANVAS, POSITIONS, VIEW

INK = 128  # 灰階低於這個值算線條
MIN_R_PX = 3.0  # 半徑 3 px 以下分不出是圓還是字
COVERAGE = 0.85  # 圓周上有線條的比例：實線圓 ≈1；長孔的半圓端、文字都低於這個值
MIN_DOTS = 3  # 前視圖連續幾個點才算隱藏線
Z_MARGIN = 0.03  # 切孔時上下各多切外形高度的 3%：點線端點有一個點距（4 px）的誤差
NORMAL_EPS = 1e-6  # 草圖法向量的 X、Y 分量比 Z 小這麼多才算垂直：只容許浮點誤差，斜孔不算
GEOM_EPS = 1e-6  # 草圖線圈的包含關係：圓要比外圈小這麼多才算在裡面（相切、壓邊都不算）
FOOTPRINT = 0.98  # 看孔位有沒有材料時的半徑比例：略小一點，避免和模型既有孔壁剛好重合
EMPTY = 0.05  # 孔的範圍裡材料體積比這個比例還少，就當作沒有材料（已經鑽好的孔，孔徑差 4.5% 以內）
RISE = (
    1e-4  # 切孔範圍裡材料比例下降之後，又比最低點多出這麼多（比例，不是百分比）就是碰到下一段材料
)
ARC_STEPS = 48  # 外圈的圓弧取樣成幾段折線來判斷包含關係


def _center(view: str) -> tuple[float, float]:
    ox, oy = POSITIONS[view]
    return ox + VIEW / 2, oy + VIEW / 2


def _px_per_mm(dims: dict) -> float:
    """render_ortho 的比例尺：最大外形尺寸占 VIEW 的 90%（三個視圖共用）。"""
    return VIEW * 0.9 / max(float(dims[k]) for k in ("width", "depth", "height"))


def _fit_circle(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float]:
    """最小平方法擬合圓（Kåsa）：x²+y² = 2ax + 2by + c。"""
    a = np.column_stack([2 * xs, 2 * ys, np.ones_like(xs)])
    (cx, cy, c), *_ = np.linalg.lstsq(a, xs**2 + ys**2, rcond=None)
    return float(cx), float(cy), float(math.sqrt(max(c + cx**2 + cy**2, 0.0)))


def _coverage(ink: np.ndarray, cx: float, cy: float, r: float) -> float:
    h, w = ink.shape
    hits = 0
    angles = np.linspace(0, 2 * math.pi, 90, endpoint=False)
    for t in angles:
        found = False
        for dr in (-1.0, 0.0, 1.0):
            x = int(round(cx + (r + dr) * math.cos(t)))
            y = int(round(cy + (r + dr) * math.sin(t)))
            if 0 <= x < w and 0 <= y < h and ink[y, x]:
                found = True
                break
        hits += found
    return hits / len(angles)


def _top_circles(gray: np.ndarray, dims: dict, s: float) -> list[tuple[float, float, float]]:
    """俯視圖裡完整落在外形內、圓周幾乎都有線條的圓：(cx, cy, r)，整張圖紙的像素座標。"""
    tx, ty = _center("top")
    hw, hd = float(dims["width"]) * s / 2, float(dims["depth"]) * s / 2
    x0, y0 = int(tx - hw) - 6, int(ty - hd) - 6
    crop = gray[y0 : int(ty + hd) + 7, x0 : int(tx + hw) + 7]
    ink = crop < INK
    max_r = 0.95 * min(hw, hd)  # 跟外形一樣大的圓是外形（法蘭盤），不是孔
    # 先放大 2 倍再找圓：小孔半徑只有 6～7 px
    big = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    found = cv2.HoughCircles(
        cv2.GaussianBlur(big, (5, 5), 1.0),
        cv2.HOUGH_GRADIENT_ALT,
        dp=1.5,
        minDist=6,
        param1=150,
        param2=0.75,
        minRadius=int(MIN_R_PX * 2),
        maxRadius=int(max_r * 2) + 2,
    )
    out: list[tuple[float, float, float, float]] = []
    yy, xx = np.nonzero(ink)
    for hx, hy, hr in [] if found is None else found[0]:
        cx, cy, r = hx / 2, hy / 2, hr / 2
        # 用線條像素重新擬合：Hough 的圓心、半徑只準到 1 px 左右
        near = np.abs(np.hypot(xx - cx, yy - cy) - r) <= 2.5
        if near.sum() < 12:
            continue
        cx, cy, r = _fit_circle(xx[near].astype(float), yy[near].astype(float))
        if not (MIN_R_PX <= r <= max_r):
            continue
        # 圓要整個落在外形裡（擋掉尺寸標註的數字 0）
        gx, gy = cx + x0, cy + y0
        if abs(gx - tx) + r > hw + 1 or abs(gy - ty) + r > hd + 1:
            continue
        cov = _coverage(ink, cx, cy, r)
        if cov < COVERAGE:
            continue
        # 線寬約 1.5 px：Hough 會在內外緣各找到一個圓，合併成一個
        dup = next(
            (
                i
                for i, c in enumerate(out)
                if math.hypot(c[0] - gx, c[1] - gy) < 2 and abs(c[2] - r) < 2.5
            ),
            None,
        )
        if dup is None:
            out.append((gx, gy, r, cov))
        elif cov > out[dup][3]:
            out[dup] = (gx, gy, r, cov)
    return [(x, y, r) for x, y, r, _ in out]


def _dot_rows(column: np.ndarray) -> list[int]:
    """一欄像素裡點線（短線段、間隔短）的上下端列：連續 MIN_DOTS 個點以上才算。

    點線的第一點與最後一點常和外形的水平線連在一起（5～8 px）：當成點線的端點，
    那一段之後另起一條；更長的是實線。
    """
    runs, start = [], None
    for i, v in enumerate(column):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(column) - 1))
    rows: list[int] = []
    chain: list[tuple[int, int]] = []

    def flush() -> None:
        if len(chain) >= MIN_DOTS:
            rows.extend((chain[0][0], chain[-1][1]))
        chain.clear()

    for a, b in runs:
        n = b - a + 1
        if chain and a - chain[-1][1] - 1 > 5:
            flush()
        if n <= 4:
            chain.append((a, b))
        elif n <= 8:
            chain.append((a, b))
            flush()
            chain.append((a, b))
        else:
            flush()
    flush()
    return rows


def _hidden_extent(gray: np.ndarray, xs: list[float], dims: dict, s: float):
    """前視圖在孔兩側（x－r、x＋r，像素）的點線上下端（像素列）；兩側都沒有點線回 None。

    兩側都有時取交集：一側可能剛好和別的孔的隱藏線重疊（mfg-002 的螺栓孔貼著中心孔）。
    """
    _, fy = _center("front")
    hh = float(dims["height"]) * s / 2
    r0, r1 = int(fy - hh) - 2, int(fy + hh) + 3
    sides = []
    for x in xs:
        c = int(round(x))
        band = gray[r0:r1, max(c - 1, 0) : c + 2].min(axis=1) < INK
        rows = _dot_rows(band)
        if rows:
            sides.append((r0 + min(rows), r0 + max(rows)))
    if not sides:
        return None
    top, bottom = max(a for a, _ in sides), min(b for _, b in sides)
    return (top, bottom) if top < bottom else None


def detect_holes(drawing: Image.Image, dims: dict) -> list[dict]:
    """圖紙（render_ortho 版面，下方可帶標題欄）→ 垂直孔 [{x, y, r, z0, z1}]。

    單位 mm，外形中心為原點；x、y 是孔中心，r 是半徑，z0～z1 是孔的上下端。
    """
    gray = np.asarray(drawing.convert("L").crop((0, 0, CANVAS, CANVAS)))
    s = _px_per_mm(dims)
    tx, ty = _center("top")
    _, fy = _center("front")
    holes: list[dict] = []
    for cx, cy, r in _top_circles(gray, dims, s):
        extent = _hidden_extent(gray, [cx - r, cx + r], dims, s)
        if extent is None:  # 前視圖沒有點線：凸台或外形，不是孔
            continue
        top, bottom = extent
        px, py = cx + 0.5, cy + 0.5  # 像素索引 → 連續座標（像素中心）
        holes.append(
            {
                "x": round((px - tx) / s, 3),
                "y": round((ty - py) / s, 3),
                "r": round(r / s, 3),
                "z0": round((fy - bottom) / s, 3),
                "z1": round((fy - top) / s, 3),
            }
        )
    # 同心圓（沉頭孔、凸台上的孔）只留最小的：深度只從最小的孔看得準
    holes.sort(key=lambda h: h["r"])
    kept: list[dict] = []
    for h in holes:
        if all(math.hypot(h["x"] - k["x"], h["y"] - k["y"]) > 0.5 for k in kept):
            kept.append(h)
    return sorted(kept, key=lambda h: (h["x"], h["y"]))


def hole_plan(drawing: Image.Image, dims: dict | None) -> tuple[list[dict] | None, str | None]:
    """標準三視圖（render_ortho 版面）要不要校正孔位：回 (孔位, None) 或 (None, 不校正的原因)。"""
    if not dims:
        return None, "沒有外形尺寸，孔位維持模型輸出"
    holes = detect_holes(drawing, dims)
    if not holes:
        return None, "俯視圖沒有偵測到圓孔，孔位維持模型輸出"
    return holes, None


# ---------------------------------------------------------------- 移除模型自己畫的垂直圓孔
def _const(node: ast.AST) -> float | None:
    try:
        v = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _chain(node: ast.AST) -> tuple[str | None, list[ast.Call]]:
    """a.b(...).c(...) → ("a", [b 呼叫, c 呼叫])；根不是變數名稱回 None。"""
    calls: list[ast.Call] = []
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append(node)
        node = node.func.value
    return (node.id if isinstance(node, ast.Name) else None), calls[::-1]


def _vertical_workplane(value: ast.AST) -> bool | None:
    """cq.Workplane(...) 的法向量是不是 ±Z；不是 Workplane 回 None。"""
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Attribute)
        and value.func.attr == "Workplane"
    ):
        return None
    if not value.args:
        return True  # 預設 XY
    arg = value.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value.upper() in ("XY", "YX")
    if (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "Plane"
    ):
        if len(arg.args) < 3:
            return False
        n = arg.args[2]
        if not (isinstance(n, ast.Call) and len(n.args) == 3):
            return False
        nx, ny, nz = (_const(a) for a in n.args)
        if None in (nx, ny, nz):
            return False
        norm = math.sqrt(nx * nx + ny * ny + nz * nz)
        # X、Y 分量只容許浮點誤差（Ortho2CAD 的 6.1e-17）；傾斜幾度的斜孔不是垂直孔，保留
        return norm > 1e-9 and math.hypot(nx, ny) <= NORMAL_EPS * norm
    return False


def _assign(stmt: ast.stmt) -> tuple[str, ast.AST] | None:
    if (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    ):
        return stmt.targets[0].id, stmt.value
    return None


# 草圖線圈的幾何，座標是草圖平面的局部座標：
# ("circle", 圓心 x, 圓心 y, 半徑) 或 ("poly", [(x, y), ...])
Loop = tuple


def _point_args(call: ast.Call) -> tuple[float, float] | None:
    if call.keywords or len(call.args) != 2:
        return None
    x, y = _const(call.args[0]), _const(call.args[1])
    return None if x is None or y is None else (x, y)


def _pair(node: ast.AST) -> tuple[float, float] | None:
    if isinstance(node, ast.Tuple) and len(node.elts) == 2:
        x, y = _const(node.elts[0]), _const(node.elts[1])
        return None if x is None or y is None else (x, y)
    return None


def _arc_points(p0, p1, p2) -> list[tuple[float, float]]:
    """p0 經 p1 到 p2 的圓弧取樣成折線點（不含 p0、含 p2）；三點共線時就是兩段直線。"""
    (x0, y0), (x1, y1), (x2, y2) = p0, p1, p2
    d = 2 * (x0 * (y1 - y2) + x1 * (y2 - y0) + x2 * (y0 - y1))
    if abs(d) < 1e-12:
        return [p1, p2]
    s0, s1, s2 = x0 * x0 + y0 * y0, x1 * x1 + y1 * y1, x2 * x2 + y2 * y2
    ux = (s0 * (y1 - y2) + s1 * (y2 - y0) + s2 * (y0 - y1)) / d
    uy = (s0 * (x2 - x1) + s1 * (x0 - x2) + s2 * (x1 - x0)) / d
    r = math.hypot(x0 - ux, y0 - uy)
    a0, a1, a2 = (math.atan2(y - uy, x - ux) for x, y in (p0, p1, p2))
    d1, d2 = (a1 - a0) % (2 * math.pi), (a2 - a0) % (2 * math.pi)
    sweep = d2 if d1 < d2 else d2 - 2 * math.pi  # 掃過的方向要經過 p1
    return [
        (
            ux + r * math.cos(a0 + sweep * i / ARC_STEPS),
            uy + r * math.sin(a0 + sweep * i / ARC_STEPS),
        )
        for i in range(1, ARC_STEPS + 1)
    ]


def _loop_geometry(calls: list[ast.Call]) -> Loop | None:
    """DeepCAD 寫法的草圖線圈（wp.moveTo().circle()，或 moveTo／lineTo／threePointArc／close 的
    封閉折線）→ 幾何；座標不是常數或有別的呼叫時回 None（確認不了就不當孔）。"""
    methods = [c.func.attr for c in calls]
    if methods[-1] == "circle":
        if methods not in (["circle"], ["moveTo", "circle"]):
            return None
        centre = (0.0, 0.0) if len(calls) == 1 else _point_args(calls[0])
        last = calls[-1]
        r = _const(last.args[0]) if len(last.args) == 1 and not last.keywords else None
        if centre is None or r is None or r <= 0:
            return None
        return ("circle", centre[0], centre[1], r)
    start = _point_args(calls[0]) if methods[0] == "moveTo" else None
    if start is None or len(calls) < 4 or methods[-1] != "close" or calls[-1].args:
        return None
    pts = [start]
    for call, method in zip(calls[1:-1], methods[1:-1], strict=True):
        if method == "lineTo":
            point = _point_args(call)
            if point is None:
                return None
            pts.append(point)
        elif method == "threePointArc" and len(call.args) == 2 and not call.keywords:
            mid, end = _pair(call.args[0]), _pair(call.args[1])
            if mid is None or end is None:
                return None
            pts.extend(_arc_points(pts[-1], mid, end))
        else:
            return None
    return ("poly", pts)


def _in_poly(x: float, y: float, pts: list[tuple[float, float]]) -> bool:
    inside = False
    for (xa, ya), (xb, yb) in zip(pts, pts[1:] + pts[:1], strict=True):
        if (ya > y) != (yb > y) and x < xa + (y - ya) * (xb - xa) / (yb - ya):
            inside = not inside
    return inside


def _edge_distance(x: float, y: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = (
        0.0
        if dx == dy == 0
        else max(0.0, min(1.0, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy)))
    )
    return math.hypot(x - (a[0] + t * dx), y - (a[1] + t * dy))


def _encloses(outer: Loop, inner: Loop) -> bool:
    """inner 是不是完全落在 outer 圍成的區域裡。圓要比外圈小一個 GEOM_EPS 才算（相切、壓邊不算）；
    折線放在裡面只看頂點（用來判斷外圈自己是不是又被別的線圈包著）。"""
    if inner[0] == "poly":
        return all(
            (math.hypot(x - outer[1], y - outer[2]) < outer[3])
            if outer[0] == "circle"
            else _in_poly(x, y, outer[1])
            for x, y in inner[1]
        )
    _, cx, cy, r = inner
    if outer[0] == "circle":
        return math.hypot(cx - outer[1], cy - outer[2]) + r < outer[3] - GEOM_EPS
    pts = outer[1]
    if not _in_poly(cx, cy, pts):
        return False
    edges = zip(pts, pts[1:] + pts[:1], strict=True)
    return min(_edge_distance(cx, cy, a, b) for a, b in edges) > r + GEOM_EPS


def strip_vertical_holes(code: str) -> tuple[str, int]:
    """移除 Ortho2CAD 程式碼裡的垂直圓孔，回 (程式碼, 移除的圓數)；認不得的寫法原樣保留。

    Ortho2CAD 的輸出是 DeepCAD 的草圖＋擠出序列：
      wp = cq.Workplane(cq.Plane(原點, x 方向, 法向量)); loopK = wp.moveTo(x, y).circle(r)
      solidN = wp.add(loopA).add(loopB).extrude(d); solid = solid.cut(solidN)
    垂直圓孔＝法向量 ±Z 的草圖：(1) 切除草圖裡互不包含的圓 → 整句移除；(2) 加料草圖裡完全落在
    某個外圈裡面、自己不在別的圈裡的圓（內圈）→ 拿掉那個 .add(loopK) 和畫圓那行。同一草圖的
    其他圓若不在任何外圈裡，是分離的凸台；環形槽、壓在邊上的圓、座標不是常數的草圖也都保留。
    只會刪節點，不會加入新的程式碼。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, 0
    vertical: dict[str, bool] = {}
    loops: dict[str, tuple[str, Loop | None]] = {}  # loopK → (workplane 變數, 幾何)
    sketches: dict[str, tuple[str, bool, list[ast.Call]]] = {}  # solidN → (workplane, 垂直, .add)
    for stmt in tree.body:
        a = _assign(stmt)
        if a is None:
            continue
        name, value = a
        v = _vertical_workplane(value)
        if v is not None:
            vertical[name] = v
            continue
        root, calls = _chain(value)
        if root not in vertical or not calls:
            continue
        methods = [c.func.attr for c in calls]
        if methods[-1] in ("circle", "close"):
            loops[name] = (root, _loop_geometry(calls))
        elif methods[-1] == "extrude" and set(methods[:-1]) == {"add"}:
            adds = calls[:-1]
            if all(len(c.args) == 1 and isinstance(c.args[0], ast.Name) for c in adds):
                sketches[name] = (root, vertical[root], adds)

    # 每個名稱被讀幾次：內圈圓只在自己的 .add() 用到一次才刪
    uses: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            uses[node.id] = uses.get(node.id, 0) + 1

    def cut_target(value: ast.AST) -> str | None:
        """solid.cut(solidN) → "solidN"。"""
        _, calls = _chain(value)
        if (
            len(calls) == 1
            and calls[0].func.attr == "cut"
            and len(calls[0].args) == 1
            and isinstance(calls[0].args[0], ast.Name)
        ):
            return calls[0].args[0].id
        return None

    cut_solids = {cut_target(a[1]) for a in map(_assign, tree.body) if a is not None} - {None}
    removed = 0
    drop_cuts: set[str] = set()
    drop_loops: set[str] = set()
    for name, (wp, is_vertical, adds) in sketches.items():
        if not is_vertical:
            continue
        names = [c.args[0].id for c in adds]
        found = [loops.get(n) for n in names]
        # 草圖裡有認不得的線圈、或線圈不是畫在同一個 workplane：確認不了包含關係，整張草圖保留
        if any(f is None or f[0] != wp or f[1] is None for f in found):
            continue
        shapes = [f[1] for f in found]
        n = len(shapes)
        if name in cut_solids:
            # 切除草圖：全是圓、而且互不包含＝一組圓孔；有包含關係的是環形槽，不是孔
            if all(s[0] == "circle" for s in shapes) and not any(
                _encloses(shapes[i], shapes[j]) for i in range(n) for j in range(n) if i != j
            ):
                drop_cuts.add(name)
            continue
        # 加料草圖：第一個 loop 是外形；後面的圓只有「正好在一個外圈裡面、那個外圈自己沒被包著」
        # 才是內圈（孔）。分離的圓（凸台）、圓柱的單一圓都保留
        parents = [
            [j for j in range(n) if j != i and _encloses(shapes[j], shapes[i])] for i in range(n)
        ]
        if any(len(p) > 1 for p in parents):
            continue  # 圈裡還有圈（島）：內外圈怎麼配對沒把握，整張草圖保留
        for i in range(1, n):
            if shapes[i][0] != "circle" or uses.get(names[i]) != 1 or len(parents[i]) != 1:
                continue
            if parents[parents[i][0]]:
                continue  # 外圈自己被別的圈包著
            drop_loops.add(names[i])
            # .add(loopK) → 直接用它前面的鏈
            call = adds[i]
            parent = call.func.value
            call.func, call.args, call.keywords = parent.func, parent.args, parent.keywords
            removed += 1

    body: list[ast.stmt] = []
    for stmt in tree.body:
        a = _assign(stmt)
        if a is not None:
            name, value = a
            if name in drop_loops:
                # CadQuery 的 moveTo().circle() 會把圓放進 workplane 共用的 pending wires，
                # 不刪這行，之後的 extrude 照樣把它當內圈
                continue
            target = cut_target(value)
            if target in drop_cuts:
                removed += len(sketches[target][2])
                continue
        body.append(stmt)
    if not removed:
        return code, 0
    tree.body = body
    return ast.unparse(tree) + "\n", removed


# ---------------------------------------------------------------- 在重建模型上切孔
def _solid_fraction(shape, cylinder) -> float:
    """圓柱裡的材料體積比例（整個圓柱和實體求交，不是取樣）。"""
    return shape.intersect(cylinder).Volume() / cylinder.Volume()


def _fraction_profile(shape, x: float, y: float, r: float, z_lo: float, z_hi: float):
    """切孔範圍 z_lo…z_hi 內，整個孔徑（半徑 r）每一段的材料體積比例，由上到下：[(上, 下, 比例)]。

    把孔徑圓柱和實體求交，在交集的頂點高度（凸起、肋、柱子的頂面與底面都會有頂點）切成一段
    一段，每一段量整個橫截面的材料體積比例；不取樣，小到幾乎看不見的凸起也會讓那一段的比例變大。
    """
    import cadquery as cq

    up = cq.Vector(0, 0, 1)
    column = cq.Solid.makeCylinder(r, z_hi - z_lo, cq.Vector(x, y, z_lo), up)
    levels = {z_lo, z_hi}
    levels.update(v.Z for v in shape.intersect(column).Vertices() if z_lo < v.Z < z_hi)
    zs = sorted(levels, reverse=True)
    profile = []
    for upper, lower in zip(zs, zs[1:], strict=False):
        if upper - lower < 1e-9 * max(abs(z_hi), abs(z_lo), 1.0):
            continue
        slab = cq.Solid.makeCylinder(r, upper - lower, cq.Vector(x, y, lower), up)
        profile.append((upper, lower, _solid_fraction(shape, slab)))
    return profile


def _next_material(profile) -> float | None:
    """材料比例『下降後又回升』＝碰到下一段材料：回升那一段的上緣 z；沒有回 None。

    從第一段有材料的地方往下看。比例單調下降（包含一路降到 0，例如孔通到橫孔的曲面穿通，
    或下面是空的）不算；進到材料之前的上升（空氣 → 材料、倒角）也不算。比例降下去之後，
    只要某一段又比最低點多出 RISE，不論降了多少（柱子連著上下兩層時只降一點點），都算。
    """
    previous = None  # 上一段的比例；None＝還沒進到材料
    trough = None  # 下降之後的最低比例；None＝還沒下降
    for upper, _lower, fraction in profile:
        if previous is None:
            if fraction > RISE:
                previous = fraction
            continue
        if trough is None:
            if fraction < previous - RISE:
                trough = fraction
            else:
                previous = fraction
        elif fraction > trough + RISE:
            return upper
        else:
            trough = min(trough, fraction)
    return None


def apply_holes(shape, holes: list[dict], dims: dict):
    """在（已縮放成 mm 的）重建模型上依圖紙孔位切孔，回 (實體, 已切孔數, 不校正的原因)。

    座標依外形比例換算：圖紙上的 x／寬度＝模型上的 x／模型寬度（y、z 同理），
    所以模型外形略有誤差時，孔仍落在對應的位置（例如仍在底座凸緣的中間）。

    從上方鑽穿第一段材料，用整個孔徑的體積驗證，不靠取樣：
    - 孔的範圍（圖紙孔深去掉兩端 3% 容差）裡沒有材料＝模型已經鑽好這個孔，不切。
    - 否則沿孔由上往下量切除範圍（含深度容差）內整個孔徑的材料體積比例；比例下降之後又回升
      （例如 1 → 0 → 1 的兩板之間、1 → 0.82 → 1 的柱子連著兩板）就是碰到下一段材料，
      整個 3D 重建都不校正，維持模型輸出並寫明原因。比例一路單調下降（包含降到 0，
      例如孔通到橫孔的曲面穿通）才切。
    """
    import cadquery as cq

    bb = shape.BoundingBox()
    w, d, h = (float(dims[k]) for k in ("width", "depth", "height"))
    if (w - d) * (bb.xlen - bb.ylen) < 0 and abs(w - d) > 0.15 * max(w, d):
        return shape, 0, "模型的寬、深方向和圖紙不同，孔位維持模型輸出"
    cx, cy, cz = (bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, (bb.zmin + bb.zmax) / 2
    fx, fy, fz = bb.xlen / w, bb.ylen / d, bb.zlen / h
    margin = Z_MARGIN * bb.zlen
    tol = 1e-6 * max(bb.xlen, bb.ylen, bb.zlen)
    up = cq.Vector(0, 0, 1)
    cuts = []
    placed = 0  # 模型自己已經在同一個位置鑽了孔
    why: list[str] = []  # 沒有切的孔各自的原因
    for hole in holes:
        r = hole["r"]
        x, y = cx + hole["x"] * fx, cy + hole["y"] * fy
        bottom, top = cz + hole["z0"] * fz, cz + hole["z1"] * fz  # 圖紙點線的下端、上端
        lo, hi = bottom - margin, top + margin
        # 俯視圖畫成實線圓＝從上方看得到：孔的上方要是空的，否則模型外形和圖紙對不上
        if hi < bb.zmax:
            above = cq.Solid.makeCylinder(r / 2, bb.zmax - hi + 1, cq.Vector(x, y, hi), up)
            if shape.intersect(above).Volume() > 0.02 * above.Volume():
                return shape, 0, "模型外形和圖紙對不上（孔位上方有材料），孔位維持模型輸出"
        span = cq.Solid.makeCylinder(FOOTPRINT * r, hi - lo, cq.Vector(x, y, lo), up)
        if _solid_fraction(shape, span) <= EMPTY:
            # 切孔範圍裡沒有材料：模型已經在這裡鑽穿了（包含比圖紙大的孔：孔位在外形範圍內、
            # 離最近的材料不超過一個孔半徑），或孔位根本在模型外
            gap = shape.distance(cq.Solid.makeCylinder(r, hi - lo, cq.Vector(x, y, lo), up))
            in_outline = bb.xmin <= x <= bb.xmax and bb.ymin <= y <= bb.ymax
            if gap <= tol or (in_outline and gap <= r):
                placed += 1
            else:
                why.append("孔位落在模型外")
            continue
        # 孔的範圍（去掉兩端的點線誤差）：裡面沒有材料＝孔已經鑽好，剩下的是更下面的材料
        e = min(margin, (top - bottom) / 4)
        core_lo, core_hi = bottom + e, top - e
        core = cq.Solid.makeCylinder(FOOTPRINT * r, core_hi - core_lo, cq.Vector(x, y, core_lo), up)
        if core_hi <= core_lo or _solid_fraction(shape, core) <= EMPTY:
            placed += 1
            continue
        z_lo, z_hi = max(lo, bb.zmin), min(hi, bb.zmax)  # 切除範圍裡有模型的部分
        if z_hi - z_lo <= tol:
            why.append("孔的深度範圍沒有碰到材料")
            continue
        z_next = _next_material(_fraction_profile(shape, x, y, r, z_lo, z_hi))
        if z_next is not None:
            return (
                shape,
                0,
                f"孔的深度範圍（含容差）碰到下一段材料（z≈{z_next:.1f} mm，材料比例下降後又回升），"
                "孔位維持模型輸出",
            )
        cuts.append(cq.Solid.makeCylinder(r, hi - lo, cq.Vector(x, y, lo), up))
    if not cuts and not placed:
        return shape, 0, f"{why[0] if why else '孔位落在模型外'}，孔位維持模型輸出"
    for cyl in cuts:
        shape = shape.cut(cyl)
    return shape, len(cuts) + placed, None
