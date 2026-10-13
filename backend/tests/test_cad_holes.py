"""3D 重建的孔位校正（docs/adr/035）：OpenCV 從俯視圖找圓孔，取代模型自己畫的垂直圓孔。"""

import asyncio
import math
from pathlib import Path

import pytest
from PIL import Image

from app.cad.holes import detect_holes, hole_plan, strip_vertical_holes
from app.cad.sandbox import LIMITS_AVAILABLE, run_cad
from app.core.config import REPO_ROOT
from app.services.cad_service import hole_plan_for, wsl_target

WSL = None if LIMITS_AVAILABLE else wsl_target()
needs_limits = pytest.mark.skipif(
    not LIMITS_AVAILABLE and WSL is None,
    reason="這個平台沒有 resource 模組、也沒設 CAD_WSL_PYTHON，模型產生的程式碼不執行",
)
FIXTURES = Path(__file__).parent / "fixtures"
ORTHO2CAD_MFG006 = (FIXTURES / "ortho2cad_mfg-006.txt").read_text(encoding="utf-8")
ORTHO2CAD_MFG002 = (FIXTURES / "ortho2cad_mfg-002.txt").read_text(encoding="utf-8")
MFG006_DIMS = {"width": 120.0, "depth": 40.0, "height": 72.0}

# 標準模型（kb/cad/*.py）裡從上方看得到的垂直孔：(x, y, 半徑, z 下緣, z 上緣)，
# 座標以外形中心為原點（mm）。mfg-002 的凸轂、mfg-004 的長孔、外形圓都不是孔
GT_HOLES = {
    "mfg-001": [(x, y, 3.3, -30, -22) for x in (10, 28) for y in (-12, 12)],
    "mfg-002": [(0, 0, 15, -11, 11)]
    + [
        (38 * math.cos(math.radians(a)), 38 * math.sin(math.radians(a)), 4.5, -11, 1)
        for a in range(0, 360, 60)
    ],
    "mfg-003": [],
    "mfg-004": [(0, 0, 19.05, -5, 5)]
    + [(x, y, 2.75, -5, 5) for x in (-23.57, 23.57) for y in (-23.57, 23.57)],
    "mfg-005": [(0, 0, 4.25, -8.5, 8.5)],
    # 注油孔從頂部（72）鑽到軸承孔（Ø35，中心高 40）：孔壁 x＝±3 處的軸承孔頂 ≈ 57.2
    "mfg-006": [(-46, 0, 5.5, -36, -20), (46, 0, 5.5, -36, -20), (0, 0, 3, 21.2, 36)],
}
POS_TOL, D_TOL, Z_TOL = (
    1.0,
    0.6,
    2.0,
)  # mm（驗收條件是直徑誤差）；點線每 4 px 一點，z 端點最多差一個週期


def kb_drawing(part_id: str) -> Image.Image:
    return Image.open(REPO_ROOT / "kb" / "drawings" / f"{part_id}.png").convert("RGB")


def kb_dims(part_id: str) -> dict:
    """知識庫圖紙的標註尺寸（建索引時由標準模型量出，cad_service 的 scale_to 也用這個）。"""
    from app.repositories.index_store import get_store

    g = get_store().get_part(part_id)["geometry"]
    return {k: float(g[k]) for k in ("width", "depth", "height")}


def assert_same_holes(found: list[dict], expected: list[tuple]) -> None:
    assert len(found) == len(expected), found
    left = list(found)
    for x, y, r, z0, z1 in expected:
        best = min(left, key=lambda h: math.hypot(h["x"] - x, h["y"] - y))
        assert math.hypot(best["x"] - x, best["y"] - y) <= POS_TOL, (x, y, best)
        assert 2 * abs(best["r"] - r) <= D_TOL, (x, y, best)
        assert best["z0"] == pytest.approx(z0, abs=Z_TOL), (x, y, best)
        assert best["z1"] == pytest.approx(z1, abs=Z_TOL), (x, y, best)
        left.remove(best)


# ---------------------------------------------------------------- 偵測
@pytest.mark.parametrize("part_id", sorted(GT_HOLES))
def test_detects_kb_drawing_holes(part_id):
    assert_same_holes(detect_holes(kb_drawing(part_id), kb_dims(part_id)), GT_HOLES[part_id])


def test_detects_y_direction_and_rejects_bosses():
    """俯視圖上方＝+Y；凸台在俯視圖也是實線圓，但前視圖是實線不是點線，不算孔。"""
    import cadquery as cq

    from app.cad.drawing import render_ortho

    plate = cq.Workplane("XY").box(60, 40, 8, centered=(True, True, False))
    boss = cq.Workplane("XY").workplane(offset=8).center(-15, -8).circle(5).extrude(6)
    hole = cq.Workplane("XY").center(15, 10).circle(3).extrude(8)
    shape = plate.union(boss).cut(hole).val()
    found = detect_holes(render_ortho(shape), {"width": 60.0, "depth": 40.0, "height": 14.0})
    assert_same_holes(found, [(15, 10, 3, -7, 1)])


def test_hole_plan_needs_dims_and_holes():
    plan, reason = hole_plan(kb_drawing("mfg-006"), kb_dims("mfg-006"))
    assert reason is None and len(plan) == 3
    plan, reason = hole_plan(kb_drawing("mfg-006"), None)
    assert plan is None and "尺寸" in reason
    plan, reason = hole_plan(kb_drawing("mfg-003"), kb_dims("mfg-003"))
    assert plan is None and "沒有" in reason


def test_hole_plan_only_for_standard_drawings():
    """照片拉正後辨識出的知識庫圖紙也用標準三視圖；照片建檔（沒有標準模型）與未收錄的不校正。"""
    from app.repositories.index_store import get_store

    part = get_store().get_part("mfg-006")
    dims = kb_dims("mfg-006")
    for layout in ("kb", "rectified"):
        plan, reason = hole_plan_for(part, dims, layout)
        assert reason is None and len(plan) == 3, layout
    plan, reason = hole_plan_for({k: v for k, v in part.items() if k != "cad"}, dims, "kb")
    assert plan is None and "照片建檔" in reason
    plan, reason = hole_plan_for(None, dims, "upload")
    assert plan is None and "未收錄" in reason
    plan, reason = hole_plan_for({**part, "drawing": "kb/drawings/nope.png"}, dims, "kb")
    assert plan is None and "失敗" in reason  # 偵測出錯不影響重建


def test_hole_plan_skips_upload_layout_even_for_known_part():
    """已知零件但照片拉正失敗（版面 upload）：照片不是標準三視圖，不能套標準圖的孔位。"""
    from app.repositories.index_store import get_store

    plan, reason = hole_plan_for(get_store().get_part("mfg-006"), kb_dims("mfg-006"), "upload")
    assert plan is None and "upload" in reason and "維持模型輸出" in reason


