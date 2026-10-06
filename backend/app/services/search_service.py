"""以圖搜圖、以文搜圖的流程編排（畫作與工廠圖紙）。"""

import time

import numpy as np
from PIL import Image

from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.core.errors import AppError
from app.rag import verify
from app.rag.embedders import embed_image, embed_text, embed_text_clip
from app.rag.preprocess import load_image
from app.rag.router import route
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services.memory_guard import guarded


def artwork_summary(a: dict) -> dict:
    return {
        "id": a["id"],
        "title_zh": a["title"]["zh"],
        "title_en": a["title"].get("en"),
        "artist_zh": a["artist"]["zh"],
        "artist_en": a["artist"].get("en"),
        "date_text": a["date_text"],
        "collection": a["collection"],
        "image_url": f"/api/v1/artworks/{a['id']}/image",
        "thumb_url": f"/api/v1/artworks/{a['id']}/image?size=thumb",
        "style_tags": a.get("style_tags", []),
    }


def load_upload(image_id: str) -> bytes:
    path = get_logs_repo().get_upload(image_id)
    if not path:
        raise AppError("IMAGE_NOT_FOUND", "找不到這張照片，可能已超過保存期限", 404)
    return (get_settings().uploads_dir / path).read_bytes()


@guarded("search_image", {"clip"})
def identify(
    image_id: str,
    top_k: int | None = None,
    img: Image.Image | None = None,
    vec: np.ndarray | None = None,
) -> dict:
    """兩階段辨識：Chinese-CLIP 粗篩 → 前 N 名做 ORB 幾何驗證。

    img／vec 是領域路由已經算好的照片與 CLIP 向量，傳進來就不重算。"""
    cfg = get_models_config().retrieval
    k = top_k or int(cfg["top_k_search"])
    t0 = time.perf_counter()
    img = img or load_image(load_upload(image_id))
    # 取 max(k, verify_top_n) 名：verify_top_n 比 top_k_search 大時，多出來的也要驗（和圖紙一樣）
    hits = get_store().search_images(
        embed_image(img) if vec is None else vec, max(k, int(cfg["verify_top_n"]))
    )

    threshold = float(cfg["image_threshold"])
    min_inliers = int(cfg["verify_min_inliers"])
    query_feats = verify.features(img)
    results = []
    for rank, h in enumerate(hits):
        inliers = None
        if rank < int(cfg["verify_top_n"]) and h.score >= threshold:
            path = REPO_ROOT / h.item["image"]["path"]
            # 退化的 homography（畫框花紋對成一點、鏡像）不算對上：docs/adr/002「退化的 homography」
            inliers = verify.count_inliers(
                query_feats, verify.kb_features(path, path.stat().st_mtime), geometry=cfg
            )
        results.append(
            {
                "artwork": artwork_summary(h.item),
                "score": round(h.score, 4),
                "inliers": inliers,
                "verified": inliers is not None and inliers >= min_inliers,
            }
        )
    # 通過驗證的排最前面（inlier 多者優先），其餘照 CLIP 分數
    results.sort(key=lambda r: (not r["verified"], -(r["inliers"] or 0), -r["score"]))
    matched = bool(results) and results[0]["verified"]
    return {
        "query_image_id": image_id,
        "threshold": threshold,
        "min_inliers": min_inliers,
        "matched": matched,
        "best_artwork_id": results[0]["artwork"]["id"] if matched else None,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "results": results[:k],
    }


