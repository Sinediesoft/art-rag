"""三視圖產生器：CadQuery／OCP 實體 → Ortho2CAD 訓練時的圖紙格式（800×800 PNG）。

照 Ortho2CAD 官方 pythonocc_for_step_to_ortho.py 移植（pythonocc → CadQuery 內附的 OCP）：
- 第一角法：前視圖在上（400,100）、俯視圖在下（400,450）、右視圖在左（50,100），各 300×300
- 隱藏線消除（HLR）：可見線實線、隱藏線點線（dasharray 1,3），線寬 1.5
- 三個視圖同一比例尺；只標外形尺寸（前視圖：高；俯視圖：寬、深），小數 4 位
圖紙格式和訓練資料一致，模型才認得；知識庫圖紙與「3D 重建結果的回投影」都用這支產生。
"""

import contextlib
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CANVAS = 800
VIEW = 300
POSITIONS = {"front": (400, 100), "top": (400, 450), "right": (50, 100)}
TITLES = {"front": "Front View", "top": "Top View", "right": "Right View"}
SS = 2  # 先畫 2 倍大再縮小，模擬 SVG 轉 PNG 的反鋸齒

_FONT_DIRS = [
    Path("/System/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/usr/share/fonts/truetype/msttcorefonts"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/opentype/noto"),
]
_FONTS = {
    "latin": ["Arial.ttf", "DejaVuSans.ttf"],
    "latin_bold": ["Arial Bold.ttf", "DejaVuSans-Bold.ttf"],
    "cjk": ["STHeiti Medium.ttc", "Arial Unicode.ttf", "NotoSansCJK-Regular.ttc"],
}


@lru_cache
def _font(
    bold: bool, size: int = 14, kind: str = ""
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for d in _FONT_DIRS:
        for n in _FONTS[kind or ("latin_bold" if bold else "latin")]:
            if (d / n).is_file():
                return ImageFont.truetype(str(d / n), size * SS)
    return ImageFont.load_default(size * SS)


# ---------------------------------------------------------------- 投影（HLR）
def _projector(view_dir):
    from OCP.gp import gp_Ax3, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    from OCP.HLRAlgo import HLRAlgo_Projector

    if abs(view_dir.X()) > 0.5:
        up = gp_Dir(0, -1, 0)
    elif abs(view_dir.Y()) > 0.5:
        up = gp_Dir(0, 0, -1)
    else:
        up = gp_Dir(0, 1, 0)
    v = gp_Vec(view_dir.X(), view_dir.Y(), view_dir.Z())
    x = gp_Vec(up.X(), up.Y(), up.Z()).Crossed(v)
    x.Normalize()
    ax = gp_Ax3(gp_Pnt(1000, 1000, 1000), view_dir, gp_Dir(x))
    t = gp_Trsf()
    t.SetTransformation(ax, gp_Ax3())
    return HLRAlgo_Projector(t, False, 0.0)


def _edges_2d(compound) -> list[list[tuple[float, float]]]:
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out = []
    if compound is None or compound.IsNull():
        return out
    exp = TopExp_Explorer(compound, TopAbs_EDGE)
    count = 0
    while exp.More() and count < 1000:
        count += 1
        try:
            c = BRepAdaptor_Curve(TopoDS.Edge_s(exp.Current()))
            a, b = c.FirstParameter(), c.LastParameter()
            if abs(b - a) > 1e-12:
                n = 1 if c.GetType() == GeomAbs_Line else 100
                pts = []
                for i in range(n + 1):
                    p = c.Value(a + (b - a) * i / n)
                    pts.append((float(p.X()), float(p.Y())))
                if len(pts) >= 2:
                    out.append(pts)
        except Exception:  # 個別邊失敗就略過（與官方腳本相同）
            pass
        exp.Next()
    return out


def _view(shape, view_dir) -> dict:
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape

    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(_projector(view_dir))
    algo.Update()
    algo.Hide()
    h = HLRBRep_HLRToShape(algo)
    visible, hidden = [], []
    for getter, dst in (
        (h.VCompound, visible),
        (h.OutLineVCompound, visible),
        (h.Rg1LineVCompound, visible),
        (h.HCompound, hidden),
        (h.OutLineHCompound, hidden),
        (h.Rg1LineHCompound, hidden),
    ):
        with contextlib.suppress(Exception):
            dst.extend(_edges_2d(getter()))
    return {"visible": visible, "hidden": hidden}


def bbox_dims(shape) -> dict[str, float]:
    """外形尺寸：width＝X、depth＝Y、height＝Z（與 Ortho2CAD 圖紙標註相同定義）。"""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)  # 不含公差膨脹，標註才會是 80.0000
    x0, y0, z0, x1, y1, z1 = box.Get()
    return {"width": abs(x1 - x0), "depth": abs(y1 - y0), "height": abs(z1 - z0)}


# ---------------------------------------------------------------- 版面
def _scale(views: dict) -> float:
    extent = 0.0
    for v in views.values():
        pts = [p for e in v["visible"] + v["hidden"] for p in e]
        if pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            extent = max(extent, max(xs) - min(xs), max(ys) - min(ys))
    return VIEW / extent * 0.9 if extent > 0 else 1.0


def _place(edges, scale):
    pts = [p for e in edges for p in e]
    if not pts:
        return []
    cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
    cy = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
    return [
        [(VIEW / 2 + (x - cx) * scale, VIEW / 2 - (y - cy) * scale) for x, y in e] for e in edges
    ]


def _rotate_cw(edges):
    c = VIEW / 2
    return [[(y - c + c, -(x - c) + c) for x, y in e] for e in edges]


def _segments(edges):
    return [
        (round(e[i][0], 2), round(e[i][1], 2), round(e[i + 1][0], 2), round(e[i + 1][1], 2))
        for e in edges
        for i in range(len(e) - 1)
    ]


def _intersect(a, b, eps=1e-6) -> bool:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b

    def orient(x1, y1, x2, y2, x3, y3):
        return (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1)

    def on(x1, y1, x2, y2, x3, y3):
        return (
            min(x1, x2) - eps <= x3 <= max(x1, x2) + eps
            and min(y1, y2) - eps <= y3 <= max(y1, y2) + eps
        )

    o1 = orient(ax1, ay1, ax2, ay2, bx1, by1)
    o2 = orient(ax1, ay1, ax2, ay2, bx2, by2)
    o3 = orient(bx1, by1, bx2, by2, ax1, ay1)
    o4 = orient(bx1, by1, bx2, by2, ax2, ay2)
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and (
        (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)
    ):
        return True
    return (
        (abs(o1) <= eps and on(ax1, ay1, ax2, ay2, bx1, by1))
        or (abs(o2) <= eps and on(ax1, ay1, ax2, ay2, bx2, by2))
        or (abs(o3) <= eps and on(bx1, by1, bx2, by2, ax1, ay1))
        or (abs(o4) <= eps and on(bx1, by1, bx2, by2, ax2, ay2))
    )


def _pick_offset(start, end, horizontal, candidates, geom_segs, used):
    for off in candidates:
        if horizontal:
            seg = (
                round(start[0], 2),
                round(start[1] - off, 2),
                round(end[0], 2),
                round(start[1] - off, 2),
            )
        else:
            seg = (
                round(start[0] + off, 2),
                round(start[1], 2),
                round(start[0] + off, 2),
                round(end[1], 2),
            )
        if any(_intersect(seg, g) for g in geom_segs) or any(_intersect(seg, u) for u in used):
            continue
        return off, seg
    off = candidates[-1]
    if horizontal:
        return off, (start[0], start[1] - off, end[0], start[1] - off)
    return off, (start[0] + off, start[1], start[0] + off, end[1])


def _dims(name, visible, hidden, actual):
    pts = [p for e in visible for p in e]
    if not pts:
        return []
    x0, x1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    h_cands = [-18, -26, -34, -42, 18, 26, 34, 42]
    v_cands = [18, 26, 34, 42, -18, -26, -34, -42]
    dims = []
    if name == "top":
        dims.append(((x0, y1), (x1, y1), True, f"{actual['width']:.4f}", h_cands))
        dims.append(((x1, y0), (x1, y1), False, f"{actual['depth']:.4f}", v_cands))
    elif name == "front":
        dims.append(((x1, y0), (x1, y1), False, f"{actual['height']:.4f}", v_cands))
    geom = _segments(list(visible) + list(hidden))
    used, out = [], []
    for start, end, horizontal, label, cands in dims:
        off, seg = _pick_offset(start, end, horizontal, cands, geom, used)
        used.append(seg)
        out.append((start, end, horizontal, label, off))
    return out


# ---------------------------------------------------------------- 繪圖
def _dotted(draw: ImageDraw.ImageDraw, pts, width):
    """SVG stroke-dasharray: 1,3＋圓端點：沿折線每 4 px 畫一個 1 px 長的圓頭短線。"""
    period, dash, carry = 4.0 * SS, 1.0 * SS, 0.0
    r = width / 2
    for (xa, ya), (xb, yb) in zip(pts, pts[1:], strict=False):
        seg = ((xb - xa) ** 2 + (yb - ya) ** 2) ** 0.5
        if seg == 0:
            continue
        t = carry
        while t < seg:
            s, e = t / seg, min(t + dash, seg) / seg
            p = (xa + (xb - xa) * s, ya + (yb - ya) * s)
            q = (xa + (xb - xa) * e, ya + (yb - ya) * e)
            draw.line([p, q], fill=0, width=round(width))
            for c in (p, q):
                draw.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=0)
            t += period
        carry = t - seg


