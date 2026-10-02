"""圖文問答流程（論文 Fig. 1c）：影像 → embedding → 相似度搜尋 → 取回脈絡 → 增強 prompt → 生成。

只有最後一步依 strategy 切換生成端。hybrid／lora 連不上時改走本地備援模型，並在 done 事件
標註 fallback；本地都失敗就回覆暫停服務，永遠不改走雲端。雲端（api_nokb／api_kb）只當對照組，
不接受使用者上傳的照片；每次回應都記錄送出本機的資料量（egress）。

工廠圖紙（part_id）走同一條流程，只換知識庫領域與 prompt 模板（drawing_v1）；
圖紙屬企業機密，雲端對照組一律不接受。

五段防護的第 3～5 段（docs/adr/012）也在這裡：
3. 檢索帶 Metadata Filter：只取目前身分看得到的圖紙（機密等級）的段落，看不到的在檢索時就濾掉
4. 後置過濾：每段都用地端規則掃描夾帶指令（間接注入），命中就移除並記錄；智慧助理（post_filter）
   再做完整的第 4 段：公開段落代號化後送 Jev 判斷注入與關聯性、內部與機密段落留在地端，最多留 3 段
5. 地端 LLM 只依留下的段落回答
"""

import json
import time
from collections.abc import AsyncIterator

from app.agent import guard
from app.core.config import REPO_ROOT, get_models_config, get_settings
from app.core.errors import AppError
from app.core.logging import log
from app.rag import rearrange as rearrange_mod
from app.rag.embedders import embed_text
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.prompt import build_messages, prompt_version
from app.rag.providers import (
    CLOUD_STRATEGIES,
    ProviderUnavailable,
    cloud_status,
    estimate_cost_twd,
    get_provider,
)
from app.rag.textproc import to_taiwan
from app.repositories.index_store import get_store
from app.repositories.logs_repo import get_logs_repo
from app.services import memory_guard
from app.services.identity import Account, require_part
from app.services.search_service import identify_any, load_upload

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
        part = store.mfg.by_id[item["part_id"]]
        return {
            **base,
            "part_id": item["part_id"],
            "title": part["name"]["zh"],
            "source_label": item["source"],
            "level": part["confidentiality"],
        }
    title = store.by_id[item["artwork_id"]]["title"]["zh"]
    # 系統計算的段落（色彩分析）沒有網址，跟工廠圖紙段落一樣帶 source_label
    label = {"source_label": item["source"]} if item.get("source") else {}
    return {
        **base,
        "artwork_id": item["artwork_id"],
        "artwork_title": title,
        "title": title,
        "level": "公開",
        **label,
    }


def visible_parts(levels: list[str] | tuple[str, ...] | None) -> set[str] | None:
    """Metadata Filter：看得到的圖紙（機密等級在 levels 裡）；levels 為 None＝不限。"""
    if levels is None:
        return None
    return {p["id"] for p in get_store().parts if p["confidentiality"] in levels}


def retrieve(
    question: str,
    artwork_id: str | None,
    part_id: str | None = None,
    levels: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """檢索段落規則（共用層 §三）：已指定畫作（或圖紙）只取它的段落；相似度門檻＋最多 k 段，
    不硬湊滿；只有比較、背景類問題（或未指定）才從同一領域的全庫補足。

    levels：目前身分看得到的機密等級（第 3 段的 Metadata Filter）。工廠圖紙從全庫補足時
    只取看得到的圖紙；畫作都是公開的，不受影響。"""
    store = get_store()
    cfg = get_models_config()
    coll, owner = (store.mfg, part_id) if part_id else (store.art, artwork_id)
    owners = visible_parts(levels) if part_id else None
    k = int(cfg.retrieval["top_k_chunks"])
    qvec = embed_text([question])[0]
    wide = not owner or any(w in question for w in cfg.global_fill_keywords)
    own = coll.search_chunks(qvec, k, owner_id=owner, owners=owners) if owner else []
    seen = {h.item["chunk_id"] for h in own}
    extra = coll.search_chunks(qvec, k, exclude=seen, owners=owners) if wide else []
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
    account: Account | None = None,
    post_filter: str | None = None,
) -> AsyncIterator[str]:
    """問答流程用到 bge-m3 與本地 Qwen3-VL（照片辨識另加 Chinese-CLIP）。

    記憶體吃緊時先釋放其他模型（services/memory_guard.py）。
    account：目前身分（資料範圍與 Metadata Filter）；None＝不限（評估腳本、單元測試直接呼叫時）。
    post_filter：第 4 段由誰判斷（jev／local，智慧助理帶）；None＝只用地端規則掃描夾帶指令。
    """
    models = {"bge", "qwen"} | ({"clip"} if image_id and not artwork_id else set())
    events = _chat_stream(
        question,
        request_id,
        strategy,
        artwork_id,
        image_id,
        use_retrieval,
        allow_fallback,
        part_id,
        rearrange,
        account,
        post_filter,
    )
    async for e in memory_guard.stream("chat", models, events):
        yield e


