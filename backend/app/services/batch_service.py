"""批次辨識（docs/adr/017）：一次丟一批照片，逐張辨識，結果一列一列串流回去，
前端組成表格、匯出 CSV。

每張照片：
1. 模糊程度（和照片建檔同一個指標與門檻 intake.max_blur）：太模糊就標出來、不辨識——
   糊照硬判容易對錯，列成「請重拍」比給一個看似有把握的答案好；
2. 領域路由＋該領域的辨識（search_service.identify_any，和以圖搜圖同一套）；指定領域就只跑那個領域；
3. 資料範圍：判成工廠圖紙但目前身分不能用工廠圖紙、或辨識出的是看不到的圖紙 → 這一列標「看不到」，
   不透露是哪一張（和 /search/drawing 回 403 同一個規則，只是批次裡不讓整批失敗）。
只用 Chinese-CLIP 與 ORB（不呼叫生成模型），全程不外送。
"""

import asyncio
import time
from collections import Counter
from collections.abc import AsyncIterator

from app.analysis import page
from app.core.config import get_models_config
from app.core.errors import AppError
from app.rag.preprocess import load_image
from app.repositories.index_store import get_store
from app.services import memory_guard
from app.services.chat_service import NO_EGRESS, sse
from app.services.identity import Account
from app.services.search_service import identify, identify_any, identify_drawing, load_upload

STATUS_LABEL = {
    "matched": "認得",
    "not_in_kb": "不在知識庫",
    "blurry": "太模糊，請重拍",
    "hidden": "目前身分看不到",
    "error": "辨識失敗",
}


def _artwork_item(a: dict) -> dict:
    return {
        "kind": "artwork",
        "id": a["id"],
        "label": a["title"]["zh"],
        "detail": f"{a['artist']['zh']}・{a['date_text']}",
        "url": f"/artworks/{a['id']}",
        "level": "公開",
    }


def _part_item(p: dict) -> dict:
    return {
        "kind": "part",
        "id": p["id"],
        "label": p["name"]["zh"],
        "detail": f"{p['part_no']}・圖號 {p['drawing_no']} rev.{p['revision']}",
        "url": f"/drawings/{p['id']}",
        "level": p["confidentiality"],
    }


def _row_from_artwork(row: dict, result: dict) -> None:
    store = get_store()
    best = result["results"][0] if result["results"] else None
    if best:
        row.update(score=best["score"], inliers=best["inliers"])
    if result["matched"]:
        row.update(
            status="matched", item=_artwork_item(store.get_artwork(result["best_artwork_id"]))
        )
    else:
        row["status"] = "not_in_kb"
        if best:
            row["closest"] = _artwork_item(store.get_artwork(best["artwork"]["id"]))


def _row_from_drawing(row: dict, result: dict, account: Account) -> None:
    store = get_store()
    if result["matched"]:
        part = store.get_part(result["best_part_id"])
        if not account.can_see(part["confidentiality"]):
            row["status"] = "hidden"
            row["note"] = "辨識出的圖紙不在你的資料範圍內，不顯示是哪一張"
            return
        best = result["results"][0]
        row.update(
            status="matched",
            item=_part_item(part),
            score=best["score"],
            inliers=best["inliers"],
            overlap=best["overlap"],
        )
        return
    row["status"] = "not_in_kb"
    visible = [r for r in result["results"] if account.can_see(r["part"]["confidentiality"])]
    if visible:
        best = visible[0]
        row.update(score=best["score"], inliers=best["inliers"], overlap=best["overlap"])
        row["closest"] = _part_item(store.get_part(best["part"]["id"]))


def identify_one(image_id: str, index: int, domain: str | None, account: Account) -> dict:
    """一張照片 → 一列結果（同步，在執行緒裡跑）。"""
    t0 = time.perf_counter()
    cfg = get_models_config().intake
    row: dict = {
        "index": index,
        "image_id": image_id,
        "domain": None,
        "route": None,
        "blur": None,
        "status": "error",
        "item": None,
        "closest": None,
        "score": None,
        "inliers": None,
        "overlap": None,
        "note": None,
    }
    try:
        img = load_image(load_upload(image_id))
        row["blur"] = round(page.blur_score(img), 3)
        if row["blur"] > cfg.max_blur:
            row["status"] = "blurry"
            row["note"] = f"模糊程度 {row['blur']:.2f}（{cfg.max_blur:.2f} 以下才辨識）"
            return row
        if domain == "art":
            row["domain"] = "art"
            _row_from_artwork(row, identify(image_id))
        elif domain == "mfg":
            row["domain"] = "mfg"
            if not account.can_read("mfg"):
                row.update(status="hidden", note="目前身分不能使用工廠圖紙")
                return row
            _row_from_drawing(row, identify_drawing(image_id), account)
        else:
            found = identify_any(image_id)
            row["route"] = found["route"]
            row["domain"] = found["route"]["domain"]
            if found["artwork_result"] is not None:
                _row_from_artwork(row, found["artwork_result"])
            elif not account.can_read("mfg"):
                row.update(status="hidden", note="看起來是工廠圖紙；目前身分不能使用工廠圖紙")
            else:
                _row_from_drawing(row, found["drawing_result"], account)
    except AppError as e:
        row.update(status="error", note=e.message)
    finally:
        row["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    return row


async def batch_stream(
    image_ids: list[str], domain: str | None, account: Account, request_id: str
) -> AsyncIterator[str]:
    """逐張辨識：每張一個 row 事件，最後 done 附統計。只用 Chinese-CLIP（不載入生成模型）。"""
    events = _batch(image_ids, domain, account, request_id)
    async for e in memory_guard.stream("batch_identify", {"clip"}, events):
        yield e


async def _batch(
    image_ids: list[str], domain: str | None, account: Account, request_id: str
) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    counts: Counter[str] = Counter()
    domains: Counter[str] = Counter()
    for i, image_id in enumerate(image_ids):
        row = await asyncio.to_thread(identify_one, image_id, i, domain, account)
        counts[row["status"]] += 1
        if row["domain"]:
            domains[row["domain"]] += 1
        yield sse("row", row)
    total = round((time.perf_counter() - t0) * 1000)
    yield sse(
        "done",
        {
            "request_id": request_id,
            "total": len(image_ids),
            "counts": {k: counts.get(k, 0) for k in STATUS_LABEL},
            "domains": {k: domains.get(k, 0) for k in ("art", "mfg")},
            "latency_ms": {"total": total, "per_image": round(total / max(len(image_ids), 1))},
            "egress": NO_EGRESS,
        },
    )
