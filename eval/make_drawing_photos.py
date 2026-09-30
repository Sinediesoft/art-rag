"""產生工廠圖紙的「模擬實拍照」：知識庫圖紙（含 kb_staging）＋未收錄零件的圖紙。

未收錄零件用和知識庫相同的版面與標題欄（最難的拒答情境：版面一樣、零件不同）。
只是拍攝真正實拍照之前的替代品；正式評估請改用實拍照（共用層 §七）。
用法：python eval/make_drawing_photos.py
"""

import asyncio
import random
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

from make_synthetic_photos import VARIANTS  # noqa: E402
from PIL import Image  # noqa: E402

from app.cad.drawing import with_title_block  # noqa: E402
from app.cad.sandbox import run_cad  # noqa: E402

OUT = Path(__file__).parent / "drawing_photos"

# 知識庫沒有的零件（標題欄同樣是虛構工廠）
UNKNOWN = {
    "unknown-01": (
        "六角支柱",
        "HEX-9001",
        """import cadquery as cq
solid = cq.Workplane("XY").polygon(6, 24).extrude(40).faces(">Z").workplane().hole(6)""",
    ),
    "unknown-02": (
        "U 型槽鋼座",
        "UCH-9002",
        """import cadquery as cq
outer = cq.Workplane("XY").box(90, 60, 50, centered=(True, True, False))
inner = cq.Workplane("XY").box(60, 60, 40, centered=(True, True, False)).translate((0, 0, 10))
solid = outer.cut(inner)""",
    ),
    "unknown-03": (
        "皮帶輪輪轂",
        "PUL-9003",
        """import cadquery as cq
rim = cq.Workplane("XY").circle(60).circle(50).extrude(25)
web = cq.Workplane("XY").workplane(offset=9).circle(50).circle(20).extrude(7)
hub = cq.Workplane("XY").circle(20).circle(9).extrude(35)
solid = rim.union(web).union(hub)""",
    ),
    "unknown-04": (
        "線性滑軌擋塊",
        "STP-9004",
        """import cadquery as cq
body = cq.Workplane("XY").box(60, 30, 25, centered=(True, True, False))
notch = cq.Workplane("XY").box(20, 30, 12, centered=(True, True, False)).translate((0, 0, 13))
holes = cq.Workplane("XY").pushPoints([(-20, 0), (20, 0)]).circle(3.4).extrude(25)
solid = body.cut(notch).cut(holes)""",
    ),
    "unknown-05": (
        "方形法蘭蓋",
        "CVR-9005",
        """import cadquery as cq
plate = cq.Workplane("XY").box(100, 100, 12, centered=(True, True, False))
boss = cq.Workplane("XY").workplane(offset=12).circle(30).extrude(15)
bolts = cq.Workplane("XY").rect(80, 80, forConstruction=True).vertices().circle(4.5).extrude(12)
solid = plate.union(boss).cut(bolts).faces(">Z").workplane().hole(20)""",
    ),
}


async def unknown_drawing(key: str, name: str, part_no: str, code: str) -> Image.Image:
    with tempfile.TemporaryDirectory() as tmp:
        r = await run_cad(code, Path(tmp), trusted=True)
        if not r.ok:
            raise RuntimeError(f"{key}: {r.error}")
        return with_title_block(
            Image.open(Path(tmp) / "reproj.png"),
            {
                "company": "示範精密機械（虛構）",
                "name": name,
                "part_no": part_no,
                "drawing_no": f"D-25-{key[-2:]}90",
                "material": "S45C 中碳鋼",
                "revision": "A",
                "confidentiality": "內部",
            },
        )


def variants(img: Image.Image, stem: str, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    base = img.convert("RGB")
    base.thumbnail((1600, 1600))
    for name, fn in VARIANTS.items():
        photo = fn(base.copy())
        photo.thumbnail((1024, 1024))
        photo.save(folder / f"{stem}__{name}.jpg", quality=80)


async def main() -> None:
    random.seed(7)
    for folder in ("kb", "kb_staging"):
        for path in sorted((ROOT / folder / "drawings").glob("*.png")):
            variants(Image.open(path), path.stem, OUT / "known")
            print("✓", path.stem)
    for key, (name, part_no, code) in UNKNOWN.items():
        img = await unknown_drawing(key, name, part_no, code)
        (OUT / "unknown").mkdir(parents=True, exist_ok=True)
        img.save(OUT / "unknown" / f"{key}.png")
        variants(img, key, OUT / "unknown")
        print("✓", key, name)


if __name__ == "__main__":
    asyncio.run(main())
