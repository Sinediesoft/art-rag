"""產生工廠圖紙（make drawings）：kb/cad/<id>.py → 三視圖＋標題欄 → kb/drawings/<id>.png。

三視圖照 Ortho2CAD 訓練資料的格式（第一角法、隱藏線點線、外形尺寸），下方加上標題欄。
標準模型改了就要重跑，再 make index。

用法：
    python pipelines/make_drawings.py                 # kb/parts 全部
    python pipelines/make_drawings.py --dir kb_staging
"""

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from PIL import Image

from app.cad.drawing import with_title_block
from app.cad.sandbox import run_cad
from app.core.config import REPO_ROOT


async def render(part: dict, base: Path) -> Path:
    code = (base / part["cad"].removeprefix("kb/")).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        r = await run_cad(code, Path(tmp), trusted=True)
        if not r.ok:
            raise RuntimeError(f"{part['id']} 標準模型執行失敗：{r.error}")
        img = Image.open(Path(tmp) / "reproj.png")
        sheet = with_title_block(
            img,
            {
                "company": part["company"],
                "name": part["name"]["zh"],
                "part_no": part["part_no"],
                "drawing_no": part["drawing_no"],
                "material": part["material"],
                "revision": part["revision"],
                "confidentiality": part["confidentiality"],
            },
        )
    out = base / part["drawing"].removeprefix("kb/")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="kb", help="kb 或 kb_staging")
    args = parser.parse_args()
    base = REPO_ROOT / args.dir
    parts = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted((base / "parts").glob("*.json"))
    ]
    if not parts:
        print(f"{base}/parts 沒有零件")
        return 1
    results = await asyncio.gather(*(render(p, base) for p in parts), return_exceptions=True)
    failed = 0
    for p, r in zip(parts, results, strict=True):
        if isinstance(r, Exception):
            failed += 1
            print(f"  ✗ {p['id']}：{r}")
        else:
            print(f"  ✓ {p['id']}  {p['name']['zh']} → {r.relative_to(REPO_ROOT)}")
    shutil.rmtree(base / "drawings" / "__pycache__", ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
