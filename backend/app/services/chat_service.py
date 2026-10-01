"""圖文問答流程（論文 Fig. 1c）：影像 → embedding → 相似度搜尋 → 取回脈絡 → 增強 prompt → 生成。

只有最後一步依 strategy 切換生成端。hybrid／lora 連不上時改走本地備援模型，並在 done 事件
標註 fallback；本地都失敗就回覆暫停服務，永遠不改走雲端。雲端（api_nokb／api_kb）只當對照組，
不接受使用者上傳的照片；每次回應都記錄送出本機的資料量（egress）。

工廠圖紙（part_id）走同一條流程，只換知識庫領域與 prompt 模板（drawing_v1）；
圖紙屬企業機密，雲端對照組一律不接受。
"""

import json
import time
from collections.abc import AsyncIterator

from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.core.logging import log
from app.rag import rearrange as rearrange_mod
from app.rag.embedders import embed_image, embed_text
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.prompt import build_messages, prompt_version
from app.rag.providers import (
    CLOUD_STRATEGIES,
    ProviderUnavailable,
    cloud_status,
    estimate_cost_twd,
    get_provider,
)
from app.rag.router import route
from app.rag.textproc import to_taiwan
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services.search_service import identify, identify_drawing, load_upload

# 備援只在本地之間：主推論伺服器 → 本地備援模型；雲端不在任何備援鏈上
FALLBACK_CHAIN = {"hybrid": ["hybrid_fallback"], "lora": ["hybrid", "hybrid_fallback"]}
NO_EGRESS = {"images": 0, "chunks": 0, "bytes": 0}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _source(i: int, h, store) -> dict:
    item = h.item
    base = {
        "ref": i + 1,
        "chunk_id": item["chunk_id"],
        "topic": item["topic"],
        "text": item["text"],
        "source_url": item["source_url"],
        "license": item["license"],
        "score": round(h.score, 4),
    }
    if "part_id" in item:
        title = store.mfg.by_id[item["part_id"]]["name"]["zh"]
        return {**base, "part_id": item["part_id"], "title": title, "source_label": item["source"]}
    title = store.by_id[item["artwork_id"]]["title"]["zh"]
    return {**base, "artwork_id": item["artwork_id"], "artwork_title": title, "title": title}


def retrieve(question: str, artwork_id: str | None, part_id: str | None = None) -> list[dict]:
    """檢索段落規則（共用層 §三）：已指定畫作（或圖紙）只取它的段落；相似度門檻＋最多 k 段，
    不硬湊滿；只有比較、背景類問題（或未指定）才從同一領域的全庫補足。"""
    store = get_store()
    cfg = get_models_config()
    coll, owner = (store.mfg, part_id) if part_id else (store.art, artwork_id)
    k = int(cfg.retrieval["top_k_chunks"])
    qvec = embed_text([question])[0]
    wide = not owner or any(w in question for w in cfg.global_fill_keywords)
    own = coll.search_chunks(qvec, k, owner_id=owner) if owner else []
    seen = {h.item["chunk_id"] for h in own}
    extra = coll.search_chunks(qvec, k, exclude=seen) if wide else []
    best = max((h.score for h in own + extra), default=0.0)
    floor = max(cfg.retrieval["min_chunk_score"], best * cfg.retrieval["relative_chunk_ratio"])
    hits = [h for h in own if h.score >= floor] or own[:1]  # 已指定時至少留最相關的 1 段
    hits += [h for h in extra if h.score >= floor][: k - len(hits)]
    return [_source(i, h, store) for i, h in enumerate(hits)]


