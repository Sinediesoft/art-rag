"""觀眾照片 vs 知識庫原圖（docs/adr/012 第 4 步，畫作）：照片對到原圖之後，比形狀與顏色。

和兩張照片互比用同一個比法（align.diff_tone），但照片和原圖的差別更多：
近拍時四角補黑、辨識給的位置差幾 px、照片比原圖模糊、整幅掛在牆上時畫的邊緣混到牆面。
這些都不是畫本身的差異，不該被框出來；在照片上加的筆畫要框出來。
直接給已知的對應關係 H，只測比對本身。
"""

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.analysis import align
from app.core.config import REPO_ROOT, get_models_config

PAINTING = REPO_ROOT / "kb/images/npm-000001.jpg"  # 谿山行旅圖：下半部有大片素色的霧
ART = get_models_config().image_compare.art


@pytest.fixture(scope="module")
def painting():
    return Image.open(PAINTING).convert("RGB")


def _closeup(ref: Image.Image, frac_box=(0.1, 0.35, 0.6, 0.86), brightness=0.85):
    """近拍原畫的一塊：放大到長邊 1200、真透視（四角往內縮，空出來的地方補黑）、變暗、輕微模糊。
    回傳 (照片, 照片 → 原圖的 H)。和 eval/make_align_photos.py 的 crop50 一樣的拍法。"""
    W, H = ref.size
    x0, y0, x1, y1 = (round(v * s) for v, s in zip(frac_box, (W, H, W, H), strict=True))
    crop = ref.crop((x0, y0, x1, y1))
    s = 1200 / max(crop.size)
    crop = crop.resize((round(crop.width * s), round(crop.height * s)), Image.Resampling.LANCZOS)
    w, h = crop.size
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    inward = [(0.03, 0.01), (0.01, 0.035), (0.025, 0.02), (0.04, 0.03)]
    dst = np.float32(
        [
            [inward[0][0] * w, inward[0][1] * h],
            [w - inward[1][0] * w, inward[1][1] * h],
            [w - inward[2][0] * w, h - inward[2][1] * h],
            [inward[3][0] * w, h - inward[3][1] * h],
        ]
    )
    m = cv2.getPerspectiveTransform(src, dst)
    photo = cv2.warpPerspective(
        np.asarray(crop), m, (w, h), flags=cv2.INTER_CUBIC, borderValue=(0, 0, 0)
    )
    photo = Image.fromarray(np.clip(photo.astype(np.float32) * brightness, 0, 255).astype(np.uint8))
    photo = photo.filter(ImageFilter.GaussianBlur(0.6))
    to_ref = np.array([[1 / s, 0, x0], [0, 1 / s, y0], [0, 0, 1]]) @ np.linalg.inv(m)
    return photo, to_ref


def _on_wall(ref: Image.Image):
    """整幅掛在牆上、斜斜地拍。回傳 (照片, 照片 → 原圖的 H)。"""
    W, H = ref.size
    pad = (90, 70)
    wall = Image.new("RGB", (W + 2 * pad[0], H + 2 * pad[1]), (236, 232, 224))
    wall.paste(ref, pad)
    w, h = wall.size
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32(
        [[0.04 * w, 0.01 * h], [0.99 * w, 0.03 * h], [0.97 * w, 0.98 * h], [0.01 * w, 0.95 * h]]
    )
    m = cv2.getPerspectiveTransform(src, dst)
    photo = cv2.warpPerspective(
        np.asarray(wall), m, (w, h), flags=cv2.INTER_CUBIC, borderValue=(236, 232, 224)
    )
    to_ref = np.array([[1, 0, -pad[0]], [0, 1, -pad[1]], [0, 0, 1]], float) @ np.linalg.inv(m)
    return Image.fromarray(photo), to_ref


def _scribble(photo: Image.Image, box) -> Image.Image:
    """在照片上用黑筆寫幾筆（使用者在照片上畫的那種）。"""
    im = photo.copy()
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = box
    rng = np.random.default_rng(3)
    for _ in range(4):
        pts = [(rng.uniform(x0, x1), rng.uniform(y0, y1)) for _ in range(6)]
        d.line(pts, fill=(15, 15, 15), width=3, joint="curve")
    return im