def _solid(draw: ImageDraw.ImageDraw, pts, width):
    draw.line(pts, fill=0, width=round(width), joint="curve")
    r = width / 2
    for c in (pts[0], pts[-1]):
        draw.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=0)


def _dimension(draw: ImageDraw.ImageDraw, ox, oy, start, end, horizontal, label, off):
    def P(x, y):
        return ((ox + x) * SS, (oy + y) * SS)

    lw = max(1, round(0.8 * SS))
    font = _font(False)
    (x1, y1), (x2, y2) = start, end
    a = 3
    if horizontal:
        dy = y1 - off
        draw.line([P(x1, y1), P(x1, dy - 5)], fill=0, width=lw)
        draw.line([P(x2, y2), P(x2, dy - 5)], fill=0, width=lw)
        draw.line([P(x1, dy), P(x2, dy)], fill=0, width=lw)
        draw.polygon([P(x1, dy), P(x1 + a, dy - a / 2), P(x1 + a, dy + a / 2)], fill=0)
        draw.polygon([P(x2, dy), P(x2 - a, dy - a / 2), P(x2 - a, dy + a / 2)], fill=0)
        tx, ty = (x1 + x2) / 2, dy + 12
    else:
        dx = x1 + off
        draw.line([P(x1, y1), P(dx + 5, y1)], fill=0, width=lw)
        draw.line([P(x2, y2), P(dx + 5, y2)], fill=0, width=lw)
        draw.line([P(dx, y1), P(dx, y2)], fill=0, width=lw)
        draw.polygon([P(dx, y1), P(dx - a / 2, y1 + a), P(dx + a / 2, y1 + a)], fill=0)
        draw.polygon([P(dx, y2), P(dx - a / 2, y2 - a), P(dx + a / 2, y2 - a)], fill=0)
        tx, ty = dx + 30, (y1 + y2) / 2
    draw.text(P(tx, ty), label, fill=0, font=font, anchor="mm")


