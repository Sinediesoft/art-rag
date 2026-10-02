"""兩張照片互比（docs/adr/012 第 3 步，畫作）：照片 B 對齊到照片 A 之後，比形狀與顏色。

用知識庫畫作模擬「同一幅畫、同樣光線、不同角度各拍一張」；差異直接給已知的對應關係 H，
只測比對本身。
"""

import io

import cv2
import numpy as np
import pytest
from PIL import Image, ImageEnhance

from app.analysis import align
from app.core.config import REPO_ROOT, get_models_config

PAINTING = REPO_ROOT / "kb/images/aic-27992.jpg"
OTHER = REPO_ROOT / "kb/images/met-436535.jpg"
PAIR = get_models_config().image_compare.pair


def _shot(
    a: Image.Image, scale=1.15, angle=3.0, brightness=1.0, warm=0
) -> tuple[Image.Image, np.ndarray]:
    """從另一個角度再拍一次：旋轉縮放、亮度與白平衡可調。回傳 (照片 B, B → A 的 H)。"""
    w, h = a.size
    m = np.vstack([cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale), [0, 0, 1]])
    b = cv2.warpPerspective(
        np.asarray(a.convert("RGB")), m, (w, h), flags=cv2.INTER_CUBIC, borderValue=(0, 0, 0)
    )
    b = b.astype(np.float32) * brightness
    b[..., 0] += warm
    b[..., 2] -= warm
    return Image.fromarray(np.clip(b, 0, 255).astype(np.uint8)), np.linalg.inv(m)


def _patch_box(a: Image.Image, frac=0.14) -> tuple[int, int, int, int]:
    w, h = a.size
    pw, ph = int(w * frac), int(h * frac)
    return (
        int(w * 0.5) - pw // 2,
        int(h * 0.5) - ph // 2,
        int(w * 0.5) + pw // 2,
        int(h * 0.5) + ph // 2,
    )


def _hit(regions, box, size, kinds) -> bool:
    w, h = size
    for r in regions:
        x0, y0, x1, y1 = r.bbox[0] * w, r.bbox[1] * h, r.bbox[2] * w, r.bbox[3] * h
        if r.kind in kinds and not (x0 > box[2] or x1 < box[0] or y0 > box[3] or y1 < box[1]):
            return True
    return False


@pytest.fixture(scope="module")
def painting():
    return Image.open(PAINTING).convert("RGB")


def test_same_painting_from_another_angle_has_no_differences(painting):
    b, h = _shot(painting)
    d = align.diff_tone(b, h, painting, PAIR)
    assert d.status == "same" and d.regions == []


def test_lighting_and_white_balance_change_is_not_a_difference(painting):
    """同一個展間、不同時間拍：整體偏暗、偏暖，不是畫作本身改變。"""
    b, h = _shot(painting, brightness=0.8, warm=12)
    assert align.diff_tone(b, h, painting, PAIR).status == "same"


def test_replaced_patch_is_a_shape_difference(painting):
    """修復補筆、偽作改了一塊：那一塊換成畫上另一處的內容。"""
    box = _patch_box(painting)
    edited = painting.copy()
    edited.paste(painting.crop((40, 40, 40 + box[2] - box[0], 40 + box[3] - box[1])), box[:2])
    b, h = _shot(edited)
    d = align.diff_tone(b, h, painting, PAIR)
    assert d.status == "changed" and _hit(d.regions, box, painting.size, {"shape", "both"})


def test_tinted_patch_is_a_color_difference(painting):
    """補色：筆觸不變、只有顏色變了。同光線拍的兩張才比得出來。"""
    box = _patch_box(painting)
    edited = painting.copy()
    patch = np.asarray(edited.crop(box)).astype(np.float32)
    patch[..., 0] = np.clip(patch[..., 0] * 1.35 + 25, 0, 255)  # 偏紅
    patch[..., 2] = np.clip(patch[..., 2] * 0.7, 0, 255)
    edited.paste(Image.fromarray(patch.astype(np.uint8)), box[:2])
    b, h = _shot(edited)
    d = align.diff_tone(b, h, painting, PAIR)
    assert d.status == "changed" and _hit(d.regions, box, painting.size, {"color", "both"})


def test_faded_patch_is_found(painting):
    """褪色：那一塊變淡、變灰。"""
    box = _patch_box(painting)
    edited = painting.copy()
    faded = ImageEnhance.Color(edited.crop(box)).enhance(0.3)
    edited.paste(ImageEnhance.Brightness(faded).enhance(1.25), box[:2])
    b, h = _shot(edited)
    d = align.diff_tone(b, h, painting, PAIR)
    assert d.status == "changed" and _hit(d.regions, box, painting.size, {"shape", "color", "both"})


def test_a_different_painting_is_global_change(painting):
    other = Image.open(OTHER).convert("RGB").resize(painting.size)
    d = align.diff_tone(other, np.eye(3), painting, PAIR)
    assert d.status == "global_change" and d.regions == []


def test_only_the_overlap_is_compared(painting):
    """照片 B 只拍到左半邊：右半邊沒拍到，不算差異。"""
    w, h = painting.size
    left = painting.crop((0, 0, w // 2, h))
    d = align.diff_tone(left, np.eye(3), painting, PAIR)
    assert d.status == "same"
    assert d.compared[:, : w // 2 - 20].mean() > 0.8 and not d.compared[:, w // 2 + 20 :].any()


def test_edge_of_a_partial_photo_on_a_plain_wall_is_not_a_difference(painting):
    """A 是掛在牆上的整幅畫，B 只拍到中間、邊界落在素色牆面上，而且比較暗。
    B 沒拍到的地方若補黑，局部亮度正規化會在平坦的牆面上把黑邊放大，做出一圈假的「形狀不同」。"""
    w, h = painting.size
    wall = Image.new("RGB", (w + 240, h + 200), (236, 232, 224))
    wall.paste(painting, (120, 100))
    box = (60, 50, wall.width - 60, wall.height - 50)  # 邊界落在牆面上
    b, hb = _shot(wall.crop(box), scale=1.1, angle=3.0, brightness=0.85)
    h_total = np.array([[1, 0, box[0]], [0, 1, box[1]], [0, 0, 1]], float) @ hb
    assert align.diff_tone(b, h_total, wall, PAIR).status == "same"


def test_tone_overlay_png_has_reference_size(painting):
    box = _patch_box(painting)
    edited = painting.copy()
    edited.paste(painting.crop((40, 40, 40 + box[2] - box[0], 40 + box[3] - box[1])), box[:2])
    b, h = _shot(edited)
    d = align.diff_tone(b, h, painting, PAIR)
    img = Image.open(io.BytesIO(align.tone_overlay_png(painting, d)))
    assert img.format == "PNG" and img.size == painting.size
