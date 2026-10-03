"""產生影像對位與比對的評估照片（docs/adr/012），每張都附正確答案（eval/align_photos/truth.json）。

- art/：知識庫畫作（kb＋kb_staging 5 幅）的近照（拍到長邊 50%、30%）與整幅掛在牆上的照片；
  答案是照片 → 原圖的單應矩陣，評估時拿來算位置框的誤差。
- mfg/：知識庫圖紙（6 張）
  - same：沒改過的圖紙，量假差異；
  - hole／erase：前視圖加一個孔或擦掉一段線（影像上直接改），答案是改動的方框；
  - revA：用 CadQuery 做的真正改版——mfg-001（L 型支架）rev.A 底板只有 2 個孔、外形不變
    （mfg-001-revA.py，產生 mfg-001-revA.png），答案是兩版圖紙線條不同的地方。展示時直接上傳這幾張。
- pair/：兩張照片互比（畫作）。同一幅畫在同樣光線下、從不同角度各拍一張（A、B），
  B 有時在中間動了手腳：換掉一塊（補筆、偽作）、塗糊一塊、加一個小印章、褪色、補色；
  same 是兩張都沒動過，量假差異。答案是改動在照片 A 上的方框。
- artedit/：畫作找不同（觀眾照片 vs 知識庫原圖，ADR 012 第 4 步）。
  知識庫畫作動了手腳之後近拍、整幅各拍一張：
  拍完在照片上用黑筆加幾筆（strokes），或畫本身換掉一塊、塗糊、加小印章、褪色、補色；
  答案是改動在原圖上的方框。沒改過的照片就是 art/ 那批。

透視用真正的單應矩陣（cv2.warpPerspective）。make_synthetic_photos.py 的 tilt 是 PIL 的 QUAD
（雙線性），不是相機會拍出來的變形，拉正後邊緣會差到 25 px；那批照片在評估裡另外當壓力測試。
用法：python eval/make_align_photos.py（固定亂數種子，重跑結果相同；revA 要能執行 CadQuery）
"""

import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "pipelines"))
sys.path.insert(0, str(Path(__file__).parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from make_synthetic_photos import VARIANTS, on_wall  # noqa: E402
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter  # noqa: E402

OUT = Path(__file__).parent / "align_photos"
LONG_EDGE = 1200
# rev.B（知識庫）底板 4 孔 → rev.A 底板 2 孔，外形不變。整段換掉，換完的程式照 ruff 的排版
REV_A_FROM = """# 底板 4-Ø6.6 通孔（M6 螺栓）
base_holes = (
    cq.Workplane("XY").pushPoints([(10, -12), (10, 12), (28, -12), (28, 12)]).circle(3.3).extrude(8)
)
"""
REV_A_TO = """# rev.A：底板 2-Ø6.6 通孔（M6 螺栓），rev.B 改成 4 孔
base_holes = cq.Workplane("XY").pushPoints([(19, -12), (19, 12)]).circle(3.3).extrude(8)
"""


def _save(img: Image.Image, path: Path, quality: int = 88) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, quality=quality)
    return path.relative_to(OUT).as_posix()


def _fit(img: Image.Image) -> tuple[Image.Image, float]:
    s = LONG_EDGE / max(img.size)
    return img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS), s


def perspective(img: Image.Image, strength: float, background) -> tuple[Image.Image, np.ndarray]:
    """真正的透視：四角各往內縮最多 strength，回傳 (照片, 原圖 → 照片的 H)。"""
    w, h = img.size
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    j = [(random.uniform(0, strength) * w, random.uniform(0, strength) * h) for _ in range(4)]
    dst = np.float32(
        [
            [j[0][0], j[0][1]],
            [w - j[1][0], j[1][1]],
            [w - j[2][0], h - j[2][1]],
            [j[3][0], h - j[3][1]],
        ]
    )
    m = cv2.getPerspectiveTransform(src, dst)
    out = cv2.warpPerspective(
        np.asarray(img.convert("RGB")), m, (w, h), flags=cv2.INTER_CUBIC, borderValue=background
    )
    return Image.fromarray(out), m


