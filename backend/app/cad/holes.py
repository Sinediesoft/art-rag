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
        return abs(nz) >= 0.99 * math.sqrt(nx * nx + ny * ny + nz * nz)
    return False


def _assign(stmt: ast.stmt) -> tuple[str, ast.AST] | None:
    if (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
    ):
        return stmt.targets[0].id, stmt.value
    return None


def strip_vertical_holes(code: str) -> tuple[str, int]:
    """移除 Ortho2CAD 程式碼裡的垂直圓孔，回 (程式碼, 移除的圓數)；認不得的寫法原樣保留。

    Ortho2CAD 的輸出是 DeepCAD 的草圖＋擠出序列：
      wp = cq.Workplane(cq.Plane(原點, x 方向, 法向量)); loopK = wp.moveTo(x, y).circle(r)
      solidN = wp.add(loopA).add(loopB).extrude(d); solid = solid.cut(solidN)
    垂直圓孔＝法向量 ±Z 的草圖：(1) 只有圓的切除 → 整句移除；(2) 加料草圖第 2 個以後的圓（內圈）
    → 拿掉那個 .add(loopK) 和畫圓那行。只會刪節點，不會加入新的程式碼。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, 0
    vertical: dict[str, bool] = {}
    circle_loops: set[str] = set()
    sketches: dict[str, tuple[str, list[ast.Call]]] = {}  # solidN → (workplane, .add 呼叫)
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
        if methods[-1] == "circle" and set(methods) <= {"moveTo", "circle"}:
            circle_loops.add(name)
        elif methods[-1] == "extrude" and set(methods[:-1]) == {"add"}:
            adds = calls[:-1]
            if all(len(c.args) == 1 and isinstance(c.args[0], ast.Name) for c in adds):
                sketches[name] = (root, adds)

    def loops(adds: list[ast.Call]) -> list[str]:
        return [c.args[0].id for c in adds]

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
    for name, (wp, adds) in sketches.items():
        if not vertical.get(wp):
            continue
        if name in cut_solids:
            if all(lp in circle_loops for lp in loops(adds)):
                drop_cuts.add(name)
            continue
        # 加料草圖：第一個 loop 是外形，後面的圓是內圈（孔）；只有一個圓的是圓柱、凸台
        for call in adds[1:]:
            lp = call.args[0].id
            if lp in circle_loops and uses.get(lp) == 1:
                drop_loops.add(lp)
                # .add(loopK) → 直接用它前面的鏈
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
                removed += len(sketches[target][1])
                continue
        body.append(stmt)
    if not removed:
        return code, 0
    tree.body = body
    return ast.unparse(tree) + "\n", removed


# ---------------------------------------------------------------- 在重建模型上切孔
def apply_holes(shape, holes: list[dict], dims: dict):
    """在（已縮放成 mm 的）重建模型上依圖紙孔位切孔，回 (實體, 已切孔數, 不校正的原因)。

    座標依外形比例換算：圖紙上的 x／寬度＝模型上的 x／模型寬度（y、z 同理），
    所以模型外形略有誤差時，孔仍落在對應的位置（例如仍在底座凸緣的中間）。
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
    cuts = []
    for hole in holes:
        x, y = cx + hole["x"] * fx, cy + hole["y"] * fy
        lo = cz + hole["z0"] * fz - margin
        hi = cz + hole["z1"] * fz + margin
        cyl = cq.Solid.makeCylinder(hole["r"], hi - lo, cq.Vector(x, y, lo), cq.Vector(0, 0, 1))
        if shape.distance(cyl) > tol:
            continue  # 碰不到模型（孔位落在模型外）
        # 俯視圖畫成實線圓＝從上方看得到：孔的上方要是空的，否則模型外形和圖紙對不上
        if hi < bb.zmax:
            above = cq.Solid.makeCylinder(
                hole["r"] / 2, bb.zmax - hi + 1, cq.Vector(x, y, hi), cq.Vector(0, 0, 1)
            )
            if shape.intersect(above).Volume() > 0.02 * above.Volume():
                return shape, 0, "模型外形和圖紙對不上（孔位上方有材料），孔位維持模型輸出"
        cuts.append(cyl)
    if not cuts:
        return shape, 0, "孔位落在模型外，孔位維持模型輸出"
    # 孔已經在原位時照切也不會多切掉材料
    for cyl in cuts:
        shape = shape.cut(cyl)
    return shape, len(cuts), None