async def _chat_stream(
    question: str,
    request_id: str,
    strategy: str = "hybrid",
    artwork_id: str | None = None,
    image_id: str | None = None,
    use_retrieval: bool = True,
    allow_fallback: bool = True,
    part_id: str | None = None,
    rearrange: bool | None = None,
    account: Account | None = None,
    post_filter: str | None = None,
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
    #    判斷是畫作還是圖紙，再走該領域的辨識（和以圖搜圖、智慧助理同一個 identify_any）。
    #    雲端策略在第 0 步已拒收照片，所以路由成圖紙時不會是雲端。
    route_info = None
    if not artwork_id and not part_id and image_id:
        found = identify_any(image_id)
        route_info = found["route"]
        domain = route_info["domain"]
        identified = found["drawing_result"] if domain == "mfg" else found["artwork_result"]
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

    # 第 1 段的資料範圍在 API 層也檢查一次：照片辨識出的圖紙、直接指定的圖紙都一樣
    if account is not None and domain == "mfg":
        try:
            require_part(account, artwork)
        except AppError as e:
            yield sse("error", {"code": e.code, "request_id": request_id, "message": e.message})
            return

    # 2. 檢索（第 3 段，帶 Metadata Filter；關檢索時仍算一次，供前端比較用，但不放進 prompt）
    levels = list(account.levels) if account is not None else None
    meta = guard.MetaFilter(
        domain,
        ["公開"] if domain == "art" else [x for x in (levels or ["內部", "機密"]) if x != "公開"],
        part_id or artwork_id,
        (artwork or {}).get("name", {}).get("zh") or (artwork or {}).get("title", {}).get("zh"),
        artwork.get("confidentiality", "公開") if artwork else None,
    )
    sources = retrieve(question, artwork_id, part_id, levels)
    candidates = len(sources)
    # 第 4 段：要放進 prompt 的段落先過濾。每段都用地端規則掃描夾帶指令；
    # 智慧助理（post_filter）再做注入＋關聯性重排（公開段落送 Jev、其他留地端），最多 3 段。
    # 其他頁面沿用段落篩選（MIRA 的 Rearrange）開關，篩選時間算在 retrieval 裡
    post = None
    rearrange_info = None
    if use_retrieval and sources:
        post = await guard.filter_passages(question, sources, post_filter or "scan", strategy)
        sources = post.kept
        for s in post.injected:
            guard.log_block(
                4,
                "間接注入（段落已移除）",
                account,
                guard.mask_pii(f"〈{s['title']}〉{s['topic']}：{s['text'][:80]}")[0],
                request_id,
                "雲端 Jev" if post.caught_by(s["chunk_id"]) == "Jev" else "地端規則",
            )
        rearrange_info = post.rearrange
        if post.mode == "scan" and rearrange_mod.enabled(rearrange):
            sources, rearrange_info = await rearrange_mod.rearrange(question, sources, strategy)
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
            "filter": meta.public(),
            "candidates": candidates,
            "post_filter": post.public() if post else None,
        },
    )

    # 3. 組 prompt（照片優先，否則用知識庫圖檔，一律長邊 1024 px）。畫作不用網頁卡片的 480 px 縮圖：
    #    Ollama 會把圖換算成差不多的 token 數（縮圖約 1,060、原圖約 1,065），
    #    縮圖省不到時間，模型反而看得比較模糊
    if image_id:
        image_jpeg = to_jpeg_bytes(load_image(load_upload(image_id)))
    elif domain == "mfg":
        image_jpeg = to_jpeg_bytes(load_image(REPO_ROOT / artwork["drawing"]))
    elif artwork:
        image_jpeg = to_jpeg_bytes(load_image(REPO_ROOT / artwork["image"]["path"]))
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
    egress = dict(NO_EGRESS)
    if used in CLOUD_STRATEGIES:
        egress = {
            "images": sum(1 for part in messages[-1]["content"] if part.get("type") == "image_url"),
            "chunks": len(sources) if use_retrieval else 0,
            "bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8")),
        }
    # 第 4 段送 Jev 的代號化公開段落也算外送（只判斷、不生成）
    jev_bytes = post.egress_bytes if post else 0
    egress = {
        **egress,
        "chunks": egress["chunks"] + (post.cloud if post and post.call else 0),
        "bytes": egress["bytes"] + jev_bytes,
        "jev_bytes": jev_bytes,
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