# ---------------------------------------------------------------- 畫作
def art_photos(rows: list[dict]) -> None:
    images = sorted((ROOT / "kb/images").glob("*.jpg")) + sorted(
        (ROOT / "kb_staging/images").glob("*.jpg")
    )
    for p in images:
        ref = Image.open(p).convert("RGB")
        W, H = ref.size
        for frac in (0.5, 0.3):
            for k in range(2):
                cw, ch = int(W * frac), int(H * frac)
                x0, y0 = random.randint(0, W - cw), random.randint(0, H - ch)
                crop, s = _fit(ref.crop((x0, y0, x0 + cw, y0 + ch)))
                photo, m = perspective(crop, 0.04, (0, 0, 0))
                photo = ImageEnhance.Brightness(photo).enhance(random.uniform(0.75, 1.1))
                photo = photo.filter(ImageFilter.GaussianBlur(0.6))
                # 照片 → 原圖：先反透視、再縮回裁切大小、再平移回原圖位置
                to_ref = np.array([[1 / s, 0, x0], [0, 1 / s, y0], [0, 0, 1]]) @ np.linalg.inv(m)
                name = _save(photo, OUT / "art" / f"{p.stem}__crop{int(frac * 100)}-{k + 1}.jpg")
                rows.append(
                    {
                        "file": name,
                        "domain": "art",
                        "target": f"artwork:{p.stem}",
                        "case": f"crop{int(frac * 100)}",
                        "h": to_ref.tolist(),
                    }
                )
        # 整幅掛在牆上、斜斜地拍
        big, s = _fit(ref)
        pad = (90, 70)
        wall = Image.new("RGB", (big.width + 2 * pad[0], big.height + 2 * pad[1]), (236, 232, 224))
        wall.paste(big, pad)
        photo, m = perspective(wall, 0.05, (236, 232, 224))
        to_ref = np.array(
            [[1 / s, 0, -pad[0] / s], [0, 1 / s, -pad[1] / s], [0, 0, 1]]
        ) @ np.linalg.inv(m)
        name = _save(photo, OUT / "art" / f"{p.stem}__whole.jpg")
        rows.append(
            {
                "file": name,
                "domain": "art",
                "target": f"artwork:{p.stem}",
                "case": "whole",
                "h": to_ref.tolist(),
            }
        )


# ---------------------------------------------------------------- 圖紙
def _blank_spot(a: np.ndarray) -> tuple[int, int]:
    for _ in range(500):
        x, y = random.randint(440, 660), random.randint(140, 360)
        if a[y - 16 : y + 16, x - 16 : x + 16].min() > 200:
            return x, y
    raise RuntimeError("前視圖找不到空白")


def edit(sheet: Image.Image, kind: str) -> tuple[Image.Image, list[int]]:
    """在前視圖（400..700, 100..400）改一處，回傳 (改後的圖紙, 改動的方框)。"""
    a = np.asarray(sheet.convert("L"))
    im = sheet.copy()
    d = ImageDraw.Draw(im)
    if kind == "hole":
        x, y = _blank_spot(a)
        d.ellipse((x - 9, y - 9, x + 9, y + 9), outline=0, width=2)
        return im, [x - 10, y - 10, x + 10, y + 10]
    ys, xs = np.where(a[110:390, 410:690] < 120)
    i = random.randrange(len(xs))
    x, y = int(xs[i]) + 410, int(ys[i]) + 110
    d.rectangle((x - 15, y - 15, x + 15, y + 15), fill="white")
    return im, [x - 15, y - 15, x + 15, y + 15]


def shoot(sheet: Image.Image, variant: str) -> Image.Image:
    big, _ = _fit(sheet)
    if variant == "ptilt":
        return perspective(on_wall(big, 0.1), 0.06, (90, 88, 86))[0]
    return VARIANTS[variant](big)