def _to_ref_box(box, h, ref_size):
    x0, y0, x1, y1 = box
    c = cv2.perspectiveTransform(np.float32([[[x0, y0], [x1, y0], [x1, y1], [x0, y1]]]), h)[0]
    W, H = ref_size
    return [c[:, 0].min() / W, c[:, 1].min() / H, c[:, 0].max() / W, c[:, 1].max() / H]


def _inside(r, b, pad=0.03) -> bool:
    """差異方框落在答案方框（放寬 pad）裡面：框得準，不是一大片剛好蓋到。"""
    return (
        r.bbox[0] >= b[0] - pad
        and r.bbox[1] >= b[1] - pad
        and r.bbox[2] <= b[2] + pad
        and r.bbox[3] <= b[3] + pad
    )


def test_closeup_with_black_corners_has_no_differences(painting):
    """近拍拉歪，照片四角補的黑色不是畫上的東西。"""
    photo, h = _closeup(painting)
    d = align.diff_tone(photo, h, painting, ART)
    assert d.status == "same", [vars(r) for r in d.regions]


def test_strokes_drawn_on_the_photo_are_found_and_nothing_else(painting):
    """使用者在照片上畫了幾筆：只框出那幾筆，霧、黑邊都不算。"""
    photo, h = _closeup(painting)
    box = (150, 620, 480, 700)  # 照片上霧的那一帶
    d = align.diff_tone(_scribble(photo, box), h, painting, ART)
    truth = _to_ref_box(box, h, painting.size)
    assert d.status == "changed"
    assert d.regions and all(_inside(r, truth) for r in d.regions), [vars(r) for r in d.regions]
    assert any(r.kind in ("shape", "both") for r in d.regions)


def test_position_a_few_pixels_off_is_refined(painting):
    """辨識給的 H 在特徵點少的地方（霧）會差幾 px：比之前先微調，不然整片都像「形狀不同」。"""
    photo, h = _closeup(painting)
    off = np.array([[1.006, 0, 4], [0, 0.995, -3], [0, 0, 1]], float) @ h
    d = align.diff_tone(photo, off, painting, ART)
    assert d.status == "same", [vars(r) for r in d.regions]


def test_blurry_photo_is_not_a_shape_difference(painting):
    """手震、沒對準焦：照片比原圖模糊，細節比不了，但不是畫改了。"""
    photo, h = _closeup(painting)
    d = align.diff_tone(photo.filter(ImageFilter.GaussianBlur(3)), h, painting, ART)
    assert d.status == "same", [vars(r) for r in d.regions]


def test_painting_edge_against_the_wall_is_not_a_difference(painting):
    """整幅掛在牆上：畫的最外圈拉正後會混到牆面的顏色。"""
    photo, h = _on_wall(painting)
    d = align.diff_tone(photo, h, painting, ART)
    assert d.status == "same", [vars(r) for r in d.regions]


def test_dim_photo_from_the_recognition_eval_is_not_a_color_difference():
    """辨識評估的模擬照（eval/photos/known）：調暗 0.65、彩度 0.8。整張一起變淡，不是哪一塊褪色。"""
    from app.rag import verify
    from app.rag.preprocess import load_image

    ref = Image.open(REPO_ROOT / "kb/images/met-436535.jpg").convert("RGB")
    photo = load_image(REPO_ROOT / "eval/photos/known/met-436535__dim.jpg")
    loc = align.locate(
        verify.features(photo), verify.features(ref), photo.size, ref.size, min_inliers=25
    )
    d = align.diff_tone(photo, loc.h, ref, ART)
    assert d.status == "same", [vars(r) for r in d.regions]


def test_desaturated_dim_photo_is_not_a_color_difference(painting):
    """展間偏暗、照片顏色比較淡（整張一起）：不是畫褪色。"""
    photo, h = _closeup(painting, brightness=0.7)
    hsv = np.asarray(photo.convert("HSV")).astype(np.float32)
    hsv[..., 1] *= 0.75
    dull = Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB")
    d = align.diff_tone(dull, h, painting, ART)
    assert d.status == "same", [vars(r) for r in d.regions]