# ---------------------------------------------------------------- 移除模型自己畫的垂直圓孔
PLANE_Z = "cq.Plane(cq.Vector(0.0, 0.0, 0.0), cq.Vector(1.0, 0.0, 0.0), cq.Vector(0.0, 0.0, 1.0))"
PLANE_Y = "cq.Plane(cq.Vector(0.0, 0.0, 0.0), cq.Vector(1.0, 0.0, 0.0), cq.Vector(0.0, -1.0, 0.0))"
RECT = ".moveTo(-0.5, -0.3).lineTo(0.5, -0.3).lineTo(0.5, 0.3).lineTo(-0.5, 0.3).close()"


def test_strip_removes_vertical_circle_cuts_from_ortho2cad_output():
    code, removed = strip_vertical_holes(ORTHO2CAD_MFG006)
    assert removed == 3
    assert "solid=solid.cut(" not in code.replace(" ", "")
    assert code.count(".union(") == ORTHO2CAD_MFG006.count(".union(")
    compile(code, "stripped.py", "exec")


def test_strip_removes_inner_circles_of_vertical_extrusions():
    code = (
        "import cadquery as cq\n"
        f"wp = cq.Workplane({PLANE_Z})\n"
        f"loop0 = wp{RECT}\n"
        "loop1 = wp.moveTo(0.2, 0.0).circle(0.05)\n"
        "solid0 = wp.add(loop0).add(loop1).extrude(0.1)\n"
        "solid = solid0\n"
    )
    out, removed = strip_vertical_holes(code)
    assert removed == 1
    # CadQuery 從同一個 workplane 畫的圓都會進 pending wires，要連畫圓那行一起刪
    assert "loop1" not in out and ".add(loop0)" in out


def test_strip_keeps_horizontal_holes_bosses_and_unknown_code():
    horizontal = (
        "import cadquery as cq\n"
        f"wp0 = cq.Workplane({PLANE_Z})\n"
        f"loop0 = wp0{RECT}\n"
        "solid0 = wp0.add(loop0).extrude(0.6)\n"
        "solid = solid0\n"
        f"wp1 = cq.Workplane({PLANE_Y})\n"
        "loop1 = wp1.moveTo(0.0, 0.3).circle(0.1)\n"
        "solid1 = wp1.add(loop1).extrude(0.3, both=True)\n"
        "solid = solid.cut(solid1)\n"
        f"wp2 = cq.Workplane({PLANE_Z})\n"
        "loop2 = wp2.moveTo(0.0, 0.0).circle(0.1)\n"
        "solid2 = wp2.add(loop2).extrude(0.8)\n"
        "solid = solid.union(solid2)\n"
    )
    assert strip_vertical_holes(horizontal) == (horizontal, 0)
    gt = (REPO_ROOT / "kb" / "cad" / "mfg-006.py").read_text(encoding="utf-8")
    assert strip_vertical_holes(gt) == (gt, 0)


# ---------------------------------------------------------------- 沙箱裡校正
def inside(shape, x: float, y: float, z: float) -> bool:
    import cadquery as cq

    return shape.isInside(cq.Vector(x, y, z), 1e-6)


def load_step(path: Path):
    import cadquery as cq

    return cq.importers.importStep(str(path)).val()