def render_ortho(shape) -> Image.Image:
    """OCP TopoDS_Shape（或 cq.Shape／cq.Workplane）→ 800×800 灰階三視圖。"""
    from OCP.gp import gp_Dir

    if hasattr(shape, "val"):  # cq.Workplane
        shape = shape.val()
    if hasattr(shape, "wrapped"):  # cq.Shape
        shape = shape.wrapped
    views = {
        "front": _view(shape, gp_Dir(0, 1, 0)),
        "top": _view(shape, gp_Dir(0, 0, 1)),
        "right": _view(shape, gp_Dir(1, 0, 0)),
    }
    actual = bbox_dims(shape)
    scale = _scale(views)

    img = Image.new("L", (CANVAS * SS, CANVAS * SS), 255)
    draw = ImageDraw.Draw(img)
    lw = 1.5 * SS
    for name, v in views.items():
        ox, oy = POSITIONS[name]
        placed = _place(v["visible"] + v["hidden"], scale)
        if name == "right":
            placed = _rotate_cw(placed)
        vis, hid = placed[: len(v["visible"])], placed[len(v["visible"]) :]
        draw.text(
            ((ox + VIEW / 2) * SS, (oy - 10) * SS),
            TITLES[name],
            fill=0,
            font=_font(True),
            anchor="ms",
        )
        for e in vis:
            _solid(draw, [((ox + x) * SS, (oy + y) * SS) for x, y in e], lw)
        for e in hid:
            _dotted(draw, [((ox + x) * SS, (oy + y) * SS) for x, y in e], lw)
        for d in _dims(name, vis, hid, actual):
            _dimension(draw, ox, oy, *d)
    return img.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS).convert("RGB")