@guarded("search_text", {"clip", "bge"})
def search_text(q: str, top_k: int | None = None) -> dict:
    """Chinese-CLIP（文字→畫面）與 bge-m3（文字→知識段落）兩路排序，用 RRF 融合。"""
    store = get_store()
    k = top_k or int(get_models_config().retrieval["top_k_search"])
    t0 = time.perf_counter()
    clip_sims = store.image_vecs @ embed_text_clip([q])[0]
    chunk_sims = store.chunk_vecs @ embed_text([q])[0]
    text_best: dict[str, float] = {}
    for c, s in zip(store.chunks, chunk_sims, strict=True):
        # 色彩段落只給問答用：依色彩找畫不在範圍內（ADR 010），不讓它影響以文搜圖的排名
        if c["chunk_id"].endswith("#color"):
            continue
        text_best[c["artwork_id"]] = max(text_best.get(c["artwork_id"], -1.0), float(s))

    ids = [a["id"] for a in store.artworks]
    clip_rank = {
        ids[i]: r for r, i in enumerate(sorted(range(len(ids)), key=lambda i: -clip_sims[i]))
    }
    text_rank = {aid: r for r, aid in enumerate(sorted(ids, key=lambda a: -text_best.get(a, -1)))}
    rrf = {aid: 1 / (60 + clip_rank[aid]) + 1 / (60 + text_rank[aid]) for aid in ids}
    order = sorted(ids, key=lambda a: -rrf[a])[:k]
    return {
        "query": q,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "results": [
            {
                "artwork": artwork_summary(store.by_id[aid]),
                "score": round(rrf[aid] / (2 / 60), 4),
                "image_score": round(float(clip_sims[ids.index(aid)]), 4),
                "text_score": round(text_best.get(aid, 0.0), 4),
            }
            for aid in order
        ],
    }


# ---------------------------------------------------------------- 工廠圖紙
def part_summary(p: dict) -> dict:
    # 圖紙、縮圖、模型網址帶上知識庫 hash：重建索引後瀏覽器不會拿到快取的舊檔
    v = get_store().manifest.get("kb_hash", "")[:8]
    return {
        "id": p["id"],
        "part_no": p["part_no"],
        "drawing_no": p["drawing_no"],
        "revision": p["revision"],
        "name_zh": p["name"]["zh"],
        "name_en": p["name"].get("en"),
        "category": p["category"],
        "material": p["material"],
        "confidentiality": p["confidentiality"],
        "geometry": p["geometry"],
        "drawing_url": f"/api/v1/parts/{p['id']}/drawing?v={v}",
        "thumb_url": f"/api/v1/parts/{p['id']}/drawing?size=thumb&v={v}",
        # 照片建檔的零件沒有標準模型（docs/adr/013）
        "model_url": f"/api/v1/parts/{p['id']}/model.stl?v={v}" if "cad" in p else None,
        "intake": "intake" in p,
        "tags": p.get("tags", []),
    }


def _drawing_path(p: dict):
    return REPO_ROOT / p["drawing"]


@guarded("search_image", {"clip"})
def identify_drawing(
    image_id: str,
    top_k: int | None = None,
    img: Image.Image | None = None,
    vec: np.ndarray | None = None,
) -> dict:
    """圖紙辨識三道關：Chinese-CLIP 粗篩 → ORB 幾何驗證（遮掉固定版面、排除退化 homography）
    → 拉正後比對線條重合度。三道都過才算辨識成功。"""
    cfg = get_models_config().drawing_retrieval
    k = top_k or int(cfg["top_k_search"])
    t0 = time.perf_counter()
    img = img or load_image(load_upload(image_id))
    mfg = get_store().mfg
    qvec = embed_image(img) if vec is None else vec
    hits = mfg.search_images(qvec, max(k, int(cfg["verify_top_n"])))
    threshold, min_inliers = float(cfg["image_threshold"]), int(cfg["verify_min_inliers"])
    min_overlap = float(cfg["verify_min_overlap"])
    query = verify.features(img)
    results = []
    for rank, h in enumerate(hits):
        inliers, overlap = None, None
        if rank < int(cfg["verify_top_n"]) and h.score >= threshold:
            path = _drawing_path(h.item)
            inliers, hom = verify.match(
                query, verify.kb_features(path, path.stat().st_mtime, drawing=True), strict=True
            )
            if hom is not None and inliers >= min_inliers:
                overlap = round(verify.ink_overlap(img, hom, path), 3)
        results.append(
            {
                "part": part_summary(h.item),
                "score": round(h.score, 4),
                "inliers": inliers,
                "overlap": overlap,
                "verified": overlap is not None and overlap >= min_overlap,
            }
        )
    results.sort(key=lambda r: (not r["verified"], -(r["inliers"] or 0), -r["score"]))
    matched = bool(results) and results[0]["verified"]
    return {
        "query_image_id": image_id,
        "threshold": threshold,
        "min_inliers": min_inliers,
        "min_overlap": min_overlap,
        "matched": matched,
        "best_part_id": results[0]["part"]["id"] if matched else None,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "results": results[:k],
    }


