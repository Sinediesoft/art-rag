"""色彩分析的純計算（docs/adr/010）：不需要索引或模型。"""

import io

import numpy as np
import pytest
from PIL import Image

from app.analysis.color import (
    COLOR_NAMES,
    analyze,
    ciede2000,
    color_name,
    lab_to_srgb,
    srgb_to_lab,
    summarize,
    temperature_masks,
)
from app.core.config import ColorAnalysisSpec
from app.rag.chunking import color_text

SPEC = ColorAnalysisSpec()


def test_srgb_to_lab_known_values():
    assert np.allclose(srgb_to_lab([255, 0, 0]), [53.24, 80.09, 67.20], atol=0.01)
    assert np.allclose(srgb_to_lab([255, 255, 255]), [100, 0, 0], atol=0.01)
    assert np.allclose(srgb_to_lab([0, 0, 0]), [0, 0, 0], atol=0.01)


def test_lab_round_trip():
    rgb = np.array([[12, 200, 77], [255, 0, 0], [0, 0, 0], [71, 62, 51]])
    assert np.array_equal(lab_to_srgb(srgb_to_lab(rgb)), rgb)


# Sharma, Wu & Dalal (2005) 的 CIEDE2000 標準測試資料（第 1、7、17、25、34 組）
@pytest.mark.parametrize(
    "lab1,lab2,want",
    [
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
        ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
    ],
)
def test_ciede2000_sharma(lab1, lab2, want):
    assert float(ciede2000(lab1, lab2)) == pytest.approx(want, abs=1e-4)


@pytest.mark.parametrize(
    "rgb,name",
    [
        ((255, 0, 0), "紅"),
        ((0, 0, 255), "藍"),
        ((0, 0, 0), "黑"),
        ((255, 255, 255), "白"),
        ((128, 128, 128), "灰"),
        ((0, 128, 0), "綠"),
        ((255, 255, 0), "黃"),
        ((255, 165, 0), "橙"),
        ((128, 0, 128), "紫"),
    ],
)
def test_color_name_basic(rgb, name):
    assert color_name(srgb_to_lab(rgb)) == name


def test_every_name_maps_to_itself():
    for name, rgb in COLOR_NAMES:
        assert color_name(srgb_to_lab(rgb)) == name


def test_temperature_wraps_around_zero_degrees():
    def lab(hue_deg: float, chroma: float) -> np.ndarray:
        h = np.radians(hue_deg)
        return np.array([50.0, chroma * np.cos(h), chroma * np.sin(h)])

    for hue, want in [(350, "warm"), (20, "warm"), (100, "warm"), (120, "cool"), (300, "cool")]:
        warm, cool, neutral = temperature_masks(lab(hue, 30), SPEC)
        assert {"warm": bool(warm), "cool": bool(cool)}[want] and not neutral
    warm, cool, neutral = temperature_masks(lab(40, 5), SPEC)  # C* < 10：中性
    assert bool(neutral) and not warm and not cool


def two_colors() -> Image.Image:
    """左 70 欄紅、右 30 欄藍（100×100，不會被縮放，占比剛好 70／30）。"""
    a = np.zeros((100, 100, 3), np.uint8)
    a[:, :70] = (255, 0, 0)
    a[:, 70:] = (0, 0, 255)
    return Image.fromarray(a)


def test_two_color_image():
    result, png = analyze(two_colors(), SPEC)
    pal = result["palette"]
    assert [p["name"] for p in pal] == ["紅", "藍"]
    assert [p["share"] for p in pal] == pytest.approx([0.7, 0.3], abs=0.01)
    assert [p["hex"] for p in pal] == ["#FF0000", "#0000FF"]
    assert result["temperature"]["warm"] == pytest.approx(0.7, abs=0.01)
    assert result["temperature"]["cool"] == pytest.approx(0.3, abs=0.01)
    assert result["summary"] == "整體偏暖、高彩度、以中間調為主"
    assert len(result["lightness"]["histogram"]) == 10
    assert sum(result["chroma"]["histogram"]) == pytest.approx(1, abs=1e-3)
    im = Image.open(io.BytesIO(png))
    assert im.mode == "P" and im.size == (100, 100)
    assert im.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_gray_gradient_is_neutral_and_low_chroma():
    a = np.repeat(np.linspace(0, 255, 200, dtype=np.uint8)[None, :, None], 50, axis=0)
    result, _ = analyze(Image.fromarray(np.repeat(a, 3, axis=2)), SPEC)
    assert result["temperature"]["neutral"] == pytest.approx(1)
    assert result["chroma"]["low"] == pytest.approx(1)
    assert result["summary"].startswith("以中性色為主、低彩度")


