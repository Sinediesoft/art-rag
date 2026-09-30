"""建索引指令（make index）：kb/*.json → 驗證 → 向量化 → data/index/ + manifest。

畫作：kb/artworks → data/index/*；工廠圖紙：kb/parts → data/index/parts/*
（另外執行標準 CadQuery 模型，存 STL／STEP 給前端 3D 檢視與 IoU 比對，
並算出外形尺寸與重量寫進基本資料段落）。

用法：
    python pipelines/build_index.py           # 驗證並重建索引
    python pipelines/build_index.py --check   # 只做 JSON Schema 與授權檢查（CI 用）

每位組員與展示主機都用同一個指令從 JSON 重建，不互傳索引檔。
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import numpy as np
from PIL import Image

from app.core.config import REPO_ROOT, get_models_config, get_settings, kb_version
from app.rag.chunking import build_chunks, build_part_chunks
from app.rag.kb import kb_hash, validate_kb, validate_parts


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip() or "no-git"
    except OSError:
        return "no-git"


async def build_parts(parts: list[dict], out: Path) -> list[dict]:
    """工廠圖紙：執行標準模型（STL／STEP／尺寸）→ 圖紙向量 → 段落向量。"""
    from app.cad.sandbox import run_cad
    from app.rag.embedders import embed_image, embed_text
    from app.rag.preprocess import load_image, to_jpeg_bytes

    (out / "thumbs").mkdir(parents=True)
    (out / "gt").mkdir()
    runs = await asyncio.gather(
        *(
            run_cad(
                (REPO_ROOT / p["cad"]).read_text(encoding="utf-8"),
                out / "gt" / p["id"],
                trusted=True,
            )
            for p in parts
        )
    )
    image_vecs, chunks, items = [], [], []
    for p, r in zip(parts, runs, strict=True):
        if not r.ok:
            raise SystemExit(f"{p['id']} 標準模型執行失敗：{r.error}")
        job = out / "gt" / p["id"]
        (job / "model.stl").rename(out / "gt" / f"{p['id']}.stl")
        (job / "model.step").rename(out / "gt" / f"{p['id']}.step")
        shutil.rmtree(job)
        dims = {k: round(v, 3) for k, v in r.data["dims"].items()}
        volume = r.data["volume"]
        item = {
            **p,
            "geometry": {
                **dims,
                "volume_mm3": round(volume, 1),
                "weight_kg": round(volume * p["density_g_cm3"] / 1e6, 4),
                "faces": r.data["faces"],
            },
        }
        img = load_image(REPO_ROOT / p["drawing"])
        image_vecs.append(embed_image(img))
        # 卡片縮圖只取三視圖區（去掉標題欄與留白），線條才看得清楚
        thumb = Image.open(REPO_ROOT / p["drawing"]).convert("RGB").crop((40, 50, 790, 760))
        thumb.thumbnail((480, 480))
        (out / "thumbs" / f"{p['id']}.jpg").write_bytes(to_jpeg_bytes(thumb, 88))
        chunks.extend(build_part_chunks(item))
        items.append(item)
        g = item["geometry"]
        print(
            f"  ✓ {p['id']}  {p['name']['zh']}  {g['width']:g}×{g['depth']:g}×{g['height']:g} mm, "
            f"{g['weight_kg']:.3f} kg"
        )
    np.save(out / "image_vecs.npy", np.stack(image_vecs).astype(np.float32))
    np.save(out / "chunk_vecs.npy", embed_text([c["text"] for c in chunks]).astype(np.float32))
    (out / "parts.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    (out / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只驗證，不建索引")
    args = parser.parse_args()

    artworks, errors = validate_kb()
    parts, part_errors = validate_parts()
    errors += part_errors
    print(
        f"知識庫版本 {kb_version()}：畫作 {len(artworks)} 筆、圖紙 {len(parts)} 筆通過驗證，"
        f"{len(errors)} 個問題"
    )
    for e in errors:
        print("  ✗", e)
    if args.check:
        return 1 if errors else 0
    if not artworks:
        print("沒有可建索引的畫作")
        return 1

    from app.rag.embedders import embed_image, embed_text
    from app.rag.preprocess import load_image, to_jpeg_bytes

    t0 = time.time()
    settings = get_settings()
    cfg = get_models_config()
    tmp = settings.index_dir.with_name("index.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "thumbs").mkdir(parents=True)

    image_vecs, chunks = [], []
    for a in artworks:
        img = load_image(REPO_ROOT / a["image"]["path"])
        image_vecs.append(embed_image(img))
        thumb = img.copy()
        thumb.thumbnail((480, 480))
        (tmp / "thumbs" / f"{a['id']}.jpg").write_bytes(to_jpeg_bytes(thumb, 82))
        chunks.extend(build_chunks(a))
        print(f"  ✓ {a['id']}  〈{a['title']['zh']}〉")

    chunk_vecs = embed_text([c["text"] for c in chunks])

    part_chunks = asyncio.run(build_parts(parts, tmp / "parts")) if parts else []

    np.save(tmp / "image_vecs.npy", np.stack(image_vecs).astype(np.float32))
    np.save(tmp / "chunk_vecs.npy", chunk_vecs.astype(np.float32))
    (tmp / "artworks.json").write_text(json.dumps(artworks, ensure_ascii=False), encoding="utf-8")
    (tmp / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "models": {
            k: {"name": s.name, "revision": s.revision, "dim": s.dim, "dtype": s.dtype}
            for k, s in cfg.embeddings.items()
        },
        "embed_mode": settings.embed_mode,
        "chunking": cfg.chunking,
        "kb_version": kb_version(),
        "kb_hash": kb_hash(artworks, parts),
        "artwork_count": len(artworks),
        "chunk_count": len(chunks),
        "part_count": len(parts),
        "part_chunk_count": len(part_chunks),
        "git_commit": git_commit(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    (tmp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 整個目錄一次換上，後端不會讀到一半的索引
    shutil.rmtree(settings.index_dir, ignore_errors=True)
    tmp.rename(settings.index_dir)
    print(
        f"索引完成：{len(artworks)} 幅畫、{len(chunks)} 個段落；"
        f"{len(parts)} 張圖紙、{len(part_chunks)} 個段落，"
        f"{time.time() - t0:.1f} 秒 → {settings.index_dir}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
