"""圖文問答流程（論文 Fig. 1c）：影像 → embedding → 相似度搜尋 → 取回脈絡 → 增強 prompt → 生成。

只有最後一步依 strategy 切換生成端。hybrid／lora 連不上時改走本地備援模型，並在 done 事件
標註 fallback；本地都失敗就回覆暫停服務，永遠不改走雲端。雲端（api_nokb／api_kb）只當對照組，
不接受使用者上傳的照片；每次回應都記錄送出本機的資料量（egress）。

工廠圖紙（part_id）走同一條流程，只換知識庫領域與 prompt 模板（drawing_v1）；
圖紙屬企業機密，雲端對照組一律不接受。

七段權限控管（docs/adr/015），2026-10-06 起每一次 /chat 都由伺服器完整執行（docs/adr/030），
請求裡沒有任何欄位能略過關卡：
1. 認證與授權：JWT 已在閘道驗過；這裡先做文件層授權
   （identity.can_view_part：領域＋機密等級＋部門）。看不到或不存在的圖紙一律降級「查無資料」，
   不讀圖檔、不檢索、不組 prompt、不呼叫任何模型
2. Jev Choice：有 /agent/route 發的有效交接票（agent/handoff.py）就沿用；
   沒有、過期或不符就在這裡重跑
3. 權限感知檢索：Metadata Filter 只照 JWT 產生（clearance <= 憑證等級 AND dept IN 公開＋憑證部門）
4. Jev Noul 雙重驗證：每段 is_relevant＋security_leak_check，任一不通過就剔除並記錄
5. Jev Score 評分重排：最低分數、最多 3 段
6. 生成閘門：可答性＋合規；不過就降級回「查無資料」，不呼叫 LLM。關閉檢索也在這裡擋下
   （只有評估模式的畫作對照組例外）
7. 地端 LLM 只依留下的段落回答；回覆先在伺服器收齊，通過輸出檢查（guard.check_output）才送出
第 4～6 段只把公開段落代號化後送 Jev，內部與機密段落留在地端判斷；由誰判斷由伺服器決定。
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator

from app.agent import guard, handoff
from app.agent.entities import get_index
from app.core.config import REPO_ROOT, get_agent_config, get_models_config, get_settings
from app.core.logging import log
from app.rag import conflict_check as conflict_mod
from app.rag.embedders import embed_text
from app.rag.preprocess import load_image, to_jpeg_bytes
from app.rag.prompt import artwork_card, build_messages, part_card, prompt_version
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
from app.services.identity import Account, can_view_part, visible_part_ids
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
    # 綁在畫面區域上的段落帶座標（docs/adr/029），前端在畫上標出那一塊
    region = {"region": item["region"]} if item.get("region") else {}
    return {
        **base,
        "artwork_id": item["artwork_id"],
        "artwork_title": title,
        "title": title,
        "level": "公開",
        **label,
        **region,
    }


def visible_parts(scope: guard.MetaFilter | None) -> set[str] | None:
    """Metadata Filter：看得到的圖紙（文件的機密等級與部門都在憑證範圍內）；None＝不限。"""
    if scope is None:
        return None
    return {
        p["id"] for p in get_store().parts if scope.allows(p["confidentiality"], p.get("owner", ""))
    }


def retrieve(
    question: str,
    artwork_id: str | None,
    part_id: str | None = None,
    scope: guard.MetaFilter | None = None,
) -> list[dict]:
    """檢索段落規則（共用層 §三）：已指定畫作（或圖紙）只取它的段落；相似度門檻＋最多 k 段，
    不硬湊滿；只有比較、背景類問題（或未指定）才從同一領域的全庫補足。

    scope：第 3 段的 Metadata Filter（照 JWT 產生）。工廠圖紙只取看得到的圖紙，
    連已指定的圖紙也一樣（看不到就沒有候選，第 6 段降級）；畫作都是公開的，不受影響。"""
    store = get_store()
    cfg = get_models_config()
    coll, owner = (store.mfg, part_id) if part_id else (store.art, artwork_id)
    owners = visible_parts(scope) if part_id else None
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


def send_image_enabled(requested: bool | None = None) -> bool:
    """問答要不要附圖（docs/adr/024）。

    優先順序：請求的 send_image ＞ .env 的 SEND_IMAGE ＞ models.yaml 的 chat.send_image。"""
    if requested is not None:
        return requested
    env = get_settings().send_image.strip().lower()
    if env in ("true", "1", "on"):
        return True
    if env in ("false", "0", "off"):
        return False
    return get_models_config().chat.send_image


INJECTED_LABEL = "評估注入（干擾段落）"


def inject_distractors(
    question: str,
    sources: list[dict],
    specs: list[dict],
    artwork_id: str | None,
    part_id: str | None,
    scope: guard.MetaFilter | None = None,
) -> list[dict]:
    """干擾段落注入（評估專用，docs/adr/019）：混進檢索結果，之後照常經過洩密掃描與段落篩選，
    量「篩選擋不擋得掉」與「模型會不會被帶偏」。

    counterfactual：呼叫端手寫的段落，掛在同一幅畫／同一張圖紙底下（看起來像真的）；
    score 用它和問題的 bge-m3 相似度，和真正段落同一把尺。
    compatible：同樣是手寫段落，但和正確答案可以同時成立（量模型會不會誤報不一致，docs/adr/028）。
    other：同領域其他畫作／圖紙中和問題最相近的真實段落；工廠圖紙只從看得到的圖紙取。
    每段標 injected，chunk_id 以 inject: 開頭，評估腳本靠它判斷有沒有引用到。"""
    store = get_store()
    domain_part = bool(part_id)
    coll, owner = (store.mfg, part_id) if domain_part else (store.art, artwork_id)
    counter = [s for s in specs if s["kind"] != "other"]  # 手寫段落：counterfactual、compatible
    n_other = sum(1 for s in specs if s["kind"] == "other")
    vecs = embed_text([question] + [s["text"] for s in counter])
    qvec, cvecs = vecs[0], vecs[1:]

    others = []
    if n_other:
        owners = visible_parts(scope) if domain_part else None
        seen = {s["chunk_id"] for s in sources}
        for h in coll.search_chunks(qvec, len(seen) + 20, exclude=seen, owners=owners):
            if h.item[coll.owner_key] != owner:
                others.append(h)
            if len(others) >= n_other:
                break

    first, last = [], []
    ci = oi = 0
    for i, spec in enumerate(specs):
        if spec["kind"] != "other":
            s = {
                "chunk_id": f"inject:{i}",
                "topic": spec.get("topic") or "干擾段落",
                "text": spec["text"],
                "source_url": "",
                "license": "",
                "score": round(float(cvecs[ci] @ qvec), 4),
                "source_label": INJECTED_LABEL,
            }
            ci += 1
            if domain_part:
                part = store.mfg.by_id[part_id]
                s |= {
                    "part_id": part_id,
                    "title": part["name"]["zh"],
                    "level": part["confidentiality"],
                }
            else:
                title = store.by_id[artwork_id]["title"]["zh"] if artwork_id else ""
                s |= {
                    "artwork_id": artwork_id,
                    "artwork_title": title,
                    "title": title,
                    "level": "公開",
                }
        else:
            if oi >= len(others):
                continue  # 同領域沒有別的段落可取（知識庫只有一筆）
            real_id = others[oi].item["chunk_id"]
            s = _source(0, others[oi], store) | {"chunk_id": f"inject:{i}:{real_id}"}
            oi += 1
        s |= {"injected": True, "injected_kind": spec["kind"]}
        (first if spec.get("position", "first") == "first" else last).append(s)
    merged = first + sources + last
    return [{**s, "ref": i + 1} for i, s in enumerate(merged)]


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
    route_ticket: str | None = None,
    eval_mode: bool = False,
    inject: list[dict] | None = None,
    send_image: bool | None = None,
    conflict_check: bool | None = None,
) -> AsyncIterator[str]:
    """問答流程用到 bge-m3 與本地 Qwen3-VL（照片辨識另加 Chinese-CLIP）。

    記憶體吃緊時先釋放其他模型（services/memory_guard.py）。
    account：目前身分（JWT 的資料範圍與 Metadata Filter）；
    None＝程式內部呼叫（單元測試、評估腳本直接呼叫），不限資料範圍，但第 2～7 段照樣執行。
    route_ticket：/agent/route 發的交接票（有效才沿用第 2 段的判斷）。
    eval_mode：評估模式（EVAL_CONTROLS＋本機，由 API 層判斷）。只有開啟時 strategy=mock 才生效、
    畫作的關檢索對照組才會生成；關閉時這些選項不會降低任何關卡。
    inject：評估用的干擾段落（docs/adr/019），API 層只在受信任的評估模式傳入。
    send_image：要不要附圖（docs/adr/024）；None＝依 .env／models.yaml。
    conflict_check：回答前先檢查參考資料有沒有互相矛盾（docs/adr/028）；None＝依 .env／models.yaml。
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
        route_ticket,
        eval_mode,
        inject,
        send_image,
        conflict_check,
    )
    async for e in memory_guard.stream("chat", models, events):
        yield e