async def chat_stream(
    question: str,
    request_id: str,
    strategy: str = "hybrid",
    artwork_id: str | None = None,
    image_id: str | None = None,
    use_retrieval: bool = True,
    allow_fallback: bool = True,
    part_id: str | None = None,
    rearrange: bool | None = None,
) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    store = get_store()
    identified = None
    cloud = strategy in CLOUD_STRATEGIES
    domain = "mfg" if part_id else "art"

    # 工廠圖紙屬企業機密：雲端對照組一律不接受（連知識庫段落也不送出）
    if cloud and domain == "mfg":
        yield sse(
            "error",
            {
                "code": "CLOUD_CONFIDENTIAL_FORBIDDEN",
                "request_id": request_id,
                "message": "工廠圖紙與製程文件屬企業機密，不送往任何雲端 API（包含對照組）",
            },
        )
        return

    # 0. 雲端對照組不接受使用者照片：使用者資料永遠不出站
    if cloud and image_id:
        yield sse(
            "error",
            {
                "code": "CLOUD_UPLOAD_FORBIDDEN",
                "request_id": request_id,
                "message": "雲端對照組不接受使用者上傳的照片，請在策略比較頁選擇知識庫畫作",
            },
        )
        return
    if cloud and not (status := cloud_status())[0]:
        yield sse(
            "error",
            {"code": "STRATEGY_UNAVAILABLE", "request_id": request_id, "message": status[1]},
        )
        return
    if strategy == "api_nokb":
        use_retrieval = False  # A1：只送照片與問題

    # 1. 以圖辨識（已指定畫作或圖紙就跳過）。只給照片時先經過領域路由（MMed-RAG 的領域辨識），
    #    判斷是畫作還是圖紙，再走該領域的辨識。雲端策略在第 0 步已拒收照片，
    #    所以路由成圖紙時不會是雲端。
    route_info = None
    if not artwork_id and not part_id and image_id:
        img = load_image(load_upload(image_id))
        vec = embed_image(img)
        r = route(vec)
        route_info, domain = r.summary(), r.domain
        if domain == "mfg":
            identified = identify_drawing(image_id, img=img, vec=vec)
        else:
            identified = identify(image_id, img=img, vec=vec)
        if not identified["matched"]:
            yield sse(
                "error",
                {
                    "code": "NOT_IN_KB",
                    "request_id": request_id,
                    "message": "知識庫中沒有這張圖紙" if domain == "mfg" else "知識庫中沒有這幅畫",
                },
            )
            return
        if domain == "mfg":
            part_id = identified["best_part_id"]
        else:
            artwork_id = identified["best_artwork_id"]
    if domain == "mfg":
        artwork = store.get_part(part_id)
        if not artwork:
            yield sse(
                "error",
                {
                    "code": "PART_NOT_FOUND",
                    "request_id": request_id,
                    "message": f"找不到圖紙 {part_id}",
                },
            )
            return
    else:
        artwork = store.get_artwork(artwork_id) if artwork_id else None
        if artwork_id and not artwork:
            yield sse(
                "error",
                {
                    "code": "ARTWORK_NOT_FOUND",
                    "request_id": request_id,
                    "message": f"找不到畫作 {artwork_id}",
                },
            )
            return

    # 2. 檢索（關檢索時仍算一次，供前端比較用，但不放進 prompt）。
    #    開啟段落篩選（MIRA 的 Rearrange）時，再請本地模型只留有幫助的段落；
    #    只有真的要放進 prompt 才篩，篩選時間算在 retrieval 裡
    sources = retrieve(question, artwork_id, part_id)
    rearrange_info = None
    if use_retrieval and rearrange_mod.enabled(rearrange):
        sources, rearrange_info = await rearrange_mod.rearrange(question, sources)
    retrieval_ms = round((time.perf_counter() - t0) * 1000)
    yield sse(
        "sources",
        {
            "request_id": request_id,
            "artwork_id": artwork_id,
            "part_id": part_id,
            "strategy": strategy,
            "identified": identified,
            "route": route_info,
            "rearrange": rearrange_info,
            "use_retrieval": use_retrieval,
            "sources": sources if use_retrieval else [],
        },
    )

    # 3. 組 prompt（照片優先，否則用知識庫圖檔；圖紙要看得清楚標註，用 1024 px 原圖）
    if image_id:
        image_jpeg = to_jpeg_bytes(load_image(load_upload(image_id)))
    elif domain == "mfg":
        image_jpeg = to_jpeg_bytes(load_image(REPO_ROOT / artwork["drawing"]))
    elif artwork:
        image_jpeg = (get_settings().index_dir / "thumbs" / f"{artwork['id']}.jpg").read_bytes()
    else:
        image_jpeg = None
    messages = build_messages(
        question,
        artwork,
        sources,
        image_jpeg,
        use_retrieval,
        include_card=strategy != "api_nokb",
        domain=domain,
    )

    # 4. 依 strategy 生成；失敗依本地備援鏈改走下一個
    chain = [strategy] + (FALLBACK_CHAIN.get(strategy, []) if allow_fallback else [])
    reasons: list[str] = []
    answer, first_token_ms, provider = "", None, None
    for current in chain:
        attempts = 1 + get_settings().retries
        for attempt in range(attempts):
            try:
                provider = get_provider(current)
                async for piece in provider.stream(messages):
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - t0) * 1000)
                    text = to_taiwan(piece)
                    answer += text
                    yield sse("token", {"text": text})
                break
            except ProviderUnavailable as e:
                provider = None
                # 連線失敗直接換下一個生成端（5 秒內改走備援）；逾時與 5xx 重試一次
                if answer or not e.retryable or attempt == attempts - 1:
                    reasons.append(f"{current}：{e}")
                    break
        if provider is not None or answer:  # 成功，或已輸出一半就不換生成端
            break
    if provider is None or not answer:
        suspended = strategy in FALLBACK_CHAIN and allow_fallback and not answer
        message = "；".join(reasons) or "生成失敗"
        if suspended:
            message = f"本地推論伺服器與備援模型都無法使用，服務暫停（不改走雲端）。{message}"
        yield sse(
            "error",
            {
                "code": "STRATEGY_UNAVAILABLE" if not answer else "GENERATION_FAILED",
                "request_id": request_id,
                "message": message,
            },
        )
        return

    # 5. 完成事件＋紀錄
    total_ms = round((time.perf_counter() - t0) * 1000)
    used = provider.strategy
    egress = NO_EGRESS
    if used in CLOUD_STRATEGIES:
        egress = {
            "images": sum(1 for part in messages[-1]["content"] if part.get("type") == "image_url"),
            "chunks": len(sources) if use_retrieval else 0,
            "bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8")),
        }
    done = {
        "request_id": request_id,
        "strategy_requested": strategy,
        "strategy_used": used,
        "model": provider.model,
        "fallback": used != strategy,
        "fallback_reason": "；".join(reasons) or None,
        "prompt_version": prompt_version(domain),
        "use_retrieval": use_retrieval,
        "latency_ms": {
            "retrieval": retrieval_ms,
            "first_token": first_token_ms,
            "generation": total_ms - retrieval_ms,
            "total": total_ms,
        },
        "tokens": {"input": provider.usage.input_tokens, "output": provider.usage.output_tokens},
        "cost_twd": estimate_cost_twd(used, provider.usage),
        "egress": egress,
    }
    yield sse("done", done)
    get_logs_repo().add_chat_log(
        {
            "request_id": request_id,
            "created_at": get_logs_repo().now(),
            "artwork_id": artwork_id,
            "part_id": part_id,
            "question": question,
            "strategy_requested": strategy,
            "strategy_used": used,
            "model": provider.model,
            "prompt_version": done["prompt_version"],
            "use_retrieval": int(use_retrieval),
            "fallback": int(done["fallback"]),
            "retrieval_ms": retrieval_ms,
            "first_token_ms": first_token_ms,
            "total_ms": total_ms,
            "input_tokens": provider.usage.input_tokens,
            "output_tokens": provider.usage.output_tokens,
            "cost_twd": done["cost_twd"],
            "egress_images": egress["images"],
            "egress_chunks": egress["chunks"],
            "egress_bytes": egress["bytes"],
            "answer": answer,
        }
    )
    log.info(
        "chat",
        extra={
            "fields": {
                "request_id": request_id,
                "strategy": strategy,
                "strategy_used": used,
                "model": provider.model,
                "prompt_version": done["prompt_version"],
                "top_k": [(s["chunk_id"], s["score"]) for s in sources],
                "rearrange": rearrange_info,
                "latency_ms": done["latency_ms"],
                "tokens": done["tokens"],
                "cost_twd": done["cost_twd"],
                "egress": egress,
            }
        },
    )