def truth_boxes(a: Image.Image, b: Image.Image) -> list[list[int]]:
    """兩張乾淨圖紙的線條不同的地方（膨脹後的連通區塊）。"""
    ia, ib = (np.asarray(x.convert("L")) < 160 for x in (a, b))
    diff = (ia ^ ib).astype(np.uint8)
    diff[800:] = 0  # 標題欄（版次 A／B）不在比對範圍
    n, _, stats, _ = cv2.connectedComponentsWithStats(cv2.dilate(diff, np.ones((15, 15), np.uint8)))
    return [
        [int(x), int(y), int(x + w), int(y + h)] for x, y, w, h, area in stats[1:] if area > 300
    ]


async def _render(part: dict, code: str, rev: str) -> Image.Image:
    from make_drawings import render

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "cad").mkdir()
        (base / f"cad/{part['id']}-{rev}.py").write_text(code, encoding="utf-8")
        p = {
            **part,
            "revision": rev,
            "cad": f"kb/cad/{part['id']}-{rev}.py",
            "drawing": f"kb/drawings/{part['id']}-{rev}.png",
        }
        return Image.open(await render(p, base)).convert("RGB")


async def rev_a() -> Image.Image:
    """mfg-001 rev.A：底板只有 2 個孔、外形不變。

    知識庫圖紙是組員在 Mac 上產生的，字型和這台不同；直接用這台重畫，視圖格裡的尺寸數字、
    標題的筆畫就和知識庫不一樣，也會被當成差異。展示的情境是「同一套系統出的新舊版」，
    所以兩版都在這台畫一次，只把兩版不同的地方（孔位與版次欄）貼到知識庫的 rev.B 圖紙上。
    """
    part = json.loads((ROOT / "kb/parts/mfg-001.json").read_text(encoding="utf-8"))
    code_b = (ROOT / "kb/cad/mfg-001.py").read_text(encoding="utf-8")
    assert REV_A_FROM in code_b, "mfg-001.py 的底板孔位改過了，rev.A 要跟著改"
    code_a = code_b.replace(REV_A_FROM, REV_A_TO)
    (OUT / "mfg-001-revA.py").write_text(code_a, encoding="utf-8")
    a, b = await _render(part, code_a, "A"), await _render(part, code_b, part["revision"])
    changed = (np.asarray(a.convert("L")) != np.asarray(b.convert("L"))).astype(np.uint8)
    patch = cv2.dilate(changed, np.ones((7, 7), np.uint8)).astype(bool)
    kb_b = np.asarray(Image.open(ROOT / "kb/drawings/mfg-001.png").convert("RGB")).copy()
    kb_b[patch] = np.asarray(a)[patch]
    img = Image.fromarray(kb_b)
    img.save(OUT / "mfg-001-revA.png", optimize=True)
    return img


def mfg_photos(rows: list[dict]) -> None:
    for p in sorted((ROOT / "kb/drawings").glob("*.png")):
        sheet = Image.open(p).convert("RGB")
        target = f"part:{p.stem}"
        for v in ("ptilt", "glare", "blur", "dim"):
            name = _save(shoot(sheet, v), OUT / "mfg" / f"{p.stem}__same-{v}.jpg")
            rows.append(
                {
                    "file": name,
                    "domain": "mfg",
                    "target": target,
                    "case": "same",
                    "variant": v,
                    "boxes": [],
                }
            )
        for kind, variants in (("hole", ("ptilt", "glare")), ("erase", ("ptilt", "blur"))):
            for v in variants:
                edited, box = edit(sheet, kind)
                name = _save(shoot(edited, v), OUT / "mfg" / f"{p.stem}__{kind}-{v}.jpg")
                rows.append(
                    {
                        "file": name,
                        "domain": "mfg",
                        "target": target,
                        "case": kind,
                        "variant": v,
                        "boxes": [box],
                    }
                )
    old = asyncio.run(rev_a())
    boxes = truth_boxes(old, Image.open(ROOT / "kb/drawings/mfg-001.png"))
    for v in ("ptilt", "glare", "blur", "dim", "tilt"):
        name = _save(shoot(old, v), OUT / "mfg" / f"mfg-001-revA__{v}.jpg")
        rows.append(
            {
                "file": name,
                "domain": "mfg",
                "target": "part:mfg-001",
                "case": "revA",
                "variant": v,
                "boxes": boxes,
            }
        )