@guarded("search_image", {"clip"})
def identify_any(image_id: str, top_k: int | None = None) -> dict:
    """不指定領域的以圖搜圖：領域路由先判斷是畫作還是工廠圖紙，只跑該領域的辨識。
    照片與 CLIP 向量只算一次，路由與辨識共用。"""
    t0 = time.perf_counter()
    img = load_image(load_upload(image_id))
    vec = embed_image(img)
    r = route(vec)
    art = identify(image_id, top_k, img, vec) if r.domain == "art" else None
    mfg = identify_drawing(image_id, top_k, img, vec) if r.domain == "mfg" else None
    return {
        "query_image_id": image_id,
        "route": r.summary(),
        "artwork_result": art,
        "drawing_result": mfg,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
    }


def rectify_to_part(img: Image.Image, part: dict) -> Image.Image | None:
    """把照片依幾何驗證的 homography 拉正到知識庫圖紙的版面（800×970），對不上就回 None。"""
    import cv2

    path = _drawing_path(part)
    ref = verify.kb_features(path, path.stat().st_mtime, drawing=True)
    inliers, h = verify.match(verify.features(img), ref, strict=True)
    if h is None or inliers < int(get_models_config().drawing_retrieval["verify_min_inliers"]):
        return None
    w, hh = Image.open(path).size
    warped = cv2.warpPerspective(
        np.asarray(img.convert("RGB")), h, (w, hh), borderValue=(255, 255, 255)
    )
    return Image.fromarray(warped)


@guarded("search_text", {"bge"})
def search_parts_text(
    q: str, top_k: int | None = None, levels: tuple[str, ...] | list[str] | None = None
) -> dict:
    """以文字找圖紙：bge-m3（文字→零件知識段落），每個零件取最相關段落的分數。

    levels：目前身分看得到的機密等級（Metadata Filter，看不到的圖紙在檢索時就濾掉）；None＝不限。"""
    mfg = get_store().mfg
    k = top_k or int(get_models_config().drawing_retrieval["top_k_search"])
    t0 = time.perf_counter()
    visible = {p["id"] for p in mfg.items if levels is None or p["confidentiality"] in levels}
    best: dict[str, tuple[float, dict]] = {}
    if mfg.chunks:
        sims = mfg.chunk_vecs @ embed_text([q])[0]
        for c, s in zip(mfg.chunks, sims, strict=True):
            if c["part_id"] not in visible:
                continue
            if c["part_id"] not in best or s > best[c["part_id"]][0]:
                best[c["part_id"]] = (float(s), c)
    order = sorted(best, key=lambda pid: -best[pid][0])[:k]
    return {
        "query": q,
        "hidden": len(mfg.items) - len(visible),
        "filter": None
        if levels is None
        else "level IN (" + ", ".join(f'"{x}"' for x in levels if x != "公開") + ")",
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "results": [
            {
                "part": part_summary(mfg.by_id[pid]),
                "score": round(best[pid][0], 4),
                "topic": best[pid][1]["topic"],
                "snippet": best[pid][1]["text"][:90],
            }
            for pid in order
        ],
    }