# ---------------------------------------------------------------- 標題欄（知識庫圖紙用）
TITLE_H = 170


def with_title_block(drawing: Image.Image, f: dict) -> Image.Image:
    """在三視圖下方加上工廠圖紙的標題欄（品名、料號、圖號、材料、機密等級…）。

    上方 800×800 仍是 Ortho2CAD 的輸入格式；送模型前會把標題欄裁掉。
    """
    w, h = CANVAS * SS, TITLE_H * SS
    block = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(block)
    m, lw = 24 * SS, max(1, round(1.2 * SS))
    x0, y0, x1, y1 = m, 10 * SS, w - m, h - 18 * SS
    d.rectangle([x0, y0, x1, y1], outline=0, width=lw * 2)
    cols = [x0, x0 + 300 * SS, x0 + 520 * SS, x1]
    rows = [y0, y0 + (y1 - y0) / 3, y0 + 2 * (y1 - y0) / 3, y1]
    for x in cols[1:-1]:
        d.line([(x, y0), (x, y1)], fill=0, width=lw)
    for y in rows[1:-1]:
        d.line([(cols[1], y), (x1, y)], fill=0, width=lw)
    cjk, small = _font(False, 17, "cjk"), _font(False, 12, "cjk")
    big = _font(False, 22, "cjk")
    cx = (cols[0] + cols[1]) / 2
    d.text((cx, y0 + 30 * SS), f["company"], fill=0, font=cjk, anchor="mm")
    d.text((cx, y0 + 68 * SS), f["name"], fill=0, font=big, anchor="mm")
    d.text(
        (cx, y1 - 18 * SS),
        f"機密等級：{f['confidentiality']}　未經許可不得複製外流",
        fill=0,
        font=small,
        anchor="mm",
    )
    cells = [
        [("料號", f["part_no"]), ("圖號", f["drawing_no"])],
        [("材料", f["material"]), ("版次", f["revision"])],
        [("單位", "mm"), ("投影", "第一角法")],
    ]
    for r, row in enumerate(cells):
        for c, (k, v) in enumerate(row):
            left = cols[1 + c]
            mid = (rows[r] + rows[r + 1]) / 2
            d.text((left + 10 * SS, mid), k, fill=90, font=small, anchor="lm")
            d.text((left + 52 * SS, mid), v, fill=0, font=cjk, anchor="lm")
    out = Image.new("L", (w, CANVAS * SS + h), 255)
    out.paste(drawing.convert("L").resize((w, CANVAS * SS), Image.Resampling.LANCZOS), (0, 0))
    out.paste(block, (0, CANVAS * SS))
    return out.resize((CANVAS, CANVAS + TITLE_H), Image.Resampling.LANCZOS).convert("RGB")