# ---------------------------------------------------------------- 兩張照片互比（畫作）
PAIR_EDITS = ("paste", "smear", "seal", "fade", "tint")


def edit_painting(ref: Image.Image, kind: str) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """在畫面中間一帶改一塊（12%×12%；印章 6%），回傳 (改後的畫, 改動的方框，原圖座標)。"""
    W, H = ref.size
    w, h = int(W * 0.12), int(H * 0.12)
    x, y = (
        random.randint(int(W * 0.15), int(W * 0.85) - w),
        random.randint(int(H * 0.15), int(H * 0.85) - h),
    )
    im, box = ref.copy(), (x, y, x + w, y + h)
    patch = ref.crop(box)
    if kind == "paste":  # 補筆、偽作改了一塊：換成畫上另一處的內容
        sx, sy = random.randint(0, W - w), random.randint(0, H - h)
        im.paste(ref.crop((sx, sy, sx + w, sy + h)), (x, y))
    elif kind == "smear":  # 塗糊
        im.paste(patch.filter(ImageFilter.GaussianBlur(8)), (x, y))
    elif kind == "fade":  # 褪色：變淡、變灰
        im.paste(
            ImageEnhance.Brightness(ImageEnhance.Color(patch).enhance(0.3)).enhance(1.25), (x, y)
        )
    elif kind == "tint":  # 補色：筆觸不變，顏色偏紅
        a = np.asarray(patch).astype(np.float32)
        a[..., 0] = np.clip(a[..., 0] * 1.35 + 25, 0, 255)
        a[..., 2] = np.clip(a[..., 2] * 0.7, 0, 255)
        im.paste(Image.fromarray(a.astype(np.uint8)), (x, y))
    else:  # 小印章（約畫面 0.4%）
        q = min(w, h) // 2
        ImageDraw.Draw(im).rectangle(
            (x, y, x + q, y + q), outline=(170, 30, 30), width=max(2, q // 8)
        )
        box = (x, y, x + q, y + q)
    return im, box


def shoot_painting(img: Image.Image, light: float, warm: float) -> tuple[Image.Image, np.ndarray]:
    """掛在牆上斜斜地拍一張，回傳 (照片, 原圖 → 照片的 H)。light、warm 是那個展間的光線。"""
    big = img.copy()
    big.thumbnail((1024, 1024))
    s = big.width / img.width
    pad = (random.randint(40, 120), random.randint(30, 90))
    wall = Image.new("RGB", (big.width + 2 * pad[0], big.height + 2 * pad[1]), (236, 232, 224))
    wall.paste(big, pad)
    photo, m = perspective(wall, 0.06, (236, 232, 224))
    a = np.asarray(photo).astype(np.float32) * light
    a[..., 0] += warm
    a[..., 2] -= warm
    photo = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(0.6)
    )
    # 上傳時本來就會縮到長邊 1024（load_image），存小一點省 repo 空間
    k = min(1.0, 1024 / max(photo.size))
    photo = photo.resize(
        (round(photo.width * k), round(photo.height * k)), Image.Resampling.LANCZOS
    )
    return photo, np.diag([k, k, 1.0]) @ m @ np.array([[s, 0, pad[0]], [0, s, pad[1]], [0, 0, 1]])


