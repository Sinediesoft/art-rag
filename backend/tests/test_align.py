"""影像對位與比對的核心（docs/adr/012）：照片在參考圖上的位置、圖紙線條的差異。

用知識庫的真實圖檔（ORB 需要紋理）；差異的測試直接給已知的對應關係 H，
只測比對本身，不受特徵點對應的影響。
"""

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.analysis import align
from app.core.config import REPO_ROOT, get_models_config
from app.rag import verify

PAINTING = REPO_ROOT / "kb/images/aic-27992.jpg"
OTHER_PAINTING = REPO_ROOT / "kb/images/npm-000001.jpg"
DRAWING = REPO_ROOT / "kb/drawings/mfg-002.png"


def _feats(path):
    return verify.kb_features(path, path.stat().st_mtime)


def _closeup(
    ref: Image.Image, box: tuple[int, int, int, int], long_edge: int = 1200
) -> Image.Image:
    """從參考圖裁一塊放大，模擬拍到畫的一部分。"""
    crop = ref.crop(box)
    s = long_edge / max(crop.size)
    return crop.resize((round(crop.width * s), round(crop.height * s)), Image.Resampling.LANCZOS)


# ---------------------------------------------------------------- 位置
def test_locate_maps_photo_corners_onto_reference():
    ref = Image.open(PAINTING).convert("RGB")
    w, h = ref.size
    box = (w // 4, h // 4, w // 4 + w // 2, h // 4 + h // 2)
    photo = _closeup(ref, box)
    loc = align.locate(
        verify.features(photo), _feats(PAINTING), photo.size, ref.size, min_inliers=20
    )
    assert loc is not None and loc.inliers >= 20
    want = np.array([[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]) / [
        w,
        h,
    ]
    assert np.abs(np.array(loc.polygon) - want).max() < 0.02
    assert loc.coverage == pytest.approx(0.25, abs=0.03)
    assert loc.center == pytest.approx([0.5, 0.5], abs=0.02)


def test_locate_whole_painting_on_wall_covers_everything():
    ref = Image.open(PAINTING).convert("RGB")
    wall = Image.new("RGB", (ref.width + 200, ref.height + 160), (236, 232, 224))
    wall.paste(ref, (100, 80))
    loc = align.locate(verify.features(wall), _feats(PAINTING), wall.size, ref.size, min_inliers=20)
    assert loc is not None and loc.coverage > 0.97


def test_locate_returns_none_for_a_different_painting():
    other = Image.open(OTHER_PAINTING).convert("RGB")
    ref = Image.open(PAINTING)
    assert (
        align.locate(verify.features(other), _feats(PAINTING), other.size, ref.size, min_inliers=20)
        is None
    )


def test_locate_rejects_mirrored_photo():
    """左右翻轉的照片：就算特徵點對得上，位置框也是鏡像的，不能當成對上。"""
    ref = Image.open(PAINTING).convert("RGB")
    h = np.array([[-1.0, 0, ref.width], [0, 1, 0], [0, 0, 1]])
    assert align.polygon_from_h(h, ref.size, ref.size) is None


# ---------------------------------------------------------------- 圖紙差異
MFG = get_models_config().image_compare.mfg


def _photo_of(sheet: Image.Image, scale: float = 1.25) -> tuple[Image.Image, np.ndarray]:
    """把圖紙放大當成照片，回傳 (照片, 照片 → 圖紙的 H)。"""
    photo = sheet.resize(
        (round(sheet.width * scale), round(sheet.height * scale)), Image.Resampling.LANCZOS
    )
    return photo, np.diag([1 / scale, 1 / scale, 1.0])


def _blank_spot(sheet: Image.Image) -> tuple[int, int]:
    """前視圖（400..700, 100..400）裡一塊 40×40 的空白，放新的孔。"""
    a = np.asarray(sheet.convert("L"))
    for y in range(130, 370, 10):
        for x in range(430, 670, 10):
            if a[y - 20 : y + 20, x - 20 : x + 20].min() > 200:
                return x, y
    raise AssertionError("前視圖找不到空白")


def _overlaps(bbox, box_px, size) -> bool:
    x0, y0, x1, y1 = (np.array(bbox) * [size[0], size[1], size[0], size[1]]).tolist()
    return not (x0 > box_px[2] or x1 < box_px[0] or y0 > box_px[3] or y1 < box_px[1])


def test_same_drawing_has_no_differences():
    sheet = Image.open(DRAWING).convert("RGB")
    photo, h = _photo_of(sheet)
    d = align.diff_ink(photo, h, sheet, MFG)
    assert d.status == "same" and d.regions == []


def test_added_hole_is_one_extra_region_at_the_right_place():
    sheet = Image.open(DRAWING).convert("RGB")
    edited = sheet.copy()
    x, y = _blank_spot(sheet)
    ImageDraw.Draw(edited).ellipse((x - 9, y - 9, x + 9, y + 9), outline=0, width=2)
    photo, h = _photo_of(edited)
    d = align.diff_ink(photo, h, sheet, MFG)
    assert d.status == "changed" and len(d.regions) == 1
    r = d.regions[0]
    assert r.kind == "extra" and _overlaps(r.bbox, (x - 9, y - 9, x + 9, y + 9), sheet.size)


def test_erased_line_is_a_missing_region():
    sheet = Image.open(DRAWING).convert("RGB")
    a = np.asarray(sheet.convert("L"))
    ys, xs = np.where(a[150:350, 450:650] < 120)
    x, y = int(xs[len(xs) // 2]) + 450, int(ys[len(ys) // 2]) + 150
    edited = sheet.copy()
    ImageDraw.Draw(edited).rectangle((x - 15, y - 15, x + 15, y + 15), fill="white")
    photo, h = _photo_of(edited)
    d = align.diff_ink(photo, h, sheet, MFG)
    assert d.status == "changed"
    assert any(
        r.kind == "missing" and _overlaps(r.bbox, (x - 15, y - 15, x + 15, y + 15), sheet.size)
        for r in d.regions
    )


def test_erased_thin_dimension_line_is_missing():
    """尺寸線只有 1 px 寬：去雜點不能把整條「缺少」的細線抹掉。"""
    sheet = Image.open(REPO_ROOT / "kb/drawings/mfg-005.png").convert("RGB")
    a = np.asarray(sheet.convert("L"))
    # 前視圖右邊的高度尺寸線（垂直細線，在前視圖的格子裡）
    col = next(x for x in range(600, 700) if (a[120:380, x] < 160).sum() > 60)
    assert (a[230, col - 2 : col + 3] < 160).sum() == 1  # 真的只有 1 px 寬
    edited = sheet.copy()
    ImageDraw.Draw(edited).rectangle((col - 3, 200, col + 3, 260), fill="white")
    photo, h = _photo_of(edited)
    d = align.diff_ink(photo, h, sheet, MFG, boxes=verify.drawing_views(sheet))
    assert any(
        r.kind == "missing" and _overlaps(r.bbox, (col - 3, 200, col + 3, 260), sheet.size)
        for r in d.regions
    )


def test_paper_edge_at_the_sheet_border_is_ignored():
    """照片裡圖紙的紙張邊緣（拉正後落在圖紙最外圈）不算差異：模擬照裡的假差異都是這種。"""
    sheet = Image.open(DRAWING).convert("RGB")
    edged = sheet.copy()
    ImageDraw.Draw(edged).rectangle((1, 1, sheet.width - 2, sheet.height - 2), outline=0, width=3)
    photo, h = _photo_of(edged)
    assert align.diff_ink(photo, h, sheet, MFG).status == "same"


def test_whole_view_rescaled_is_global_change():
    """外形尺寸一改，三視圖整張依新外形縮放：不列一堆區塊，改回報整體變了。"""
    sheet = Image.open(DRAWING).convert("RGB")
    views = sheet.crop((0, 0, 800, 800))
    small = views.resize((680, 680), Image.Resampling.LANCZOS)
    edited = sheet.copy()
    edited.paste((255, 255, 255), (0, 0, 800, 800))
    edited.paste(small, (60, 60))
    photo, h = _photo_of(edited)
    d = align.diff_ink(photo, h, sheet, MFG)
    assert d.status == "global_change" and d.regions == []


def test_drawing_view_boxes_follow_the_drawing_layout():
    from app.cad.drawing import POSITIONS, VIEW

    sheet = Image.open(DRAWING)
    want = sorted((x, y, x + VIEW, y + VIEW) for x, y in POSITIONS.values())
    assert sorted(verify.drawing_views(sheet)) == want


def test_marks_outside_the_three_views_are_ignored():
    """左下角那格沒有視圖（第一角法），紙張的邊、桌面的東西拉正後常落在這種地方。"""
    sheet = Image.open(DRAWING).convert("RGB")
    edited = sheet.copy()
    ImageDraw.Draw(edited).line((60, 500, 340, 700), fill=0, width=3)
    ImageDraw.Draw(edited).line((25, 100, 25, 700), fill=0, width=3)
    photo, h = _photo_of(edited)
    assert align.diff_ink(photo, h, sheet, MFG, boxes=verify.drawing_views(sheet)).status == "same"


def test_each_view_is_refined_separately():
    """紙張彎曲、鏡頭變形：同一個 H 在不同視圖差幾 px。每個視圖各自微調後不算差異。"""
    sheet = Image.open(DRAWING).convert("RGB")
    edited = sheet.copy()
    front = sheet.crop((400, 100, 700, 400))
    edited.paste((255, 255, 255), (400, 100, 700, 400))
    edited.paste(front, (406, 104))  # 只有前視圖偏了 (6, 4) px
    photo, h = _photo_of(edited)
    boxes = verify.drawing_views(sheet)
    assert align.diff_ink(photo, h, sheet, MFG, boxes=boxes).status == "same"


def test_refinement_cannot_hide_a_moved_hole():
    """微調只吸收整個視圖的小偏移；視圖裡單獨一個孔移了位置，仍然要找得到。"""
    sheet = Image.open(DRAWING).convert("RGB")
    edited = sheet.copy()
    x, y = _blank_spot(sheet)
    ImageDraw.Draw(edited).ellipse((x - 9, y - 9, x + 9, y + 9), outline=0, width=2)
    photo, h = _photo_of(edited)
    d = align.diff_ink(photo, h, sheet, MFG, boxes=verify.drawing_views(sheet))
    assert d.status == "changed" and any(
        _overlaps(r.bbox, (x - 9, y - 9, x + 9, y + 9), sheet.size) for r in d.regions
    )


def _stretched_front(sheet: Image.Image, width: int) -> Image.Image:
    """前視圖以中心橫向拉長（外形尺寸小改時，三視圖的比例只差一點點）。"""
    edited = sheet.copy()
    off = (width - 300) // 2
    front = sheet.crop((400, 100, 700, 400)).resize((width, 300), Image.Resampling.LANCZOS)
    edited.paste(front.crop((off, 0, off + 300, 300)), (400, 100))
    return edited


def test_refine_never_rescales():
    """微調只准平移、旋轉：仿射微調會把 3% 的拉長修成 1.025 倍，差異就被吃掉一大半。"""
    sheet = Image.open(DRAWING).convert("RGB")
    photo, h = _photo_of(_stretched_front(sheet, 309))
    warped, covered = verify.rectify(np.asarray(photo.convert("L")), h, sheet.size)
    g = np.asarray(sheet.convert("L"))
    a = align._refine(g[85:415, 385:715], warped[85:415, 385:715], covered[85:415, 385:715], MFG)
    assert a is None or abs(np.linalg.det(a[:, :2]) - 1) < 1e-3


def test_three_percent_proportion_change_is_found():
    """偵測下限：比例差 3%（300 px 寬的視圖差 9 px）找得到；2% 在容許距離（3 px）以內，看不出來。"""
    sheet = Image.open(DRAWING).convert("RGB")
    photo, h = _photo_of(_stretched_front(sheet, 309))
    assert align.diff_ink(photo, h, sheet, MFG, boxes=verify.drawing_views(sheet)).status != "same"


def test_blurred_dotted_lines_are_not_missing():
    """模糊的照片裡，點線（隱藏線）變得很淡：判「缺少」用寬鬆的門檻，淡淡的也算還在。"""
    sheet = Image.open(DRAWING).convert("RGB")
    photo, h = _photo_of(sheet, scale=1.0)
    photo = photo.filter(ImageFilter.GaussianBlur(1.6))
    d = align.diff_ink(photo, h, sheet, MFG, boxes=verify.drawing_views(sheet))
    assert not any(r.kind == "missing" for r in d.regions)


def test_overlay_png_has_reference_size():
    sheet = Image.open(DRAWING).convert("RGB")
    edited = sheet.copy()
    x, y = _blank_spot(sheet)
    ImageDraw.Draw(edited).ellipse((x - 9, y - 9, x + 9, y + 9), outline=0, width=2)
    photo, h = _photo_of(edited)
    d = align.diff_ink(photo, h, sheet, MFG)
    png = align.overlay_png(sheet, d)
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size == sheet.size
    # 多出來的孔塗成藍色
    px = np.asarray(img.convert("RGB")).astype(int)
    blue = (px[..., 2] > 180) & (px[..., 0] < 120)
    assert blue[y - 12 : y + 12, x - 12 : x + 12].any()


def test_ink_overlap_unchanged_by_shared_layers():
    """ink_overlap 改用共用的 ink_layers 之後，同一張圖紙的照片重合度仍然很高。

    改寫前後的數值是否完全相同，用 78 組（照片, 圖紙）另外比對過（開發日誌）。"""
    sheet = Image.open(DRAWING).convert("RGB")
    photo, h = _photo_of(sheet)
    q_ink, r_ink, valid = verify.ink_layers(photo, h, sheet)
    assert q_ink.shape == r_ink.shape == valid.shape == (sheet.height, sheet.width)
    assert verify.ink_overlap(photo, h, DRAWING) > 0.9


def test_rectify_marks_only_covered_pixels_valid():
    sheet = Image.open(DRAWING).convert("RGB")
    half = sheet.crop((0, 0, 400, 970))
    warped, covered = verify.rectify(np.asarray(half.convert("L")), np.eye(3), sheet.size)
    assert warped.shape == covered.shape == (970, 800)
    assert covered[:, :380].all() and not covered[:, 420:].any()
    assert np.count_nonzero(warped[:, 420:] < 255) == 0  # 照片外面填白