@needs_limits
def test_run_cad_snaps_ortho2cad_holes_to_drawing(tmp_path):
    """mfg-006 的 Ortho2CAD 實際輸出：右安裝孔偏到 +33.9 mm、注油孔在軸承孔下方往下鑽、孔都沒穿。"""
    holes = detect_holes(kb_drawing("mfg-006"), MFG006_DIMS)
    r = asyncio.run(run_cad(ORTHO2CAD_MFG006, tmp_path, scale_to=MFG006_DIMS, holes=holes, wsl=WSL))
    assert r.ok, r.error
    info = r.data["holes"]
    assert info["status"] == "applied" and info["removed"] == 3 and info["applied"] == 3
    shape = load_step(tmp_path / "model.step")
    bb = shape.BoundingBox()
    cx, cy = (bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2

    def at(x_mm: float, y_mm: float, z_mm: float):
        """圖紙座標（外形中心為原點，z 從底面算，mm）→ 重建模型座標（依外形等比例換算）。"""
        return (
            cx + x_mm * bb.xlen / 120,
            cy + y_mm * bb.ylen / 40,
            bb.zmin + z_mm * bb.zlen / 72,
        )

    for x in (-46, 46):
        assert not inside(shape, *at(x, 0, 8))  # 底座中段：孔在圖紙位置
        assert not inside(shape, *at(x, 0, 0.3))  # 貫穿底面
    assert inside(shape, *at(33.9, 0, 8))  # 原本偏掉的右安裝孔已填回
    assert not inside(shape, *at(0, 0, 69))  # 注油孔在頂部
    assert inside(shape, *at(10.8, 0, 10))  # 原本往下鑽的注油孔已填回
    assert inside(shape, *at(0, 0, 10))  # 軸承孔下方仍是實心


@needs_limits
def test_run_cad_replaces_inner_circle_holes(tmp_path):
    """mfg-002 的 Ortho2CAD 實際輸出：螺栓孔畫成法蘭盤草圖的內圈，4 個在 0°/90°/180°/270°，
    圖紙是 6 個在 0°/60°/…；校正後只剩圖紙上的 6 個。"""
    dims = {"width": 100.0, "depth": 100.0, "height": 22.0}
    holes = detect_holes(kb_drawing("mfg-002"), dims)
    r = asyncio.run(run_cad(ORTHO2CAD_MFG002, tmp_path, scale_to=dims, holes=holes, wsl=WSL))
    assert r.ok, r.error
    assert r.data["holes"]["applied"] == 7
    shape = load_step(tmp_path / "model.step")
    bb = shape.BoundingBox()
    cx, cy, z = (bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2, bb.zmin + 3
    f = bb.xlen / 100
    for a in range(0, 360, 60):
        x, y = 38 * math.cos(math.radians(a)), 38 * math.sin(math.radians(a))
        assert not inside(shape, cx + x * f, cy + y * f, z), a
    assert inside(shape, cx, cy + 34.5 * f, z)  # 模型原本 90° 的螺栓孔已填回
    assert inside(shape, cx, cy - 34.5 * f, z)
    assert not inside(shape, cx, cy, z)  # 中心孔


@needs_limits
def test_run_cad_keeps_model_when_shape_disagrees(tmp_path):
    """照片拉正後的 Ortho2CAD 實際輸出（mfg-002）：外形上下顛倒（高圓柱頂著薄盤），
    圖紙上的孔位上方都是材料。孔從上方看得到才會畫成實線圓，所以外形對不上：整個維持模型原樣。"""
    code = (FIXTURES / "ortho2cad_mfg-002_flipped.txt").read_text(encoding="utf-8")
    dims = {"width": 100.0, "depth": 100.0, "height": 22.0}
    holes = detect_holes(kb_drawing("mfg-002"), dims)
    plain = asyncio.run(run_cad(code, tmp_path / "a", scale_to=dims, wsl=WSL))
    snapped = asyncio.run(run_cad(code, tmp_path / "b", scale_to=dims, holes=holes, wsl=WSL))
    assert plain.ok and snapped.ok, (plain.error, snapped.error)
    info = snapped.data["holes"]
    assert info["status"] == "skipped" and "對不上" in info["reason"] and info["applied"] == 0
    assert snapped.data["volume"] == pytest.approx(plain.data["volume"], rel=1e-6)


@needs_limits
def test_run_cad_holes_are_idempotent_on_ground_truth(tmp_path):
    """標準模型的孔已經在正確位置：校正後體積不變（不會多鑽、也不會鑽穿別處）。"""
    gt = (REPO_ROOT / "kb" / "cad" / "mfg-006.py").read_text(encoding="utf-8")
    holes = detect_holes(kb_drawing("mfg-006"), MFG006_DIMS)
    plain = asyncio.run(run_cad(gt, tmp_path / "a", scale_to=MFG006_DIMS, trusted=True))
    snapped = asyncio.run(
        run_cad(gt, tmp_path / "b", scale_to=MFG006_DIMS, holes=holes, trusted=True)
    )
    assert plain.ok and snapped.ok, (plain.error, snapped.error)
    assert snapped.data["holes"]["removed"] == 0 and snapped.data["holes"]["applied"] == 3
    assert snapped.data["volume"] == pytest.approx(plain.data["volume"], rel=5e-3)


@needs_limits
def test_run_cad_skips_holes_when_orientation_differs(tmp_path):
    """模型把寬、深對調（深＞寬，圖紙是寬＞深）：孔位對不上，保持原樣並說明原因。"""
    code = "import cadquery as cq\nsolid = cq.Workplane('XY').box(0.3, 0.95, 0.57)\n"
    holes = detect_holes(kb_drawing("mfg-006"), MFG006_DIMS)
    r = asyncio.run(run_cad(code, tmp_path, scale_to=MFG006_DIMS, holes=holes, wsl=WSL))
    assert r.ok, r.error
    info = r.data["holes"]
    assert info["status"] == "skipped" and "方向" in info["reason"] and info["applied"] == 0


@needs_limits
def test_run_cad_without_holes_reports_nothing(tmp_path):
    code = "import cadquery as cq\nsolid = cq.Workplane('XY').box(1.0, 0.5, 0.25)\n"
    r = asyncio.run(run_cad(code, tmp_path, wsl=WSL))
    assert r.ok and r.data.get("holes") is None


@needs_limits
def test_reconstruct_stream_reports_hole_snapping(client):
    """LLM_MODE=mock 回傳標準模型：孔位校正照樣執行，結果帶校正資訊。"""
    import json

    r = client.post("/api/v1/cad/reconstruct", json={"part_id": "mfg-006"})
    events = [
        (lines["event"], json.loads(lines["data"]))
        for lines in (
            dict(line.split(": ", 1) for line in block.splitlines())
            for block in r.text.strip().split("\n\n")
        )
    ]
    result = next(d for e, d in events if e == "result")
    assert result["ok"], result["error"]
    holes = result["holes"]
    assert holes["status"] == "applied" and holes["detected"] == 3 and holes["applied"] == 3
    assert result["iou"] > 0.95


# ---------------------------------------------------------------- 退件修正（code review 56a8943）
def sketch(loops: str, adds: str, tail: str = "solid = solid0\n", plane: str = PLANE_Z) -> str:
    return (
        "import cadquery as cq\n"
        f"wp = cq.Workplane({plane})\n{loops}solid0 = wp{adds}.extrude(0.1)\n{tail}"
    )


CIRCLE = "loop{n} = wp.moveTo({x}, {y}).circle({r})\n"
HALF_DISC = (
    "loop0 = wp.moveTo(0.0, 0.0).lineTo(1.0, 0.0).threePointArc((0.5, 0.5), (0.0, 0.0)).close()\n"
)


@pytest.mark.parametrize(
    ("loops", "adds", "removed"),
    [
        # 外形矩形裡的圓：內圈孔
        (
            f"loop0 = wp{RECT}\n" + CIRCLE.format(n=1, x=0.2, y=0.0, r=0.05),
            ".add(loop0).add(loop1)",
            1,
        ),
        # 同一草圖裡另一個互不包含的圓：凸台，不是孔
        (
            CIRCLE.format(n=0, x=-0.5, y=0.0, r=0.2) + CIRCLE.format(n=1, x=0.5, y=0.0, r=0.2),
            ".add(loop0).add(loop1)",
            0,
        ),
        # 矩形旁邊的圓（外形之外）
        (
            f"loop0 = wp{RECT}\n" + CIRCLE.format(n=1, x=2.0, y=0.0, r=0.1),
            ".add(loop0).add(loop1)",
            0,
        ),
        # 壓在矩形邊上：沒有完全落在裡面
        (
            f"loop0 = wp{RECT}\n" + CIRCLE.format(n=1, x=0.5, y=0.0, r=0.1),
            ".add(loop0).add(loop1)",
            0,
        ),
        # 圓外圈＋內圈孔＋另一個分離的凸台：只刪內圈
        (
            CIRCLE.format(n=0, x=0.0, y=0.0, r=1.0)
            + CIRCLE.format(n=1, x=0.3, y=0.0, r=0.1)
            + CIRCLE.format(n=2, x=3.0, y=0.0, r=0.5),
            ".add(loop0).add(loop1).add(loop2)",
            1,
        ),
        # 外形含圓弧：圓在弧內 → 孔；在弧外 → 凸台
        (HALF_DISC + CIRCLE.format(n=1, x=0.5, y=0.2, r=0.05), ".add(loop0).add(loop1)", 1),
        (HALF_DISC + CIRCLE.format(n=1, x=0.5, y=0.6, r=0.05), ".add(loop0).add(loop1)", 0),
        # 圈裡還有圈（外圈、中圈、內圈同心）：內外圈怎麼配對沒把握，整張草圖保留
        (
            CIRCLE.format(n=0, x=0.0, y=0.0, r=1.0)
            + CIRCLE.format(n=1, x=0.0, y=0.0, r=0.6)
            + CIRCLE.format(n=2, x=0.0, y=0.0, r=0.2),
            ".add(loop0).add(loop1).add(loop2)",
            0,
        ),
        # 座標不是常數：確認不了包含關係，保留
        (
            "k = 0.2\n" + f"loop0 = wp{RECT}\n" + "loop1 = wp.moveTo(k, 0.0).circle(0.05)\n",
            ".add(loop0).add(loop1)",
            0,
        ),
    ],
    ids=[
        "inner-hole",
        "separate-bosses",
        "beside-outline",
        "straddles-edge",
        "hole-and-boss",
        "inside-arc",
        "outside-arc",
        "island",
        "unknown-coords",
    ],
)
def test_strip_removes_only_circles_confirmed_inside_an_outer_loop(loops, adds, removed):
    code = sketch(loops, adds)
    out, n = strip_vertical_holes(code)
    assert n == removed
    if not removed:
        assert out == code
    else:
        compile(out, "stripped.py", "exec")
        assert ".add(loop0)" in out


BOSSES = """import cadquery as cq
solid = cq.Workplane('XY').box(60, 40, 8, centered=(True, True, False))
wp0 = cq.Workplane(cq.Plane(cq.Vector(0, 0, 8), cq.Vector(1, 0, 0), cq.Vector(0, 0, 1)))
loop0 = wp0.moveTo(-15, 0).circle(5)
loop1 = wp0.moveTo(15, 0).circle(5)
solid0 = wp0.add(loop0).add(loop1).extrude(6)
solid = solid.union(solid0)
wp1 = cq.Workplane('XY')
loop2 = wp1.moveTo(0, 0).circle(3)
solid1 = wp1.add(loop2).extrude(8)
solid = solid.cut(solid1)
"""


def test_strip_keeps_separate_circular_bosses_of_one_sketch():
    out, removed = strip_vertical_holes(BOSSES)
    assert removed == 1  # 只有 wp1 的圓孔
    assert "wp0.add(loop0).add(loop1).extrude(6)" in out and "loop1 = " in out
    assert "solid.cut(" not in out


@needs_limits
def test_run_cad_keeps_separate_bosses_when_snapping(tmp_path):
    """同一草圖兩個分離的圓凸台（高 6 mm）＋一個垂直孔：校正孔位不能把凸台當內圈孔刪掉。"""
    dims = {"width": 60.0, "depth": 40.0, "height": 14.0}
    hole = {"x": 0.0, "y": 0.0, "r": 3.0, "z0": -7.0, "z1": 1.0}
    plain = asyncio.run(run_cad(BOSSES, tmp_path / "a", scale_to=dims, wsl=WSL))
    snapped = asyncio.run(run_cad(BOSSES, tmp_path / "b", scale_to=dims, holes=[hole], wsl=WSL))
    assert plain.ok and snapped.ok, (plain.error, snapped.error)
    assert snapped.data["holes"]["removed"] == 1 and snapped.data["holes"]["applied"] == 1
    shape = load_step(tmp_path / "b" / "model.step")
    for x in (-15, 15):
        assert inside(shape, x, 0, 11), x  # 兩個凸台都在
    assert not inside(shape, 0, 0, 4)  # 孔
    assert snapped.data["volume"] == pytest.approx(plain.data["volume"], rel=1e-6)


def test_strip_keeps_ring_cut_but_removes_separate_circle_cuts():
    ring = sketch(
        CIRCLE.format(n=0, x=0.0, y=0.0, r=0.5) + CIRCLE.format(n=1, x=0.0, y=0.0, r=0.3),
        ".add(loop0).add(loop1)",
        tail="solid = solid.cut(solid0)\n",
    )
    assert strip_vertical_holes(ring) == (ring, 0)  # 環形槽不是孔
    pair = sketch(
        CIRCLE.format(n=0, x=-0.5, y=0.0, r=0.1) + CIRCLE.format(n=1, x=0.5, y=0.0, r=0.1),
        ".add(loop0).add(loop1)",
        tail="solid = solid.cut(solid0)\n",
    )
    out, removed = strip_vertical_holes(pair)
    assert removed == 2 and "solid.cut(" not in out


@pytest.mark.parametrize(
    ("normal", "removed"),
    [
        ("0.0, 0.0, 1.0", 1),
        ("0.0, 0.0, -1.0", 1),
        ("1e-12, -3e-13, 1.0", 1),  # 浮點微誤差（Ortho2CAD 的 6.1e-17 之類）
        ("0.1, 0.0, 1.0", 0),  # 傾斜 5.7°
        ("0.0, 0.01, 1.0", 0),  # 傾斜 0.57°
        ("0.0, 0.0, 0.0", 0),  # 零向量不是方向
    ],
)
def test_strip_removes_only_axis_aligned_vertical_holes(normal, removed):
    plane = f"cq.Plane(cq.Vector(0.0, 0.0, 0.0), cq.Vector(0.0, 1.0, 0.0), cq.Vector({normal}))"
    code = sketch(
        CIRCLE.format(n=0, x=0.0, y=0.0, r=0.1),
        ".add(loop0)",
        tail="solid = solid.cut(solid0)\n",
        plane=plane,
    )
    out, n = strip_vertical_holes(code)
    assert n == removed
    assert (out != code) == bool(removed)


def box(x, y, z, w, d, h):
    import cadquery as cq

    return cq.Workplane("XY").box(w, d, h, centered=False).translate((x, y, z))


DIMS_50 = {"width": 60.0, "depth": 40.0, "height": 50.0}
_PLANS: dict = {}


def detected_plan(key, base, hole_z0=40.0):
    """參考模型（只在上板 hole_z0…50 鑽中心 Ø6 孔）真實渲染、偵測的圖紙孔位；同一組幾何只算一次。"""
    import cadquery as cq

    from app.cad.drawing import render_ortho

    if key not in _PLANS:
        reference = base.val().cut(
            cq.Solid.makeCylinder(3, 50 - hole_z0, cq.Vector(0, 0, hole_z0), cq.Vector(0, 0, 1))
        )
        _PLANS[key] = detect_holes(render_ortho(reference), DIMS_50)
        assert len(_PLANS[key]) == 1, _PLANS[key]
    return _PLANS[key]


def stacked_plates(gap: float, *, pillar: float | None = None, web: bool = False):
    """上板 z＝40…50、下板在它下面隔 gap 的空隙（厚 10），兩板靠遠處的側壁相連。
    pillar：孔位中心一根方柱（邊長）連著兩板；web：孔邊一根細立壁（x＝2…2.5、y＝±0.25）貫穿空隙。
    回 (實體, 圖紙孔位)；圖紙孔位來自沒有柱、沒有立壁的參考模型。"""
    base = box(-30, -20, 40, 60, 40, 10).union(box(-30, -20, 30 - gap, 60, 40, 10))
    base = base.union(box(-30, -20, 0, 5, 40, 50))
    plan = detected_plan(("stacked", gap), base)
    model = base
    if pillar:
        model = model.union(box(-pillar / 2, -pillar / 2, 30 - gap, pillar, pillar, 10 + gap))
    if web:
        model = model.union(box(2, -0.25, 30 - gap, 0.5, 0.5, 20 + gap))
    return model.val(), plan


def assert_untouched(out, applied, reason, shape, *, why=None):
    """維持模型原樣：沒有校正、體積不變、有原因。"""
    assert applied == 0 and reason and "維持模型輸出" in reason, (applied, reason)
    if why:
        assert why in reason, reason
    assert out.Volume() == pytest.approx(shape.Volume(), rel=1e-12)


@pytest.mark.parametrize("gap", [3.0, 1.0, 0.2])
def test_apply_holes_drills_only_the_first_plate(gap):
    """上板＋空隙＋下板：孔徑圓柱和實體求交，上板是從頂面往下連通的第一塊，底部和圖紙孔深對得上。
    切孔停在上板下緣，不論空隙多小，下板都完整。"""
    from app.cad.holes import apply_holes

    shape, plan = stacked_plates(gap)
    out, applied, reason = apply_holes(shape, plan, DIMS_50)
    assert (applied, reason) == (1, None)
    assert not inside(out, 0, 0, 45) and not inside(out, 0, 0, 40.3)  # 上板鑽穿到下緣
    for z in (30 - gap + 0.5, 39.9 - gap):
        assert inside(out, 0, 0, z) and inside(out, 2.9, 0, z)  # 空隙下面的下板完整
    assert out.Volume() == pytest.approx(shape.Volume() - math.pi * 3**2 * 10, rel=2e-3)


def test_apply_holes_drills_only_the_first_of_three_plates():
    from app.cad.holes import apply_holes

    base = box(-30, -20, 40, 60, 40, 10).union(box(-30, -20, 28, 60, 40, 10))
    base = base.union(box(-30, -20, 16, 60, 40, 10)).union(box(-30, -20, 0, 5, 40, 50))
    plan = detected_plan("three", base)
    out, applied, reason = apply_holes(base.val(), plan, DIMS_50)
    assert (applied, reason) == (1, None)
    assert not inside(out, 0, 0, 45)
    assert inside(out, 0, 0, 37.5) and inside(out, 0, 0, 33) and inside(out, 0, 0, 21)


@pytest.mark.parametrize("gap", [1.0, 0.2])
def test_apply_holes_skips_when_the_first_block_ends_away_from_the_drawn_depth(gap):
    """圖紙孔底已經在下板裡（比上板下緣低），第一塊的底部和圖紙孔深差超過容差：不校正，
    維持模型原樣並說明原因。"""
    from app.cad.holes import apply_holes

    depth = 5.0  # 圖紙孔底在上板下緣之下 5 mm
    plan = [{"x": 0.0, "y": 0.0, "r": 3.0, "z0": 40 - depth - 25, "z1": 25.0}]
    shape, _ = stacked_plates(gap)
    out, applied, reason = apply_holes(shape, plan, DIMS_50)
    assert_untouched(out, applied, reason, shape, why="下一段材料")


@pytest.mark.parametrize(
    ("gap", "existing_r", "cut"),
    [(3.0, 1.0, True), (3.0, 2.9, False), (3.0, 3.0, False), (3.0, 4.0, False)]
    + [(1.0, 1.0, True), (1.0, 2.9, False), (1.0, 3.0, False), (1.0, 4.0, False)],
)
def test_apply_holes_existing_upper_hole_never_moves_cut_to_second_plate(gap, existing_r, cut):
    """上板已經有孔（比圖紙小、差不多、剛好、比圖紙大）：比圖紙小的孔擴到圖紙孔徑，其餘已經
    鑽好，不切；不論哪一種，下板都完整。"""
    import cadquery as cq

    from app.cad.holes import apply_holes

    shape, plan = stacked_plates(gap)
    drilled = shape.cut(
        cq.Solid.makeCylinder(existing_r, 10, cq.Vector(0, 0, 40), cq.Vector(0, 0, 1))
    )
    out, applied, reason = apply_holes(drilled, plan, DIMS_50)
    assert (applied, reason) == (1, None)
    lower = cq.Solid.makeBox(60, 40, 10, cq.Vector(-30, -20, 30 - gap))
    assert out.intersect(lower).Volume() == pytest.approx(drilled.intersect(lower).Volume())
    assert not inside(out, 2.5, 0, 45)  # 上板的孔到圖紙的孔徑（比圖紙大的孔不會縮小）
    if not cut:
        assert out.Volume() == pytest.approx(drilled.Volume(), rel=1e-9)


@pytest.mark.parametrize("gap", [3.0, 1.0])
def test_apply_holes_skips_when_a_web_connects_the_plates_inside_the_bore(gap):
    """孔徑內一根細立壁把兩板連成同一塊：從頂面往下連通的第一塊一路到下板底，和圖紙孔深對不上：
    不校正，維持模型原樣並說明原因。"""
    from app.cad.holes import apply_holes

    shape, plan = stacked_plates(gap, web=True)
    out, applied, reason = apply_holes(shape, plan, DIMS_50)
    assert_untouched(out, applied, reason, shape, why="下一段材料")


@pytest.mark.parametrize("size", [3.0, 4.0, 4.8, 5.4])
def test_apply_holes_skips_when_a_dense_pillar_bridges_the_plates(size):
    """兩板之間一根方柱佔孔徑的三成到九成多（邊長 3～5.4 mm，4.8 mm 約 82% 是審查的重現）：
    柱子把兩板連成同一塊，不論佔多少比例都不校正，下板孔側一點都不能少。"""
    from app.cad.holes import apply_holes

    shape, plan = stacked_plates(1.0, pillar=size)
    out, applied, reason = apply_holes(shape, plan, DIMS_50)
    assert_untouched(out, applied, reason, shape, why="下一段材料")
    assert inside(out, 2.8, 0, 38.75)  # 孔側的下板


@pytest.mark.parametrize("kind", ["horizontal-hole", "sphere"])
def test_apply_holes_skips_when_a_cavity_leaves_the_block_connected(kind):
    """孔徑內有水平通孔（Ø4，中心 z＝39）或球形腔（半徑 2，中心 z＝39），材料比例在曲面上先降後升
    （審查 0219543 的重現）：孔徑圓柱和實體的交集仍連成一塊，一路到底面，和圖紙孔深（10 mm 深）
    對不上：不校正，體積與腔下方重新出現的材料都不變。"""
    import cadquery as cq

    from app.cad.drawing import render_ortho
    from app.cad.holes import apply_holes

    dims = {"width": 60.0, "depth": 40.0, "height": 50.0}
    block = cq.Workplane("XY").box(60, 40, 50, centered=(True, True, False)).val()
    reference = block.cut(cq.Solid.makeCylinder(3, 10, cq.Vector(0, 0, 40), cq.Vector(0, 0, 1)))
    plan = detect_holes(render_ortho(reference), dims)
    assert len(plan) == 1, plan
    if kind == "horizontal-hole":
        cavity = cq.Solid.makeCylinder(2, 60, cq.Vector(0, -30, 39), cq.Vector(0, 1, 0))
    else:
        cavity = cq.Solid.makeSphere(2, cq.Vector(0, 0, 39))
    shape = block.cut(cavity)
    out, applied, reason = apply_holes(shape, plan, dims)
    assert_untouched(out, applied, reason, shape, why="下一段材料")
    assert inside(shape, 1.99, 0, 38.661) and inside(out, 1.99, 0, 38.661)  # 腔下方重新出現的材料


def test_apply_holes_skips_a_true_blind_hole():
    """真正的盲孔：圖紙只有 10 mm 深的孔，模型是實心塊體一路到底。第一塊的底部（底面）和圖紙孔深
    對不上，不校正（人類接受）。"""
    import cadquery as cq

    from app.cad.holes import apply_holes

    dims = {"width": 60.0, "depth": 40.0, "height": 50.0}
    block = cq.Workplane("XY").box(60, 40, 50, centered=(True, True, False)).val()
    plan = [{"x": 0.0, "y": 0.0, "r": 3.0, "z0": 15.0, "z1": 25.0}]
    out, applied, reason = apply_holes(block, plan, dims)
    assert_untouched(out, applied, reason, block, why="下一段材料")


_LOWER = {"base_top": 29.0}


def layered_model(*pads):
    """上板 z＝40…50（60×40）、下層基座 z＝0…29，遠處立壁相連；pads 是下層上的凸起／肋
    (x, y, w, d, 頂面 z)，從 z＝29 長到頂面。回 (實體, 圖紙孔位, 外形)：圖紙孔位是參考模型
    （只在上板鑽中心 Ø6 孔）真實渲染、偵測的結果。"""
    base = box(-30, -20, 40, 60, 40, 10).union(box(-30, -20, 0, 60, 40, 29))
    base = base.union(box(-30, -20, 0, 5, 40, 50))
    plan = detected_plan("layered", base)
    model = base
    for x, y, w, d, top in pads:
        model = model.union(box(x, y, 29, w, d, top - 29))
    return model.val(), plan, DIMS_50


def lower_volume_kept(before, after, below: float) -> tuple[float, float]:
    """z＜below 的材料體積：切孔前後（切孔不該動到這個範圍）。"""
    import cadquery as cq

    zone = cq.Solid.makeBox(60, 40, below, cq.Vector(-30, -20, 0))
    return before.intersect(zone).Volume(), after.intersect(zone).Volume()


def check_pads(pads):
    """凸起都和上板隔著空隙（頂面低於上板下緣）：上板是第一塊，切孔停在它的下緣，凸起與下層完整。
    凸起的頂面碰到上板（頂面＝40）：連成同一塊，和圖紙孔深對不上，不校正。"""
    from app.cad.holes import apply_holes

    shape, plan, dims = layered_model(*pads)
    out, applied, reason = apply_holes(shape, plan, dims)
    before, after = lower_volume_kept(shape, out, 39.9)
    assert after == pytest.approx(before, rel=1e-9), pads
    if any(top >= 40.0 for *_, top in pads):
        assert_untouched(out, applied, reason, shape, why="下一段材料")
    else:
        assert (applied, reason) == (1, None), pads
        assert not inside(out, 0, 0, 45), pads


def test_apply_holes_lower_pad_between_old_sample_lines_is_never_cut():
    """下層基座上一個局部凸起（x＝1…1.4、y＝0.7…1.1、頂面 z＝39，與上板隔 1 mm）落在孔徑內：
    審查用固定取樣線漏掉了它，容差切傷凸起。凸起和上板不相連，切孔停在上板下緣，凸起完整。"""
    from app.cad.holes import apply_holes

    shape, plan, dims = layered_model((1.0, 0.7, 0.4, 0.4, 39.0))
    assert inside(shape, 1.2, 0.9, 38.75)
    out, applied, reason = apply_holes(shape, plan, dims)
    assert (applied, reason) == (1, None)
    assert not inside(out, 0, 0, 45) and not inside(out, 2.5, 0, 45)
    assert inside(out, 1.2, 0.9, 38.75)  # 凸起完整
    before, after = lower_volume_kept(shape, out, 39.9)
    assert after == pytest.approx(before, rel=1e-9)


@pytest.mark.parametrize(
    "pads",
    [
        [(2.0, -0.2, 0.8, 0.4, 39.9)],  # 凸起頂面離上板只有 0.1 mm
        [(-1.0, -2.6, 0.3, 2.0, 38.0)],  # 細肋
        [(0.5, 0.5, 2.0, 2.0, 36.0)],  # 大一點的凸台
        [(-2.7, 0.3, 0.4, 0.4, 39.0), (2.6, -0.5, 0.3, 0.3, 39.5), (0.1, -2.5, 0.2, 0.2, 38.5)],
        [(0.0, 0.0, 0.2, 0.2, 39.95)],  # 正在孔軸上，頂面幾乎貼著上板
        [(-2.9, -0.5, 5.8, 1.0, 38.0)],  # 一條橫過孔徑的肋
        [(-2.9, -0.5, 5.8, 1.0, 40.0)],  # 一條肋橫過孔徑，連到上板（約 21%）：連成一塊，不校正
        [(-2.2, -2.2, 4.4, 4.4, 40.0)],  # 一根粗柱連到上板（約 75%）：連成一塊，不校正
    ],
    ids=[
        "almost-touching",
        "thin-rib",
        "boss",
        "three-pads",
        "on-axis",
        "wide-rib",
        "bridge-rib",
        "bridge-pillar",
    ],
)
def test_apply_holes_lower_features_never_get_cut(pads):
    check_pads(pads)


def test_apply_holes_random_lower_features_are_never_cut():
    """亂數放進孔徑內的下層小凸起／肋（固定種子，頂面都低於上板下緣）：切孔停在上板下緣，
    下層體積一律不變。"""
    import random

    rng = random.Random(20261011)
    for _ in range(16):
        pads = []
        for _ in range(rng.randint(1, 4)):
            w, d = rng.uniform(0.15, 1.2), rng.uniform(0.15, 1.2)
            while True:
                x, y = rng.uniform(-3, 3), rng.uniform(-3, 3)
                if (abs(x) + w) ** 2 + (abs(y) + d) ** 2 < 2.9**2:
                    break
            pads.append((x, y, w, d, rng.uniform(30.0, 39.9)))
        check_pads(pads)


def test_apply_holes_opens_the_hole_fully_into_a_curved_ceiling():
    """孔通到下面的橫向圓孔（天花板是曲面，越靠孔邊越低）：孔要在整個孔徑通到橫孔，
    不能因為只切到曲面最高點而在孔邊留一圈材料；橫孔下方的材料不能碰。"""
    import cadquery as cq

    from app.cad.drawing import render_ortho
    from app.cad.holes import apply_holes

    dims = {"width": 60.0, "depth": 40.0, "height": 40.0}
    block = cq.Workplane("XY").box(60, 40, 40, centered=(True, True, False)).val()
    bore = cq.Solid.makeCylinder(10, 60, cq.Vector(0, -30, 15), cq.Vector(0, 1, 0))
    model = block.cut(bore)
    reference = model.cut(cq.Solid.makeCylinder(3, 20, cq.Vector(0, 0, 22), cq.Vector(0, 0, 1)))
    plan = detect_holes(render_ortho(reference), dims)
    assert len(plan) == 1, plan
    out, applied, reason = apply_holes(model, plan, dims)
    assert (applied, reason) == (1, None)
    assert not inside(out, 0, 0, 30) and not inside(out, 2.8, 0, 35)  # 注油孔
    ceiling_at_rim = 15 + (100 - 2.8**2) ** 0.5  # x＝2.8 處橫孔頂 ≈ 24.6
    assert not inside(out, 2.8, 0, ceiling_at_rim + 0.05)  # 孔邊也通到橫孔
    assert inside(out, 0, 0, 3) and inside(out, 12, 0, 15)  # 橫孔下方、旁邊的材料沒動


def test_apply_holes_cuts_a_sheet_thinner_than_the_margin():
    """頂部的薄板（1 mm）比 3% 容差（1.5 mm）還薄：孔照樣要切穿，不能被當成『孔已經在那裡』。"""
    import cadquery as cq

    from app.cad.holes import apply_holes

    def box(x, y, z, w, d, h):
        return cq.Workplane("XY").box(w, d, h, centered=False).translate((x, y, z))

    shape = (
        box(-30, -20, 0, 60, 40, 40)
        .union(box(-30, -20, 49, 60, 40, 1))
        .union(box(-30, -20, 0, 5, 40, 50))
    ).val()
    dims = {"width": 60.0, "depth": 40.0, "height": 50.0}
    plan = [{"x": 0.0, "y": 0.0, "r": 3.0, "z0": 24.0, "z1": 25.0}]
    out, applied, reason = apply_holes(shape, plan, dims)
    assert (applied, reason) == (1, None)
    assert not inside(out, 0, 0, 49.5)  # 薄板被鑽穿
    assert inside(out, 0, 0, 39.5)  # 下面的塊體沒被碰


def test_apply_holes_counts_existing_hole_without_cutting():
    """模型自己已經在同一個位置鑽穿了孔：算校正完成，不必再切。"""
    import cadquery as cq

    from app.cad.holes import apply_holes

    dims = {"width": 60.0, "depth": 40.0, "height": 10.0}
    plate = cq.Workplane("XY").box(60, 40, 10, centered=(True, True, False)).val()
    drilled = plate.cut(cq.Solid.makeCylinder(3, 12, cq.Vector(0, 0, -1), cq.Vector(0, 0, 1)))
    out, applied, reason = apply_holes(
        drilled, [{"x": 0, "y": 0, "r": 3, "z0": -5.0, "z1": 5.0}], dims
    )
    assert (applied, reason) == (1, None)
    assert out.Volume() == pytest.approx(drilled.Volume(), rel=1e-6)


def test_apply_holes_skips_hole_outside_model():
    import cadquery as cq

    from app.cad.holes import apply_holes

    dims = {"width": 60.0, "depth": 40.0, "height": 10.0}
    plate = cq.Workplane("XY").box(60, 40, 10, centered=(True, True, False)).val()
    out, applied, reason = apply_holes(
        plate, [{"x": 50.0, "y": 0, "r": 3, "z0": -5.0, "z1": 5.0}], dims
    )
    assert applied == 0 and "模型外" in reason and out.Volume() == pytest.approx(plate.Volume())


# ---------------------------------------------------------------- 退件修正（architecture b3bf22e）
# 孔位校正是全有或全無：任何一個計畫孔做不到，就在切任何孔之前整體回退（ADR 035）
BLOCK_50 = {"width": 50.0, "depth": 50.0, "height": 50.0}
GOOD_HOLE = {"x": 0.0, "y": 0.0, "r": 3.0, "z0": -25.0, "z1": 25.0}  # 實心塊體的貫穿孔
OUTSIDE_HOLE = {"x": 100.0, "y": 0.0, "r": 3.0, "z0": -25.0, "z1": 25.0}
BLIND_HOLE = {"x": 10.0, "y": 0.0, "r": 3.0, "z0": -10.0, "z1": 25.0}  # 圖紙孔底在塊體中間


def solid_block():
    return box(-25, -25, 0, 50, 50, 50).val()


def test_apply_holes_good_hole_alone_is_cut():
    """對照組：好孔單獨給時照常切，後面的回退測試才有意義。"""
    from app.cad.holes import apply_holes

    block = solid_block()
    out, applied, reason = apply_holes(block, [GOOD_HOLE], BLOCK_50)
    assert (applied, reason) == (1, None)
    assert not inside(out, 0, 0, 25)
    assert out.Volume() == pytest.approx(block.Volume() - math.pi * 3**2 * 50, rel=1e-3)


@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize(
    ("bad", "why"),
    [(OUTSIDE_HOLE, "模型外"), (BLIND_HOLE, "對不上")],
    ids=["outside-the-model", "blind-hole"],
)
def test_apply_holes_rolls_back_every_hole_when_one_cannot_be_applied(bad, why, bad_first):
    """architecture b3bf22e 的重現：50 mm 塊體，中心貫穿孔可切，另一個孔落在模型外／圖紙是盲孔。
    不論先後，一律 applied＝0、原因寫明、形狀不變（好孔也不能切），runner 才會用原程式碼重建。"""
    from app.cad.holes import apply_holes

    block = solid_block()
    plan = [bad, GOOD_HOLE] if bad_first else [GOOD_HOLE, bad]
    out, applied, reason = apply_holes(block, plan, BLOCK_50)
    assert_untouched(out, applied, reason, block, why=why)
    assert inside(out, 0, 0, 25)  # 好孔也沒切


def test_apply_holes_rolls_back_when_the_bore_finds_no_material(monkeypatch):
    """孔徑圓柱裡拆不出材料塊（理論上不會發生的防線）：同樣整體回退，不能只略過這個孔。"""
    from app.cad import holes as holes_module

    real = holes_module._bore_blocks
    monkeypatch.setattr(
        holes_module, "_bore_blocks", lambda s, x, y, r, bb: [] if x > 5 else real(s, x, y, r, bb)
    )
    block = solid_block()
    out, applied, reason = holes_module.apply_holes(
        block, [GOOD_HOLE, {**GOOD_HOLE, "x": 10.0}], BLOCK_50
    )
    assert_untouched(out, applied, reason, block, why="沒有碰到材料")
    assert inside(out, 0, 0, 25)


def test_apply_holes_counts_existing_hole_next_to_a_cut():
    """反向對照：模型已鑽好的孔不是失敗，和要切的孔並存時兩個都算（applied＝2）。"""
    import cadquery as cq

    from app.cad.holes import apply_holes

    bore = cq.Solid.makeCylinder(3, 60, cq.Vector(10, 0, -5), cq.Vector(0, 0, 1))
    drilled = solid_block().cut(bore)
    existing = {**GOOD_HOLE, "x": 10.0}
    out, applied, reason = apply_holes(drilled, [GOOD_HOLE, existing], BLOCK_50)
    assert (applied, reason) == (2, None)
    assert not inside(out, 0, 0, 25) and not inside(out, 10, 0, 25)


PLATE_WITH_MODEL_HOLE = """import cadquery as cq
solid = cq.Workplane('XY').box(60, 40, 10, centered=(True, True, False))
wp1 = cq.Workplane('XY')
loop1 = wp1.moveTo(-10, 0).circle(3)
solid1 = wp1.add(loop1).extrude(10)
solid = solid.cut(solid1)
"""
PLATE_DIMS = {"width": 60.0, "depth": 40.0, "height": 10.0}


@needs_limits
def test_run_cad_restores_model_holes_when_one_planned_hole_fails(tmp_path):
    """模型在 x＝−10 畫了垂直孔（校正會先移除）。圖紙孔位在 x＝+10，另一個孔落在模型外：
    整體回退，模型原本的孔恢復、+10 的孔不切、結果寫明跳過原因。只給好孔時照常校正。"""
    good = {"x": 10.0, "y": 0.0, "r": 3.0, "z0": -5.0, "z1": 5.0}
    outside = {"x": 100.0, "y": 0.0, "r": 3.0, "z0": -5.0, "z1": 5.0}
    plain = asyncio.run(
        run_cad(PLATE_WITH_MODEL_HOLE, tmp_path / "a", scale_to=PLATE_DIMS, wsl=WSL)
    )
    ok = asyncio.run(
        run_cad(PLATE_WITH_MODEL_HOLE, tmp_path / "b", scale_to=PLATE_DIMS, holes=[good], wsl=WSL)
    )
    bad = asyncio.run(
        run_cad(
            PLATE_WITH_MODEL_HOLE,
            tmp_path / "c",
            scale_to=PLATE_DIMS,
            holes=[good, outside],
            wsl=WSL,
        )
    )
    assert plain.ok and ok.ok and bad.ok, (plain.error, ok.error, bad.error)

    assert ok.data["holes"]["status"] == "applied" and ok.data["holes"]["removed"] == 1
    snapped = load_step(tmp_path / "b" / "model.step")
    assert inside(snapped, -10, 0, 5) and not inside(snapped, 10, 0, 5)  # 孔搬到圖紙位置

    info = bad.data["holes"]
    assert info["status"] == "skipped" and info["applied"] == 0 and info["removed"] == 0
    assert "模型外" in info["reason"] and info["detected"] == 2
    kept = load_step(tmp_path / "c" / "model.step")
    assert not inside(kept, -10, 0, 5)  # 模型自己的孔恢復
    assert inside(kept, 10, 0, 5)  # 好孔沒有單獨生效
    assert bad.data["volume"] == pytest.approx(plain.data["volume"], rel=1e-9)


@needs_limits
def test_reconstruct_stream_reports_skipped_reason_when_one_hole_fails(client, monkeypatch):
    """SSE 結果：多孔計畫中一個孔落在模型外，整個校正跳過、帶原因，3D 維持標準模型。"""
    import json

    from app.services import cad_service

    real = cad_service.hole_plan_for

    def with_outside_hole(*args, **kwargs):
        holes, reason = real(*args, **kwargs)
        return [*holes, {"x": 500.0, "y": 0.0, "r": 3.0, "z0": -36.0, "z1": 36.0}], reason

    monkeypatch.setattr(cad_service, "hole_plan_for", with_outside_hole)
    r = client.post("/api/v1/cad/reconstruct", json={"part_id": "mfg-006"})
    events = [
        (lines["event"], json.loads(lines["data"]))
        for lines in (
            dict(line.split(": ", 1) for line in block.splitlines())
            for block in r.text.strip().split("\n\n")
        )
    ]
    result = next(d for e, d in events if e == "result")
    assert result["ok"], result["error"]
    holes = result["holes"]
    assert holes["status"] == "skipped" and holes["applied"] == 0 and holes["detected"] == 4
    assert "模型外" in holes["reason"]
    assert result["iou"] > 0.95


@needs_limits
def test_reconstruct_with_unrectified_photo_keeps_model_as_is(client):
    """已知零件加一張對不上的照片：拉正失敗（版面 upload），不能套標準三視圖的孔位。"""
    path = REPO_ROOT / "kb" / "images" / "npm-000001.jpg"
    up = client.post("/api/v1/images", files={"file": (path.name, path.read_bytes(), "image/jpeg")})
    r = client.post(
        "/api/v1/cad/reconstruct",
        json={"part_id": "mfg-006", "image_id": up.json()["image_id"]},
    )
    events = {}
    for block in r.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events[fields["event"]] = __import__("json").loads(fields["data"])
    assert events["meta"]["layout"] == "upload"
    result = events["result"]
    assert result["ok"], result["error"]
    holes = result["holes"]
    assert holes["status"] == "skipped" and holes["detected"] == 0
    assert "upload" in holes["reason"]
    assert result["iou"] is not None