def pair_photos(rows: list[dict]) -> None:
    images = sorted((ROOT / "kb/images").glob("*.jpg")) + sorted(
        (ROOT / "kb_staging/images").glob("*.jpg")
    )
    for p in images:
        ref = Image.open(p).convert("RGB")
        for case in ("same", "same", *PAIR_EDITS):
            light, warm = random.uniform(0.8, 1.05), random.uniform(-6, 6)
            a, ha = shoot_painting(ref, light, warm)
            edited, box = (ref, None) if case == "same" else edit_painting(ref, case)
            # 同一個展間：B 的光線只差一點點
            b, _ = shoot_painting(
                edited, light * random.uniform(0.96, 1.04), warm + random.uniform(-2, 2)
            )
            n = sum(r["file"].startswith(f"pair/{p.stem}__{case}") for r in rows) + 1
            stem = f"pair/{p.stem}__{case}-{n}"
            boxes = []
            if box:
                c = (
                    np.array(
                        [
                            [box[0], box[1], 1],
                            [box[2], box[1], 1],
                            [box[2], box[3], 1],
                            [box[0], box[3], 1],
                        ],
                        float,
                    )
                    @ ha.T
                )
                c = c[:, :2] / c[:, 2:]
                boxes = [
                    [
                        int(c[:, 0].min()),
                        int(c[:, 1].min()),
                        int(c[:, 0].max()) + 1,
                        int(c[:, 1].max()) + 1,
                    ]
                ]
            rows.append(
                {
                    "file": _save(b, OUT / f"{stem}__B.jpg", quality=85),
                    "ref_file": _save(a, OUT / f"{stem}__A.jpg", quality=85),
                    "domain": "pair",
                    "case": case,
                    "boxes": boxes,
                }
            )


# ---------------------------------------------------------------- 畫作找不同：照片 vs 知識庫原圖
ART_EDITS = ("strokes", *PAIR_EDITS)


def scribble(photo: Image.Image, region) -> tuple[Image.Image, np.ndarray]:
    """在照片上用黑筆亂寫幾筆（使用者在照片上畫的那種），回傳 (照片, 改到的像素)。"""
    im = photo.copy()
    d = ImageDraw.Draw(im)
    x0, y0, x1, y1 = region
    for _ in range(random.randint(3, 6)):
        x, y = random.uniform(x0, x1), random.uniform(y0, y1)
        pts = [(x, y)]
        for _ in range(random.randint(4, 10)):
            x = min(max(x + random.uniform(-25, 25), x0), x1)
            y = min(max(y + random.uniform(-25, 25), y0), y1)
            pts.append((x, y))
        d.line(pts, fill=(15, 15, 15), width=random.randint(2, 4), joint="curve")
    changed = np.abs(np.asarray(im, np.int16) - np.asarray(photo, np.int16)).max(axis=2) > 30
    return im, changed


def closeup(ref: Image.Image, must=None) -> tuple[Image.Image, np.ndarray]:
    """近拍長邊 50%（和 art/ 的 crop50 同一個拍法，四角補黑），裁切範圍包含 must（原圖座標）。
    回傳 (照片, 照片 → 原圖的 H)。"""
    W, H = ref.size
    cw, ch = int(W * 0.5), int(H * 0.5)
    if must:
        x0 = random.randint(max(0, must[2] - cw), min(W - cw, must[0]))
        y0 = random.randint(max(0, must[3] - ch), min(H - ch, must[1]))
    else:
        x0, y0 = random.randint(0, W - cw), random.randint(0, H - ch)
    crop, s = _fit(ref.crop((x0, y0, x0 + cw, y0 + ch)))
    photo, m = perspective(crop, 0.04, (0, 0, 0))
    photo = ImageEnhance.Brightness(photo).enhance(random.uniform(0.75, 1.1))
    photo = photo.filter(ImageFilter.GaussianBlur(0.6))
    return photo, np.array([[1 / s, 0, x0], [0, 1 / s, y0], [0, 0, 1]]) @ np.linalg.inv(m)