def _stage(trace: list[dict], n: int, status: str, by: str, detail: str) -> None:
    """可觀測軌跡：每一段執行了沒有、結果、由誰判斷（sources／done 事件的 pipeline）。"""
    trace.append(
        {"stage": n, "name": guard.STAGE_NAMES[n], "status": status, "by": by, "detail": detail}
    )


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
    route_ticket: str | None = None,
    eval_mode: bool = False,
    inject: list[dict] | None = None,
    send_image: bool | None = None,
    conflict_check: bool | None = None,
) -> AsyncIterator[str]:
    t0 = time.perf_counter()
    store = get_store()
    gcfg = get_agent_config()["guard"]
    identified = None
    requested = strategy
    # strategy=mock 只在評估模式生效；否則照一般問答走主推論伺服器（關卡完全一樣）
    if strategy == "mock" and not eval_mode:
        strategy = "hybrid"
    cloud = strategy in CLOUD_STRATEGIES
    domain = "mfg" if part_id else "art"
    trace: list[dict] = []
    # 個資遮蔽：之後的檢索、prompt、紀錄都用遮蔽後的問句
    asked = guard.mask_pii(question.strip())[0]

    def finish_without_llm(
        stage: int, message: str, model: str, sources_event: dict | None = None, egress=None
    ) -> list[str]:
        """第 stage 段擋下：回統一的訊息，不讀圖檔、不組 prompt、不呼叫 LLM。"""
        total_ms = round((time.perf_counter() - t0) * 1000)
        out = []
        if sources_event is None:
            sources_event = {
                "request_id": request_id,
                "artwork_id": None,
                "part_id": None,
                "strategy": requested,
                "identified": None,
                "route": None,
                "rearrange": None,
                "conflict_check": None,
                "use_retrieval": use_retrieval,
                "sources": [],
                "filter": None,
                "candidates": 0,
                "post_filter": None,
                "pipeline": list(trace),
            }
            out.append(sse("sources", sources_event))
        out.append(sse("token", {"text": message}))
        jev_bytes = (egress or {}).get("bytes", 0)
        done = {
            "request_id": request_id,
            "strategy_requested": requested,
            "strategy_used": strategy,
            "model": model,
            "fallback": False,
            "fallback_reason": None,
            "prompt_version": prompt_version(domain),
            "use_retrieval": use_retrieval,
            "latency_ms": {
                "retrieval": total_ms,
                "first_token": None,
                "generation": 0,
                "total": total_ms,
            },
            "tokens": {"input": 0, "output": 0},
            "cost_twd": 0.0,
            "egress": {
                **NO_EGRESS,
                "chunks": (egress or {}).get("chunks", 0),
                "bytes": jev_bytes,
                "jev_bytes": jev_bytes,
            },
            "degraded": True,
            "pipeline": list(trace),
        }
        out.append(sse("done", done))
        get_logs_repo().add_chat_log(
            {
                "request_id": request_id,
                "created_at": get_logs_repo().now(),
                "artwork_id": artwork_id if stage > 1 else None,
                "part_id": part_id if stage > 1 else None,
                "question": asked,
                "strategy_requested": requested,
                "strategy_used": "gate",
                "model": model,
                "prompt_version": done["prompt_version"],
                "use_retrieval": int(use_retrieval),
                "fallback": 0,
                "retrieval_ms": total_ms,
                "first_token_ms": None,
                "total_ms": total_ms,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_twd": 0.0,
                "egress_images": 0,
                "egress_chunks": done["egress"]["chunks"],
                "egress_bytes": jev_bytes,
                "answer": message,
            }
        )
        return out

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

    # 以圖辨識（已指定畫作或圖紙就跳過）。只給照片時先經過領域路由（MMed-RAG 的領域辨識），
    # 判斷是畫作還是圖紙，再走該領域的辨識（和以圖搜圖、智慧助理同一個 identify_any）。
    # 文件層授權先做（docs/adr/030）：圖紙只在目前身分看得到的圖紙裡辨識，
    # 看不到的圖紙不算相似度、不讀特徵與圖檔；辨識不到就和「知識庫沒有」一樣。
    # 雲端策略在上面已拒收照片，所以路由成圖紙時不會是雲端。
    route_info = None
    if not artwork_id and not part_id and image_id:
        visible = visible_part_ids(account, store.parts) if account is not None else None
        found = identify_any(image_id, part_ids=visible)
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

    # ------------------------------------------------------------ 第 1 段：文件層授權
    # 不存在與看不到的圖紙一律同一個降級回應（不透露有沒有這份文件），
    # 在讀圖檔、作品卡、段落或呼叫任何模型之前就結束
    if domain == "mfg":
        artwork = store.get_part(part_id)
        if artwork is None or (account is not None and not can_view_part(account, artwork)):
            _stage(trace, 1, "block", "地端", "指定的圖紙不在你的資料範圍內 → 查無資料")
            for e in finish_without_llm(1, gcfg["degrade_message"], "第 1 段（未呼叫 LLM）"):
                yield e
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
    doc = None
    if artwork:
        doc = {
            "id": artwork["id"],
            "label": artwork["name"]["zh"] if domain == "mfg" else artwork["title"]["zh"],
            "level": artwork.get("confidentiality", "公開") if domain == "mfg" else "公開",
            "dept": artwork.get("owner", "") if domain == "mfg" else "公開",
        }
    _stage(
        trace,
        1,
        "pass",
        "地端",
        "JWT 已在閘道驗過；" + ("指定的文件在資料範圍內" if doc else "沒有指定文件")
        if account is not None
        else "程式內部呼叫（不限資料範圍）",
    )

    # ------------------------------------------------------------ 第 2 段：Jev Choice
    fp = guard.fingerprint(asked)
    ticket = (
        handoff.redeem(route_ticket, account.id, fp, part_id, artwork_id)
        if account is not None and not image_id
        else None
    )
    if ticket is not None:
        post_mode = ticket.engine
        _stage(trace, 2, "pass", ticket.guard_engine, "沿用智慧助理第 2 段的判斷（交接票有效）")
    else:
        index = await asyncio.to_thread(get_index)
        masked = index.pseudonymize(asked, index.find(asked))
        g2 = await guard.guard_input(asked, masked.text, masked.mapping, "read", True)
        note = "交接票無效或不符，重新判斷；" if route_ticket else ""
        if not g2.passed:
            judge = "雲端 Jev（只收代號化文字）" if g2.engine == "jev" else "地端規則"
            guard.log_block(
                2, g2.tag or "惡意輸入", account, f"/chat 問句雜湊 {fp}", request_id, judge
            )
            _stage(trace, 2, "block", g2.engine, f"{note}{g2.reason}")
            for e in finish_without_llm(
                2,
                gcfg["blocked_message"],
                "第 2 段（未呼叫 LLM）",
                egress={"bytes": g2.egress_bytes},
            ):
                yield e
            return
        post_mode = "jev"
        _stage(trace, 2, "pass", g2.engine, f"{note}{g2.checks[0].detail if g2.checks else ''}")

    # ------------------------------------------------------------ 第 3 段：Metadata Filter 檢索
    meta = guard.MetaFilter.of(account, domain, doc) if account is not None else None
    # 評估模式的畫作關檢索對照組仍檢索一次（供前端比較），但段落不放進 prompt
    if use_retrieval or (eval_mode and domain == "art"):
        sources = retrieve(asked, artwork_id, part_id, meta)
        if inject:
            sources = inject_distractors(asked, sources, inject, artwork_id, part_id, meta)
        _stage(
            trace,
            3,
            "pass",
            "地端",
            f"{len(sources)} 段候選" + (f"；{meta.text}" if meta else ""),
        )
    else:
        sources = []
        _stage(trace, 3, "skip", "地端", "關閉檢索")
    candidates = len(sources)

    # ------------------------------------------------------------ 第 4～6 段
    conflict_info = None
    context_note = None
    card = None
    if artwork:
        card = {
            "level": doc["level"],
            "text": part_card(artwork) if domain == "mfg" else artwork_card(artwork),
            "domain": domain,
        }
    post = None
    rearrange_info = None
    if not use_retrieval:
        if eval_mode and domain == "art":
            # 評估模式的對照組（關檢索、雲端・無檢索）：公開畫作只依作品資料與圖回答
            for n in (4, 5, 6):
                _stage(trace, n, "skip", "地端", "評估模式：關檢索對照組，不放參考段落")
        else:
            _stage(trace, 4, "skip", "地端", "關閉檢索：沒有段落可驗證")
            _stage(trace, 5, "skip", "地端", "關閉檢索：沒有段落可重排")
            _stage(trace, 6, "block", "地端", "關閉檢索時無法確認答得出來 → 不讓 LLM 臆測")
            for e in finish_without_llm(6, gcfg["degrade_message"], "生成閘門（未呼叫 LLM）"):
                yield e
            return
    else:
        post = await guard.process_passages(asked, sources, post_mode, strategy, card, rearrange)
        sources = post.kept
        for s in post.flagged:
            guard.log_block(
                4,
                "洩密風險（段落已剔除）",
                account,
                f"段落 {s['chunk_id']}",
                request_id,
                "雲端 Jev" if post.caught_by(s["chunk_id"]) == "Jev" else "地端規則",
            )
        rearrange_info = post.rearrange
        judged = len(post.verify.checks) - len(post.flagged)
        kept = len(post.kept) if post.gate.passed else 0
        _stage(
            trace,
            4,
            "pass",
            post.verify.engine,
            f"剔除洩密 {len(post.flagged)} 段；{judged} 段判斷相關性",
        )
        _stage(
            trace,
            5,
            "pass",
            post.rerank.engine,
            f"最低分數＋最多 {guard.MAX_CONTEXT} 段：留 {kept} 段",
        )
        _stage(
            trace,
            6,
            "pass" if post.gate.passed else "block",
            post.gate.engine,
            "；".join(c.detail for c in post.gate.checks),
        )
        # 矛盾檢查（docs/adr/028）：安全篩選後仍有至少兩段時才問本地模型；
        # 有矛盾就在參考資料後面加提醒。這個開關不影響第 1～7 段安全關卡。
        if not post.degraded and conflict_mod.enabled(conflict_check):
            conflict_info = await conflict_mod.check(asked, sources, strategy)
            if conflict_info and conflict_info["conflict"]:
                context_note = conflict_mod.note(conflict_info["refs"])
    retrieval_ms = round((time.perf_counter() - t0) * 1000)
    sources_event = {
        "request_id": request_id,
        "artwork_id": artwork_id,
        "part_id": part_id,
        "strategy": requested,
        "identified": identified,
        "route": route_info,
        "rearrange": rearrange_info,
        "conflict_check": conflict_info,
        "use_retrieval": use_retrieval,
        "sources": sources if use_retrieval else [],
        "filter": meta.public() if meta else None,
        "candidates": candidates,
        "post_filter": post.public() if post else None,
        "pipeline": list(trace),
    }
    yield sse("sources", sources_event)

    # 第 6 段生成閘門沒過：降級回應，不呼叫 LLM（不讓模型臆測，也不透露有文件但沒有權限）
    if post and post.degraded:
        for e in finish_without_llm(
            6,
            post.gate.message or gcfg["degrade_message"],
            "生成閘門（未呼叫 LLM）",
            sources_event,
            egress={"bytes": post.egress_bytes, "chunks": post.cloud if post.calls else 0},
        ):
            yield e
        return

    # ------------------------------------------------------------ 第 7 段：本地 LLM 生成
    # 組 prompt（照片優先，否則用知識庫圖檔，一律長邊 1024 px）。畫作不用網頁卡片的 480 px 縮圖：
    # Ollama 會把圖換算成差不多的 token 數（縮圖約 1,060、原圖約 1,065），
    # 縮圖省不到時間，模型反而看得比較模糊。已辨識或指定且檢索開著時可設定不送圖；
    # 關檢索的評估對照組照樣送圖，才能量檢索增益（docs/adr/024）。
    attach_image = not (artwork and use_retrieval) or send_image_enabled(send_image)
    if not attach_image:
        image_jpeg = None
    elif image_id:
        image_jpeg = to_jpeg_bytes(load_image(load_upload(image_id)))
    elif domain == "mfg":
        image_jpeg = to_jpeg_bytes(load_image(REPO_ROOT / artwork["drawing"]))
    elif artwork:
        image_jpeg = to_jpeg_bytes(load_image(REPO_ROOT / artwork["image"]["path"]))
    else:
        image_jpeg = None
    # 關檢索對照組檢索出的段落只給前端比較，不交給生成端；引用編號也只能指向這裡的段落
    given = sources if use_retrieval else []
    messages = build_messages(
        asked,
        artwork,
        given,
        image_jpeg,
        use_retrieval,
        include_card=strategy != "api_nokb",
        domain=domain,
        note=context_note,
    )

    # 依 strategy 生成；失敗依本地備援鏈改走下一個。
    # 回覆先在伺服器收齊：通過輸出檢查才送給使用者（不合規的原輸出不可以先流出去）
    chain = [strategy] + (FALLBACK_CHAIN.get(strategy, []) if allow_fallback else [])
    reasons: list[str] = []
    pieces: list[str] = []
    first_token_ms, provider = None, None
    for current in chain:
        attempts = 1 + get_settings().retries
        for attempt in range(attempts):
            try:
                provider = get_provider(current)
                async for piece in provider.stream(messages):
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - t0) * 1000)
                    pieces.append(to_taiwan(piece))
                break
            except ProviderUnavailable as e:
                provider = None
                # 連線失敗直接換下一個生成端（5 秒內改走備援）；逾時與 5xx 重試一次
                if pieces or not e.retryable or attempt == attempts - 1:
                    reasons.append(f"{current}：{e}")
                    break
        if provider is not None or pieces:  # 成功，或已輸出一半就不換生成端
            break
    answer = "".join(pieces)
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

    # 第 7 段輸出檢查：未授權文件識別資訊、帳密／金鑰、個資、內部資料、被剔除段落的內容、無效引用
    hidden = guard.hidden_terms(account, store.parts)
    problems = guard.check_output(answer, given, hidden, post.flagged if post else [])
    if problems:
        guard.log_block(
            7,
            "輸出不合規（原輸出未送出）",
            account,
            f"違規：{'、'.join(problems)}；回覆雜湊 {guard.fingerprint(answer)}",
            request_id,
            "地端輸出檢查",
        )
        _stage(trace, 7, "block", "地端", f"輸出檢查沒過（{'、'.join(problems)}）→ 原輸出不送出")
        for e in finish_without_llm(
            7,
            gcfg["output_blocked_message"],
            f"{provider.model}（第 7 段輸出檢查擋下）",
            sources_event,
            egress={"bytes": post.egress_bytes if post else 0},
        ):
            yield e
        return
    _stage(trace, 7, "pass", provider.strategy, f"{provider.model} 生成；輸出檢查通過")
    for piece in pieces:
        yield sse("token", {"text": piece})

    # 完成事件＋紀錄
    total_ms = round((time.perf_counter() - t0) * 1000)
    used = provider.strategy
    egress = dict(NO_EGRESS)
    if used in CLOUD_STRATEGIES:
        egress = {
            "images": sum(1 for part in messages[-1]["content"] if part.get("type") == "image_url"),
            "chunks": len(sources) if use_retrieval else 0,
            "bytes": len(json.dumps(messages, ensure_ascii=False).encode("utf-8")),
        }
    # 第 4～6 段送 Jev 的代號化公開段落也算外送（只判斷、不生成）
    jev_bytes = post.egress_bytes if post else 0
    egress = {
        **egress,
        "chunks": egress["chunks"] + (post.cloud if post and post.calls else 0),
        "bytes": egress["bytes"] + jev_bytes,
        "jev_bytes": jev_bytes,
    }
    done = {
        "request_id": request_id,
        "strategy_requested": requested,
        "strategy_used": used,
        "model": provider.model,
        "fallback": used != strategy,
        "fallback_reason": "；".join(reasons) or None,
        "prompt_version": prompt_version(domain),
        "use_retrieval": use_retrieval,
        "image_sent": image_jpeg is not None,  # docs/adr/024
        "latency_ms": {
            "retrieval": retrieval_ms,
            "first_token": first_token_ms,
            "generation": total_ms - retrieval_ms,
            "total": total_ms,
        },
        "tokens": {"input": provider.usage.input_tokens, "output": provider.usage.output_tokens},
        "cost_twd": estimate_cost_twd(used, provider.usage),
        "egress": egress,
        "degraded": False,
        "pipeline": list(trace),
    }
    yield sse("done", done)
    get_logs_repo().add_chat_log(
        {
            "request_id": request_id,
            "created_at": get_logs_repo().now(),
            "artwork_id": artwork_id,
            "part_id": part_id,
            "question": asked,
            "strategy_requested": requested,
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
