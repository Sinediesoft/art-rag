"""產生「模擬實拍照」：從原始高解析圖加上透視、裁切、反光、模糊、牆面背景。

只是 D 拍攝真正實拍照之前的替代品；正式評估請改用實拍照（共用層 §七）。
用法：python eval/make_synthetic_photos.py <原始圖目錄>
"""

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

OUT = Path(__file__).parent / "photos" / "known"


def perspective(img: Image.Image, strength: float) -> Image.Image:
    w, h = img.size
    dx, dy = w * strength, h * strength
    quad = (
        random.uniform(0, dx),
        random.uniform(0, dy),
        random.uniform(0, dx),
        h - random.uniform(0, dy),
        w - random.uniform(0, dx),
        h - random.uniform(0, dy),
        w - random.uniform(0, dx),
        random.uniform(0, dy),
    )
    return img.transform((w, h), Image.Transform.QUAD, quad, Image.Resampling.BICUBIC)


def on_wall(img: Image.Image, margin: float) -> Image.Image:
    w, h = img.size
    pad_w, pad_h = int(w * margin), int(h * margin)
    wall = Image.new(
        "RGB",
        (w + 2 * pad_w, h + 2 * pad_h),
        random.choice([(236, 232, 224), (205, 198, 186), (90, 88, 86), (245, 245, 243)]),
    )
    wall.paste(img, (pad_w, pad_h))
    return wall


def glare(img: Image.Image) -> Image.Image:
    overlay = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(overlay)
    w, h = img.size
    cx, cy, r = random.uniform(0.2, 0.8) * w, random.uniform(0.2, 0.6) * h, 0.25 * min(w, h)
    d.ellipse((cx - r, cy - r * 0.6, cx + r, cy + r * 0.6), fill=150)
    overlay = overlay.filter(ImageFilter.GaussianBlur(r / 2))
    return Image.composite(Image.new("RGB", img.size, "white"), img, overlay)


def crop(img: Image.Image, keep: float) -> Image.Image:
    w, h = img.size
    cw, ch = int(w * keep), int(h * keep)
    x, y = random.randint(0, w - cw), random.randint(0, h - ch)
    return img.crop((x, y, x + cw, y + ch))


VARIANTS = {
    "tilt": lambda im: on_wall(perspective(im, 0.08), 0.12),
    "glare": lambda im: glare(on_wall(im, 0.06)),
    "crop": lambda im: crop(im, 0.7),
    "dim": lambda im: ImageEnhance.Color(ImageEnhance.Brightness(im).enhance(0.65)).enhance(0.8),
    "blur": lambda im: (
        on_wall(im, 0.2).filter(ImageFilter.GaussianBlur(2.5)).rotate(4, expand=True)
    ),
}

if __name__ == "__main__":
    random.seed(42)
    OUT.mkdir(parents=True, exist_ok=True)
    for src in sorted(Path(sys.argv[1]).glob("*-*.jpg")):
        if src.stem.startswith("unknown"):
            continue
        base = Image.open(src).convert("RGB")
        base.thumbnail((1600, 1600))
        for name, fn in VARIANTS.items():
            photo = fn(base.copy())
            photo.thumbnail((1024, 1024))
            photo.save(OUT / f"{src.stem}__{name}.jpg", quality=80)
        print("✓", src.stem)