def hung(ref: Image.Image) -> tuple[Image.Image, np.ndarray]:
    """整幅掛在牆上、斜斜地拍（和 art/ 的 whole 同一個拍法）。"""
    big, s = _fit(ref)
    pad = (90, 70)
    wall = Image.new("RGB", (big.width + 2 * pad[0], big.height + 2 * pad[1]), (236, 232, 224))
    wall.paste(big, pad)
    photo, m = perspective(wall, 0.05, (236, 232, 224))
    photo = ImageEnhance.Brightness(photo).enhance(random.uniform(0.8, 1.05))
    return photo, np.array(
        [[1 / s, 0, -pad[0] / s], [0, 1 / s, -pad[1] / s], [0, 0, 1]]
    ) @ np.linalg.inv(m)


def art_edit_photos(rows: list[dict]) -> None:
    """知識庫畫作動了手腳之後拍照（近拍、整幅各一張）；strokes 是拍完在照片上加筆。
    答案是改動在原圖上的方框。存長邊 1024（上傳時本來就會縮到這麼大）。"""
    images = sorted((ROOT / "kb/images").glob("*.jpg")) + sorted(
        (ROOT / "kb_staging/images").glob("*.jpg")
    )
    for p in images:
        ref = Image.open(p).convert("RGB")
        W, H = ref.size
        for kind in ART_EDITS:
            for style in ("crop50", "whole"):
                if kind == "strokes":
                    photo, hm = closeup(ref) if style == "crop50" else hung(ref)
                    # 筆畫寫在照片裡畫作範圍的中間一帶
                    c = cv2.perspectiveTransform(
                        np.float32([[[0, 0], [W, 0], [W, H], [0, H]]]), np.linalg.inv(hm)
                    )[0]
                    bx0, by0 = max(c[:, 0].min(), 0), max(c[:, 1].min(), 0)
                    bx1, by1 = min(c[:, 0].max(), photo.width), min(c[:, 1].max(), photo.height)
                    bw, bh = bx1 - bx0, by1 - by0
                    rw, rh = bw * random.uniform(0.15, 0.3), bh * random.uniform(0.04, 0.08)
                    rx = random.uniform(bx0 + 0.1 * bw, bx1 - 0.1 * bw - rw)
                    ry = random.uniform(by0 + 0.1 * bh, by1 - 0.1 * bh - rh)
                    photo, changed = scribble(photo, (rx, ry, rx + rw, ry + rh))
                    ys, xs = np.nonzero(changed)
                    pts = cv2.perspectiveTransform(np.float32(np.c_[xs, ys])[None], hm)[0]
                    box = [
                        int(pts[:, 0].min()),
                        int(pts[:, 1].min()),
                        int(pts[:, 0].max()) + 1,
                        int(pts[:, 1].max()) + 1,
                    ]
                else:
                    edited, box = edit_painting(ref, kind)
                    photo, hm = closeup(edited, box) if style == "crop50" else hung(edited)
                    box = list(box)
                k = min(1.0, 1024 / max(photo.size))
                photo = photo.resize(
                    (round(photo.width * k), round(photo.height * k)), Image.Resampling.LANCZOS
                )
                rows.append(
                    {
                        "file": _save(photo, OUT / "artedit" / f"{p.stem}__{kind}-{style}.jpg", 85),
                        "domain": "artedit",
                        "target": f"artwork:{p.stem}",
                        "case": kind,
                        "variant": style,
                        "boxes": [box],
                        "h": (hm @ np.diag([1 / k, 1 / k, 1.0])).tolist(),
                    }
                )


if __name__ == "__main__":
    random.seed(12)
    rows: list[dict] = []
    art_photos(rows)
    mfg_photos(rows)
    pair_photos(rows)  # 新的一批都放在最後：前面幾批的亂數不變，照片和之前產生的一樣
    art_edit_photos(rows)
    (OUT / "truth.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"{len(rows)} 張 → {OUT.relative_to(ROOT)}")