def test_analyze_is_deterministic():
    rng = np.random.default_rng(7)
    img = Image.fromarray(rng.integers(0, 256, (120, 160, 3), dtype=np.uint8))
    assert analyze(img, SPEC) == analyze(img, SPEC)


def _summary(warm=0.4, cool=0.3, neutral=0.3, median=15.0, dark=0.2, mid=0.5, light=0.3) -> str:
    temperature = {"warm": warm, "cool": cool, "neutral": neutral}
    return summarize(
        temperature, {"dark": dark, "mid": mid, "light": light}, {"median": median}, SPEC
    )


@pytest.mark.parametrize(
    "warm,cool,neutral,want",
    [
        (0.3, 0.2, 0.5, "以中性色為主"),  # 中性色剛好 50%
        (0.5, 0.0, 0.5, "以中性色為主"),  # 中性色過半時優先於偏暖
        (0.5, 0.2, 0.3, "整體偏暖"),
        (0.5, 0.25, 0.25, "整體偏暖"),  # 暖色剛好是冷色的 2 倍
        (0.2, 0.5, 0.3, "整體偏冷"),
        (0.25, 0.5, 0.25, "整體偏冷"),  # 冷色剛好是暖色的 2 倍
        (0.4, 0.3, 0.3, "冷暖並陳"),
    ],
)
def test_summarize_temperature_rules(warm, cool, neutral, want):
    assert _summary(warm=warm, cool=cool, neutral=neutral).split("、")[0] == want


@pytest.mark.parametrize(
    "median,want",
    [
        (0, "低彩度"),
        (9.99, "低彩度"),
        (10, "中等彩度"),
        (24.99, "中等彩度"),
        (25, "高彩度"),
        (60, "高彩度"),
    ],
)
def test_summarize_chroma_bands(median, want):
    assert _summary(median=median).split("、")[1] == want


@pytest.mark.parametrize(
    "dark,mid,light,want",
    [
        (0.6, 0.3, 0.1, "以暗調為主"),
        (0.2, 0.5, 0.3, "以中間調為主"),
        (0.1, 0.3, 0.6, "以亮調為主"),
        (0.34, 0.33, 0.33, "以暗調為主"),  # 差距很小也取最大的那一段
    ],
)
def test_summarize_tone_by_max_share(dark, mid, light, want):
    assert _summary(dark=dark, mid=mid, light=light).split("、")[2] == want


def test_summarize_full_sentence():
    assert _summary(0.5, 0.2, 0.3, median=30, dark=0.1, mid=0.3, light=0.6) == (
        "整體偏暖、高彩度、以亮調為主"
    )


@pytest.mark.parametrize(
    "summary,want",
    [
        (
            "整體偏暖、低彩度、以暗調為主",
            "；整體偏暖、低彩度、以暗調為主。",
        ),  # 已經以「整體」開頭，不重複
        ("整體偏冷、中等彩度、以中間調為主", "；整體偏冷、中等彩度、以中間調為主。"),
        ("以中性色為主、低彩度、以暗調為主", "；整體而言以中性色為主、低彩度、以暗調為主。"),
        ("冷暖並陳、高彩度、以亮調為主", "；整體而言冷暖並陳、高彩度、以亮調為主。"),
    ],
)
def test_color_text_does_not_repeat_overall(summary, want):
    colors = {
        "palette": [{"name": "紅", "hex": "#FF0000", "share": 0.7}],
        "temperature": {"warm": 0.7, "cool": 0.1, "neutral": 0.2},
        "lightness": {"dark": 0.2, "mid": 0.5, "light": 0.3},
        "chroma": {"median": 12.0},
        "summary": summary,
    }
    text = color_text({"title": {"zh": "測試畫"}, "colors": colors})
    assert want in text and "整體而言整體" not in text
